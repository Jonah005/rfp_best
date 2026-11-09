"""The task copilot: conversations, messages, and the answer path.

Conversation memory has three layers, and the reason for each is different.

1. **Recent turns, verbatim.** The last N messages, so the dialogue is coherent.
2. **Rolled-up summaries.** Older turns compressed into `conversation_summary`
   rows covering an explicit message range, so a long thread stays affordable
   without the older part silently vanishing.
3. **Reconstructed context, every time.** Requirements, attached evidence,
   received answers and freshly retrieved chunks -- rebuilt on every message
   against live permissions. Never carried forward from an earlier turn.

Layer 3 is the important one. A naive implementation appends retrieved passages
to a growing transcript, which means a passage retrieved while a grant was
active stays in the conversation after that grant is revoked. Rebuilding per
message makes revocation take effect on the next question.

The model never decides anything here. It receives a bounded context block and
writes prose; permissions, task state, evidence access and citations are all
computed before and after the call.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, PermissionDenied, ValidationError
from app.core.logging import get_logger
from app.gateway.registry import get as get_call_type
from app.models import ChatMessage, ConversationSummary, TaskConversation
from app.models.enums import ChatRole
from app.services import context_builder
from app.services import focus as focus_service
from app.services import tasks as task_service
from app.services.citations import CITATION as _CITATION
from app.services.citations import recorded_columns, refs_in

log = get_logger("copilot")

RECENT_TURNS = 12
# Roll up when the verbatim tail grows past this.
SUMMARY_THRESHOLD = 24
SUMMARY_BATCH = 12

# Every citable prefix, not just the retrieved chunks. This matched only [S#]
# for as long as [S#] was the only kind of source; when approved evidence [E#]
# and cross-department answers [R#] became citable, the extractor was not
# updated -- so an answer grounded entirely in approved evidence recorded zero
# citations and displayed as "no sources cited". Exactly backwards for the
# reviewer it was meant to inform.
# Re-exported for callers and tests that have always imported it from here.
# The pattern itself is built from CONTEXT_KINDS, so adding a kind updates the
# extractor rather than requiring somebody to remember this line exists.
CITATION = _CITATION


@dataclass
class Answer:
    message: ChatMessage
    sources: list[dict]
    out_of_scope: bool
    refusal: dict | None = None


def create_conversation(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    title: str | None = None,
) -> TaskConversation:
    """Explicit creation only.

    A GET that creates a conversation makes conversation counts meaningless and
    lets a membership check be sidestepped by merely looking at a page.
    """
    actor = user_id or ctx.actor_user_id
    if actor is None:
        raise ValidationError("A conversation belongs to a person.")
    if not task_service.is_on_task(session, ctx, task_id=task_id, user_id=actor):
        raise PermissionDenied("You are not on this task.")
    conversation = task_service.ensure_conversation(session, ctx, task_id=task_id, user_id=actor)
    if title and not conversation.title:
        conversation.title = title
        session.flush()
    return conversation


def get_conversation(
    session: Session, ctx: ExecutionContext, *, conversation_id: uuid.UUID
) -> TaskConversation:
    conversation = session.get(TaskConversation, conversation_id)
    if conversation is None or conversation.org_id != ctx.org_id:
        raise NotFound("That conversation does not exist.")
    return conversation


def conversation_for(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID, user_id: uuid.UUID
) -> TaskConversation | None:
    """Read only. Returns None rather than creating -- see create_conversation."""
    return session.execute(
        select(TaskConversation).where(
            TaskConversation.org_id == ctx.org_id,
            TaskConversation.task_id == task_id,
            TaskConversation.user_id == user_id,
        )
    ).scalar_one_or_none()


def _assert_own(ctx: ExecutionContext, conversation: TaskConversation) -> None:
    """A conversation is private to its person.

    Not even the task owner reads someone else's working thread: half-formed
    exploration is exactly the material that should not travel.
    """
    if ctx.actor_user_id is not None and conversation.user_id != ctx.actor_user_id:
        raise PermissionDenied("This conversation belongs to another person.")


ALL_FOCUS = "*"


def _newest_first():
    """Newest first; a question and its answer share a transaction timestamp, so the
    answer counts as the later of the two."""
    return (
        ChatMessage.created_at.desc(),
        case((ChatMessage.role == "USER", 0), else_=1).desc(),
    )


def _only_focus(query, focus_key: str | None):
    """Keep the turns of one thread: the task as a whole (None) or one focus."""
    if focus_key == ALL_FOCUS:
        return query
    if focus_key is None:
        return query.where(ChatMessage.focus_key.is_(None))
    return query.where(ChatMessage.focus_key == focus_key)


def messages(
    session: Session,
    ctx: ExecutionContext,
    *,
    conversation_id: uuid.UUID,
    limit: int = 50,
    focus_key: str | None = ALL_FOCUS,
) -> list[ChatMessage]:
    conversation = get_conversation(session, ctx, conversation_id=conversation_id)
    _assert_own(ctx, conversation)
    rows = (
        session.execute(
            _only_focus(
                select(ChatMessage).where(
                    ChatMessage.org_id == ctx.org_id,
                    ChatMessage.conversation_id == conversation_id,
                ),
                focus_key,
            )
            .order_by(*_newest_first())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return list(reversed(rows))


def summaries(
    session: Session, ctx: ExecutionContext, *, conversation_id: uuid.UUID
) -> list[ConversationSummary]:
    return list(
        session.execute(
            select(ConversationSummary)
            .where(
                ConversationSummary.org_id == ctx.org_id,
                ConversationSummary.conversation_id == conversation_id,
            )
            .order_by(ConversationSummary.created_at)
        )
        .scalars()
        .all()
    )


def _history_block(
    session: Session,
    ctx: ExecutionContext,
    *,
    conversation_id: uuid.UUID,
    focus_key: str | None = None,
) -> str:
    """The turns of THIS thread only.

    A thread is the task as a whole, or one focus (some requirements, one section).
    Another thread's turns are never shown, so what was said about one thing cannot
    be mistaken for talk about another. Rolled-up summaries belong to the task-wide
    thread only; a focus thread is short and shown verbatim.
    """
    parts: list[str] = []
    rolled = summaries(session, ctx, conversation_id=conversation_id) if focus_key is None else []
    if rolled:
        parts.append("EARLIER IN THIS CONVERSATION (summarised)")
        parts.extend(f"- {s.summary}" for s in rolled)
        parts.append("")
    recent = (
        session.execute(
            _only_focus(
                select(ChatMessage).where(
                    ChatMessage.org_id == ctx.org_id,
                    ChatMessage.conversation_id == conversation_id,
                    ChatMessage.role.in_([ChatRole.USER, ChatRole.ASSISTANT]),
                ),
                focus_key,
            )
            .order_by(*_newest_first())
            .limit(RECENT_TURNS)
        )
        .scalars()
        .all()
    )
    if recent:
        parts.append("RECENT TURNS")
        for message in reversed(recent):
            parts.append(f"{message.role.value}: {message.content}")
    return "\n".join(parts)


def ask(
    session: Session,
    ctx: ExecutionContext,
    *,
    conversation_id: uuid.UUID,
    question: str,
    gateway,
    embedder=None,
    can_change_diagrams: bool | None = None,
    focus: dict | None = None,
) -> Answer:
    """One turn: rebuild context, call the model, record what it was shown."""
    if not (question or "").strip():
        raise ValidationError("A question needs text.")
    conversation = get_conversation(session, ctx, conversation_id=conversation_id)
    _assert_own(ctx, conversation)

    # What the person is looking at, checked against the task. Resolved before
    # anything is written, so a bad claim is refused rather than recorded.
    resolved = focus_service.resolve(session, ctx, conversation.task_id, focus)
    key = resolved.key if resolved else None
    stored = resolved.data if resolved else None

    user_message = _append(
        session,
        ctx,
        conversation=conversation,
        role=ChatRole.USER,
        content=question.strip(),
        focus_key=key,
        focus=stored,
    )

    # A diagram request is handled in this same conversation: the assistant draws,
    # changes, restyles or reviews, and says what it did. Anything else falls through
    # to the ordinary answer below.
    from app.services import diagram_chat

    previous = session.execute(
        select(ChatMessage)
        .where(
            ChatMessage.org_id == ctx.org_id,
            ChatMessage.conversation_id == conversation.id,
            ChatMessage.role == ChatRole.ASSISTANT,
            ChatMessage.id != user_message.id,
            ChatMessage.focus_key.is_(None) if key is None else ChatMessage.focus_key == key,
        )
        .order_by(*_newest_first())
        .limit(1)
    ).scalar_one_or_none()
    recent = [
        {"role": m.role.value, "text": (m.content or "")[:400]}
        for m in reversed(
            session.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.org_id == ctx.org_id,
                    ChatMessage.conversation_id == conversation.id,
                    ChatMessage.id != user_message.id,
                    ChatMessage.focus_key.is_(None)
                    if key is None
                    else ChatMessage.focus_key == key,
                )
                .order_by(*_newest_first())
                .limit(6)
            )
            .scalars()
            .all()
        )
    ]
    handled = diagram_chat.handle(
        session,
        ctx,
        task_id=conversation.task_id,
        question=question,
        last_assistant=previous,
        gateway=gateway,
        recent=recent,
        can_write=can_change_diagrams,
        focus=({"label": resolved.label, "text": resolved.block[:1500]} if resolved else None),
    )
    if handled is not None:
        if (handled["trace"] or {}).get("writeup"):
            handled["trace"]["writeup"]["conversation_id"] = str(conversation.id)
        assistant = _append(
            session,
            ctx,
            conversation=conversation,
            role=ChatRole.ASSISTANT,
            content=handled["content"],
            retrieval_trace=handled["trace"],
            focus_key=key,
            focus=stored,
        )
        return Answer(message=assistant, sources=[], out_of_scope=False)

    built = context_builder.build(
        session,
        ctx,
        task_id=conversation.task_id,
        # The question alone can be "is this covered?" -- retrieval needs the words of
        # what "this" is.
        query=(question + " " + resolved.hint[:600]) if resolved else question,
        user_id=conversation.user_id,
        embedder=embedder,
    )

    # With a focus the model still has something to say when no company evidence
    # was admissible: what the focus itself records (no evidence, a colleague's
    # reply, the company's assumption) is exactly what the person is asking about,
    # and it is all task data they already see on screen -- nothing from the
    # restricted corpus. A blanket "nothing covers that" would hide it. The prompt
    # tells the model what to do when the context holds no answer.
    refuse = built.out_of_scope and resolved is None
    if refuse:
        # Not a flat refusal, and not one message for two different problems.
        # "You are not allowed to see this" and "nothing on file covers this"
        # need opposite next steps, and telling someone the second while saying
        # the first sends them to ask a colleague for material that does not
        # exist.
        refusal = built.refusal()
        if refusal["kind"] == "NOT_PERMITTED":
            owners = ", ".join(d["name"] for d in refusal["likely_owning_departments"])
            content = (
                "I can't answer that from anything this task is authorized to see.\n\n"
                + (f"Likely owner(s): {owners}.\n" if owners else "")
                + "Use 'Ask another department' below: they reply in writing and can "
                "attach the evidence that backs the answer."
            )
        else:
            owners = ", ".join(d["name"] for d in refusal["likely_owning_departments"])
            content = (
                "Nothing in this task's evidence covers that.\n\n"
                "This is not a permission problem -- I can see this task's material and "
                "none of it is about your question. Try naming the requirement or the "
                "document you have in mind."
                + (
                    f"\n\nIf another department holds it, these are the likely ones: {owners}."
                    if owners
                    else ""
                )
            )
        assistant = _append(
            session,
            ctx,
            conversation=conversation,
            role=ChatRole.ASSISTANT,
            content=content,
            focus_key=key,
            focus=stored,
            retrieval_trace={
                **built.trace,
                "outcome": refusal["kind"].lower(),
                # Carried on the message so the screen can offer the request
                # ladder as buttons rather than prose. Without the department
                # ids here, a refusal is a paragraph telling someone to do
                # something the product gives them no way to do.
                "suggested_departments": refusal["likely_owning_departments"],
                "request_options": [o["request_type"] for o in refusal["options"]],
            },
        )
        return Answer(message=assistant, sources=[], out_of_scope=True, refusal=refusal)

    history = _history_block(session, ctx, conversation_id=conversation_id, focus_key=key)
    prompt = "\n\n".join(
        part
        for part in (
            history,
            built.prompt_block(),
            _departments_block(session, ctx, built),
            resolved.block if resolved else "",
            f"QUESTION: {question}",
        )
        if part
    )
    response = gateway.generate("task_copilot", prompt, ctx=ctx, session=session)

    cited = _cited_sources(response.text, built.sources)
    # Which citations landed in which column is decided by CONTEXT_KINDS, not
    # by a list written out here. Both times this was hand-written, one kind
    # was extracted and then dropped on the floor -- and the symptom was an
    # answer reporting that it rested on nothing while citing its source four
    # times. A kind added to the table without a column now fails at import.
    refs = refs_in(response.text)
    recorded = recorded_columns(
        refs,
        {
            "evidence": built.evidence_refs,
            "resolution": built.resolution_refs,
            "retrieved": {f"S{i}": s.id for i, s in enumerate(built.sources, start=1)},
        },
    )
    # Chunk ids stay as the resolver computed them: it matches on the source
    # list rather than on ordinal position, which survives re-ranking.
    recorded["cited_chunk_ids"] = [s.id for s in cited]
    assistant = _append(
        session,
        ctx,
        conversation=conversation,
        role=ChatRole.ASSISTANT,
        content=response.text,
        # Which prompt produced this answer. The column has existed since the
        # table was created and nothing ever wrote to it, so an answer could
        # never be traced to the instructions that shaped it -- and after a
        # prompt was rewritten there was no way to tell an answer from before
        # the change from one after it. In a system whose whole claim is that
        # every statement is attributable, "which rules were in force" is not
        # an optional field.
        prompt_version=get_call_type("task_copilot").prompt_version,
        focus_key=key,
        focus=stored,
        **recorded,
        retrieval_trace={
            **built.trace,
            **_request_buttons(
                session,
                ctx,
                built=built,
                question=question,
                answer_text=response.text,
                gateway=gateway,
            ),
            "outcome": "answered",
            "focus": resolved.label if resolved else None,
            "prompt_version": get_call_type("task_copilot").prompt_version,
            "sources_offered": [s.as_dict() for s in built.sources],
            "sources_cited": sorted(refs),
        },
        model_id=response.model_id,
    )

    maybe_summarize(session, ctx, conversation_id=conversation_id, gateway=gateway)
    _ = user_message
    return Answer(
        message=assistant,
        sources=[s.as_dict() for s in cited],
        out_of_scope=False,
    )


def _departments_block(session: Session, ctx: ExecutionContext, built) -> str:
    """The departments that actually exist, so a gap is sent somewhere real.

    Seen live: asked who holds third-party licence terms, the copilot named
    "Procurement / Vendor Management" and "IT / Architecture" -- neither exists
    in the organization, so the advice could not be followed. Listing the real
    ones (and marking the likely holders) is what lets the answer be acted on.
    """
    from app.models import Department, UserDepartment

    rows = (
        session.execute(
            select(Department)
            .where(
                Department.org_id == ctx.org_id,
                Department.id.in_(
                    select(UserDepartment.department_id).where(UserDepartment.org_id == ctx.org_id)
                ),
            )
            .order_by(Department.name)
        )
        .scalars()
        .all()
    )
    if not rows:
        return ""
    likely = {d["code"] for d in built.suggested_departments}
    lines = ["DEPARTMENTS IN THIS COMPANY (name only these when saying who to ask)"]
    for d in rows:
        if d.id == built.task.department_id:
            mark = "  <- THIS TASK'S OWN DEPARTMENT: its part is the reader's own work"
        elif d.code.value in likely:
            mark = "  <- likely holder for this task"
        else:
            mark = ""
        from app.services.routing import responsibilities_of

        duty = responsibilities_of(d)
        lines.append(f"- {d.name} ({d.code.value}){': ' + duty if duty else ''}{mark}")
    return "\n".join(lines)


def _request_buttons(
    session: Session, ctx: ExecutionContext, *, built, question: str, answer_text: str, gateway
) -> dict:
    """Offer the request buttons under an answer when another department is needed.

    A separate model call proposes whether and whom (`department_routing`);
    `routing.route` checks every name against the departments that exist and
    falls back to rules if the call fails. The buttons only pre-fill a request;
    a person still sends it.
    """
    from app.services import routing
    from app.services.context_builder import REQUEST_LADDER

    decision = routing.route(
        session,
        ctx,
        task=built.task,
        question=question,
        answer_text=answer_text,
        requirement_block=built.requirement_block or "",
        fallback_departments=built.suggested_departments,
        gateway=gateway,
    )
    trace: dict = {"routing": decision}
    if decision["needed"]:
        trace["suggested_departments"] = decision["departments"]
        trace["request_options"] = [o["request_type"] for o in REQUEST_LADDER]
    return trace


def _cited_sources(text: str, sources) -> list:
    """Which offered sources the answer actually cited.

    Stored separately from what was offered. An answer citing two of eight
    sources is a different thing from one that had two available, and a reviewer
    needs to see which.
    """
    refs = set(CITATION.findall(text or ""))
    return [s for s in sources if s.ref in refs]


def _append(
    session: Session,
    ctx: ExecutionContext,
    *,
    conversation: TaskConversation,
    role: ChatRole,
    content: str,
    cited_chunk_ids: list[uuid.UUID] | None = None,
    cited_evidence_ids: list[uuid.UUID] | None = None,
    cited_request_ids: list[uuid.UUID] | None = None,
    prompt_version: str | None = None,
    retrieval_trace: dict | None = None,
    model_id: str | None = None,
    focus_key: str | None = None,
    focus: dict | None = None,
) -> ChatMessage:
    message = ChatMessage(
        # Stamped here, not by the database, which gives every row of one transaction the
        # same time and so cannot tell a question from the answer saved with it.
        created_at=datetime.now(timezone.utc),
        focus_key=focus_key,
        focus=focus,
        org_id=ctx.org_id,
        conversation_id=conversation.id,
        role=role,
        content=content,
        cited_chunk_ids=cited_chunk_ids or None,
        cited_evidence_ids=cited_evidence_ids or None,
        cited_request_ids=cited_request_ids or None,
        prompt_version=prompt_version,
        retrieval_trace=retrieval_trace,
        model_id=model_id,
        request_id=ctx.request_id,
    )
    session.add(message)
    conversation.message_count += 1
    conversation.last_message_at = datetime.now(timezone.utc)
    session.flush()
    return message


def record_event(
    session: Session,
    ctx: ExecutionContext,
    *,
    conversation_id: uuid.UUID,
    content: str,
) -> ChatMessage:
    """A system event in the thread -- an answer arriving, a grant expiring.

    Written as a message so the contributor sees why their context changed
    between one question and the next.
    """
    conversation = get_conversation(session, ctx, conversation_id=conversation_id)
    return _append(
        session, ctx, conversation=conversation, role=ChatRole.SYSTEM_EVENT, content=content
    )


def maybe_summarize(
    session: Session,
    ctx: ExecutionContext,
    *,
    conversation_id: uuid.UUID,
    gateway,
) -> ConversationSummary | None:
    """Roll up the oldest unsummarised turns once the thread gets long.

    The summary records exactly which messages it covers. A summary that cannot
    say what it replaces cannot be safely regenerated or re-expanded, and
    silently becomes a second source of truth.
    """
    existing = summaries(session, ctx, conversation_id=conversation_id)
    covered_to = existing[-1].covers_to_message_id if existing else None

    query = (
        select(ChatMessage)
        .where(
            ChatMessage.org_id == ctx.org_id,
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.role.in_([ChatRole.USER, ChatRole.ASSISTANT]),
            # Only the task-wide thread is rolled up; a focus thread stays verbatim.
            ChatMessage.focus_key.is_(None),
        )
        .order_by(ChatMessage.created_at, case((ChatMessage.role == "USER", 0), else_=1))
    )
    all_messages = list(session.execute(query).scalars().all())
    if covered_to is not None:
        ids = [m.id for m in all_messages]
        if covered_to in ids:
            all_messages = all_messages[ids.index(covered_to) + 1 :]

    if len(all_messages) <= SUMMARY_THRESHOLD:
        return None

    batch = all_messages[:SUMMARY_BATCH]
    transcript = "\n".join(f"{m.role.value}: {m.content}" for m in batch)
    response = gateway.generate("conversation_summary", transcript, ctx=ctx, session=session)

    summary = ConversationSummary(
        org_id=ctx.org_id,
        conversation_id=conversation_id,
        covers_from_message_id=batch[0].id,
        covers_to_message_id=batch[-1].id,
        summary=response.text.strip(),
        token_count=len(response.text.split()),
    )
    session.add(summary)
    session.flush()
    return summary


def transcript(session: Session, ctx: ExecutionContext, *, conversation_id: uuid.UUID) -> dict:
    """The whole thread as a reviewer sees it: summaries, turns, and traces."""
    conversation = get_conversation(session, ctx, conversation_id=conversation_id)
    _assert_own(ctx, conversation)
    return {
        "conversation_id": str(conversation.id),
        "task_id": str(conversation.task_id),
        "user_id": str(conversation.user_id),
        "message_count": conversation.message_count,
        "summaries": [
            {
                "summary": s.summary,
                "covers_from": str(s.covers_from_message_id),
                "covers_to": str(s.covers_to_message_id),
            }
            for s in summaries(session, ctx, conversation_id=conversation.id)
        ],
        "messages": [
            {
                "id": str(m.id),
                "role": m.role.value,
                "content": m.content,
                "cited_chunk_ids": [str(c) for c in (m.cited_chunk_ids or [])],
                "model_id": m.model_id,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages(session, ctx, conversation_id=conversation.id, limit=500)
        ],
    }


def retrieval_trace(session: Session, ctx: ExecutionContext, *, message_id: uuid.UUID) -> dict:
    """Exactly what the model was shown for one answer, and why.

    This is the route an investigator takes after a suspected leak. Without it,
    "the model must have seen it" is an argument nobody can settle.
    """
    message = session.get(ChatMessage, message_id)
    if message is None or message.org_id != ctx.org_id:
        raise NotFound("That message does not exist.")
    conversation = get_conversation(session, ctx, conversation_id=message.conversation_id)
    _assert_own(ctx, conversation)
    return {
        "message_id": str(message.id),
        "role": message.role.value,
        "model_id": message.model_id,
        "trace": message.retrieval_trace or {},
    }
