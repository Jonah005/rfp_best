"""A blocked clause answered by an approved promise stops counting as a blocker."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.models import Department, ResponseArtifact
from app.models.enums import (
    ApprovalStatus,
    ApprovalStepState,
    BidReadiness,
    DepartmentCode,
)
from app.services import artifacts, readiness
from app.services import assessment as assessment_service
from app.services import tasks as task_service
from tests.integration.test_a_customer_clause_is_not_left_in_a_task import (  # noqa: F401
    env,
    fresh_org,
)
from tests.integration.test_assessment_readiness import (
    TODAY,
    _company_version,
    _new_bid,
    _requirement,
)
from tests.integration.test_tasks_and_requests import _departments, _user

pytestmark = pytest.mark.integration

CLAUSE = "Using the Core OMS system access Jadwa provides, the solution shall check client exists."


def _build(s, org, env_):
    ctx = ExecutionContext(org_id=org)
    apply_tenant_context(s, ctx)
    _departments(s, org, [DepartmentCode.SECURITY])
    bid = _new_bid(s, ctx)
    version = _company_version(s, ctx, env_)
    requirement = _requirement(s, ctx, bid=bid, doc_version=version, code="R-093", text=CLAUSE)
    owner = _user(s, org, "owner")
    approver_ctx = ExecutionContext(
        org_id=org,
        actor_user_id=owner.id,
        role_codes=frozenset({"BID_MANAGER", "SOLUTION_ARCHITECT"}),
    )
    department = s.execute(select(Department).where(Department.org_id == org)).scalars().first()
    task = task_service.create_task(
        s,
        approver_ctx,
        bid_id=bid.id,
        title="Core OMS",
        department_id=department.id,
        owner_user_id=owner.id,
        requirement_ids=[requirement.id],
    )
    assessment_service.assess_requirement(s, ctx, requirement=requirement, today=TODAY)
    artifact = s.execute(
        select(ResponseArtifact).where(ResponseArtifact.task_id == task.id)
    ).scalar_one()
    return ctx, approver_ctx, bid, requirement, artifact, owner


def test_an_approved_promise_moves_a_blocked_clause_out_of_the_blockers(app_sessionmaker, env):  # noqa: F811
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx, actor, bid, requirement, artifact, owner = _build(s, org, env)
        summary = readiness.summarize(s, ctx, bid_id=bid.id)
        blocked_before = summary.critical_blockers
        assert blocked_before == 1 and summary.answered_by_commitment == 0

        artifacts.save_draft(
            s,
            actor,
            artifact_id=artifact.id,
            statements=[
                {
                    "statement_class": "PROPOSED_COMMITMENT",
                    "text": "We have not integrated with Core OMS before; we will build it from "
                    "Jadwa's specification and test it in their sandbox.",
                    "required_approver_role": "BID_MANAGER",
                    "commitment_owner_user_id": owner.id,
                }
            ],
        )
        # Promised but not yet approved: still a blocker.
        summary = readiness.summarize(s, ctx, bid_id=bid.id)
        assert summary.critical_blockers == blocked_before

        commitment = artifacts.list_commitments(s, actor, bid_id=bid.id)[0]
        artifacts.decide_commitment(
            s, actor, commitment_id=commitment.id, decision=ApprovalStatus.APPROVED
        )
        artifacts.submit(s, actor, artifact_id=artifact.id, approval_roles=["BID_MANAGER"])
        step = artifacts.approval_chain(
            s, actor, entity_type="ARTIFACT_VERSION", entity_id=artifact.current_version_id
        )[0]
        artifacts.decide_approval(
            s, actor, approval_id=step.id, decision=ApprovalStepState.APPROVED
        )

        summary = readiness.summarize(s, ctx, bid_id=bid.id)
        assert summary.critical_blockers == 0
        assert summary.mandatory_unready == 0
        assert summary.answered_by_commitment == 1
        # Never counted as proof.
        assert summary.requirement_coverage_pct == 0.0
        row = readiness.technical_matrix(s, ctx, bid_id=bid.id)[0]
        assert row["bid_readiness"] == BidReadiness.BLOCKED.value
        assert row["answered_by_commitment"] is True
        s.rollback()
