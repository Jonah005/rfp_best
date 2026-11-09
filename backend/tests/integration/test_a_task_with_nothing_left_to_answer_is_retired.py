"""A package whose requirements were all moved or withdrawn stops counting."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.models import ResponseArtifact, Task, TaskRequirement
from app.models.enums import ArtifactState, TaskState
from app.services import requirements as requirement_service
from app.services import tasks as task_service
from tests.integration.test_a_customer_clause_is_not_left_in_a_task import (  # noqa: F401
    _build,
    env,
    fresh_org,
)

pytestmark = pytest.mark.integration


def test_withdrawn_requirements_retire_their_task_and_set_the_answer_aside(
    app_sessionmaker,
    env,  # noqa: F811
):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid, _legal, _mixed, bidder = _build(s, ctx, env)
        task = s.execute(
            select(Task)
            .join(TaskRequirement, TaskRequirement.task_id == Task.id)
            .where(TaskRequirement.requirement_id == bidder.id)
        ).scalar_one()
        assert "Covers:" in (task.grouping_rationale or "")

        # Folded into nothing: no survivor, so the link is dropped.
        for gone in (bidder, _mixed):
            requirement_service.withdraw(s, ctx, requirement=gone, successors=[])
        assert task_service.retire_tasks_without_requirements(s, ctx, bid_id=bid.id) >= 1
        assert task.state is TaskState.COMPLETED
        assert "Retired" in task.grouping_rationale
        artifact = s.execute(
            select(ResponseArtifact).where(ResponseArtifact.task_id == task.id)
        ).scalar_one()
        assert artifact.state is ArtifactState.SUPERSEDED
        # Release no longer sees it as unresolved, and a second pass is a no-op.
        assert task_service.retire_tasks_without_requirements(s, ctx, bid_id=bid.id) == 0
