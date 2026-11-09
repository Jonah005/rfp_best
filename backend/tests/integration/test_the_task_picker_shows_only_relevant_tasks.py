# ruff: noqa: F811
"""Who sees which tasks in the picker, and who handed them over."""

from __future__ import annotations

import pytest

from app.core.context import ExecutionContext
from app.core.permissions import Perm
from app.models import Department, Task, User, UserDepartment
from app.services import tasks as task_service
from tests.integration.test_a_customer_clause_is_not_left_in_a_task import (  # noqa: F401
    env,
    fresh_org,
)
from tests.integration.test_a_diagram_in_the_task_workspace import setup  # noqa: F401

pytestmark = pytest.mark.integration


def _person(s, org, name):
    user = User(org_id=org, email=f"{name.lower()}-pick@example.com", display_name=name)
    s.add(user)
    s.flush()
    return user


def test_a_department_member_sees_their_departments_tasks_and_any_given_to_them(setup):
    s, ctx, task, _ = setup
    org = ctx.org_id
    own = s.get(Department, task.department_id)
    from sqlalchemy import select

    other = (
        s.execute(select(Department).where(Department.org_id == org, Department.id != own.id))
        .scalars()
        .first()
    )
    assert other is not None
    elsewhere = Task(
        org_id=org,
        bid_id=task.bid_id,
        code="ZZ-01",
        title="Somebody else's work",
        department_id=other.id,
        priority=task.priority,
        state=task.state,
        retrieval_scope=task.retrieval_scope,
    )
    s.add(elsewhere)
    s.flush()
    omar = _person(s, org, "Omar R.")
    sara = _person(s, org, "Sara K.")
    s.add(UserDepartment(org_id=org, user_id=sara.id, department_id=own.id, is_primary=True))
    s.flush()

    manager = ExecutionContext(
        org_id=org,
        actor_user_id=omar.id,
        permissions=frozenset({Perm.TASK_READ.value, Perm.TASK_ASSIGN.value}),
    )
    member = ExecutionContext(
        org_id=org,
        actor_user_id=sara.id,
        permissions=frozenset({Perm.TASK_READ.value}),
        department_ids=frozenset({own.id}),
    )
    codes = lambda rows: {r["code"] for r in rows}  # noqa: E731
    everyone = codes(task_service.tasks_for_picker(s, manager, bid_id=task.bid_id))
    mine = codes(task_service.tasks_for_picker(s, member, bid_id=task.bid_id))
    assert "ZZ-01" in everyone and task.code in everyone
    assert task.code in mine and "ZZ-01" not in mine

    # The bid manager hands Sara a task from another department, by name.
    elsewhere.owner_user_id = sara.id
    s.flush()
    from app.services import audit

    audit.record(
        s,
        manager,
        action="task.owner_assigned",
        entity_type="task",
        entity_id=elsewhere.id,
        bid_id=task.bid_id,
        after={"owner_user_id": str(sara.id)},
    )
    rows = task_service.tasks_for_picker(s, member, bid_id=task.bid_id)
    given = next(r for r in rows if r["code"] == "ZZ-01")
    assert given["assigned_by"] == "Omar R."
    assert next(r for r in rows if r["code"] == task.code)["assigned_by"] is None
