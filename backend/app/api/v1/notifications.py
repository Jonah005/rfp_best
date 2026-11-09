"""What is waiting on the person currently signed in.

Every route here is scoped to the caller and takes no recipient parameter. That
is the whole security model of this module, and it is deliberately not
expressible from outside: there is no query string that reads somebody else's
feed, because the service reads `ctx.actor_user_id` and nothing else.

No permission guards these routes either, and that is correct rather than an
oversight. A notification is addressed to one person by name; holding it back
until they also hold `task:read` would mean the one thing a permission check
protects here is a person's own doorbell. What the notification may say is
constrained at the point it is written, not at the point it is read.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_context, get_db
from app.core.context import ExecutionContext
from app.schemas.notifications import NotificationOut, UnreadCount
from app.services import notifications as service

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=list[NotificationOut])
def list_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=service.PAGE, ge=1, le=service.PAGE),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[NotificationOut]:
    """The bell's dropdown asks for unread; the activity list asks for all."""
    rows = service.for_user(session, ctx, unread_only=unread_only, limit=limit)
    return [NotificationOut.model_validate(row) for row in rows]


@router.get("/count", response_model=UnreadCount)
def count(
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> UnreadCount:
    """Polled on a timer, so it stays a single indexed count and returns one int."""
    return UnreadCount(unread=service.unread_count(session, ctx))


@router.post("/{notification_id}/read", response_model=NotificationOut)
def mark_read(
    notification_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> NotificationOut:
    return NotificationOut.model_validate(
        service.mark_read(session, ctx, notification_id=notification_id)
    )


@router.post("/read-all", response_model=UnreadCount)
def mark_all_read(
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> UnreadCount:
    """Clears the badge. Read state only -- the activity list keeps every row."""
    service.mark_all_read(session, ctx)
    return UnreadCount(unread=0)
