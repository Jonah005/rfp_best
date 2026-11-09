from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_context, get_db, require
from app.core.context import ExecutionContext
from app.core.permissions import Perm
from app.models import AuditEvent
from app.schemas.common import AuditOut

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=list[AuditOut], dependencies=[Depends(require(Perm.AUDIT_READ))])
def list_events(
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    action: str | None = None,
    limit: int = Query(default=100, le=500),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[AuditOut]:
    stmt = select(AuditEvent).where(AuditEvent.org_id == ctx.org_id)
    if entity_type:
        stmt = stmt.where(AuditEvent.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditEvent.entity_id == entity_id)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    stmt = stmt.order_by(AuditEvent.occurred_at.desc()).limit(limit)
    return [AuditOut.model_validate(e) for e in session.execute(stmt).scalars().all()]
