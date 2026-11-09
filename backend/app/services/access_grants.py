"""AccessGrant lifecycle.

Correction 3 -- the three cross-department outcomes have deliberately different
consequences, and this module is where that difference is real:

  * answer          -> no grant at all
  * share_evidence  -> ONE narrow task-scoped READ grant PER shared evidence id
  * grant_access    -> the broader requested task-scoped grant
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.models import AccessGrant
from app.models.enums import GrantPermission, GrantResourceType, GrantState
from app.services import audit

DEFAULT_GRANT_TTL = timedelta(days=30)


def _validate_expiry(expires_at: datetime) -> datetime:
    if expires_at.tzinfo is None:
        raise ValidationError("expires_at must be timezone-aware.")
    if expires_at <= datetime.now(timezone.utc):
        raise ValidationError("expires_at must be in the future.")
    return expires_at


def create_grant(
    session: Session,
    ctx: ExecutionContext,
    *,
    grantee_user_id: uuid.UUID,
    task_id: uuid.UUID,
    resource_type: GrantResourceType,
    resource_id: uuid.UUID,
    permission: GrantPermission = GrantPermission.READ,
    reason: str,
    expires_at: datetime | None = None,
    origin_request_id: uuid.UUID | None = None,
) -> AccessGrant:
    if not reason or not reason.strip():
        raise ValidationError("A reason is required for every access grant.")
    expiry = _validate_expiry(expires_at or datetime.now(timezone.utc) + DEFAULT_GRANT_TTL)

    grant = AccessGrant(
        org_id=ctx.org_id,
        grantee_user_id=grantee_user_id,
        task_id=task_id,
        resource_type=resource_type,
        resource_id=resource_id,
        permission=permission,
        reason=reason.strip(),
        expires_at=expiry,
        granted_by=ctx.actor_user_id,
        origin_request_id=origin_request_id,
    )
    session.add(grant)
    session.flush()
    audit.record(
        session,
        ctx,
        action="access_grant.created",
        entity_type="access_grant",
        entity_id=grant.id,
        after={
            "grantee_user_id": str(grantee_user_id),
            "task_id": str(task_id),
            "resource_type": resource_type.value,
            "resource_id": str(resource_id),
            "permission": permission.value,
            "expires_at": expiry.isoformat(),
        },
        reason=reason,
    )
    return grant


def share_evidence(
    session: Session,
    ctx: ExecutionContext,
    *,
    grantee_user_id: uuid.UUID,
    task_id: uuid.UUID,
    evidence_ids: Sequence[uuid.UUID],
    reason: str,
    expires_at: datetime | None = None,
    origin_request_id: uuid.UUID | None = None,
) -> list[AccessGrant]:
    """Share Evidence: one narrow READ grant per evidence id, nothing wider."""
    if not evidence_ids:
        raise ValidationError("At least one evidence id is required to share evidence.")
    return [
        create_grant(
            session,
            ctx,
            grantee_user_id=grantee_user_id,
            task_id=task_id,
            resource_type=GrantResourceType.EVIDENCE,
            resource_id=eid,
            permission=GrantPermission.READ,
            reason=reason,
            expires_at=expires_at,
            origin_request_id=origin_request_id,
        )
        for eid in evidence_ids
    ]


def revoke_grant(
    session: Session,
    ctx: ExecutionContext,
    *,
    grant_id: uuid.UUID,
    reason: str,
) -> AccessGrant:
    grant = session.get(AccessGrant, grant_id)
    if grant is None or grant.org_id != ctx.org_id:
        raise NotFound("Access grant not found.")
    if grant.revoked_at is not None:
        return grant
    before = {"effective_state": grant.effective_state().value}
    grant.revoked_at = datetime.now(timezone.utc)
    grant.revoked_by = ctx.actor_user_id
    grant.revoke_reason = reason
    session.flush()
    audit.record(
        session,
        ctx,
        action="access_grant.revoked",
        entity_type="access_grant",
        entity_id=grant.id,
        before=before,
        after={"effective_state": GrantState.REVOKED.value},
        reason=reason,
    )
    return grant


def revoke_for_task(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID, reason: str
) -> int:
    """Called when a task completes or its bid closes."""
    grants = (
        session.execute(
            select(AccessGrant).where(
                AccessGrant.org_id == ctx.org_id,
                AccessGrant.task_id == task_id,
                AccessGrant.revoked_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    for grant in grants:
        revoke_grant(session, ctx, grant_id=grant.id, reason=reason)
    return len(grants)


def list_grants(
    session: Session,
    ctx: ExecutionContext,
    *,
    grantee_user_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    effective_state: GrantState | None = None,
) -> list[AccessGrant]:
    stmt = select(AccessGrant).where(AccessGrant.org_id == ctx.org_id)
    if grantee_user_id:
        stmt = stmt.where(AccessGrant.grantee_user_id == grantee_user_id)
    if task_id:
        stmt = stmt.where(AccessGrant.task_id == task_id)
    grants = list(session.execute(stmt).scalars().all())
    if effective_state is not None:
        now = datetime.now(timezone.utc)
        grants = [g for g in grants if g.effective_state(now=now) is effective_state]
    return grants


def materialize_states(session: Session, ctx: ExecutionContext) -> int:
    """Expiry sweep.

    Writes `materialized_state` for listing screens only. Nothing in the
    authorization path reads it -- that path always recomputes from
    expires_at/revoked_at.
    """
    now = datetime.now(timezone.utc)
    grants = (
        session.execute(select(AccessGrant).where(AccessGrant.org_id == ctx.org_id)).scalars().all()
    )
    changed = 0
    for grant in grants:
        state = grant.effective_state(now=now)
        if grant.materialized_state != state:
            grant.materialized_state = state
            grant.materialized_at = now
            changed += 1
    session.flush()
    return changed
