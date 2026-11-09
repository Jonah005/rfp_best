"""Phase P: what an amendment invalidates, and how learning stays governed.

Two failure modes are being prevented.

An amendment arriving after work is approved must make that work *visibly*
stale. Leaving an approved answer looking finished when the clause beneath it
changed is how a bid is submitted against a superseded requirement.

And nothing the system learns from an outcome may edit canonical knowledge on
its own. Feedback becomes a candidate a human decides on; approval creates a new
fact rather than overwriting the old one.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.errors import NotFound, StateConflict
from app.core.errors import ValidationError as AppValidationError
from app.db.session import apply_tenant_context
from app.models import (
    AmendmentImpact,
    Document,
    DocumentVersion,
    KnowledgeUpdateCandidate,
    Outcome,
    RequirementAssessment,
)
from app.models.enums import (
    ApprovalStepState,
    ArtifactState,
    AssessmentState,
    CandidateState,
    ConfidentialityClass,
    DocumentSide,
    OutcomeResult,
    TaskState,
)
from app.services import amendments, artifacts, ingestion, release
from app.services import tasks as task_service
from app.services.storage import InMemoryStorage
from tests.integration.test_assessment_readiness import (
    ISO_TEXT,
    TODAY,
    _company_version,
    _evidence,
    _requirement,
)
from tests.integration.test_release_lifecycle import _ctx, _user

pytestmark = pytest.mark.integration

from app.gateway.embeddings import HashingEmbeddingProvider  # noqa: E402
from app.ingestion.safety import EicarAwareScanner  # noqa: E402
from app.models import Department  # noqa: E402
from app.services import assessment as assessment_service  # noqa: E402
from app.services import bids  # noqa: E402

AMENDMENT_TEXT = b"""# 4.2 Certification scope

The bidder shall hold ISO 27001 certification covering all delivery locations.
"""


def _amended_scene(session, org_id, users):
    """An approved answer to a requirement, plus a tender amendment touching it."""
    storage = InMemoryStorage()
    scanner = EicarAwareScanner()
    embedder = HashingEmbeddingProvider()
    env = (org_id, storage, scanner, embedder)

    actor = _user(session, org_id, users[org_id]["manager"])
    ctx = _ctx(org_id, actor.id, "BID_MANAGER")

    opportunity = bids.create_opportunity(
        session,
        ctx,
        customer_name="Amendment Customer",
        tender_name=f"Amendment Tender {uuid.uuid4().hex[:6]}",
    )
    bid = bids.promote_to_bid(session, ctx, opportunity_id=opportunity.id)
    bids.ensure_intake_open(session, ctx, bid_id=bid.id)

    company_version = _company_version(session, ctx, env)
    _evidence(session, ctx, doc_version=company_version, excerpt=ISO_TEXT)
    requirement = _requirement(
        session, ctx, bid=bid, doc_version=company_version, code="SEC-1", text=ISO_TEXT
    )
    assessment_service.assess_requirement(session, ctx, requirement=requirement, today=TODAY)

    department = (
        session.execute(select(Department).where(Department.org_id == org_id)).scalars().first()
    )
    task = task_service.create_task(
        session,
        ctx,
        bid_id=bid.id,
        title="Certification response",
        department_id=department.id,
        owner_user_id=actor.id,
        requirement_ids=[requirement.id],
    )
    artifact = artifacts.for_task(session, ctx, task_id=task.id)
    version = artifacts.save_draft(
        session,
        ctx,
        artifact_id=artifact.id,
        statements=[
            {
                "statement_class": "HUMAN_PROVIDED_INPUT",
                "text": "We hold ISO 27001 for the Dubai delivery centre.",
            }
        ],
    )
    artifacts.submit(session, ctx, artifact_id=artifact.id, approval_roles=["BID_MANAGER"])
    approval = artifacts.approval_chain(
        session, ctx, entity_type="ARTIFACT_VERSION", entity_id=version.id
    )[0]
    artifacts.decide_approval(
        session, ctx, approval_id=approval.id, decision=ApprovalStepState.APPROVED
    )

    amendment = ingestion.register_upload(
        session,
        ctx,
        data=AMENDMENT_TEXT,
        filename=f"amendment-{uuid.uuid4().hex[:6]}.md",
        side=DocumentSide.TENDER,
        bid_id=bid.id,
        confidentiality_class=ConfidentialityClass.CONFIDENTIAL,
        scanner=scanner,
        storage=storage,
    )
    return ctx, bid, requirement, task, artifact, amendment


# --- amendment impact --------------------------------------------------------


def test_an_amendment_makes_approved_work_visibly_stale(app_sessionmaker, seeded_orgs, users):
    """The requirement's identity is stable, so the cascade can find everything
    hanging off it: the assessment, the task, and the approved answer."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, requirement, task, artifact, amendment = _amended_scene(session, org_id, users)
        assert task.state is TaskState.COMPLETED
        assert artifact.state is ArtifactState.APPROVED

        impact = amendments.propagate(
            session,
            ctx,
            bid_id=bid.id,
            amendment_document_version_id=amendment.id,
            affected_requirement_ids=[requirement.id],
            reason="Amendment 1 widened clause 4.2 to all delivery locations.",
        )

        assert impact.affected_requirement_ids == [requirement.id]
        assert task.id in impact.affected_task_ids
        assert artifact.id in impact.affected_artifact_ids

        session.refresh(task)
        session.refresh(artifact)
        assert task.state is TaskState.REVALIDATION_REQUIRED
        assert artifact.state is ArtifactState.REVALIDATION_REQUIRED
        assert artifact.revalidation_reason

        current = session.execute(
            select(RequirementAssessment).where(
                RequirementAssessment.org_id == org_id,
                RequirementAssessment.requirement_id == requirement.id,
                RequirementAssessment.is_current.is_(True),
            )
        ).scalar_one()
        assert current.assessment_state is AssessmentState.STALE
        assert "Amendment 1" in current.stale_reason
        session.commit()


def test_a_stale_assessment_keeps_the_verdict_it_had(app_sessionmaker, seeded_orgs, users):
    """STALE means 'this was our verdict and may no longer hold', which is more
    useful to a reviewer than an emptied field."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, requirement, _task, _artifact, amendment = _amended_scene(session, org_id, users)
        before = session.execute(
            select(RequirementAssessment).where(
                RequirementAssessment.org_id == org_id,
                RequirementAssessment.requirement_id == requirement.id,
                RequirementAssessment.is_current.is_(True),
            )
        ).scalar_one()
        prior_readiness = before.bid_readiness
        prior_evidence = before.evidence_status

        amendments.propagate(
            session,
            ctx,
            bid_id=bid.id,
            amendment_document_version_id=amendment.id,
            affected_requirement_ids=[requirement.id],
            reason="Scope widened.",
        )
        session.refresh(before)
        assert before.bid_readiness is prior_readiness
        assert before.evidence_status is prior_evidence
        assert before.assessment_state is AssessmentState.STALE
        session.commit()


def test_propagating_the_same_amendment_twice_is_idempotent(app_sessionmaker, seeded_orgs, users):
    """Re-running intake after a re-upload must not multiply the impact record."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, requirement, _t, _a, amendment = _amended_scene(session, org_id, users)
        first = amendments.propagate(
            session,
            ctx,
            bid_id=bid.id,
            amendment_document_version_id=amendment.id,
            affected_requirement_ids=[requirement.id],
            reason="Scope widened.",
        )
        second = amendments.propagate(
            session,
            ctx,
            bid_id=bid.id,
            amendment_document_version_id=amendment.id,
            affected_requirement_ids=[requirement.id],
            reason="Scope widened.",
        )
        assert first.id == second.id
        assert len(amendments.for_bid(session, ctx, bid_id=bid.id)) == 1
        session.commit()


def test_an_impact_needs_an_explanation_and_real_requirements(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, requirement, _t, _a, amendment = _amended_scene(session, org_id, users)

        with pytest.raises(AppValidationError, match="at least one"):
            amendments.propagate(
                session,
                ctx,
                bid_id=bid.id,
                amendment_document_version_id=amendment.id,
                affected_requirement_ids=[],
                reason="Something changed.",
            )
        with pytest.raises(AppValidationError, match="explanation"):
            amendments.propagate(
                session,
                ctx,
                bid_id=bid.id,
                amendment_document_version_id=amendment.id,
                affected_requirement_ids=[requirement.id],
                reason="   ",
            )
        with pytest.raises(AppValidationError, match="do not belong"):
            amendments.propagate(
                session,
                ctx,
                bid_id=bid.id,
                amendment_document_version_id=amendment.id,
                affected_requirement_ids=[requirement.id, uuid.uuid4()],
                reason="Scope widened.",
            )
        session.rollback()


def test_a_company_document_cannot_drive_an_amendment_cascade(app_sessionmaker, seeded_orgs, users):
    """The authority boundary again, at the last place it could be crossed.

    A company document must never be able to invalidate tender scope.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, requirement, _t, _a, _amendment = _amended_scene(session, org_id, users)
        company_side = (
            session.execute(
                select(DocumentVersion)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    DocumentVersion.org_id == org_id,
                    Document.side == DocumentSide.COMPANY,
                )
            )
            .scalars()
            .first()
        )
        assert company_side is not None

        with pytest.raises(NotFound):
            amendments.propagate(
                session,
                ctx,
                bid_id=bid.id,
                amendment_document_version_id=company_side.id,
                affected_requirement_ids=[requirement.id],
                reason="Trying to widen scope from our own methodology.",
            )
        session.rollback()


def test_amendment_impacts_do_not_cross_tenants(app_sessionmaker, seeded_orgs, users):
    org_a, org_b = seeded_orgs
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, requirement, _t, _a, amendment = _amended_scene(session, org_a, users)
        amendments.propagate(
            session,
            ctx,
            bid_id=bid.id,
            amendment_document_version_id=amendment.id,
            affected_requirement_ids=[requirement.id],
            reason="Scope widened.",
        )
        bid_id = bid.id
        session.commit()

    with app_sessionmaker() as session:
        session.begin()
        other = _ctx(org_b, uuid.uuid4())
        apply_tenant_context(session, other)
        assert (
            session.execute(select(AmendmentImpact).where(AmendmentImpact.bid_id == bid_id))
            .scalars()
            .all()
            == []
        )
        assert amendments.for_bid(session, other, bid_id=bid_id) == []
        session.rollback()


# --- outcome and governed learning -------------------------------------------


def test_evaluator_feedback_becomes_a_candidate_not_a_fact(app_sessionmaker, seeded_orgs, users):
    """Canonical knowledge is never updated automatically.

    A lost bid's feedback is exactly the material someone would be tempted to
    write straight into the knowledge base. It becomes a proposal instead.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, _r, _t, _a, _amendment = _amended_scene(session, org_id, users)

        outcome = release.record_outcome(
            session,
            ctx,
            bid_id=bid.id,
            result=OutcomeResult.LOST,
            loss_reason="Priced above the incumbent.",
            evaluator_feedback="The methodology section lacked local delivery references.",
        )
        assert outcome.result is OutcomeResult.LOST

        candidates = (
            session.execute(
                select(KnowledgeUpdateCandidate).where(
                    KnowledgeUpdateCandidate.org_id == org_id,
                    KnowledgeUpdateCandidate.origin_entity_id == outcome.id,
                )
            )
            .scalars()
            .all()
        )
        assert len(candidates) == 1
        assert candidates[0].state is CandidateState.PROPOSED
        assert candidates[0].origin_type == "OUTCOME_FEEDBACK"
        assert candidates[0].target_id is None, "nothing canonical was written"
        session.commit()


def test_an_outcome_is_recorded_once(app_sessionmaker, seeded_orgs, users):
    """A second, different outcome for one bid is a data-entry error, not a fact."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, _r, _t, _a, _amendment = _amended_scene(session, org_id, users)
        release.record_outcome(session, ctx, bid_id=bid.id, result=OutcomeResult.WON)
        with pytest.raises(StateConflict, match="already has a recorded outcome"):
            release.record_outcome(session, ctx, bid_id=bid.id, result=OutcomeResult.LOST)
        session.commit()


def test_a_candidate_is_decided_once_and_a_rejection_writes_nothing(
    app_sessionmaker, seeded_orgs, users
):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, _r, _t, _a, _amendment = _amended_scene(session, org_id, users)
        outcome = release.record_outcome(
            session,
            ctx,
            bid_id=bid.id,
            result=OutcomeResult.LOST,
            evaluator_feedback="Add more local references.",
        )
        candidate = (
            session.execute(
                select(KnowledgeUpdateCandidate).where(
                    KnowledgeUpdateCandidate.origin_entity_id == outcome.id
                )
            )
            .scalars()
            .one()
        )

        with pytest.raises(AppValidationError):
            release.decide_candidate(
                session, ctx, candidate_id=candidate.id, decision=CandidateState.PROPOSED
            )

        release.decide_candidate(
            session,
            ctx,
            candidate_id=candidate.id,
            decision=CandidateState.REJECTED,
            comment="Already covered by an existing reference.",
        )
        assert candidate.state is CandidateState.REJECTED
        assert candidate.target_id is None
        assert candidate.reviewed_by == ctx.actor_user_id

        with pytest.raises(StateConflict, match="already been decided"):
            release.decide_candidate(
                session, ctx, candidate_id=candidate.id, decision=CandidateState.APPROVED
            )
        session.commit()


def test_an_outcome_without_feedback_proposes_nothing(app_sessionmaker, seeded_orgs, users):
    """A won bid with no evaluator comment yields no LESSON -- the system does
    not invent feedback. It does propose the won project itself into company
    knowledge (see test_after_the_answers.py), which is a fact, not a lesson."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, _r, _t, _a, _amendment = _amended_scene(session, org_id, users)
        outcome = release.record_outcome(
            session, ctx, bid_id=bid.id, result=OutcomeResult.WON, award_value=1_250_000
        )
        origins = [
            c.origin_type
            for c in session.execute(
                select(KnowledgeUpdateCandidate).where(
                    KnowledgeUpdateCandidate.origin_entity_id == outcome.id
                )
            ).scalars()
        ]
        assert "OUTCOME_FEEDBACK" not in origins
        assert origins == ["OUTCOME_AWARD"]
        session.commit()


def test_outcomes_do_not_cross_tenants(app_sessionmaker, seeded_orgs, users):
    org_a, org_b = seeded_orgs
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, _r, _t, _a, _amendment = _amended_scene(session, org_a, users)
        release.record_outcome(session, ctx, bid_id=bid.id, result=OutcomeResult.WON)
        bid_id = bid.id
        session.commit()

    with app_sessionmaker() as session:
        session.begin()
        apply_tenant_context(session, _ctx(org_b, uuid.uuid4()))
        assert (
            session.execute(select(Outcome).where(Outcome.bid_id == bid_id)).scalars().all() == []
        )
        session.rollback()
