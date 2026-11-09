"""Access grant routes.

The three cross-department outcomes stay three distinct routes so the
least-privilege choice is visible in the API surface and in the audit log.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_context, get_db, require
from app.core.context import ExecutionContext
from app.core.errors import NotFound
from app.core.permissions import Perm
from app.models import AccessGrant
from app.models.enums import GrantPermission, GrantResourceType, GrantState
from app.schemas.common import GrantCreate, GrantOut, RevokeRequest, ShareEvidenceRequest
from app.services import access_grants

router = APIRouter(tags=["grants"])


def _out(grant: AccessGrant) -> GrantOut:
    return GrantOut(
        id=grant.id,
        grantee_user_id=grant.grantee_user_id,
        task_id=grant.task_id,
        resource_type=grant.resource_type.value,
        resource_id=grant.resource_id,
        permission=grant.permission.value,
        reason=grant.reason,
        granted_by=grant.granted_by,
        granted_at=grant.granted_at,
        expires_at=grant.expires_at,
        revoked_at=grant.revoked_at,
        # Computed live, never read from a stored column.
        effective_state=grant.effective_state().value,
    )


@router.post(
    "/grants",
    response_model=GrantOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Perm.GRANT_CREATE))],
    summary="Grant Access - the broader task-scoped grant",
)
def create_grant(
    payload: GrantCreate,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> GrantOut:
    grant = access_grants.create_grant(
        session,
        ctx,
        grantee_user_id=payload.grantee_user_id,
        task_id=payload.task_id,
        resource_type=GrantResourceType(payload.resource_type),
        resource_id=payload.resource_id,
        permission=GrantPermission(payload.permission),
        reason=payload.reason,
        expires_at=payload.expires_at,
    )
    return _out(grant)


@router.post(
    "/grants/share-evidence",
    response_model=list[GrantOut],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Perm.GRANT_CREATE))],
    summary="Share Evidence - one narrow task-scoped READ grant per evidence id",
)
def share_evidence(
    payload: ShareEvidenceRequest,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[GrantOut]:
    grants = access_grants.share_evidence(
        session,
        ctx,
        grantee_user_id=payload.grantee_user_id,
        task_id=payload.task_id,
        evidence_ids=payload.evidence_ids,
        reason=payload.reason,
        expires_at=payload.expires_at,
    )
    return [_out(g) for g in grants]


@router.get("/grants", response_model=list[GrantOut])
def list_grants(
    task_id: uuid.UUID | None = None,
    grantee_user_id: uuid.UUID | None = None,
    effective_state: str | None = Query(
        default=None,
        description="Evaluated against (revoked_at IS NULL AND expires_at > now()), not a column.",
    ),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[GrantOut]:
    grants = access_grants.list_grants(
        session,
        ctx,
        grantee_user_id=grantee_user_id,
        task_id=task_id,
        effective_state=GrantState(effective_state) if effective_state else None,
    )
    return [_out(g) for g in grants]


@router.get("/me/grants", response_model=list[GrantOut])
def my_grants(
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[GrantOut]:
    grants = access_grants.list_grants(
        session, ctx, grantee_user_id=ctx.actor_user_id, effective_state=GrantState.ACTIVE
    )
    return [_out(g) for g in grants]


@router.post(
    "/grants/{grant_id}/revoke",
    response_model=GrantOut,
    dependencies=[Depends(require(Perm.GRANT_REVOKE))],
)
def revoke(
    grant_id: uuid.UUID,
    payload: RevokeRequest,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> GrantOut:
    grant = access_grants.revoke_grant(session, ctx, grant_id=grant_id, reason=payload.reason)
    if grant is None:
        raise NotFound("Access grant not found.")
    return _out(grant)
