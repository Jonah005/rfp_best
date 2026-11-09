"""Engine, session factory, and transaction-local tenant context.

Correction 11: the RLS tenant GUC is set with SET LOCAL inside the transaction
that needs it. A session-level SET would survive the connection's return to the
pool and leak one request's tenant into the next request that borrowed the same
connection -- precisely the failure the policy exists to prevent.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.context import ExecutionContext, require_context
from app.core.logging import get_logger


def escape_for_alembic_config(url: str) -> str:
    """Escape ``%`` for Alembic's configparser-backed ``set_main_option``.

    Alembic stores main options in a ConfigParser that performs ``%``
    interpolation, so a value containing a literal ``%`` raises at read time.
    Percent signs are common in real connection strings -- a URL-encoded socket
    path, or simply a password containing ``%`` after percent-encoding -- so the
    value is escaped rather than assumed safe.
    """
    return url.replace("%", "%%")


TENANT_GUC = "app.current_org"
ACTOR_GUC = "app.current_actor"

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = create_engine(
            s.database_url,
            echo=s.db_echo,
            pool_size=s.db_pool_size,
            max_overflow=s.db_max_overflow,
            pool_pre_ping=True,
            future=True,
        )
        _install_pool_guards(_engine)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
        )
    return _SessionFactory


def _install_pool_guards(engine: Engine) -> None:
    """Assert that no tenant identity rides a connection back into the pool.

    SET LOCAL is discarded at COMMIT/ROLLBACK, so this should always hold. It is
    checked anyway: a silent leak here would be a cross-tenant disclosure, which
    is worth one cheap RESET per checkin to rule out.
    """

    @event.listens_for(engine, "reset")
    def _reset(dbapi_connection, connection_record, reset_state):  # noqa: ANN001
        try:
            with dbapi_connection.cursor() as cur:
                cur.execute(f"RESET {TENANT_GUC}; RESET {ACTOR_GUC};")
        except Exception as exc:  # pragma: no cover - connection already broken
            # Raising here would mask the original failure and the connection is
            # being discarded anyway. The tenant GUC is transaction-local, so a
            # broken connection cannot carry one back into the pool -- but log
            # it, because a pattern of these is worth noticing.
            get_logger("db.pool").debug("guc_reset_failed", error=type(exc).__name__)


def apply_tenant_context(session: Session, ctx: ExecutionContext) -> None:
    """Bind the tenant to the CURRENT transaction only."""
    session.execute(
        text(f"SELECT set_config('{TENANT_GUC}', :org, true)"),
        {"org": str(ctx.org_id)},
    )
    session.execute(
        text(f"SELECT set_config('{ACTOR_GUC}', :actor, true)"),
        {"actor": str(ctx.actor_user_id) if ctx.actor_user_id else ""},
    )


@contextmanager
def tenant_session(ctx: ExecutionContext | None = None) -> Iterator[Session]:
    """Open a transaction, bind the tenant transaction-locally, commit or roll back.

    This is the only sanctioned way to obtain a session -- for request handlers,
    for Celery tasks and for scripts alike (correction 14).
    """
    context = ctx or require_context()
    session = get_session_factory()()
    try:
        session.begin()
        apply_tenant_context(session, context)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def current_tenant(session: Session) -> uuid.UUID | None:
    raw = session.execute(text(f"SELECT current_setting('{TENANT_GUC}', true)")).scalar()
    return uuid.UUID(raw) if raw else None
