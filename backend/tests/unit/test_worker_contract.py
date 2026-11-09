"""Correction 14: workers are not a privilege side door."""

from __future__ import annotations

import inspect
import uuid

import pytest


def test_tasks_module_never_opens_a_raw_session():
    """Worker tasks must go through tenant_session/domain services only."""
    import app.workers.tasks as tasks

    source = inspect.getsource(tasks)
    for forbidden in ("get_session_factory(", "create_engine(", "Session("):
        assert forbidden not in source, (
            f"worker tasks must not construct sessions directly (found {forbidden!r})"
        )


def test_tenant_task_refuses_to_run_without_an_org():
    from app.workers.celery_app import TenantTask

    class _Probe(TenantTask):
        name = "probe"

        def run(self, **kwargs):
            return "ran"

    with pytest.raises(RuntimeError) as exc:
        _Probe().__call__()
    assert "org_id" in str(exc.value)


def test_worker_context_attributes_unattended_work_to_a_worker_actor():
    """Background work with no initiating user is attributed to WORKER.

    The audit trail must never show an anonymous mutation, so the context
    factory picks the actor kind rather than defaulting to USER.
    """
    from app.workers.celery_app import worker_context

    src = inspect.getsource(worker_context)
    assert 'actor_kind="USER" if actor_user_id else "WORKER"' in src


def test_worker_context_requires_a_tenant_session():
    """The unit of work must come from tenant_session, which issues SET LOCAL."""
    from app.workers.celery_app import worker_context

    assert "tenant_session(ctx)" in inspect.getsource(worker_context)


def test_all_foundation_tasks_register_at_worker_startup():
    """A worker that silently registers nothing is a very quiet outage."""
    from app.workers.celery_app import celery_app

    celery_app.loader.import_default_modules()  # what a real worker does
    registered = {name for name in celery_app.tasks if name.startswith("app.")}
    assert registered == {
        "app.workers.tasks.sweep_grant_states",
        "app.workers.tasks.revoke_task_grants",
        "app.workers.tasks.health_probe",
        "app.workers.tasks.parse_and_index_document",
        "app.workers.tasks.expire_stale_knowledge",
        "app.workers.tasks.analyze_tender",
    }


def test_company_upload_dispatches_the_tenant_index_and_extraction_task(monkeypatch):
    """A registered upload must enter the worker pipeline automatically."""
    from app.api.v1.knowledge import _enqueue_document_index, upload_document
    from app.workers.tasks import parse_and_index_document

    # The dispatch must hang off commit, never off BackgroundTasks. Starlette
    # runs background tasks inside the response, before `get_db` resumes to
    # commit, so a worker started that way looks for a row that is not yet
    # visible — and under eager execution the resulting NotFound propagates
    # back and rolls the upload's own transaction back.
    source = inspect.getsource(upload_document)
    assert "run_after_commit" in source
    assert "background_tasks" not in source
    assert "_enqueue_document_index" in source

    version_id = uuid.uuid4()
    org_id = uuid.uuid4()
    actor_user_id = uuid.uuid4()
    dispatched = []
    monkeypatch.setattr(
        parse_and_index_document,
        "apply_async",
        lambda *, kwargs: dispatched.append(kwargs),
    )

    _enqueue_document_index(
        version_id=version_id,
        org_id=org_id,
        actor_user_id=actor_user_id,
    )

    assert dispatched == [
        {
            "version_id": str(version_id),
            "org_id": str(org_id),
            "actor_user_id": str(actor_user_id),
        }
    ]


def test_beat_schedule_points_at_a_real_task():
    from app.workers.celery_app import celery_app

    celery_app.loader.import_default_modules()
    for entry in celery_app.conf.beat_schedule.values():
        assert entry["task"] in celery_app.tasks, f"beat references unknown task {entry['task']}"
