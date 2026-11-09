"""Who needs to know, and have they seen it.

One `notify()` is the whole write surface. Call sites say what happened and to
whom it matters; everything else -- suppressing the actor, resolving a
department to its members, choosing where the link lands -- happens here. That
matters more than it looks: routing rules drift the moment they are copied,
and the failure mode is not a crash but a person quietly never being told.

Two rules hold everywhere in this module.

A notification never tells anyone something they could not otherwise see. It
carries a title, a kind, and a place to go -- never evidence text, never a
document extract, never the contents of a record the recipient may not open.
"HR answered your request" is safe to send to the requester; the answer itself
is already governed by the request, and the copilot's authorized context is
where it belongs. Anything else would be a side channel around a permission
check, built by the one part of the system nobody thinks to audit.

And nobody is notified of their own action. A person who just clicked a button
does not need to be told they clicked it, and a feed full of your own work is
a feed people stop reading.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound
from app.models import Notification, UserDepartment

log = structlog.get_logger(__name__)

# How many a person can accumulate before the list stops being a list. The
# unread badge is uncapped; this bounds the page.
PAGE = 100


def notify(
    session: Session,
    ctx: ExecutionContext,
    *,
    recipients: list[uuid.UUID],
    kind: str,
    title: str,
    screen: str,
    body: str | None = None,
    bid_id: uuid.UUID | None = None,
    focus_id: uuid.UUID | None = None,
) -> list[Notification]:
    """Tell each recipient once, never the actor, never a duplicate."""
    unique: list[uuid.UUID] = []
    for user_id in recipients:
        if user_id is None or user_id == ctx.actor_user_id or user_id in unique:
            continue
        unique.append(user_id)

    rows = [
        Notification(
            org_id=ctx.org_id,
            recipient_user_id=user_id,
            actor_user_id=ctx.actor_user_id,
            kind=kind,
            title=title,
            body=body,
            screen=screen,
            bid_id=bid_id,
            focus_id=focus_id,
        )
        for user_id in unique
    ]
    for row in rows:
        session.add(row)
    if rows:
        session.flush()
        log.info("notified", kind=kind, recipients=len(rows))
    return rows


def department_members(
    session: Session, ctx: ExecutionContext, *, department_id: uuid.UUID
) -> list[uuid.UUID]:
    """Everyone in a department.

    A request is addressed to a department, not a person, so telling "the
    department" means telling each of its members. A department with no members
    returns nothing and the request is raised into an empty room -- which is a
    real condition worth seeing rather than a routing bug to paper over.
    """
    return list(
        session.execute(
            select(UserDepartment.user_id).where(
                UserDepartment.org_id == ctx.org_id,
                UserDepartment.department_id == department_id,
            )
        )
        .scalars()
        .all()
    )


def for_user(
    session: Session,
    ctx: ExecutionContext,
    *,
    unread_only: bool = False,
    limit: int = PAGE,
) -> list[Notification]:
    """This person's notifications, newest first.

    Scoped to `ctx.actor_user_id` and to nothing else. There is deliberately no
    recipient parameter: an endpoint that accepted one would let anyone read
    anyone's feed by changing a query string, and every caller wants their own.
    """
    if ctx.actor_user_id is None:
        return []
    query = select(Notification).where(
        Notification.org_id == ctx.org_id,
        Notification.recipient_user_id == ctx.actor_user_id,
    )
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    query = query.order_by(Notification.created_at.desc()).limit(min(limit, PAGE))
    return list(session.execute(query).scalars().all())


def unread_count(session: Session, ctx: ExecutionContext) -> int:
    if ctx.actor_user_id is None:
        return 0
    return int(
        session.execute(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.org_id == ctx.org_id,
                Notification.recipient_user_id == ctx.actor_user_id,
                Notification.read_at.is_(None),
            )
        ).scalar_one()
    )


def mark_read(
    session: Session, ctx: ExecutionContext, *, notification_id: uuid.UUID
) -> Notification:
    """Mark one as read. Only ever your own."""
    row = session.get(Notification, notification_id)
    # Same answer for "does not exist" and "is not yours": telling someone that
    # a notification they cannot read does exist is itself a disclosure.
    if row is None or row.org_id != ctx.org_id or row.recipient_user_id != ctx.actor_user_id:
        raise NotFound("That notification does not exist.")
    if row.read_at is None:
        row.read_at = datetime.now(timezone.utc)
        session.flush()
    return row


def mark_all_read(session: Session, ctx: ExecutionContext) -> int:
    """Clear the badge. Read state only -- nothing is deleted."""
    if ctx.actor_user_id is None:
        return 0
    result = session.execute(
        update(Notification)
        .where(
            Notification.org_id == ctx.org_id,
            Notification.recipient_user_id == ctx.actor_user_id,
            Notification.read_at.is_(None),
        )
        .values(read_at=datetime.now(timezone.utc))
    )
    session.flush()
    return int(getattr(result, "rowcount", 0) or 0)
