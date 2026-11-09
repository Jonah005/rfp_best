"""Task copilot routes.

Two shapes matter here.

`POST /conversations/{id}/messages` returns **202** with a job id rather than
blocking for the length of a model call, and the client streams the result from
`GET .../stream`. The answer itself is written to the database by the worker
before the job completes, so a dropped stream loses the animation and never the
answer.

`GET /tasks/{id}/conversation` **does not create** a conversation. Creation is a
POST. A read that writes makes conversation counts meaningless and lets a
membership check be sidestepped by merely opening a page.
"""

from __future__ import annotations

import json
import threading
import uuid

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_context, get_db, require
from app.core.context import ExecutionContext
from app.core.errors import NotFound
from app.core.permissions import Perm
from app.db.session import apply_tenant_context, get_session_factory
from app.gateway.gateway import build_gateway
from app.schemas.copilot import (
    AskIn,
    ConversationOut,
    CreateConversationIn,
    MessageOut,
)
from app.services import context_builder, copilot, writeup
from app.services.jobs import get_store

router = APIRouter(tags=["copilot"])


@router.post(
    "/tasks/{task_id}/conversations",
    response_model=ConversationOut,
    dependencies=[Depends(require(Perm.CHAT_USE))],
)
def create_conversation(
    task_id: uuid.UUID,
    payload: CreateConversationIn | None = None,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ConversationOut:
    conversation = copilot.create_conversation(
        session, ctx, task_id=task_id, title=payload.title if payload else None
    )
    return ConversationOut.model_validate(conversation)


@router.get(
    "/tasks/{task_id}/conversation",
    response_model=ConversationOut,
    dependencies=[Depends(require(Perm.CHAT_USE))],
)
def my_conversation(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ConversationOut:
    """The caller's own conversation on this task. Never creates one."""
    if ctx.actor_user_id is None:
        raise NotFound("No conversation.")
    conversation = copilot.conversation_for(
        session, ctx, task_id=task_id, user_id=ctx.actor_user_id
    )
    if conversation is None:
        raise NotFound(
            "You have no conversation on this task yet. Create one with POST "
            "/tasks/{task_id}/conversations."
        )
    return ConversationOut.model_validate(conversation)


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=list[MessageOut],
    dependencies=[Depends(require(Perm.CHAT_USE))],
)
def list_messages(
    conversation_id: uuid.UUID,
    limit: int = Query(default=50, le=500),
    focus: str | None = Query(default=None, description='"task", a focus key, or omit for all'),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[MessageOut]:
    key = copilot.ALL_FOCUS if focus is None else (None if focus == "task" else focus)
    rows = copilot.messages(
        session, ctx, conversation_id=conversation_id, limit=limit, focus_key=key
    )
    return [MessageOut.model_validate(m) for m in rows]


@router.get(
    "/conversations/{conversation_id}/transcript",
    response_model=dict,
    dependencies=[Depends(require(Perm.CHAT_USE))],
)
def transcript(
    conversation_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Summaries and turns together, as a reviewer reads them."""
    return copilot.transcript(session, ctx, conversation_id=conversation_id)


@router.get(
    "/messages/{message_id}/retrieval-trace",
    response_model=dict,
    dependencies=[Depends(require(Perm.CHAT_USE))],
)
def retrieval_trace(
    message_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Exactly what the model was shown for this answer, and why.

    The route an investigator takes after a suspected leak. Without it, "the
    model must have seen it" is an argument nobody can settle.
    """
    return copilot.retrieval_trace(session, ctx, message_id=message_id)


@router.get(
    "/tasks/{task_id}/context-preview",
    response_model=dict,
    dependencies=[Depends(require(Perm.CHAT_USE))],
)
def context_preview(
    task_id: uuid.UUID,
    q: str = Query(min_length=1),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """What the copilot *would* be shown for this question, without asking it.

    Exposed so the permission boundary can be inspected directly rather than
    inferred from an answer.
    """
    built = context_builder.build(session, ctx, task_id=task_id, query=q)
    return {
        "out_of_scope": built.out_of_scope,
        "sources": [s.as_dict() for s in built.sources],
        "trace": built.trace,
        "refusal": built.refusal() if built.out_of_scope else None,
    }


# --- asking: 202 then stream -------------------------------------------------


def _run_ask(
    *,
    job_id: uuid.UUID,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    question: str,
    request_id: uuid.UUID,
    can_change_diagrams: bool = False,
    focus: dict | None = None,
) -> None:
    """Answer in a worker thread, with its own session and tenant binding."""
    store = get_store()
    job = store.get(job_id, org_id=org_id)
    if job is None:
        return
    store.start(job)
    session = get_session_factory()()
    try:
        session.begin()
        ctx = ExecutionContext(org_id=org_id, actor_user_id=user_id, request_id=request_id)
        apply_tenant_context(session, ctx)
        store.emit(job, "status", {"state": "building_context"})
        answer = copilot.ask(
            session,
            ctx,
            conversation_id=conversation_id,
            question=question,
            gateway=build_gateway(),
            can_change_diagrams=can_change_diagrams,
            focus=focus,
        )
        session.commit()
        # A write-up starts only now: the diagrams it explains were made in this same
        # turn and are not visible to another transaction until the commit above.
        raw_plan = (answer.message.retrieval_trace or {}).get("writeup")
        writeup_job_id = (
            writeup.start_background(
                writeup.Plan.from_dict(raw_plan),
                org_id=org_id,
                user_id=user_id,
                request_id=request_id,
            )
            if raw_plan
            else None
        )
        store.finish(
            job,
            {
                "message_id": str(answer.message.id),
                "content": answer.message.content,
                "sources": answer.sources,
                "out_of_scope": answer.out_of_scope,
                "refusal": answer.refusal,
                "writeup_job_id": writeup_job_id,
            },
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the client as an event
        session.rollback()
        store.fail(job, f"{type(exc).__name__}: {exc}")
    finally:
        session.close()


@router.post(
    "/conversations/{conversation_id}/messages",
    status_code=202,
    response_model=dict,
    dependencies=[Depends(require(Perm.CHAT_USE))],
)
def ask(
    conversation_id: uuid.UUID,
    payload: AskIn,
    response: Response,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Accept the question and return immediately. Stream the answer separately."""
    conversation = copilot.get_conversation(session, ctx, conversation_id=conversation_id)
    copilot._assert_own(ctx, conversation)

    store = get_store()
    job = store.create(org_id=ctx.org_id, user_id=ctx.actor_user_id, kind="copilot_ask")
    thread = threading.Thread(
        target=_run_ask,
        kwargs={
            "job_id": job.id,
            "org_id": ctx.org_id,
            "user_id": conversation.user_id,
            "conversation_id": conversation_id,
            "question": payload.content,
            "request_id": ctx.request_id,
            # The worker has no permissions of its own, so what the caller may do to
            # diagrams is decided here, where they were authenticated.
            "can_change_diagrams": ctx.has_permission(Perm.ARTIFACT_WRITE.value),
            "focus": payload.focus.model_dump(mode="json") if payload.focus else None,
        },
        daemon=True,
    )
    thread.start()
    response.headers["Location"] = f"/api/v1/jobs/{job.id}/stream"
    return {
        "job_id": str(job.id),
        "state": job.state,
        "stream_url": f"/api/v1/jobs/{job.id}/stream",
        "result_url": f"/api/v1/jobs/{job.id}",
    }


@router.post(
    "/conversations/{conversation_id}/attachments",
    response_model=dict,
    dependencies=[Depends(require(Perm.CHAT_USE))],
)
def attach_picture(
    conversation_id: uuid.UUID,
    file: UploadFile = File(...),
    note: str = Form(default=""),
    use_vision: bool = Form(default=False),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """A picture of a diagram, attached to the chat like anything else you would say.

    The reply lands in the same thread. The picture only leaves the platform when the
    person ticked the box for it.
    """
    from app.core.config import get_settings
    from app.models.enums import ChatRole
    from app.services import diagram_chat

    conversation = copilot.get_conversation(session, ctx, conversation_id=conversation_id)
    copilot._assert_own(ctx, conversation)
    data = file.file.read(get_settings().vision_max_image_bytes + 1)
    said = (note or "").strip() or "Here is a diagram."
    copilot._append(
        session,
        ctx,
        conversation=conversation,
        role=ChatRole.USER,
        content=f"{said}\n[picture attached: {file.filename or 'image'}]",
    )
    reply = diagram_chat.picture_reply(
        session,
        ctx,
        task_id=conversation.task_id,
        image=data,
        use_vision=use_vision,
        can_write=ctx.has_permission(Perm.ARTIFACT_WRITE.value),
    )
    message = copilot._append(
        session,
        ctx,
        conversation=conversation,
        role=ChatRole.ASSISTANT,
        content=reply["content"],
        retrieval_trace=reply["trace"],
    )
    return {"message_id": str(message.id)}


@router.get("/jobs/{job_id}", response_model=dict)
def job_status(
    job_id: uuid.UUID,
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Poll, for clients that cannot hold a stream open."""
    job = get_store().get(job_id, org_id=ctx.org_id)
    if job is None:
        raise NotFound("That job does not exist.")
    store = get_store()
    progress = next(
        (e["data"] for e in reversed(store.events(job)) if e["event"] == "progress"), None
    )
    return {**job.as_dict(), "result": job.result, "progress": progress}


@router.get("/jobs/{job_id}/stream")
def job_stream(
    job_id: uuid.UUID,
    ctx: ExecutionContext = Depends(get_current_context),
) -> StreamingResponse:
    """Server-sent events for one job.

    Every event the job has already emitted is replayed first, so a client that
    connects a moment late still sees the whole sequence.
    """
    store = get_store()
    job = store.get(job_id, org_id=ctx.org_id)
    if job is None:
        raise NotFound("That job does not exist.")

    def events():
        # Events are read from the store, not off the job object, so a replica
        # that did not start this job can still replay everything emitted so far
        # and then follow it to completion.
        sent = 0
        while True:
            available = store.events(job)
            while sent < len(available):
                item = available[sent]
                sent += 1
                yield f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"
            refreshed = store.get(job.id, org_id=ctx.org_id)
            state = refreshed.state if refreshed else job.state
            if state in ("DONE", "FAILED") and sent >= len(store.events(job)):
                return
            if not store.wait(job, 0.25) and state not in ("DONE", "FAILED"):
                yield ": keep-alive\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
