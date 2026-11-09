"""Phase H against a real database.

Requirements come from the tender; evidence comes from company knowledge. This
module is where the two meet, so the assertions are about the meeting rules:
only governed evidence counts, stale evidence is visibly stale, every dashboard
number opens into the requirements that produced it, and the bid/no-bid verdict
is a recommendation a human can overrule on the record.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.core.errors import ValidationError
from app.db.session import apply_tenant_context
from app.gateway.embeddings import HashingEmbeddingProvider
from app.ingestion.safety import EicarAwareScanner
from app.models import (
    AuditEvent,
    Department,
    Evidence,
    Requirement,
    RequirementAssessment,
    RequirementVersion,
)
from app.models.enums import (
    ApprovalStatus,
    AssessmentState,
    BidNoBidResult,
    BidReadiness,
    ConfidentialityClass,
    DepartmentCode,
    DocumentSide,
    DocumentType,
    EvidenceStatus,
    KnowledgeStatus,
    RequirementApplicability,
    RequirementCategory,
    RequirementLifecycle,
    RiskLevel,
)
from app.services import assessment, bids, ingestion, readiness
from app.services.storage import InMemoryStorage

pytestmark = pytest.mark.integration

TODAY = date(2026, 6, 1)

COMPANY_TEXT = b"""# ISO 27001 certification

The company holds a valid ISO 27001 information security management
certification covering penetration testing services.
"""


@pytest.fixture
def fresh_org(owner_engine):
    """A tenant of this test's own.

    Evidence retrieval matches on shared terms, so evidence committed by one
    test would otherwise become a candidate in the next. Isolating the tenant
    keeps each case's arithmetic about the rows that case created.
    """
    from sqlalchemy.orm import sessionmaker

    from app.models import Organization

    org_id = uuid.uuid4()
    Owner = sessionmaker(bind=owner_engine, future=True)
    with Owner() as s:
        s.add(
            Organization(
                id=org_id,
                name=f"Assessment tenant {org_id.hex[:6]}",
                slug=f"assess-{org_id.hex[:10]}",
            )
        )
        s.commit()
    return org_id


@pytest.fixture
def env(fresh_org):
    return fresh_org, InMemoryStorage(), EicarAwareScanner(), HashingEmbeddingProvider()


def _company_version(session, ctx, env):
    """A real company document version, so evidence has somewhere to point."""
    _org, storage, scanner, _ = env
    return ingestion.register_upload(
        session,
        ctx,
        data=COMPANY_TEXT,
        filename=f"iso-{uuid.uuid4().hex[:6]}.md",
        side=DocumentSide.COMPANY,
        bid_id=None,
        confidentiality_class=ConfidentialityClass.INTERNAL,
        scanner=scanner,
        storage=storage,
    )


def _new_bid(session, ctx):
    opportunity = bids.create_opportunity(
        session,
        ctx,
        customer_name="ADNOC",
        tender_name=f"Assessment {uuid.uuid4().hex[:6]}",
    )
    bid = bids.promote_to_bid(session, ctx, opportunity_id=opportunity.id)
    bids.ensure_intake_open(session, ctx, bid_id=bid.id)
    return bid


def _requirement(
    session,
    ctx,
    *,
    bid,
    doc_version,
    code,
    text,
    category=RequirementCategory.SECURITY,
    mandatory=True,
    applicability=RequirementApplicability.ACTIVE,
):
    requirement = Requirement(
        org_id=ctx.org_id,
        bid_id=bid.id,
        code=code,
        category=category,
        is_mandatory=mandatory,
        is_scored=False,
        is_conditional=False,
        applicability=applicability,
        lifecycle_state=RequirementLifecycle.CONFIRMED,
        risk=RiskLevel.MEDIUM,
    )
    session.add(requirement)
    session.flush()
    version = RequirementVersion(
        org_id=ctx.org_id,
        requirement_id=requirement.id,
        version_no=1,
        requirement_text=text,
        source_document_version_id=doc_version.id,
        source_locator={"block_ordinal": 1},
        extracted_by="test",
    )
    session.add(version)
    session.flush()
    requirement.current_version_id = version.id
    session.flush()
    return requirement


def _evidence(
    session,
    ctx,
    *,
    doc_version,
    excerpt,
    status=KnowledgeStatus.CURRENT_VERIFIED,
    approval=ApprovalStatus.APPROVED,
    expiry=None,
):
    item = Evidence(
        org_id=ctx.org_id,
        source_document_version_id=doc_version.id,
        source_locator={"block_ordinal": 1},
        excerpt=excerpt,
        evidence_type=DocumentType.CERTIFICATE,
        approval_status=approval,
        confidentiality_class=ConfidentialityClass.INTERNAL,
        status=status,
        approved_for_reuse=approval is ApprovalStatus.APPROVED,
        expiry_date=expiry,
    )
    session.add(item)
    session.flush()
    return item


ISO_TEXT = "The bidder shall hold a valid ISO 27001 information security certification."


# --- what counts as evidence ------------------------------------------------


def test_unapproved_evidence_never_backs_an_assessment(app_sessionmaker, env):
    """Perfectly true and still not usable.

    Unapproved company material may be accurate, but a customer-facing claim
    must rest on something a knowledge administrator has signed off. The
    assessment must therefore read NONE, not "we found something".
    """
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        _evidence(
            s,
            ctx,
            doc_version=version,
            excerpt=ISO_TEXT,
            approval=ApprovalStatus.PENDING_APPROVAL,
        )
        requirement = _requirement(s, ctx, bid=bid, doc_version=version, code="R1", text=ISO_TEXT)

        result = assessment.assess_requirement(s, ctx, requirement=requirement, today=TODAY)
        assert result.evidence_status is EvidenceStatus.NONE
        assert result.bid_readiness is BidReadiness.BLOCKED
        s.commit()


def test_approved_current_evidence_makes_a_requirement_ready(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        _evidence(s, ctx, doc_version=version, excerpt=ISO_TEXT, expiry=TODAY + timedelta(365))
        requirement = _requirement(s, ctx, bid=bid, doc_version=version, code="R1", text=ISO_TEXT)

        result = assessment.assess_requirement(s, ctx, requirement=requirement, today=TODAY)
        assert result.evidence_status is EvidenceStatus.CURRENT_VERIFIED
        # DRAFTABLE, not READY, and the distinction is the point.
        #
        # The evidence is current, approved, the right type and matched -- and
        # nothing has READ it for whether it proves this requirement. In this
        # fixture no model runs, so no verdict exists. READY claims a drafter
        # has nothing left to resolve; on an unread shortlist that is not true.
        # R-019 on the live bid went READY on seven such excerpts.
        assert result.bid_readiness is BidReadiness.DRAFTABLE
        assert "has been judged proof" in result.rationale

        attached = assessment.evidence_for(s, ctx, assessment_id=result.id)
        assert attached, "the winning evidence must be citable from the assessment"
        assert result.reasoning_trace["candidates"], "the trace must record what was considered"
        s.commit()


def test_expired_certificate_is_needs_refresh_and_never_current(app_sessionmaker, env):
    """The failure mode this design exists to prevent, end to end."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        _evidence(
            s,
            ctx,
            doc_version=version,
            excerpt=ISO_TEXT,
            expiry=TODAY - timedelta(days=30),
        )
        requirement = _requirement(s, ctx, bid=bid, doc_version=version, code="R1", text=ISO_TEXT)

        result = assessment.assess_requirement(s, ctx, requirement=requirement, today=TODAY)
        assert result.evidence_status is EvidenceStatus.EXPIRED
        assert result.evidence_status is not EvidenceStatus.CURRENT_VERIFIED
        assert result.bid_readiness is BidReadiness.NEEDS_REFRESH
        s.commit()


# --- versioning and human override ------------------------------------------


def test_reassessment_supersedes_rather_than_overwrites(app_sessionmaker, env):
    """The prior verdict stays readable; only one row is current."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        requirement = _requirement(s, ctx, bid=bid, doc_version=version, code="R1", text=ISO_TEXT)

        first = assessment.assess_requirement(s, ctx, requirement=requirement, today=TODAY)
        assert first.evidence_status is EvidenceStatus.NONE

        # The certificate is uploaded and approved after the first pass.
        _evidence(s, ctx, doc_version=version, excerpt=ISO_TEXT, expiry=TODAY + timedelta(365))
        second = assessment.assess_requirement(s, ctx, requirement=requirement, today=TODAY)

        assert second.version == first.version + 1
        assert second.is_current is True
        assert first.is_current is False
        history = assessment.assessment_history(s, ctx, requirement_id=requirement.id)
        assert [a.version for a in history] == [1, 2]
        s.commit()


def test_only_one_assessment_per_requirement_can_be_current(app_sessionmaker, env):
    """Enforced by a partial unique index, not by service discipline alone."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        requirement = _requirement(s, ctx, bid=bid, doc_version=version, code="R1", text=ISO_TEXT)
        first = assessment.assess_requirement(s, ctx, requirement=requirement, today=TODAY)
        assessment.assess_requirement(s, ctx, requirement=requirement, today=TODAY)

        first.is_current = True
        with pytest.raises(Exception, match="uq_assessment_current"):
            s.flush()
        s.rollback()


def test_a_human_override_records_who_and_why(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        requirement = _requirement(s, ctx, bid=bid, doc_version=version, code="R1", text=ISO_TEXT)
        assessment.assess_requirement(s, ctx, requirement=requirement, today=TODAY)

        with pytest.raises(ValidationError, match="reason"):
            assessment.adjust(
                s, ctx, requirement_id=requirement.id, bid_readiness=BidReadiness.READY, reason=" "
            )

        adjusted = assessment.adjust(
            s,
            ctx,
            requirement_id=requirement.id,
            bid_readiness=BidReadiness.READY,
            reason="Certificate held outside the platform; confirmed by Compliance.",
        )
        assert adjusted.bid_readiness is BidReadiness.READY
        assert adjusted.assessment_state is AssessmentState.HUMAN_ADJUSTED
        assert adjusted.human_adjustment_reason

        events = (
            s.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org, AuditEvent.action == "assessment.adjusted"
                )
            )
            .scalars()
            .all()
        )
        assert events, "a human override must be auditable"
        assert events[-1].before_state["bid_readiness"] == BidReadiness.BLOCKED.value
        s.commit()


def test_marking_stale_keeps_the_previous_verdict_visible(app_sessionmaker, env):
    """An amendment invalidates a verdict; it does not erase it."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        requirement = _requirement(s, ctx, bid=bid, doc_version=version, code="R1", text=ISO_TEXT)
        before = assessment.assess_requirement(s, ctx, requirement=requirement, today=TODAY)

        count = assessment.mark_stale(
            s, ctx, requirement_ids=[requirement.id], reason="Amendment 2 changed clause 4.2."
        )
        assert count == 1
        current = assessment.current_assessment(s, ctx, requirement_id=requirement.id)
        assert current.assessment_state is AssessmentState.STALE
        assert current.bid_readiness is before.bid_readiness
        assert "Amendment 2" in current.stale_reason
        s.commit()


# --- the bid-level view -----------------------------------------------------


def _populated_bid(s, ctx, env):
    """A bid with one ready, one expired, one blocked and one not-applicable.

    The requirement wordings deliberately share no vocabulary with each other's
    evidence. Candidate retrieval here is lexical, and these tests are about
    what the engine concludes from a candidate set, not about how good the
    retrieval is at building one.
    """
    bid = _new_bid(s, ctx)
    version = _company_version(s, ctx, env)
    _evidence(s, ctx, doc_version=version, excerpt=ISO_TEXT, expiry=TODAY + timedelta(365))
    _evidence(
        s,
        ctx,
        doc_version=version,
        excerpt="The company maintains a documented disaster recovery runbook.",
        expiry=TODAY - timedelta(days=10),
    )

    _requirement(s, ctx, bid=bid, doc_version=version, code="SEC-1", text=ISO_TEXT)
    _requirement(
        s,
        ctx,
        bid=bid,
        doc_version=version,
        code="TEC-1",
        text="A documented disaster recovery runbook must exist and be maintained.",
        category=RequirementCategory.TECHNICAL,
    )
    _requirement(
        s,
        ctx,
        bid=bid,
        doc_version=version,
        code="ELG-1",
        text="Audited annual accounts covering three fiscal years accompany every offer.",
        category=RequirementCategory.ELIGIBILITY,
    )
    _requirement(
        s,
        ctx,
        bid=bid,
        doc_version=version,
        code="SUB-1",
        text="Subcontractor profiles are provided whenever subcontracting occurs.",
        category=RequirementCategory.SUBMISSION,
        applicability=RequirementApplicability.NOT_APPLICABLE,
    )
    return bid


def test_assess_bid_covers_every_requirement_and_reports_both_axes(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _populated_bid(s, ctx, env)

        report = assessment.assess_bid(s, ctx, bid_id=bid.id, today=TODAY)
        assert report.assessed == 4
        assert report.skipped_not_applicable == 1
        # DRAFTABLE where this once said READY: the row has current approved
        # evidence and no model ran to read it. See
        # test_approved_current_evidence_makes_a_requirement_ready.
        assert report.by_readiness[BidReadiness.DRAFTABLE.value] == 1
        assert BidReadiness.READY.value not in report.by_readiness
        assert report.by_readiness[BidReadiness.NEEDS_REFRESH.value] == 1
        assert report.by_readiness[BidReadiness.BLOCKED.value] == 1
        # Both axes are reported, and they are not the same partition.
        assert report.by_evidence_status[EvidenceStatus.EXPIRED.value] == 1
        s.commit()


def test_every_dashboard_number_opens_into_its_requirements(app_sessionmaker, env):
    """A figure a user cannot drill into is a figure they cannot act on."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _populated_bid(s, ctx, env)
        assessment.assess_bid(s, ctx, bid_id=bid.id, today=TODAY)

        summary = readiness.summarize(s, ctx, bid_id=bid.id)
        assert summary.total_requirements == 4
        assert summary.applicable_requirements == 3
        assert summary.not_applicable == 1

        for metric, expected in (
            ("CRITICAL_BLOCKERS", summary.critical_blockers),
            ("MANDATORY_UNREADY", summary.mandatory_unready),
            ("OUTSTANDING_ACTIONS", summary.outstanding_actions),
        ):
            items = readiness.drill_through(s, ctx, bid_id=bid.id, metric=metric)
            assert len(items) == expected, f"{metric} does not open into its own count"

        blocked = readiness.drill_through(s, ctx, bid_id=bid.id, metric=BidReadiness.BLOCKED.value)
        assert [row["code"] for row in blocked] == ["ELG-1"]
        assert blocked[0]["rationale"], "a blocker must explain itself"

        with pytest.raises(ValidationError, match="Unknown readiness metric"):
            readiness.drill_through(s, ctx, bid_id=bid.id, metric="MOSTLY_FINE")
        s.rollback()


def test_not_applicable_requirements_are_out_of_the_coverage_denominator(app_sessionmaker, env):
    """A condition that does not apply is not a gap to chase."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _populated_bid(s, ctx, env)
        assessment.assess_bid(s, ctx, bid_id=bid.id, today=TODAY)

        summary = readiness.summarize(s, ctx, bid_id=bid.id)
        # One of three applicable requirements is READY.
        assert summary.requirement_coverage_pct == pytest.approx(33.3)
        assert BidReadiness.NOT_APPLICABLE.value not in summary.by_readiness
        s.rollback()


def test_the_matrices_partition_by_category(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _populated_bid(s, ctx, env)
        assessment.assess_bid(s, ctx, bid_id=bid.id, today=TODAY)

        eligibility = readiness.eligibility_matrix(s, ctx, bid_id=bid.id)
        technical = readiness.technical_matrix(s, ctx, bid_id=bid.id)
        submission = readiness.submission_matrix(s, ctx, bid_id=bid.id)

        assert [r["code"] for r in eligibility] == ["ELG-1"]
        assert {r["code"] for r in technical} == {"SEC-1", "TEC-1"}
        assert [r["code"] for r in submission] == ["SUB-1"]
        s.rollback()


# --- bid / no-bid -----------------------------------------------------------


def test_a_blocked_eligibility_requirement_forces_a_red_recommendation(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _populated_bid(s, ctx, env)
        assessment.assess_bid(s, ctx, bid_id=bid.id, today=TODAY)

        recommendation = readiness.recommend_bid_no_bid(s, ctx, bid_id=bid.id)
        assert recommendation.result is BidNoBidResult.RED
        assert "ELG-1" in recommendation.factors["eligibility_blocked"]
        assert any("Eligibility" in reason for reason in recommendation.blocking_factors)
        s.rollback()


def test_a_human_may_overrule_the_recommendation_and_it_is_recorded(app_sessionmaker, env):
    """Management decides. The product's job is to make the disagreement legible."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _populated_bid(s, ctx, env)
        assessment.assess_bid(s, ctx, bid_id=bid.id, today=TODAY)

        with pytest.raises(ValidationError, match="rationale"):
            readiness.record_bid_no_bid(
                s, ctx, bid_id=bid.id, result=BidNoBidResult.GREEN, rationale=""
            )

        decision = readiness.record_bid_no_bid(
            s,
            ctx,
            bid_id=bid.id,
            result=BidNoBidResult.GREEN,
            rationale="The financial statements exist offline and will be attached.",
        )
        assert decision.result is BidNoBidResult.GREEN
        assert decision.recommended_result is BidNoBidResult.RED
        assert decision.factors["eligibility_blocked"] == ["ELG-1"]

        events = (
            s.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org,
                    AuditEvent.action == "bid.no_bid_decision_recorded",
                )
            )
            .scalars()
            .all()
        )
        assert events[-1].after_state["overrode_recommendation"] is True
        assert readiness.current_decision(s, ctx, bid_id=bid.id).id == decision.id
        s.commit()


def test_a_new_decision_supersedes_the_previous_one(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _populated_bid(s, ctx, env)
        assessment.assess_bid(s, ctx, bid_id=bid.id, today=TODAY)

        first = readiness.record_bid_no_bid(
            s, ctx, bid_id=bid.id, result=BidNoBidResult.AMBER, rationale="Pending review."
        )
        second = readiness.record_bid_no_bid(
            s, ctx, bid_id=bid.id, result=BidNoBidResult.RED, rationale="Finance declined."
        )
        s.refresh(first)
        assert first.is_current is False
        assert readiness.current_decision(s, ctx, bid_id=bid.id).id == second.id
        s.commit()


# --- department recommendation ----------------------------------------------


def test_department_recommendation_is_a_suggestion_not_an_assignment(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        for code in (DepartmentCode.SECURITY, DepartmentCode.BID_MANAGEMENT):
            if not s.execute(
                select(Department).where(Department.org_id == org, Department.code == code)
            ).scalar_one_or_none():
                s.add(Department(org_id=org, code=code, name=code.value.title()))
        s.flush()
        bid = _populated_bid(s, ctx, env)

        assigned = assessment.recommend_departments(s, ctx, bid_id=bid.id)
        assert assigned >= 2

        security = s.execute(
            select(Requirement).where(
                Requirement.org_id == org,
                Requirement.bid_id == bid.id,
                Requirement.code == "SEC-1",
            )
        ).scalar_one()
        assert security.recommended_department_id is not None

        # Running it again must not overwrite a bid manager's own choice.
        again = assessment.recommend_departments(s, ctx, bid_id=bid.id)
        assert again == 0
        s.commit()


def test_an_assessment_cannot_be_read_across_tenants(app_sessionmaker, seeded_orgs, env):
    """RLS, proven as the constrained application role."""
    org_a = env[0]
    org_b = seeded_orgs[1]
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org_a)
        apply_tenant_context(s, ctx)
        bid = _populated_bid(s, ctx, env)
        assessment.assess_bid(s, ctx, bid_id=bid.id, today=TODAY)
        bid_id = bid.id
        s.commit()

    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ExecutionContext(org_id=org_b))
        rows = (
            s.execute(select(RequirementAssessment).where(RequirementAssessment.bid_id == bid_id))
            .scalars()
            .all()
        )
        assert rows == []
        s.rollback()
