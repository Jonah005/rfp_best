"""Parts of the response draft that are waiting on another department.

A writer who needs something from another department should not have to leave the
part blank, and should not forget it is provisional once something is written there.
So the part is marked: it keeps whatever the writer put in it, shows who it is waiting
on, and stays marked until a person says it has been fixed.

Asking a department about requirements (or about a gap that bears on them) marks those
requirements' parts automatically; a writer can also mark a part by hand. The mark is
its own row, not part of the draft's versions, so a new version of the draft cannot
lose it and clearing it does not create a version.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.models import CrossDepartmentRequest, Department, DraftHold, Requirement
from app.services import tasks as task_service
from app.services.todo import REPLY_STATES


def place(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    requirement_ids: list[uuid.UUID] | None = None,
    section: str | None = None,
    department_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    clarification_id: uuid.UUID | None = None,
    note: str | None = None,
) -> list[DraftHold]:
    """Mark parts as waiting. A part already waiting on the same request is left alone."""
    task = task_service.get_task(session, ctx, task_id=task_id)
    on_task = {r.id for r in task_service.task_requirements(session, ctx, task_id=task.id)}
    targets: list[tuple[uuid.UUID | None, str | None]] = [
        (rid, None) for rid in (requirement_ids or []) if rid in on_task
    ]
    if section and section.strip():
        targets.append((None, section.strip()[:200]))
    if not targets:
        raise ValidationError("Pick a requirement or a section to mark.")
    existing = session.execute(
        select(DraftHold).where(
            DraftHold.org_id == ctx.org_id, DraftHold.task_id == task.id, DraftHold.state == "open"
        )
    ).scalars()
    seen = {(h.requirement_id, h.section, h.request_id) for h in existing}
    made: list[DraftHold] = []
    for rid, sec in targets:
        if (rid, sec, request_id) in seen:
            continue
        hold = DraftHold(
            org_id=ctx.org_id,
            task_id=task.id,
            requirement_id=rid,
            section=sec,
            department_id=department_id,
            request_id=request_id,
            clarification_id=clarification_id,
            note=(note or "").strip() or None,
            state="open",
            raised_by=ctx.actor_user_id,
        )
        session.add(hold)
        made.append(hold)
    session.flush()
    return made


def _hold(session: Session, ctx: ExecutionContext, task_id: uuid.UUID, hold_id: uuid.UUID):
    hold = session.get(DraftHold, hold_id)
    if hold is None or hold.org_id != ctx.org_id or hold.task_id != task_id:
        raise NotFound("That part of the draft is not marked on this task.")
    return hold


def fix(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID, hold_id: uuid.UUID
) -> DraftHold:
    """The writer says this part has been put right: the mark goes."""
    hold = _hold(session, ctx, task_id, hold_id)
    hold.state = "fixed"
    hold.fixed_by = ctx.actor_user_id
    hold.fixed_at = datetime.now(timezone.utc)
    session.flush()
    return hold


def reopen(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID, hold_id: uuid.UUID
) -> DraftHold:
    hold = _hold(session, ctx, task_id, hold_id)
    hold.state = "open"
    hold.fixed_by = None
    hold.fixed_at = None
    session.flush()
    return hold


def for_task(session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID) -> list[dict]:
    """Every open mark on the task's draft, with who it is waiting on and whether they replied."""
    task_service.get_task(session, ctx, task_id=task_id)
    holds = list(
        session.execute(
            select(DraftHold)
            .where(
                DraftHold.org_id == ctx.org_id,
                DraftHold.task_id == task_id,
                DraftHold.state == "open",
            )
            .order_by(DraftHold.created_at)
        ).scalars()
    )
    if not holds:
        return []
    codes = {
        r.id: r.code
        for r in session.execute(
            select(Requirement).where(
                Requirement.org_id == ctx.org_id,
                Requirement.id.in_({h.requirement_id for h in holds if h.requirement_id}),
            )
        ).scalars()
    }
    requests = {
        r.id: r
        for r in session.execute(
            select(CrossDepartmentRequest).where(
                CrossDepartmentRequest.org_id == ctx.org_id,
                CrossDepartmentRequest.id.in_({h.request_id for h in holds if h.request_id}),
            )
        ).scalars()
    }
    departments = {
        d.id: d.name
        for d in session.execute(
            select(Department).where(Department.org_id == ctx.org_id)
        ).scalars()
    }
    out = []
    for h in holds:
        req = requests.get(h.request_id) if h.request_id else None
        dept_id = h.department_id or (req.target_department_id if req else None)
        department = departments.get(dept_id) if dept_id else None
        reply = req.resolution_text if req is not None and req.state in REPLY_STATES else None
        out.append(
            {
                "id": str(h.id),
                "requirement_code": codes.get(h.requirement_id) if h.requirement_id else None,
                "section": h.section,
                "department": department,
                "note": h.note,
                "request_id": str(h.request_id) if h.request_id else None,
                "clarification_id": str(h.clarification_id) if h.clarification_id else None,
                "reply_arrived": bool(req is not None and req.state in REPLY_STATES),
                "reply": reply,
                "placeholder": f"[Waiting for {department or 'another department'}]",
            }
        )
    return out
