"""Hot paths must issue a bounded number of queries.

An N+1 is invisible on a demo task with three requirements and expensive on a
bid with three hundred -- and the copilot context is rebuilt on *every* message,
so the cost lands on the most frequent operation in the product.

These tests count real SQL statements rather than timing anything, so they fail
for the right reason and do not depend on the machine.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import event

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.models import Department
from app.models.enums import DepartmentCode
from app.services import context_builder
from app.services import tasks as task_service
from tests.integration.test_assessment_readiness import (
    _company_version,
    _new_bid,
    _requirement,
)

pytestmark = pytest.mark.integration

from app.gateway.embeddings import HashingEmbeddingProvider  # noqa: E402
from app.ingestion.safety import EicarAwareScanner  # noqa: E402
from app.services.storage import InMemoryStorage  # noqa: E402

CONTRIBUTOR_PERMS = frozenset({"document:read", "knowledge:read", "evidence:read"})


@contextmanager
def counted(session):
    """Count SQL statements issued on this session."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


@pytest.fixture
def fresh_org(owner_engine):
    from sqlalchemy.orm import sessionmaker

    from app.models import Organization

    org_id = uuid.uuid4()
    Owner = sessionmaker(bind=owner_engine, future=True)
    with Owner() as s:
        s.add(
            Organization(id=org_id, name=f"Q {org_id.hex[:6]}", slug=f"q-{org_id.hex[:10]}")
        )
        s.commit()
    return org_id


@pytest.fixture
def env(fresh_org):
    return fresh_org, InMemoryStorage(), EicarAwareScanner(), HashingEmbeddingProvider()


def _scene(session, ctx, env, *, requirement_count: int):
    from app.models import User

    dept = Department(org_id=ctx.org_id, code=DepartmentCode.SECURITY, name="Security")
    session.add(dept)
    session.flush()
    user = User(
        org_id=ctx.org_id,
        email=f"q-{uuid.uuid4().hex[:8]}@example.com",
        display_name="Contributor",
    )
    session.add(user)
    session.flush()

    bid = _new_bid(session, ctx)
    version = _company_version(session, ctx, env)
    requirement_ids = [
        _requirement(
            session,
            ctx,
            bid=bid,
            doc_version=version,
            code=f"SEC-{n:03d}",
            text=f"Control {n}: the bidder shall maintain documented security procedures.",
        ).id
        for n in range(requirement_count)
    ]
    task = task_service.create_task(
        session,
        ctx,
        bid_id=bid.id,
        title="Security requirements",
        department_id=dept.id,
        owner_user_id=user.id,
        requirement_ids=requirement_ids,
    )
    return user, task


def _count_for(session, env, org, *, requirement_count: int) -> int:
    ctx = ExecutionContext(org_id=org)
    apply_tenant_context(session, ctx)
    user, task = _scene(session, ctx, env, requirement_count=requirement_count)

    actor = ExecutionContext(
        org_id=org, actor_user_id=user.id, permissions=CONTRIBUTOR_PERMS
    )
    with counted(session) as statements:
        context_builder.build(
            session,
            actor,
            task_id=task.id,
            query="documented security procedures",
            embedder=HashingEmbeddingProvider(),
        )
    return len(statements)


def test_context_building_does_not_scale_its_query_count_with_requirements(
    app_sessionmaker, env
):
    """Ten times the requirements must not mean ten times the round trips."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        small = _count_for(s, env, org, requirement_count=3)
        s.rollback()

    with app_sessionmaker() as s:
        s.begin()
        large = _count_for(s, env, org, requirement_count=40)
        s.rollback()

    # Before batching this grew by roughly 2 queries per requirement, so 40
    # requirements cost ~74 more than 3. A small constant of extra statements is
    # fine; growth proportional to the requirement count is the bug.
    assert large <= small + 5, (
        f"query count grew with requirements: {small} -> {large}. "
        "Something in the context path is issuing per-requirement queries again."
    )


def test_context_building_stays_cheap_in_absolute_terms(app_sessionmaker, env):
    """A guard against the count creeping up one query at a time."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        count = _count_for(s, env, org, requirement_count=40)
        s.rollback()
    assert count < 40, f"context building issued {count} statements for one message"
