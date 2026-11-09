"""Request/task execution context.

The tenant identity for a unit of work lives here and is pushed into PostgreSQL
transaction-locally (SET LOCAL). It is never stored on a pooled connection.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field

_current: ContextVar[ExecutionContext | None] = ContextVar("bidready_ctx", default=None)


@dataclass(frozen=True)
class ExecutionContext:
    org_id: uuid.UUID
    actor_user_id: uuid.UUID | None = None
    actor_kind: str = "USER"  # USER | SYSTEM | WORKER
    request_id: uuid.UUID = field(default_factory=uuid.uuid4)
    permissions: frozenset[str] = frozenset()
    role_codes: frozenset[str] = frozenset()
    department_ids: frozenset[uuid.UUID] = frozenset()
    ip: str | None = None

    def has_permission(self, code: str) -> bool:
        return code in self.permissions


def set_context(ctx: ExecutionContext) -> Token:
    return _current.set(ctx)


def reset_context(token: Token) -> None:
    """Clear the execution context.

    A ContextVar Token is only valid in the context that created it. FastAPI
    runs sync endpoints in a threadpool, so a generator dependency's setup and
    teardown can execute in different contexts and the token is then rejected.
    Clearing the variable is the correct outcome either way -- the important
    invariant is that no context survives the request, not which mechanism
    removes it.
    """
    try:
        _current.reset(token)
    except ValueError:
        _current.set(None)


def get_context() -> ExecutionContext | None:
    return _current.get()


def require_context() -> ExecutionContext:
    ctx = _current.get()
    if ctx is None:
        raise RuntimeError(
            "No execution context. Every database unit of work must run inside a "
            "tenant context so RLS and audit have an org and an actor."
        )
    return ctx
