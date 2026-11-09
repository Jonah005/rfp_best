"""Request-scoped dependencies: tenancy, authentication, authorization.

Order matters and is enforced here rather than left to each handler:
authenticate -> build execution context -> open a transaction and bind the
tenant transaction-locally -> check the required permission.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext, reset_context, set_context
from app.core.errors import AuthenticationError, PermissionDenied
from app.core.permissions import Perm
from app.core.security import decode_token
from app.db.session import apply_tenant_context, get_session_factory
from app.models import User
from app.services import identity
from app.services.permission_evaluator import PermissionEvaluator

_bearer = HTTPBearer(auto_error=False)


def get_raw_session() -> Iterator[Session]:
    """A session with NO tenant context. Login only."""
    session = get_session_factory()()
    try:
        session.begin()
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_current_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> ExecutionContext:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("An access token is required.")
    payload = decode_token(credentials.credentials, expect="access")

    org_id = uuid.UUID(payload["org"])
    user_id = uuid.UUID(payload["sub"])

    # Permissions are resolved per request against the database rather than
    # trusted from the token, so a revoked role takes effect immediately.
    session = get_session_factory()()
    try:
        session.begin()
        apply_tenant_context(session, ExecutionContext(org_id=org_id, actor_user_id=user_id))
        user = session.get(User, user_id)
        if user is None or not user.is_active or user.org_id != org_id:
            raise AuthenticationError("The account is no longer active.")
        ctx = ExecutionContext(
            org_id=org_id,
            actor_user_id=user_id,
            actor_kind="USER",
            request_id=getattr(request.state, "request_id", uuid.uuid4()),
            permissions=frozenset(identity.effective_permissions(session, user)),
            role_codes=frozenset(identity.role_codes(session, user)),
            department_ids=frozenset(identity.department_ids(session, user)),
            ip=request.client.host if request.client else None,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    request.state.ctx = ctx
    return ctx


def get_db(ctx: ExecutionContext = Depends(get_current_context)) -> Iterator[Session]:
    """A transaction with the tenant bound transaction-locally (SET LOCAL)."""
    token = set_context(ctx)
    session = get_session_factory()()
    try:
        session.begin()
        apply_tenant_context(session, ctx)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        reset_context(token)


def get_evaluator(
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> PermissionEvaluator:
    return PermissionEvaluator(session, ctx)


def require(*permissions: Perm):
    """Dependency factory: the caller must hold every listed permission."""

    def _dep(ctx: ExecutionContext = Depends(get_current_context)) -> ExecutionContext:
        missing = [p.value for p in permissions if p.value not in ctx.permissions]
        if missing:
            raise PermissionDenied("Missing required permission(s): " + ", ".join(missing))
        return ctx

    return _dep


def require_any(*permissions: Perm):
    """Dependency factory: the caller must hold at least one listed permission.

    This is the coarse half of a two-part check, and it is only correct when the
    fine half exists. Use it where a scoped permission and an unscoped one open
    the same route but grant different reach -- the route can no longer decide
    the answer alone, because the answer depends on the row being acted on. The
    route keeps out everyone holding neither; the service decides between the two.

    A route guarded this way MUST re-check in the service. Reaching for it to
    widen a route without that second check turns a scoped permission into an
    unscoped one.
    """

    def _dep(ctx: ExecutionContext = Depends(get_current_context)) -> ExecutionContext:
        if not any(p.value in ctx.permissions for p in permissions):
            raise PermissionDenied(
                "Missing required permission(s): " + " or ".join(p.value for p in permissions)
            )
        return ctx

    return _dep
