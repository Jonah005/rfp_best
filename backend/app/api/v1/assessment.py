"""Assessment, matrices and readiness routes.

Read routes outnumber action routes here on purpose: this is the part of the
product people interrogate. Every dashboard number has a drill-through, and
every matrix row carries its clause citation and its evidence ids so a reviewer
can follow any figure back to the text it came from.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_context, get_db, require
from app.core.context import ExecutionContext
from app.core.errors import NotFound
from app.core.permissions import Perm
from app.models.enums import (
    AssessmentEvidenceRole,
    BidNoBidResult,
    BidReadiness,
    EvidenceStatus,
)
from app.schemas.assessment import (
    AssessmentAdjust,
    AssessmentDetail,
    AssessmentEvidenceOut,
    AssessmentOut,
    AttachEvidence,
    BidNoBidOut,
    BidNoBidRecommendationOut,
    BidNoBidRecord,
    ReadinessOut,
)
from app.services import assessment as assessment_service
from app.services import readiness as readiness_service

router = APIRouter(tags=["assessment"])


# --- running an assessment --------------------------------------------------


@router.post(
    "/bids/{bid_id}/assess",
    response_model=dict,
    dependencies=[Depends(require(Perm.ASSESSMENT_WRITE))],
)
def assess_bid(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Assess every requirement in the bid against governed company evidence."""
    return assessment_service.assess_bid(session, ctx, bid_id=bid_id).as_dict()


@router.post(
    "/bids/{bid_id}/recommend-departments",
    response_model=dict,
    dependencies=[Depends(require(Perm.ASSESSMENT_WRITE))],
)
def recommend_departments(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return {
        "assigned": assessment_service.recommend_departments(session, ctx, bid_id=bid_id)
    }


# --- inspecting one requirement ---------------------------------------------


@router.get(
    "/requirements/{requirement_id}/assessment",
    response_model=AssessmentDetail,
    dependencies=[Depends(require(Perm.ASSESSMENT_READ))],
)
def get_assessment(
    requirement_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> AssessmentDetail:
    """The verdict, its rationale, and every piece of evidence behind it."""
    current = assessment_service.current_assessment(
        session, ctx, requirement_id=requirement_id
    )
    if current is None:
        raise NotFound("This requirement has not been assessed yet.")
    pairs = assessment_service.evidence_for(session, ctx, assessment_id=current.id)
    return AssessmentDetail(
        assessment=AssessmentOut.model_validate(current),
        evidence=[
            AssessmentEvidenceOut(
                evidence_id=evidence.id,
                role=link.role.value,
                weight=float(link.weight) if link.weight is not None else None,
                contribution_note=link.contribution_note,
                retrieval_rank=link.retrieval_rank,
                excerpt=evidence.excerpt,
                evidence_status=evidence.status.value,
                approval_status=evidence.approval_status.value,
                expiry_date=evidence.expiry_date.isoformat() if evidence.expiry_date else None,
                source_locator=evidence.source_locator,
            )
            for link, evidence in pairs
        ],
    )


@router.get(
    "/requirements/{requirement_id}/assessment/history",
    response_model=list[AssessmentOut],
    dependencies=[Depends(require(Perm.ASSESSMENT_READ))],
)
def assessment_history(
    requirement_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[AssessmentOut]:
    rows = assessment_service.assessment_history(session, ctx, requirement_id=requirement_id)
    return [AssessmentOut.model_validate(a) for a in rows]


@router.patch(
    "/requirements/{requirement_id}/assessment",
    response_model=AssessmentOut,
    dependencies=[Depends(require(Perm.ASSESSMENT_WRITE))],
)
def adjust_assessment(
    requirement_id: uuid.UUID,
    payload: AssessmentAdjust,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> AssessmentOut:
    """Override the computed verdict. The reason is mandatory and audited."""
    adjusted = assessment_service.adjust(
        session,
        ctx,
        requirement_id=requirement_id,
        evidence_status=EvidenceStatus(payload.evidence_status)
        if payload.evidence_status
        else None,
        bid_readiness=BidReadiness(payload.bid_readiness) if payload.bid_readiness else None,
        reason=payload.reason,
    )
    return AssessmentOut.model_validate(adjusted)


@router.post(
    "/requirements/{requirement_id}/assessment/evidence",
    response_model=dict,
    dependencies=[Depends(require(Perm.ASSESSMENT_WRITE))],
)
def attach_evidence(
    requirement_id: uuid.UUID,
    payload: AttachEvidence,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    link = assessment_service.attach_evidence(
        session,
        ctx,
        requirement_id=requirement_id,
        evidence_id=payload.evidence_id,
        role=AssessmentEvidenceRole(payload.role),
        note=payload.note,
    )
    return {"assessment_id": str(link.assessment_id), "evidence_id": str(link.evidence_id)}


# --- matrices ---------------------------------------------------------------


@router.get(
    "/bids/{bid_id}/matrices/eligibility",
    response_model=list[dict],
    dependencies=[Depends(require(Perm.ASSESSMENT_READ))],
)
def eligibility_matrix(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    """Can the company qualify at all? Checked before drafting effort is spent."""
    return readiness_service.eligibility_matrix(session, ctx, bid_id=bid_id)


@router.get(
    "/bids/{bid_id}/matrices/technical",
    response_model=list[dict],
    dependencies=[Depends(require(Perm.ASSESSMENT_READ))],
)
def technical_matrix(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    return readiness_service.technical_matrix(session, ctx, bid_id=bid_id)


@router.get(
    "/bids/{bid_id}/matrices/submission",
    response_model=list[dict],
    dependencies=[Depends(require(Perm.ASSESSMENT_READ))],
)
def submission_matrix(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    return readiness_service.submission_matrix(session, ctx, bid_id=bid_id)


@router.get(
    "/bids/{bid_id}/matrices/contract",
    response_model=list[dict],
    dependencies=[Depends(require(Perm.ASSESSMENT_READ))],
)
def contract_register(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    return readiness_service.contract_register(session, ctx, bid_id=bid_id)


# --- readiness --------------------------------------------------------------


@router.get(
    "/bids/{bid_id}/readiness",
    response_model=ReadinessOut,
    dependencies=[Depends(require(Perm.ASSESSMENT_READ))],
)
def readiness(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ReadinessOut:
    """Several measures, never one percentage.

    A single number hides the difference between "not written yet" and "cannot
    be proven", and those need different people to do different things.
    """
    return ReadinessOut(**readiness_service.summarize(session, ctx, bid_id=bid_id).as_dict())


@router.get(
    "/bids/{bid_id}/readiness/{metric}/items",
    response_model=list[dict],
    dependencies=[Depends(require(Perm.ASSESSMENT_READ))],
)
def readiness_items(
    bid_id: uuid.UUID,
    metric: str,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    """The requirements behind any readiness number.

    Accepts a readiness state (`READY`, `BLOCKED`, ...), an evidence status
    (`CURRENT_VERIFIED`, `EXPIRED`, ...), or one of `UNASSESSED`,
    `CRITICAL_BLOCKERS`, `OUTSTANDING_ACTIONS`, `MANDATORY_UNREADY`.
    """
    return readiness_service.drill_through(session, ctx, bid_id=bid_id, metric=metric)


# --- bid / no-bid -----------------------------------------------------------


@router.get(
    "/bids/{bid_id}/bid-no-bid/recommendation",
    response_model=BidNoBidRecommendationOut,
    dependencies=[Depends(require(Perm.ASSESSMENT_READ))],
)
def bid_no_bid_recommendation(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> BidNoBidRecommendationOut:
    """What the factors suggest. Management still decides."""
    return BidNoBidRecommendationOut(
        **readiness_service.recommend_bid_no_bid(session, ctx, bid_id=bid_id).as_dict()
    )


@router.post(
    "/bids/{bid_id}/bid-no-bid",
    response_model=BidNoBidOut,
    dependencies=[Depends(require(Perm.BID_WRITE))],
)
def record_bid_no_bid(
    bid_id: uuid.UUID,
    payload: BidNoBidRecord,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> BidNoBidOut:
    decision = readiness_service.record_bid_no_bid(
        session,
        ctx,
        bid_id=bid_id,
        result=BidNoBidResult(payload.result),
        rationale=payload.rationale,
    )
    return BidNoBidOut.model_validate(decision)


@router.get(
    "/bids/{bid_id}/bid-no-bid",
    response_model=BidNoBidOut,
    dependencies=[Depends(require(Perm.BID_READ))],
)
def get_bid_no_bid(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> BidNoBidOut:
    decision = readiness_service.current_decision(session, ctx, bid_id=bid_id)
    if decision is None:
        raise NotFound("No bid/no-bid decision has been recorded for this bid.")
    return BidNoBidOut.model_validate(decision)
