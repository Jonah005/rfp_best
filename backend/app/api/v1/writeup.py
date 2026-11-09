"""Write-up routes: where a task's write-up stands, finishing it, and keeping it."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_context, get_db, require
from app.core.context import ExecutionContext
from app.core.errors import NotFound, PermissionDenied
from app.core.permissions import Perm
from app.services import artifacts, draft_holds, writeup
from app.services import diagrams as dg

router = APIRouter(tags=["writeup"])

READ = [Depends(require(Perm.ARTIFACT_READ))]
WRITE = [Depends(require(Perm.ARTIFACT_WRITE))]


@router.get("/tasks/{task_id}/writeup", dependencies=READ)
def status(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Asked whenever the task opens: is a write-up running, what is missing, is it final."""
    return jsonable_encoder(writeup.status(session, ctx, task_id=task_id))


@router.post("/tasks/{task_id}/writeup/missing", dependencies=WRITE, status_code=202)
def write_missing(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Write the sections for diagrams that have none yet, leaving the rest alone."""
    state = writeup.status(session, ctx, task_id=task_id)
    wanted = {m["diagram_id"] for m in state["missing"]}
    diagrams = [
        d for d in dg.diagrams_for_task(session, ctx, task_id=task_id) if str(d.id) in wanted
    ]
    plan = writeup.plan_for(session, ctx, task_id=task_id, diagrams=diagrams, instruction=None)
    return _start(session, ctx, plan)


@router.post("/tasks/{task_id}/writeup/all", dependencies=WRITE, status_code=202)
def write_everything(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Write the whole response: a section for every diagram kept in, and one answering
    the task's requirements. Sections already written are written again."""
    diagrams = [d for d in dg.diagrams_for_task(session, ctx, task_id=task_id) if not d.left_out]
    plan = writeup.plan_for(
        session, ctx, task_id=task_id, diagrams=diagrams, instruction=None, requirements=True
    )
    return _start(session, ctx, plan)


def _start(session: Session, ctx: ExecutionContext, plan: writeup.Plan) -> dict:
    # The plan is read from committed rows by the worker, so commit before it starts.
    session.commit()
    if ctx.actor_user_id is None:
        raise PermissionDenied("A write-up is started by a signed-in person.")
    job_id = writeup.start_background(
        plan, org_id=ctx.org_id, user_id=ctx.actor_user_id, request_id=ctx.request_id
    )
    return {"started": job_id is not None, "job_id": job_id}


@router.post("/tasks/{task_id}/writeup/requirements", dependencies=WRITE, status_code=202)
def write_requirements(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Answer the task's requirements (again), in their own section."""
    plan = writeup.plan_for(
        session, ctx, task_id=task_id, diagrams=[], instruction=None, requirements=True
    )
    return _start(session, ctx, plan)


@router.post(
    "/tasks/{task_id}/writeup/diagrams/{diagram_id}/include", dependencies=WRITE, status_code=202
)
def include_diagram(
    task_id: uuid.UUID,
    diagram_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Put a diagram in the written response and write its section."""
    diagram = dg.get_diagram(session, ctx, diagram_id)
    if diagram.task_id != task_id:
        raise NotFound("That diagram is not on this task.")
    dg.set_left_out(session, ctx, diagram_id=diagram_id, left_out=False)
    plan = writeup.plan_for(session, ctx, task_id=task_id, diagrams=[diagram], instruction=None)
    return _start(session, ctx, plan)


@router.post("/tasks/{task_id}/writeup/diagrams/{diagram_id}/leave-out", dependencies=WRITE)
def leave_out_diagram(
    task_id: uuid.UUID,
    diagram_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Keep a diagram on the task but out of the written response; its section goes."""
    diagram = dg.get_diagram(session, ctx, diagram_id)
    if diagram.task_id != task_id:
        raise NotFound("That diagram is not on this task.")
    dg.set_left_out(session, ctx, diagram_id=diagram_id, left_out=True)
    dropped = 0
    try:
        artifact = artifacts.for_task(session, ctx, task_id=task_id)
        dropped = artifacts.drop_diagram_section(
            session, ctx, artifact_id=artifact.id, diagram_id=diagram_id
        )
    except NotFound:
        pass
    return {"left_out": True, "statements_removed": dropped}


@router.post("/tasks/{task_id}/writeup/finalize", dependencies=WRITE)
def finalize(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return jsonable_encoder(writeup.finalize(session, ctx, task_id=task_id))


@router.get("/tasks/{task_id}/writeup/final", dependencies=READ)
def final(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """The finalized write-up with each diagram as drawn when it was finalized."""
    package = writeup.final_package(session, ctx, task_id=task_id)
    return jsonable_encoder(package or {"finalized_at": None, "sections": []})


class HoldIn(BaseModel):
    requirement_ids: list[uuid.UUID] = Field(default_factory=list)
    section: str | None = None
    department_id: uuid.UUID | None = None
    note: str | None = Field(default=None, max_length=600)


@router.get("/tasks/{task_id}/holds", dependencies=READ)
def holds(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    """The parts of the draft that are waiting on another department."""
    return jsonable_encoder(draft_holds.for_task(session, ctx, task_id=task_id))


@router.post("/tasks/{task_id}/holds", dependencies=WRITE, status_code=201)
def mark_waiting(
    task_id: uuid.UUID,
    payload: HoldIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    """Mark a requirement's part, or a section, as waiting on another department."""
    draft_holds.place(
        session,
        ctx,
        task_id=task_id,
        requirement_ids=payload.requirement_ids,
        section=payload.section,
        department_id=payload.department_id,
        note=payload.note,
    )
    return jsonable_encoder(draft_holds.for_task(session, ctx, task_id=task_id))


@router.post("/tasks/{task_id}/holds/{hold_id}/fixed", dependencies=WRITE)
def mark_fixed(
    task_id: uuid.UUID,
    hold_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    """The writer has put that part right: the mark goes."""
    draft_holds.fix(session, ctx, task_id=task_id, hold_id=hold_id)
    return jsonable_encoder(draft_holds.for_task(session, ctx, task_id=task_id))
