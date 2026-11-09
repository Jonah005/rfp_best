"""Audit logging.

Every significant transition writes an AuditEvent in the SAME transaction as the
change. If the audit write fails, the transition fails -- an unlogged state
change is worse than a rejected request.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.models import AuditEvent
from app.models.enums import ActorKind


def record(
    session: Session,
    ctx: ExecutionContext,
    *,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    bid_id: uuid.UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        org_id=ctx.org_id,
        actor_user_id=ctx.actor_user_id,
        actor_kind=ActorKind(ctx.actor_kind),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        bid_id=bid_id,
        before_state=before,
        after_state=after,
        reason=reason,
        request_id=ctx.request_id,
        ip=ctx.ip,
    )
    session.add(event)
    session.flush()
    return event
