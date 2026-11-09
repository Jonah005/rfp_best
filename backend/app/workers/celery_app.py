"""Celery application and the tenant-aware task base.

Correction 14: workers are a scheduling mechanism, not a privilege boundary.
Every task resolves (org_id, actor) from its payload, opens a transaction, binds
the tenant transaction-locally, and writes ONLY through the same domain services
the API uses. RLS, composite tenant foreign keys, permission checks, transition
guards and audit writes therefore apply identically to background work.

No task opens a raw session or issues plain SQL against tenant tables.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps

from celery import Celery, Task

from app.core.config import get_settings
from app.core.context import ExecutionContext, reset_context, set_context
from app.core.logging import configure_logging, get_logger
from app.db.session import tenant_session

log = get_logger("worker")
settings = get_settings()

celery_app = Celery(
    "bidready",
    broker=settings.redis_url,
    backend=settings.redis_url,
    # Task modules must be imported for registration; a worker that silently
    # registers nothing is a very quiet outage.
    include=["app.workers.tasks"],
)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    # Inline execution for the local single-process runner. Eager propagation is
    # on so a failing task raises where it was called instead of vanishing.
    task_always_eager=settings.celery_eager,
    task_eager_propagates=settings.celery_eager,
    beat_schedule={
        "materialize-grant-states": {
            "task": "app.workers.tasks.sweep_grant_states",
            "schedule": 3600.0,
        }
    },
)


@contextmanager
def worker_context(
    org_id: str | uuid.UUID,
    *,
    actor_user_id: str | uuid.UUID | None = None,
) -> Iterator[tuple[ExecutionContext, object]]:
    """Open a tenant-bound unit of work for a background task."""
    ctx = ExecutionContext(
        org_id=uuid.UUID(str(org_id)),
        actor_user_id=uuid.UUID(str(actor_user_id)) if actor_user_id else None,
        # Work with no initiating user is attributed to an explicit WORKER actor
        # so the audit trail never shows an anonymous mutation.
        actor_kind="USER" if actor_user_id else "WORKER",
    )
    token = set_context(ctx)
    try:
        with tenant_session(ctx) as session:
            yield ctx, session
    finally:
        reset_context(token)


class TenantTask(Task):
    """Base class that refuses to run without a tenant."""

    abstract = True

    def __call__(self, *args, **kwargs):
        if "org_id" not in kwargs:
            raise RuntimeError(
                f"Task {self.name} was invoked without org_id. Background work must run "
                "inside a tenant context so RLS and audit apply."
            )
        return super().__call__(*args, **kwargs)


def tenant_task(name: str) -> Callable:
    """Decorator: registers a task that receives (ctx, session) already bound."""

    def decorator(fn: Callable) -> Callable:
        @celery_app.task(name=name, base=TenantTask, bind=True)
        @wraps(fn)
        def wrapper(self, *args, org_id: str, actor_user_id: str | None = None, **kwargs):
            with worker_context(org_id, actor_user_id=actor_user_id) as (ctx, session):
                log.info("worker_task_start", task=name, org_id=org_id)
                result = fn(ctx, session, *args, **kwargs)
                log.info("worker_task_done", task=name, org_id=org_id)
                return result

        return wrapper

    return decorator


configure_logging(json_output=not settings.env == "development")
