"""Every department with people has one head; the rest are team members.

New work goes to the head, and leftover work is settled onto the head.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.models import Department
from app.models.enums import DepartmentCode, TaskState
from app.services import identity
from app.services import tasks as task_service
from tests.integration.test_assessment_readiness import _new_bid

pytestmark = pytest.mark.integration


def _session(app_engine, org):
    factory = sessionmaker(bind=app_engine, autoflush=False, expire_on_commit=False, future=True)
    s = factory()
    s.begin()
    ctx = ExecutionContext(org_id=org)
    apply_tenant_context(s, ctx)
    return s, ctx


def _department(s, org):
    taken = set(s.execute(select(Department.code).where(Department.org_id == org)).scalars())
    code = next(c for c in DepartmentCode if c not in taken)
    d = Department(org_id=org, code=code, name=code.value.title())
    s.add(d)
    s.flush()
    return d


def _person(s, ctx, name, department):
    return identity.create_user(
        s,
        ctx,
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name,
        department_ids_to_assign=[department.id],
    )


def test_the_first_person_in_becomes_head_and_the_rest_are_team_members(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _session(app_engine, org)
    try:
        dept = _department(s, org)
        first = _person(s, ctx, "first", dept)
        second = _person(s, ctx, "second", dept)
        assert dept.head_user_id == first.id
        assert identity.position_in(dept, first.id) == "Head"
        assert identity.position_in(dept, second.id) == "Team member"
    finally:
        s.rollback()
        s.close()


def test_settling_names_a_head_where_there_is_none(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _session(app_engine, org)
    try:
        dept = _department(s, org)
        person = _person(s, ctx, "solo", dept)
        dept.head_user_id = None
        s.flush()
        identity.settle_department_heads(s, ctx)
        assert dept.head_user_id == person.id
        every = s.execute(select(Department).where(Department.org_id == org)).scalars().all()
        staffed = {d.id for d in every if d.head_user_id}
        assert dept.id in staffed
    finally:
        s.rollback()
        s.close()


def test_new_work_goes_to_the_head_and_leftovers_are_settled(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _session(app_engine, org)
    try:
        dept = _department(s, org)
        head = _person(s, ctx, "head", dept)
        _person(s, ctx, "mate", dept)
        outsider = _department(s, org)
        other = _person(s, ctx, "other", outsider)
        bid = _new_bid(s, ctx)

        made = task_service.create_task(
            s, ctx, bid_id=bid.id, title="New work", department_id=dept.id
        )
        assert made.owner_user_id == head.id

        orphan = task_service.create_task(
            s, ctx, bid_id=bid.id, title="Orphan", department_id=dept.id
        )
        orphan.owner_user_id = None
        strayed = task_service.create_task(
            s, ctx, bid_id=bid.id, title="Strayed", department_id=dept.id
        )
        strayed.owner_user_id = other.id
        started = task_service.create_task(
            s, ctx, bid_id=bid.id, title="Started", department_id=dept.id
        )
        started.owner_user_id = other.id
        started.state = TaskState.IN_PROGRESS
        s.flush()

        counts = task_service.settle_task_owners(s, ctx)
        assert counts["filled"] >= 1 and counts["moved"] >= 1  # other tests leave tasks too
        assert orphan.owner_user_id == head.id
        assert strayed.owner_user_id == head.id
        assert started.owner_user_id == other.id  # work in progress is never moved
    finally:
        s.rollback()
        s.close()
