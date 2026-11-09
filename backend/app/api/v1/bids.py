"""Opportunity, bid and tender-intelligence routes.

Tender documents are uploaded here rather than through the knowledge router,
because a tender document must belong to a bid and a company document must not.
Keeping the two upload paths separate makes that boundary visible in the API
surface instead of hiding it behind a `side` parameter a caller could get wrong.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Body, Depends, File, Form, Query, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_context, get_db, require, require_any
from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.core.permissions import Perm
from app.db.post_commit import run_after_commit
from app.models import (
    Bid,
    Deliverable,
    Document,
    DocumentVersion,
    Opportunity,
    Requirement,
    RequirementVersion,
)
from app.models.enums import (
    BidState,
    ClarificationSeverity,
    ClarificationStatus,
    ConfidentialityClass,
    DeliverableStatus,
    DocumentSide,
    RequirementApplicability,
    RequirementCategory,
)
from app.schemas.knowledge import DocumentVersionOut
from app.schemas.tender import (
    AnalysisOut,
    ApplicabilityChange,
    BidOut,
    BidStateChange,
    ClarificationAssign,
    ClarificationMove,
    ClarificationOut,
    ClarificationStatusChange,
    ClauseResolutionOut,
    ConditionVariables,
    DeliverableOut,
    OpportunityCorrection,
    OpportunityCreate,
    OpportunityOut,
    PrecedenceOverride,
    RequirementDetail,
    RequirementOut,
    RequirementVersionOut,
    ScopeConfidenceOut,
)
from app.services import (
    audit,
    ingestion,
    precedence,
    requirements,
    tender_analysis,
)
from app.services import (
    bids as bid_service,
)
from app.services import (
    clarifications as clarification_service,
)

router = APIRouter(tags=["bids"])


# --- opportunities ----------------------------------------------------------


@router.post(
    "/opportunities",
    response_model=OpportunityOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Perm.OPPORTUNITY_WRITE))],
)
def create_opportunity(
    payload: OpportunityCreate,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> OpportunityOut:
    opportunity = bid_service.create_opportunity(
        session,
        ctx,
        customer_name=payload.customer_name,
        tender_name=payload.tender_name,
        tender_reference=payload.tender_reference,
        bid_manager_user_id=payload.bid_manager_user_id,
        account_manager_user_id=payload.account_manager_user_id,
        deadline=payload.deadline,
        currency=payload.currency,
        region=payload.region,
        estimated_value=payload.estimated_value,
        source=payload.source,
    )
    return OpportunityOut.model_validate(opportunity)


@router.get(
    "/opportunities",
    response_model=list[OpportunityOut],
    dependencies=[Depends(require(Perm.OPPORTUNITY_READ))],
)
def list_opportunities(
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[OpportunityOut]:
    rows = (
        session.execute(select(Opportunity).where(Opportunity.org_id == ctx.org_id)).scalars().all()
    )
    return [OpportunityOut.model_validate(o) for o in rows]


@router.patch(
    "/opportunities/{opportunity_id}",
    response_model=OpportunityOut,
    dependencies=[Depends(require(Perm.OPPORTUNITY_WRITE))],
)
def correct_opportunity(
    opportunity_id: uuid.UUID,
    payload: OpportunityCorrection,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> OpportunityOut:
    opportunity = bid_service.correct_opportunity(
        session,
        ctx,
        opportunity_id=opportunity_id,
        changes=payload.model_dump(exclude_unset=True),
    )
    return OpportunityOut.model_validate(opportunity)


@router.post(
    "/opportunities/{opportunity_id}/promote",
    response_model=BidOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Perm.BID_WRITE))],
)
def promote_opportunity(
    opportunity_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> BidOut:
    bid = bid_service.promote_to_bid(session, ctx, opportunity_id=opportunity_id)
    return BidOut.model_validate(bid)


# --- bids -------------------------------------------------------------------


@router.get("/bids", response_model=list[BidOut], dependencies=[Depends(require(Perm.BID_READ))])
def list_bids(
    state: str | None = None,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[BidOut]:
    stmt = select(Bid).where(Bid.org_id == ctx.org_id)
    if state:
        stmt = stmt.where(Bid.state == BidState(state))
    return [BidOut.model_validate(b) for b in session.execute(stmt).scalars().all()]


@router.get("/bids/{bid_id}", response_model=BidOut, dependencies=[Depends(require(Perm.BID_READ))])
def get_bid(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> BidOut:
    return BidOut.model_validate(bid_service.get_bid(session, ctx, bid_id=bid_id))


@router.post(
    "/bids/{bid_id}/state",
    response_model=BidOut,
    dependencies=[Depends(require(Perm.BID_WRITE))],
)
def change_bid_state(
    bid_id: uuid.UUID,
    payload: BidStateChange,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> BidOut:
    bid = bid_service.transition(
        session, ctx, bid_id=bid_id, to_state=BidState(payload.to_state), reason=payload.reason
    )
    return BidOut.model_validate(bid)


# --- tender documents -------------------------------------------------------


@router.post(
    "/bids/{bid_id}/documents",
    response_model=DocumentVersionOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Perm.DOCUMENT_UPLOAD))],
)
async def upload_tender_document(
    bid_id: uuid.UUID,
    file: UploadFile = File(...),
    confidentiality_class: str = Form("CONFIDENTIAL"),
    title: str | None = Form(None),
    language: str = Form("en"),
    document_id: uuid.UUID | None = Form(None),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> DocumentVersionOut:
    """Upload one tender-package file.

    Validation and scanning are synchronous, so an unsafe file never reaches a
    parser and the caller gets an immediate answer. Parsing, chunking and
    embedding are enqueued.
    """
    bid_service.ensure_intake_open(session, ctx, bid_id=bid_id)
    data = await file.read()
    version = ingestion.register_upload(
        session,
        ctx,
        data=data,
        filename=file.filename or "upload",
        side=DocumentSide.TENDER,
        bid_id=bid_id,
        confidentiality_class=ConfidentialityClass(confidentiality_class),
        title=title,
        language=language,
        declared_mime=file.content_type,
        document_id=document_id,
    )
    # Enqueued on commit, not as a BackgroundTask: Starlette runs background
    # tasks inside the response, before `get_db` resumes to commit, so the
    # worker would look for a row that is not visible yet. See db/post_commit.
    version_id, org_id, actor_user_id = version.id, ctx.org_id, ctx.actor_user_id
    run_after_commit(
        session,
        lambda: _enqueue_document_index(
            version_id=version_id, org_id=org_id, actor_user_id=actor_user_id
        ),
        label="parse_and_index_document",
    )
    return DocumentVersionOut.model_validate(version)


def _enqueue_document_index(
    *, version_id: uuid.UUID, org_id: uuid.UUID, actor_user_id: uuid.UUID | None
) -> None:
    from app.workers.tasks import parse_and_index_document

    parse_and_index_document.apply_async(
        kwargs={
            "version_id": str(version_id),
            "org_id": str(org_id),
            "actor_user_id": str(actor_user_id) if actor_user_id else None,
        }
    )


@router.post(
    "/bids/{bid_id}/documents/{version_id}/reparse",
    response_model=DocumentVersionOut,
    dependencies=[Depends(require(Perm.DOCUMENT_UPLOAD))],
)
def reparse_tender_document(
    bid_id: uuid.UUID,
    version_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> DocumentVersionOut:
    """Parse an already-uploaded version again with the current parser.

    Needed whenever the parser improves: every document already in the system
    still holds the output of the version that parsed it, and re-uploading the
    same file to get the benefit leaves a duplicate behind.

    Synchronous rather than queued, because the caller is a person who just
    pressed a button and wants to know whether it worked.
    """
    version = ingestion.parse_and_index(session, ctx, version_id=version_id, force=True)
    return DocumentVersionOut.model_validate(version)


@router.get(
    "/bids/{bid_id}/documents",
    response_model=list[DocumentVersionOut],
    dependencies=[Depends(require(Perm.DOCUMENT_READ))],
)
def list_tender_documents(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[DocumentVersionOut]:
    rows = (
        session.execute(
            select(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                DocumentVersion.org_id == ctx.org_id,
                Document.bid_id == bid_id,
                Document.side == DocumentSide.TENDER,
            )
            .order_by(DocumentVersion.created_at)
        )
        .scalars()
        .all()
    )
    return [DocumentVersionOut.model_validate(v) for v in rows]


# --- analysis ---------------------------------------------------------------


@router.post(
    "/bids/{bid_id}/analyze",
    response_model=AnalysisOut,
    dependencies=[Depends(require(Perm.BID_WRITE))],
)
def analyze_bid(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> AnalysisOut:
    """Run the full tender-understanding pass.

    Also exposed as a Celery task for large packages; the synchronous route
    exists because it is the one a test or a small tender can use directly.
    """
    result = tender_analysis.analyze_bid(session, ctx, bid_id=bid_id)
    return AnalysisOut(**result.as_dict())


@router.get("/bids/{bid_id}/tender-facts", dependencies=[Depends(require(Perm.BID_READ))])
def get_tender_facts(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return bid_service.get_bid(session, ctx, bid_id=bid_id).tender_facts or {}


# --- precedence -------------------------------------------------------------


@router.post(
    "/bids/{bid_id}/precedence/resolve",
    response_model=dict,
    dependencies=[Depends(require(Perm.BID_WRITE))],
)
def resolve_precedence(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return precedence.resolve_bid_precedence(session, ctx, bid_id=bid_id).as_dict()


@router.get(
    "/bids/{bid_id}/precedence",
    response_model=list[ClauseResolutionOut],
    dependencies=[Depends(require(Perm.BID_READ))],
)
def list_precedence(
    bid_id: uuid.UUID,
    unresolved_only: bool = False,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[ClauseResolutionOut]:
    rows = precedence.current_resolutions(session, ctx, bid_id=bid_id)
    if unresolved_only:
        rows = [r for r in rows if r.winning_document_version_id is None]
    return [ClauseResolutionOut.model_validate(r) for r in rows]


@router.post(
    "/bids/{bid_id}/precedence/{clause_ref}/override",
    response_model=ClauseResolutionOut,
    dependencies=[Depends(require(Perm.BID_WRITE))],
)
def override_precedence(
    bid_id: uuid.UUID,
    clause_ref: str,
    payload: PrecedenceOverride,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ClauseResolutionOut:
    resolution = precedence.override(
        session,
        ctx,
        bid_id=bid_id,
        clause_ref=clause_ref,
        winning_document_version_id=payload.winning_document_version_id,
        reason=payload.reason,
    )
    return ClauseResolutionOut.model_validate(resolution)


# --- requirements -----------------------------------------------------------


@router.get(
    "/bids/{bid_id}/requirements",
    response_model=list[RequirementOut],
    dependencies=[Depends(require(Perm.REQUIREMENT_READ))],
)
def list_requirements(
    bid_id: uuid.UUID,
    category: str | None = None,
    mandatory: bool | None = None,
    applicability: str | None = None,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[RequirementOut]:
    rows = requirements.list_requirements(
        session,
        ctx,
        bid_id=bid_id,
        category=RequirementCategory(category) if category else None,
        mandatory=mandatory,
        applicability=RequirementApplicability(applicability) if applicability else None,
    )
    return [RequirementOut.model_validate(r) for r in rows]


@router.get(
    "/requirements/{requirement_id}",
    response_model=RequirementDetail,
    dependencies=[Depends(require(Perm.REQUIREMENT_READ))],
)
def get_requirement(
    requirement_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> RequirementDetail:
    requirement = session.get(Requirement, requirement_id)
    if requirement is None or requirement.org_id != ctx.org_id:
        raise NotFound("Requirement not found.")
    versions = (
        session.execute(
            select(RequirementVersion)
            .where(
                RequirementVersion.org_id == ctx.org_id,
                RequirementVersion.requirement_id == requirement_id,
            )
            .order_by(RequirementVersion.version_no)
        )
        .scalars()
        .all()
    )
    current = next((v for v in versions if v.id == requirement.current_version_id), None)
    return RequirementDetail(
        requirement=RequirementOut.model_validate(requirement),
        current_version=RequirementVersionOut.model_validate(current) if current else None,
        versions=[RequirementVersionOut.model_validate(v) for v in versions],
    )


@router.post(
    "/bids/{bid_id}/requirements/confirm",
    response_model=dict,
    dependencies=[Depends(require(Perm.REQUIREMENT_WRITE))],
)
def confirm_requirements(
    bid_id: uuid.UUID,
    requirement_ids: list[uuid.UUID] | None = Body(default=None, embed=True),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Accept that these are what the customer asked for.

    `requirement_ids` confirms only the rows a reviewer has read. Omitting it
    confirms the whole bid, which is the old behaviour and is a rubber stamp
    whenever the list is long -- kept for the case where somebody means it.
    """
    return {
        "confirmed": requirements.confirm_requirements(
            session, ctx, bid_id=bid_id, requirement_ids=requirement_ids
        )
    }


@router.post(
    "/requirements/{requirement_id}/applicability",
    response_model=RequirementOut,
    dependencies=[Depends(require(Perm.REQUIREMENT_WRITE))],
)
def set_applicability(
    requirement_id: uuid.UUID,
    payload: ApplicabilityChange,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> RequirementOut:
    requirement = requirements.set_applicability(
        session,
        ctx,
        requirement_id=requirement_id,
        applicability=RequirementApplicability(payload.applicability),
        reason=payload.reason,
    )
    return RequirementOut.model_validate(requirement)


@router.post(
    "/bids/{bid_id}/requirements/evaluate-conditions",
    response_model=dict,
    dependencies=[Depends(require(Perm.REQUIREMENT_WRITE))],
)
def evaluate_conditions(
    bid_id: uuid.UUID,
    payload: ConditionVariables,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return requirements.evaluate_conditions(
        session, ctx, bid_id=bid_id, variables=payload.variables
    )


# --- clarifications ---------------------------------------------------------


@router.get(
    "/bids/{bid_id}/clarifications",
    response_model=list[ClarificationOut],
    dependencies=[Depends(require(Perm.BID_READ))],
)
def list_clarifications(
    bid_id: uuid.UUID,
    severity: str | None = None,
    clarification_status: str | None = Query(default=None, alias="status"),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[ClarificationOut]:
    rows = clarification_service.list_issues(
        session,
        ctx,
        bid_id=bid_id,
        severity=ClarificationSeverity(severity) if severity else None,
        status=ClarificationStatus(clarification_status) if clarification_status else None,
    )
    return clarification_service.describe(session, ctx, rows)


@router.post(
    "/clarifications/{issue_id}/status",
    response_model=ClarificationOut,
    dependencies=[Depends(require(Perm.BID_WRITE))],
)
def update_clarification(
    issue_id: uuid.UUID,
    payload: ClarificationStatusChange,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ClarificationOut:
    issue = clarification_service.update_status(
        session,
        ctx,
        issue_id=issue_id,
        status=ClarificationStatus(payload.status),
        client_response=payload.client_response,
        reason=payload.reason,
    )
    return clarification_service.describe(session, ctx, [issue])[0]


@router.post(
    "/bids/{bid_id}/clarifications/route",
    response_model=dict,
    dependencies=[Depends(require(Perm.TASK_ASSIGN))],
)
def route_clarifications(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Have the AI choose the department of every open clarification that has none
    from a requirement. A person's own choice is left alone."""
    from app.gateway.gateway import build_gateway

    return clarification_service.route_open_issues(
        session, ctx, bid_id=bid_id, gateway=build_gateway()
    )


@router.post(
    "/bids/{bid_id}/clarifications/compare-routing",
    response_model=dict,
    dependencies=[Depends(require(Perm.BID_READ))],
)
def compare_clarification_routing(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Fixed rules versus the AI for every open clarification. Changes nothing."""
    from app.gateway.gateway import build_gateway

    return clarification_service.compare_routing(
        session, ctx, bid_id=bid_id, gateway=build_gateway()
    )


@router.get(
    "/clarifications/{issue_id}/assignable",
    response_model=dict,
    dependencies=[Depends(require(Perm.BID_READ))],
)
def clarification_assignable(
    issue_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Who the caller may make the owner of this clarification."""
    return clarification_service.assignable(session, ctx, issue_id=issue_id)


@router.post(
    "/clarifications/{issue_id}/owner",
    response_model=ClarificationOut,
    # Same two doors as task assignment; the service decides which one applies.
    dependencies=[Depends(require_any(Perm.TASK_ASSIGN, Perm.TASK_ASSIGN_IN_DEPARTMENT))],
)
def assign_clarification(
    issue_id: uuid.UUID,
    payload: ClarificationAssign,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ClarificationOut:
    issue = clarification_service.assign_owner(
        session, ctx, issue_id=issue_id, owner_user_id=payload.owner_user_id
    )
    return clarification_service.describe(session, ctx, [issue])[0]


@router.post(
    "/clarifications/{issue_id}/department",
    response_model=ClarificationOut,
    dependencies=[Depends(require(Perm.TASK_ASSIGN))],
)
def move_clarification(
    issue_id: uuid.UUID,
    payload: ClarificationMove,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ClarificationOut:
    """Re-route a misfiled clarification (bid manager); the new head becomes owner."""
    issue = clarification_service.move_to_department(
        session, ctx, issue_id=issue_id, department_id=payload.department_id
    )
    return clarification_service.describe(session, ctx, [issue])[0]


# --- deliverables and scope confidence --------------------------------------


@router.get(
    "/bids/{bid_id}/deliverables",
    response_model=list[DeliverableOut],
    dependencies=[Depends(require(Perm.BID_READ))],
)
def list_deliverables(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[DeliverableOut]:
    rows = (
        session.execute(
            select(Deliverable)
            .where(Deliverable.org_id == ctx.org_id, Deliverable.bid_id == bid_id)
            .order_by(Deliverable.name)
        )
        .scalars()
        .all()
    )
    return [DeliverableOut.model_validate(d) for d in rows]


class DeliverableStatusChange(BaseModel):
    status: str
    note: str


@router.post(
    "/deliverables/{deliverable_id}/status",
    response_model=DeliverableOut,
    dependencies=[Depends(require(Perm.PACKAGE_ASSEMBLE))],
)
def set_deliverable_status(
    deliverable_id: uuid.UUID,
    payload: DeliverableStatusChange,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> DeliverableOut:
    """A person says where a required submission stands, and why.

    Nothing marks one ready by itself: a signed copy, a CV pack or a certificate
    is ready when someone has the real thing in hand. The note is the record.
    """
    row = session.get(Deliverable, deliverable_id)
    if row is None or row.org_id != ctx.org_id:
        raise NotFound("That deliverable does not exist.")
    try:
        new = DeliverableStatus(payload.status)
    except ValueError:
        raise ValidationError(
            "The status is IDENTIFIED, IN_PROGRESS, READY, SUBMITTED or NOT_APPLICABLE."
        ) from None
    if len(payload.note.strip()) < 3:
        raise ValidationError("Say in a few words why, so the record shows the decision.")
    before = row.status
    row.status = new
    session.flush()
    audit.record(
        session,
        ctx,
        action="deliverable.status_changed",
        entity_type="deliverable",
        entity_id=row.id,
        bid_id=row.bid_id,
        before={"status": before.value},
        after={"status": new.value},
        reason=payload.note.strip(),
    )
    return DeliverableOut.model_validate(row)


@router.get(
    "/bids/{bid_id}/scope-confidence",
    response_model=ScopeConfidenceOut,
    dependencies=[Depends(require(Perm.BID_READ))],
)
def get_scope_confidence(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ScopeConfidenceOut:
    record = tender_analysis.get_scope_confidence(session, ctx, bid_id=bid_id)
    if record is None:
        raise NotFound("Scope confidence has not been computed for this bid yet.")
    return ScopeConfidenceOut.model_validate(record)
