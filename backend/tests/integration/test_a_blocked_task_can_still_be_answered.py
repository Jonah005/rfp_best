"""A blocked task is blocked on proof, not on being answered.

23 of 41 tasks on the live bid were BLOCKED because a mandatory requirement had
no evidence that could prove it. Submit refused every one of them, so a person
who had written an honest answer -- "we will do this, subject to confirmation" --
could not send it for review. The review and the commitment approvals are the
control; the readiness views keep showing the requirement as blocked.

What still holds a blocked task back is a requirement with no answer at all.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.errors import IllegalTransition
from app.ingestion.safety import EicarAwareScanner
from app.models import Requirement, RequirementVersion, Task, TaskRequirement
from app.models.enums import (
    ConfidentialityClass,
    DocumentSide,
    RequirementApplicability,
    RequirementCategory,
    RequirementLifecycle,
    RiskLevel,
    TaskState,
)
from app.services import artifacts, ingestion
from app.services.storage import InMemoryStorage
from tests.integration.test_artifact_lifecycle import _actor, _make_task

pytestmark = pytest.mark.integration


def _blocked_task_with_one_requirement(session, org_id, actor_id):
    ctx, task, artifact = _make_task(session, org_id, actor_id)
    # The scene states its own requirement; drop the one the fixture attached.
    from sqlalchemy import delete as _delete

    from app.models import ArtifactRequirement

    session.execute(
        _delete(ArtifactRequirement).where(ArtifactRequirement.artifact_id == artifact.id)
    )
    session.execute(_delete(TaskRequirement).where(TaskRequirement.task_id == task.id))
    session.flush()
    doc_version = ingestion.register_upload(
        session,
        ctx,
        data=b"# Tender\n\nThe portal must support single sign-on.\n",
        filename=f"tender-{uuid.uuid4().hex[:6]}.md",
        side=DocumentSide.TENDER,
        bid_id=task.bid_id,
        confidentiality_class=ConfidentialityClass.INTERNAL,
        scanner=EicarAwareScanner(),
        storage=InMemoryStorage(),
    )
    requirement = Requirement(
        org_id=org_id,
        bid_id=task.bid_id,
        code="R-921",
        category=RequirementCategory.TECHNICAL,
        applicability=RequirementApplicability.ACTIVE,
        lifecycle_state=RequirementLifecycle.DRAFT_EXTRACTED,
        risk=RiskLevel.MEDIUM,
        is_mandatory=True,
    )
    session.add(requirement)
    session.flush()
    version = RequirementVersion(
        org_id=org_id,
        requirement_id=requirement.id,
        version_no=1,
        requirement_text="The portal must support single sign-on.",
        source_document_version_id=doc_version.id,
        source_locator={"block_ordinal": 1},
        extracted_by="test",
    )
    session.add(version)
    session.flush()
    requirement.current_version_id = version.id
    session.add(TaskRequirement(org_id=org_id, task_id=task.id, requirement_id=requirement.id))
    session.get(Task, task.id).state = TaskState.BLOCKED
    session.flush()
    return ctx, task, artifact


def test_a_blocked_task_with_an_unanswered_requirement_is_refused(
    app_sessionmaker, seeded_orgs, users
):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor = _actor(session, org_id, users[org_id]["manager"])
        ctx, _, artifact = _blocked_task_with_one_requirement(session, org_id, actor.id)
        artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[{"statement_class": "HUMAN_PROVIDED_INPUT", "text": "Some note."}],
        )
        with pytest.raises(IllegalTransition) as raised:
            artifacts.submit(session, ctx, artifact_id=artifact.id, approval_roles=["BID_MANAGER"])
        assert "R-921" in str(raised.value)
        session.rollback()


def test_a_blocked_task_whose_requirements_are_answered_goes_to_review(
    app_sessionmaker, seeded_orgs, users
):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor = _actor(session, org_id, users[org_id]["manager"])
        ctx, task, artifact = _blocked_task_with_one_requirement(session, org_id, actor.id)
        artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[
                {
                    "statement_class": "PROPOSED_COMMITMENT",
                    "text": "We will integrate single sign-on, subject to confirming the provider.",
                    "required_approver_role": "SOLUTION_ARCHITECT",
                    "commitment_owner_user_id": actor.id,
                    "requirement_codes": ["R-921"],
                }
            ],
        )
        artifacts.submit(session, ctx, artifact_id=artifact.id, approval_roles=["BID_MANAGER"])
        assert session.get(Task, task.id).state is not TaskState.BLOCKED
        session.rollback()
