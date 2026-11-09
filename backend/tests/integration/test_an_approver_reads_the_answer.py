"""An approval step shows the answer it asks to be approved.

The screen used to show an entity type and the first eight characters of an
identifier. An approver can only be held to what they could read.
"""

from __future__ import annotations

import pytest

from app.models.enums import ApprovalStepState
from app.services import artifacts
from tests.integration.test_artifact_lifecycle import _actor, _make_task

pytestmark = pytest.mark.integration


def _first_step(session, ctx, artifact):
    return artifacts.approval_chain(
        session, ctx, entity_type="ARTIFACT_VERSION", entity_id=artifact.current_version_id
    )[0]


def test_the_review_carries_the_text_and_marks_what_changed(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor = _actor(session, org_id, users[org_id]["manager"])
        ctx, task, artifact = _make_task(session, org_id, actor.id)

        artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[
                {"statement_class": "HUMAN_PROVIDED_INPUT", "text": "Testing takes six weeks."},
                {"statement_class": "HUMAN_PROVIDED_INPUT", "text": "A kept sentence."},
            ],
        )
        artifacts.submit(session, ctx, artifact_id=artifact.id, approval_roles=["BID_MANAGER"])
        step = _first_step(session, ctx, artifact)
        artifacts.decide_approval(
            session,
            ctx,
            approval_id=step.id,
            decision=ApprovalStepState.REJECTED,
            comment="Make it eight weeks.",
        )
        artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[
                {"statement_class": "HUMAN_PROVIDED_INPUT", "text": "Testing takes eight weeks."},
                {"statement_class": "HUMAN_PROVIDED_INPUT", "text": "A kept sentence."},
            ],
            change_reason="Corrected the duration.",
        )
        artifacts.submit(session, ctx, artifact_id=artifact.id, approval_roles=["BID_MANAGER"])
        latest = [
            a
            for a in artifacts.approval_chain(
                session, ctx, entity_type="ARTIFACT_VERSION", entity_id=artifact.current_version_id
            )
        ][0]

        review = artifacts.approval_review(session, ctx, approval_id=latest.id)

        assert review["reviewable"] is True
        assert review["task"]["code"] == task.code
        assert review["version_no"] == 2 and review["previous_version_no"] == 1
        by_text = {row["text"]: row["change"] for row in review["statements"]}
        assert by_text["Testing takes eight weeks."] == "NEW"
        assert by_text["A kept sentence."] == "UNCHANGED"
        assert [r["text"] for r in review["removed_statements"]] == ["Testing takes six weeks."]
        session.commit()


def test_an_approval_that_is_not_a_written_response_says_so(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor = _actor(session, org_id, users[org_id]["manager"])
        ctx, _task, artifact = _make_task(session, org_id, actor.id)
        artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[{"statement_class": "HUMAN_PROVIDED_INPUT", "text": "Something."}],
        )
        artifacts.submit(session, ctx, artifact_id=artifact.id, approval_roles=["BID_MANAGER"])
        step = _first_step(session, ctx, artifact)
        step.entity_type = "SOMETHING_ELSE"
        session.flush()
        review = artifacts.approval_review(session, ctx, approval_id=step.id)
        assert review["reviewable"] is False
        session.commit()


def test_the_review_lists_live_requirements_and_survives_a_missing_version(
    app_sessionmaker, seeded_orgs, users
):
    """The review once read a field that is not on the requirement row, so every
    approval for an answer with linked requirements failed with a server error.
    A withdrawn requirement is not something the approver is asked to judge."""
    from app.models import ArtifactRequirement
    from app.models.enums import (
        RequirementApplicability,
        RequirementCategory,
        RequirementLifecycle,
        RiskLevel,
    )
    from app.models.tender import Requirement

    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor = _actor(session, org_id, users[org_id]["manager"])
        ctx, task, artifact = _make_task(session, org_id, actor.id)
        for code, lifecycle in (
            ("R-901", RequirementLifecycle.CONFIRMED),
            ("R-902", RequirementLifecycle.WITHDRAWN),
        ):
            requirement = Requirement(
                org_id=org_id,
                bid_id=task.bid_id,
                code=code,
                category=RequirementCategory.TECHNICAL,
                applicability=RequirementApplicability.ACTIVE,
                lifecycle_state=lifecycle,
                risk=RiskLevel.LOW,
                is_mandatory=True,
            )
            session.add(requirement)
            session.flush()
            session.add(
                ArtifactRequirement(
                    org_id=org_id, artifact_id=artifact.id, requirement_id=requirement.id
                )
            )
        session.flush()
        artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[{"statement_class": "HUMAN_PROVIDED_INPUT", "text": "One sentence."}],
        )
        artifacts.submit(session, ctx, artifact_id=artifact.id, approval_roles=["BID_MANAGER"])
        step = _first_step(session, ctx, artifact)

        review = artifacts.approval_review(session, ctx, approval_id=step.id)

        codes = [r["code"] for r in review["requirements"] if not r["code"].startswith("FX-")]
        assert codes == ["R-901"]
        session.commit()
