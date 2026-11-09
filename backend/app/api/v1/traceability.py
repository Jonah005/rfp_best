"""Read-only views over the record: claim trace, task timeline, bid dashboard,
the customer clarification letter, and the cross-section consistency check."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_context, get_db, require
from app.core.context import ExecutionContext
from app.core.permissions import Perm
from app.services import consistency, traceability

router = APIRouter(tags=["traceability"])

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@router.get("/statements/{statement_id}/trace", dependencies=[Depends(require(Perm.TASK_READ))])
def trace_statement(
    statement_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Why a claim was made: requirement, evidence, approvals, and where it was released."""
    return traceability.trace_statement(session, ctx, statement_id=statement_id)


@router.get("/tasks/{task_id}/timeline", dependencies=[Depends(require(Perm.TASK_READ))])
def task_timeline(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    """Everything that happened on a task, its answer, approvals and requests, in order."""
    return traceability.timeline(session, ctx, task_id=task_id)


@router.get("/bids/{bid_id}/dashboard", dependencies=[Depends(require(Perm.BID_READ))])
def bid_dashboard(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Coverage, evidence, tasks, approvals and package readiness as separate measures."""
    return traceability.bid_dashboard(session, ctx, bid_id=bid_id)


@router.get("/bids/{bid_id}/clarification-letter", dependencies=[Depends(require(Perm.BID_READ))])
def clarification_letter(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> Response:
    """The open customer questions as a Word document, without changing anything."""
    content, count = traceability.customer_letter(session, ctx, bid_id=bid_id)
    return Response(
        content=content,
        media_type=DOCX,
        headers={
            "Content-Disposition": 'attachment; filename="clarification-request.docx"',
            "X-Question-Count": str(count),
        },
    )


@router.post(
    "/bids/{bid_id}/clarification-letter/send",
    dependencies=[Depends(require(Perm.BID_WRITE))],
)
def send_clarification_letter(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> Response:
    """The same letter, and every question in it marked SENT_TO_CLIENT."""
    content, count = traceability.customer_letter(session, ctx, bid_id=bid_id, mark_sent=True)
    return Response(
        content=content,
        media_type=DOCX,
        headers={
            "Content-Disposition": 'attachment; filename="clarification-request.docx"',
            "X-Question-Count": str(count),
        },
    )


@router.post("/bids/{bid_id}/consistency-check", dependencies=[Depends(require(Perm.BID_WRITE))])
def consistency_check(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Check approved answers against each other; open a resolution task per contradiction."""
    return consistency.check_bid(session, ctx, bid_id=bid_id)


class ResolveConflict(BaseModel):
    outcome: str
    note: str


@router.post(
    "/tasks/{task_id}/resolve-conflict",
    dependencies=[Depends(require(Perm.TASK_WRITE))],
)
def resolve_conflict(
    task_id: uuid.UUID,
    payload: ResolveConflict,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Settle a conflict task: fixed in the answers, or not a real conflict."""
    task = consistency.resolve(
        session, ctx, task_id=task_id, outcome=payload.outcome, note=payload.note
    )
    return {"id": str(task.id), "state": task.state.value}
