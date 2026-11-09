"""Amendment 02: 25 web applications becomes 35, and approved work is reopened.

The flagging existed (`amendments.propagate`) but had to be called by hand with
the requirement ids supplied, so it never ran: an approved answer to the old
wording survived the amendment. Re-extraction now calls it for every
requirement a superseding document rewords.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.models import AmendmentImpact, Department, Requirement, ResponseArtifact
from app.models.enums import ArtifactState, DepartmentCode, DocumentType, TaskState
from app.services import precedence, requirements, tasks
from tests.integration.test_tender_intelligence import (
    AMENDMENT_TEXT,
    RFP_TEXT,
    _new_bid,
    _upload,
    env,  # noqa: F401 - pytest fixture
    gateway_for,
    ordinals_in,
)

# ruff: noqa: F811 - `env` is a fixture, imported and then requested by name

pytestmark = pytest.mark.integration


def _scope(text: str):
    def factory(prompt: str) -> dict:
        ordinals = ordinals_in(prompt)
        return {
            "requirements": [
                {
                    "requirement_text": text,
                    "normalized_requirement": "penetration-testing-scope",
                    "category": "TECHNICAL",
                    "is_mandatory": True,
                    "risk": "HIGH",
                    "source_block_ordinal": ordinals[0],
                    "confidence": 0.95,
                }
            ],
            "no_requirement_block_ordinals": ordinals[1:],
        }

    return factory


def test_an_amendment_sends_approved_work_back_for_revalidation(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx, name="Amendment 02")
        department = s.execute(
            select(Department).where(Department.org_id == org)
        ).scalars().first() or Department(org_id=org, code=DepartmentCode.SECURITY, name="Security")
        s.add(department)
        s.flush()
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)
        requirements.extract_requirements(
            s,
            ctx,
            bid_id=bid.id,
            gateway=gateway_for({"requirement_decomposition": _scope("Test 25 applications.")}),
        )
        requirement = (
            s.execute(select(Requirement).where(Requirement.bid_id == bid.id)).scalars().one()
        )

        # Approved work on the original wording (the approval path itself is
        # tested elsewhere; this is about what happens to it afterwards).
        task = tasks.create_task(
            s,
            ctx,
            bid_id=bid.id,
            title="VAPT methodology",
            department_id=department.id,
            requirement_ids=[requirement.id],
        )
        task.state = TaskState.APPROVED
        artifact = s.execute(
            select(ResponseArtifact).where(ResponseArtifact.task_id == task.id)
        ).scalar_one()
        artifact.state = ArtifactState.APPROVED
        s.flush()

        _upload(
            s,
            ctx,
            env,
            data=AMENDMENT_TEXT,
            filename="amendment_02.md",
            bid_id=bid.id,
            doc_type=DocumentType.AMENDMENT,
            issue_date=date(2026, 8, 11),
        )
        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)
        report = requirements.extract_requirements(
            s,
            ctx,
            bid_id=bid.id,
            gateway=gateway_for({"requirement_decomposition": _scope("Test 35 applications.")}),
        )

        assert report.amended, "the reworded requirement must be reported as amended"
        s.refresh(task)
        s.refresh(artifact)
        assert artifact.state is ArtifactState.REVALIDATION_REQUIRED
        assert "amendment_02.md" in (artifact.revalidation_reason or "")
        assert task.state is TaskState.REVALIDATION_REQUIRED
        impacts = (
            s.execute(select(AmendmentImpact).where(AmendmentImpact.bid_id == bid.id))
            .scalars()
            .all()
        )
        assert len(impacts) == 1 and requirement.id in impacts[0].affected_requirement_ids
        s.rollback()


def test_rereading_an_unchanged_tender_reopens_nothing(app_sessionmaker, env):
    """The same document read twice is not an amendment."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx, name="No amendment")
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)
        for _ in range(2):
            report = requirements.extract_requirements(
                s,
                ctx,
                bid_id=bid.id,
                gateway=gateway_for({"requirement_decomposition": _scope("Test 25 applications.")}),
            )
        assert report.amended == []
        assert not s.execute(
            select(AmendmentImpact).where(AmendmentImpact.bid_id == bid.id)
        ).first()
        s.rollback()
