"""The after-commit hook, tested for the two things that actually went wrong.

This exists because of a real failure: both upload routes enqueued their
parse/index job through FastAPI's ``BackgroundTasks``, believing that ran after
the request's transaction committed. It does not — Starlette runs background
tasks from inside ``Response.__call__``, which is still inside the route's
dependency exit stack, so ``get_db`` has not reached ``session.commit()`` yet.
The worker then could not see the row it was told to parse, raised ``NotFound``,
and under ``task_eager_propagates`` that exception unwound back through the
response and rolled the upload's own transaction back. The caller got a 201
with a document id for a document that no longer existed.

The first two tests pin the ordering guarantee. The third pins a bug introduced
by the first attempt at the fix: removing the listener from inside the dispatch
loop raises ``RuntimeError: deque mutated during iteration`` out of
``session.commit()``, which converts a perfectly good write into a 500.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.post_commit import run_after_commit


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    with Session(engine) as session:
        yield session


def test_hook_runs_only_after_the_commit_completes(session):
    events: list[str] = []
    session.execute(text("select 1"))
    run_after_commit(session, lambda: events.append("enqueued"), label="test")

    assert events == [], "the hook must not fire before commit"
    session.commit()
    assert events == ["enqueued"]


def test_hook_never_runs_when_the_transaction_rolls_back(session):
    """The whole point: no job for data that does not exist."""
    events: list[str] = []
    session.execute(text("select 1"))
    run_after_commit(session, lambda: events.append("enqueued"), label="test")

    session.rollback()
    assert events == []


def test_commit_survives_a_hook_that_is_registered_and_fired(session):
    """Regression: `event.remove` inside dispatch broke `commit` itself.

    The failure was not in the hook — it was that `session.commit()` raised
    `RuntimeError: deque mutated during iteration`, so a committed write came
    back to the caller as a 500.
    """
    session.execute(text("select 1"))
    run_after_commit(session, lambda: None, label="test")
    session.commit()  # must not raise


def test_a_failing_hook_does_not_fail_the_request(session):
    """The write is already durable; losing the follow-up job is recoverable."""
    session.execute(text("select 1"))

    def explode() -> None:
        raise RuntimeError("broker unreachable")

    run_after_commit(session, explode, label="test")
    session.commit()  # swallowed and logged, not raised


def test_hook_fires_once_even_across_later_commits(session):
    calls: list[int] = []
    session.execute(text("select 1"))
    run_after_commit(session, lambda: calls.append(1), label="test")

    session.commit()
    session.execute(text("select 1"))
    session.commit()

    assert calls == [1]
