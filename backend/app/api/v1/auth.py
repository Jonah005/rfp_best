from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_context, get_db, get_raw_session
from app.core.config import get_settings
from app.core.context import ExecutionContext
from app.core.errors import AuthenticationError
from app.core.security import create_token, decode_token
from app.db.session import tenant_session
from app.models import User
from app.schemas.common import LoginRequest, MeOut, RefreshRequest, TokenPair, UserOut
from app.services import identity

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenPair)
def login(payload: LoginRequest, session: Session = Depends(get_raw_session)) -> TokenPair:
    # Step 1: resolve the tenant. This is the only query in the application that
    # runs without a tenant context, and it goes through the narrow
    # auth_lookup_user() function rather than reading app_user directly.
    who = identity.authenticate(session, email=payload.email, password=payload.password)

    # Step 2: everything else happens inside the user's own tenant.
    ctx = ExecutionContext(org_id=who.org_id, actor_user_id=who.user_id)
    with tenant_session(ctx) as tenant_db:
        identity.record_login(tenant_db, ctx, user_id=who.user_id)

    s = get_settings()
    return TokenPair(
        access_token=create_token(user_id=who.user_id, org_id=who.org_id, kind="access"),
        refresh_token=create_token(user_id=who.user_id, org_id=who.org_id, kind="refresh"),
        expires_in=s.access_token_ttl_minutes * 60,
    )


@router.post("/refresh", response_model=TokenPair)
def refresh(payload: RefreshRequest, session: Session = Depends(get_raw_session)) -> TokenPair:
    claims = decode_token(payload.refresh_token, expect="refresh")
    user_id = uuid.UUID(claims["sub"])
    org_id = uuid.UUID(claims["org"])

    ctx = ExecutionContext(org_id=org_id, actor_user_id=user_id)
    with tenant_session(ctx) as tenant_db:
        user = tenant_db.get(User, user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("The account is no longer active.")

    s = get_settings()
    return TokenPair(
        access_token=create_token(user_id=user_id, org_id=org_id, kind="access"),
        refresh_token=create_token(user_id=user_id, org_id=org_id, kind="refresh"),
        expires_in=s.access_token_ttl_minutes * 60,
    )


@router.get("/me", response_model=MeOut)
def me(
    ctx: ExecutionContext = Depends(get_current_context),
    session: Session = Depends(get_db),
) -> MeOut:
    user = session.get(User, ctx.actor_user_id)
    return MeOut(
        user=UserOut.model_validate(user),
        org_id=ctx.org_id,
        roles=sorted(ctx.role_codes),
        departments=sorted(ctx.department_ids, key=str),
        permissions=sorted(ctx.permissions),
    )


@router.get("/oidc/authorize", include_in_schema=True, status_code=501)
def oidc_authorize() -> dict:
    """Reserved seam. Identity is an adapter; this is where OIDC/SSO lands."""
    return {"detail": "OIDC is not enabled in this deployment."}
