"""Matrices, bid readiness, and the bid/no-bid gate.

Readiness is deliberately several numbers, not one. A single percentage hides
the difference between "we have not written it yet" and "we cannot prove it",
and those need different people to do different things. Every figure here is a
deterministic aggregation over current assessments, and every one drills through
to the items behind it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import ValidationError
from app.models import (
    BidNoBidDecision,
    ClarificationIssue,
    Deliverable,
    Evidence,
    Requirement,
    RequirementAssessment,
    RequirementVersion,
)
from app.models.enums import (
    BidNoBidResult,
    BidReadiness,
    ClarificationStatus,
    EvidenceStatus,
    RequirementApplicability,
    RequirementCategory,
)
from app.services import audit, clarifications
from app.services.requirement_scope import LIVE_REQUIREMENT

# Readiness states that stop a bid rather than merely slowing it.
CRITICAL_STATES = frozenset({BidReadiness.BLOCKED})
# States that mean somebody must act before drafting can finish.
ACTION_STATES = frozenset(
    {
        BidReadiness.NEEDS_REFRESH,
        BidReadiness.NEEDS_COMPANY_CONFIRMATION,
        BidReadiness.NEEDS_CLIENT_CLARIFICATION,
        BidReadiness.REQUIRES_SIGNATURE,
        BidReadiness.BLOCKED,
    }
)
ELIGIBILITY_CATEGORIES = frozenset({RequirementCategory.ELIGIBILITY, RequirementCategory.LEGAL})
SUBMISSION_CATEGORIES = frozenset(
    {RequirementCategory.SUBMISSION, RequirementCategory.ADMINISTRATIVE}
)
CONTRACT_CATEGORIES = frozenset({RequirementCategory.COMMERCIAL, RequirementCategory.CONTRACTUAL})


@dataclass
class ReadinessSummary:
    total_requirements: int = 0
    applicable_requirements: int = 0
    not_applicable: int = 0
    # Clauses that bind the customer. Counted, and kept out of the denominator
    # like NOT_APPLICABLE: the bidder cannot make them READY, so leaving them in
    # would cap coverage below 100% for a reason nobody can act on.
    customer_obligations: int = 0
    assessed: int = 0
    by_readiness: dict[str, int] = field(default_factory=dict)
    by_evidence_status: dict[str, int] = field(default_factory=dict)
    requirement_coverage_pct: float = 0.0
    evidence_readiness_pct: float = 0.0
    critical_blockers: int = 0
    open_clarifications: int = 0
    outstanding_actions: int = 0
    mandatory_unready: int = 0
    # BLOCKED for want of library proof, but answered by an approved promise.
    answered_by_commitment: int = 0
    deliverables_total: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _pct(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else round(100 * numerator / denominator, 1)


def answered_by_approved_commitment(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID
) -> set[str]:
    """Codes of requirements whose owner has answered with a promise a named role approved.

    Readiness measures PROOF in the library. Some mandatory clauses will never
    have proof there -- a customer's own system we have not integrated with --
    and the honest answer is "we have not done this, here is how we will". The
    task owner writes that with the assistant, the role that would stand behind
    it approves the promise, and the artifact is approved. That answer is done;
    leaving the clause red as BLOCKED hid the real work behind a number nobody
    could move. It is reported separately instead, never as coverage.

    A requirement counts only when an APPROVED artifact for it holds a
    PROPOSED_COMMITMENT statement whose commitment is APPROVED. A statement
    that names the codes it answers narrows this to those codes; one that names
    none stands for every requirement of its task.
    """
    from app.models import (
        ArtifactRequirement,
        ArtifactStatement,
        Commitment,
        ResponseArtifact,
    )
    from app.models.enums import ApprovalStatus, ArtifactState, StatementClass

    rows = session.execute(
        select(ResponseArtifact.id, ArtifactStatement.requirement_codes)
        .join(
            ArtifactStatement,
            ArtifactStatement.artifact_version_id == ResponseArtifact.approved_version_id,
        )
        .join(Commitment, Commitment.id == ArtifactStatement.commitment_id)
        .where(
            ResponseArtifact.org_id == ctx.org_id,
            ResponseArtifact.bid_id == bid_id,
            ResponseArtifact.state == ArtifactState.APPROVED,
            ArtifactStatement.statement_class == StatementClass.PROPOSED_COMMITMENT,
            Commitment.approval_status == ApprovalStatus.APPROVED,
        )
    ).all()
    if not rows:
        return set()
    task_codes: dict[uuid.UUID, set[str]] = {}
    for artifact_id in {r[0] for r in rows}:
        task_codes[artifact_id] = set(
            session.execute(
                select(Requirement.code)
                .join(ArtifactRequirement, ArtifactRequirement.requirement_id == Requirement.id)
                .where(
                    ArtifactRequirement.org_id == ctx.org_id,
                    ArtifactRequirement.artifact_id == artifact_id,
                    LIVE_REQUIREMENT,
                )
            ).scalars()
        )
    answered: set[str] = set()
    for artifact_id, codes in rows:
        named = {c for c in (codes or []) if c in task_codes[artifact_id]}
        answered |= named or task_codes[artifact_id]
    return answered


def summarize(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID) -> ReadinessSummary:
    rows = session.execute(
        select(Requirement, RequirementAssessment)
        .outerjoin(
            RequirementAssessment,
            (RequirementAssessment.requirement_id == Requirement.id)
            & (RequirementAssessment.is_current.is_(True)),
        )
        .where(Requirement.org_id == ctx.org_id, Requirement.bid_id == bid_id, LIVE_REQUIREMENT)
    ).all()

    summary = ReadinessSummary(total_requirements=len(rows))
    promised = answered_by_approved_commitment(session, ctx, bid_id=bid_id)
    ready_like = 0
    evidenced = 0

    for requirement, assessment in rows:
        if requirement.applicability is RequirementApplicability.NOT_APPLICABLE:
            summary.not_applicable += 1
            continue
        if assessment is not None and assessment.bid_readiness is BidReadiness.CUSTOMER_OBLIGATION:
            summary.customer_obligations += 1
            summary.by_readiness[assessment.bid_readiness.value] = (
                summary.by_readiness.get(assessment.bid_readiness.value, 0) + 1
            )
            continue
        summary.applicable_requirements += 1
        if assessment is None:
            continue
        summary.assessed += 1
        summary.by_readiness[assessment.bid_readiness.value] = (
            summary.by_readiness.get(assessment.bid_readiness.value, 0) + 1
        )
        summary.by_evidence_status[assessment.evidence_status.value] = (
            summary.by_evidence_status.get(assessment.evidence_status.value, 0) + 1
        )
        if assessment.bid_readiness in (BidReadiness.READY, BidReadiness.DRAFTABLE):
            ready_like += 1
        if assessment.evidence_status in (
            EvidenceStatus.CURRENT_VERIFIED,
            EvidenceStatus.PARTIAL,
        ):
            evidenced += 1
        if assessment.bid_readiness is BidReadiness.BLOCKED and requirement.code in promised:
            summary.answered_by_commitment += 1
            continue
        if assessment.bid_readiness in CRITICAL_STATES:
            summary.critical_blockers += 1
        if assessment.bid_readiness in ACTION_STATES:
            summary.outstanding_actions += 1
        if requirement.is_mandatory and assessment.bid_readiness not in (
            BidReadiness.READY,
            BidReadiness.DRAFTABLE,
            BidReadiness.NOT_APPLICABLE,
            BidReadiness.CUSTOMER_OBLIGATION,
        ):
            summary.mandatory_unready += 1

    # Coverage counts NOT_APPLICABLE out of the denominator entirely: a
    # conditional requirement that does not apply is not a gap to chase.
    summary.requirement_coverage_pct = _pct(ready_like, summary.applicable_requirements)
    summary.evidence_readiness_pct = _pct(evidenced, summary.applicable_requirements)

    summary.open_clarifications = session.execute(
        select(func.count())
        .select_from(ClarificationIssue)
        .where(
            ClarificationIssue.org_id == ctx.org_id,
            ClarificationIssue.bid_id == bid_id,
            ClarificationIssue.status.in_(
                [ClarificationStatus.OPEN, ClarificationStatus.SENT_TO_CLIENT]
            ),
        )
    ).scalar_one()
    summary.deliverables_total = session.execute(
        select(func.count())
        .select_from(Deliverable)
        .where(Deliverable.org_id == ctx.org_id, Deliverable.bid_id == bid_id)
    ).scalar_one()
    return summary


def drill_through(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    metric: str,
) -> list[dict]:
    """The items behind a dashboard number.

    Every figure on the readiness screen must be traceable to the requirements
    that produced it. A number a user cannot open is a number they cannot act
    on.
    """
    rows = session.execute(
        select(Requirement, RequirementAssessment, RequirementVersion)
        .outerjoin(
            RequirementAssessment,
            (RequirementAssessment.requirement_id == Requirement.id)
            & (RequirementAssessment.is_current.is_(True)),
        )
        .outerjoin(RequirementVersion, RequirementVersion.id == Requirement.current_version_id)
        .where(Requirement.org_id == ctx.org_id, Requirement.bid_id == bid_id, LIVE_REQUIREMENT)
        .order_by(Requirement.code)
    ).all()

    promised = answered_by_approved_commitment(session, ctx, bid_id=bid_id)

    def matches(requirement: Requirement, assessment: RequirementAssessment | None) -> bool:
        key = metric.upper()
        if key == "ANSWERED_BY_COMMITMENT":
            return (
                assessment is not None
                and assessment.bid_readiness is BidReadiness.BLOCKED
                and requirement.code in promised
            )
        # Same rule as the summary: a promised clause is reported on its own.
        if (
            key in {"CRITICAL_BLOCKERS", "OUTSTANDING_ACTIONS", "MANDATORY_UNREADY"}
            and assessment is not None
            and assessment.bid_readiness is BidReadiness.BLOCKED
            and requirement.code in promised
        ):
            return False
        if key == "UNASSESSED":
            return (
                assessment is None
                and requirement.applicability is not RequirementApplicability.NOT_APPLICABLE
            )
        if key == "CRITICAL_BLOCKERS":
            return assessment is not None and assessment.bid_readiness in CRITICAL_STATES
        if key == "OUTSTANDING_ACTIONS":
            return assessment is not None and assessment.bid_readiness in ACTION_STATES
        if key == "MANDATORY_UNREADY":
            return (
                requirement.is_mandatory
                and assessment is not None
                and assessment.bid_readiness
                not in (
                    BidReadiness.READY,
                    BidReadiness.DRAFTABLE,
                    BidReadiness.NOT_APPLICABLE,
                    BidReadiness.CUSTOMER_OBLIGATION,
                )
            )
        if key in {s.value for s in BidReadiness}:
            return assessment is not None and assessment.bid_readiness.value == key
        if key in {s.value for s in EvidenceStatus}:
            return assessment is not None and assessment.evidence_status.value == key
        raise ValidationError(f"Unknown readiness metric '{metric}'.")

    return [
        {
            "requirement_id": str(requirement.id),
            "code": requirement.code,
            "category": requirement.category.value,
            "is_mandatory": requirement.is_mandatory,
            "applicability": requirement.applicability.value,
            "text": version.requirement_text if version else None,
            "evidence_status": assessment.evidence_status.value if assessment else None,
            "bid_readiness": assessment.bid_readiness.value if assessment else None,
            "rationale": assessment.rationale if assessment else None,
        }
        for requirement, assessment, version in rows
        if matches(requirement, assessment)
    ]


# --- matrices ---------------------------------------------------------------


def _matrix_rows(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    categories: frozenset[RequirementCategory] | None,
    exclude: frozenset[RequirementCategory] | None = None,
) -> list[dict]:
    rows = session.execute(
        select(Requirement, RequirementAssessment, RequirementVersion)
        .outerjoin(
            RequirementAssessment,
            (RequirementAssessment.requirement_id == Requirement.id)
            & (RequirementAssessment.is_current.is_(True)),
        )
        .outerjoin(RequirementVersion, RequirementVersion.id == Requirement.current_version_id)
        .where(Requirement.org_id == ctx.org_id, Requirement.bid_id == bid_id, LIVE_REQUIREMENT)
        .order_by(Requirement.code)
    ).all()

    promised = answered_by_approved_commitment(session, ctx, bid_id=bid_id)
    out: list[dict] = []
    for requirement, assessment, version in rows:
        if categories is not None and requirement.category not in categories:
            continue
        if exclude is not None and requirement.category in exclude:
            continue
        evidence_ids = []
        if assessment is not None:
            evidence_ids = [
                str(e)
                for e in session.execute(
                    select(Evidence.id)
                    .join(
                        _assessment_evidence_table(),
                        _assessment_evidence_table().c.evidence_id == Evidence.id,
                    )
                    .where(
                        _assessment_evidence_table().c.assessment_id == assessment.id,
                        Evidence.org_id == ctx.org_id,
                    )
                )
                .scalars()
                .all()
            ]
        out.append(
            {
                "requirement_id": str(requirement.id),
                "code": requirement.code,
                "category": requirement.category.value,
                # Surfaced so a reviewer can see which rows the classifier moved
                # off the bidder and check them. A relabelling nobody can audit
                # is worse than no relabelling.
                "obligation_owner": (
                    requirement.obligation_owner.value if requirement.obligation_owner else "BIDDER"
                ),
                "mandatory": requirement.is_mandatory,
                "scored": requirement.is_scored,
                "applicability": requirement.applicability.value,
                # Whether a person has accepted this as what the customer asked for.
                # An answer to a clause nobody has confirmed cannot be approved.
                "lifecycle_state": requirement.lifecycle_state.value,
                "requirement_text": version.requirement_text if version else None,
                "clause_ref": version.clause_ref if version else None,
                "source_locator": version.source_locator if version else None,
                "evidence_status": assessment.evidence_status.value if assessment else None,
                "bid_readiness": assessment.bid_readiness.value if assessment else None,
                "answered_by_commitment": bool(
                    assessment
                    and assessment.bid_readiness is BidReadiness.BLOCKED
                    and requirement.code in promised
                ),
                "rationale": assessment.rationale if assessment else None,
                "evidence_ids": evidence_ids,
            }
        )
    return out


def _assessment_evidence_table():
    from app.models import AssessmentEvidence

    return AssessmentEvidence.__table__


def eligibility_matrix(session, ctx, *, bid_id: uuid.UUID) -> list[dict]:
    """Can the company qualify at all?"""
    return _matrix_rows(session, ctx, bid_id=bid_id, categories=ELIGIBILITY_CATEGORIES)


def technical_matrix(session, ctx, *, bid_id: uuid.UUID) -> list[dict]:
    return _matrix_rows(
        session,
        ctx,
        bid_id=bid_id,
        categories=None,
        exclude=ELIGIBILITY_CATEGORIES | SUBMISSION_CATEGORIES | CONTRACT_CATEGORIES,
    )


def submission_matrix(session, ctx, *, bid_id: uuid.UUID) -> list[dict]:
    return _matrix_rows(session, ctx, bid_id=bid_id, categories=SUBMISSION_CATEGORIES)


def contract_register(session, ctx, *, bid_id: uuid.UUID) -> list[dict]:
    return _matrix_rows(session, ctx, bid_id=bid_id, categories=CONTRACT_CATEGORIES)


# --- bid / no-bid -----------------------------------------------------------


@dataclass
class BidNoBidRecommendation:
    result: BidNoBidResult
    factors: dict
    blocking_factors: list[str]

    def as_dict(self) -> dict:
        return {
            "result": self.result.value,
            "factors": self.factors,
            "blocking_factors": self.blocking_factors,
        }


def recommend_bid_no_bid(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID
) -> BidNoBidRecommendation:
    """A recommendation, not a decision.

    Management decides; the product's job is to make the factors explicit so the
    decision is informed rather than optimistic.
    """
    from app.services.tender_analysis import get_scope_confidence

    summary = summarize(session, ctx, bid_id=bid_id)
    issue_counts = clarifications.counts(session, ctx, bid_id=bid_id)
    confidence = get_scope_confidence(session, ctx, bid_id=bid_id)

    eligibility = eligibility_matrix(session, ctx, bid_id=bid_id)
    eligibility_blocked = [
        row["code"]
        for row in eligibility
        if row["bid_readiness"] == BidReadiness.BLOCKED.value
        and not row.get("answered_by_commitment")
    ]

    factors = {
        "requirement_coverage_pct": summary.requirement_coverage_pct,
        "evidence_readiness_pct": summary.evidence_readiness_pct,
        "critical_blockers": summary.critical_blockers,
        "mandatory_unready": summary.mandatory_unready,
        "open_clarifications": summary.open_clarifications,
        "blocking_clarifications": issue_counts.get("blocking", 0),
        "eligibility_blocked": eligibility_blocked,
        "scope_confidence": confidence.level.value if confidence else None,
        "unassessed": summary.applicable_requirements - summary.assessed,
    }

    blocking: list[str] = []
    if eligibility_blocked:
        blocking.append("Eligibility requirements cannot be met: " + ", ".join(eligibility_blocked))
    if issue_counts.get("blocking", 0):
        blocking.append("Blocking clarifications are unresolved.")
    if confidence is not None and confidence.level.value == "LOW":
        blocking.append("Tender scope is not understood well enough to commit.")

    if blocking:
        result = BidNoBidResult.RED
    elif (
        summary.critical_blockers
        or summary.mandatory_unready
        or summary.open_clarifications
        or factors["unassessed"]
    ):
        result = BidNoBidResult.AMBER
    else:
        result = BidNoBidResult.GREEN

    return BidNoBidRecommendation(result=result, factors=factors, blocking_factors=blocking)


def record_bid_no_bid(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    result: BidNoBidResult,
    rationale: str,
) -> BidNoBidDecision:
    if not (rationale or "").strip():
        raise ValidationError("A bid/no-bid decision requires a rationale.")

    recommendation = recommend_bid_no_bid(session, ctx, bid_id=bid_id)
    previous = session.execute(
        select(BidNoBidDecision).where(
            BidNoBidDecision.org_id == ctx.org_id,
            BidNoBidDecision.bid_id == bid_id,
            BidNoBidDecision.is_current.is_(True),
        )
    ).scalar_one_or_none()
    if previous is not None:
        previous.is_current = False
        session.flush()

    decision = BidNoBidDecision(
        org_id=ctx.org_id,
        bid_id=bid_id,
        result=result,
        factors=recommendation.factors,
        rationale=rationale.strip(),
        recommended_result=recommendation.result,
        decided_by=ctx.actor_user_id,
        decided_at=datetime.now(timezone.utc),
        is_current=True,
        blocking_factors=recommendation.blocking_factors or None,
    )
    session.add(decision)
    session.flush()

    audit.record(
        session,
        ctx,
        action="bid.no_bid_decision_recorded",
        entity_type="bid_no_bid_decision",
        entity_id=decision.id,
        bid_id=bid_id,
        before={"result": previous.result.value} if previous else None,
        after={
            "result": result.value,
            "recommended": recommendation.result.value,
            # Worth seeing at a glance: a human overriding a RED recommendation
            # is the decision most likely to be revisited later.
            "overrode_recommendation": result is not recommendation.result,
        },
        reason=rationale,
    )
    return decision


def current_decision(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID
) -> BidNoBidDecision | None:
    return session.execute(
        select(BidNoBidDecision).where(
            BidNoBidDecision.org_id == ctx.org_id,
            BidNoBidDecision.bid_id == bid_id,
            BidNoBidDecision.is_current.is_(True),
        )
    ).scalar_one_or_none()
