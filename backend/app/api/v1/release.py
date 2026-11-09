"""Proposal assembly, validation, release and outcome routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_context, get_db, require
from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.core.permissions import Perm
from app.models import (
    GeneratedDocument,
    GeneratedDocumentVersion,
    KnowledgeUpdateCandidate,
    Outcome,
)
from app.models.enums import (
    CandidateState,
    ExceptionState,
    InclusionMode,
    OutcomeResult,
    OutputMode,
)
from app.schemas.release import (
    AmendmentImpactIn,
    AmendmentImpactOut,
    ArchiveIn,
    AssembleIn,
    CandidateOut,
    ConsolidateOut,
    DecisionIn,
    DeriveStructureIn,
    ExceptionOut,
    ExceptionRequestIn,
    ExtrasIn,
    ExtrasOut,
    FindingOut,
    GeneratedVersionOut,
    JoinReportIn,
    JoinReportOut,
    MapArtifactIn,
    NoteIn,
    OutcomeIn,
    OutcomeOut,
    PackageOut,
    SectionOut,
    StructureOut,
    ValidationRunOut,
)
from app.services import (
    amendments,
    clarification_register,
    report_consolidate,
    report_edit,
    report_extras,
)
from app.services import release as release_service
from app.services.storage import get_storage

router = APIRouter(tags=["release"])


def _enum(enum_cls, value: str, label: str):
    try:
        return enum_cls(value)
    except ValueError as exc:
        allowed = ", ".join(sorted(member.value for member in enum_cls))
        raise ValidationError(f"Unknown {label} '{value}'. Allowed: {allowed}.") from exc


def _structure_out(session: Session, ctx: ExecutionContext, structure) -> StructureOut:
    rows = release_service.sections(session, ctx, structure_id=structure.id)
    return StructureOut(
        **StructureOut.model_validate(structure).model_dump(exclude={"sections"}),
        sections=[SectionOut.model_validate(row) for row in rows],
    )


@router.post(
    "/bids/{bid_id}/amendment-impact",
    response_model=AmendmentImpactOut,
    dependencies=[Depends(require(Perm.REQUIREMENT_WRITE))],
)
def propagate_amendment(
    bid_id: uuid.UUID,
    payload: AmendmentImpactIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> AmendmentImpactOut:
    return AmendmentImpactOut.model_validate(
        amendments.propagate(
            session,
            ctx,
            bid_id=bid_id,
            amendment_document_version_id=payload.amendment_document_version_id,
            affected_requirement_ids=payload.affected_requirement_ids,
            reason=payload.reason,
        )
    )


@router.get(
    "/bids/{bid_id}/amendment-impact",
    response_model=list[AmendmentImpactOut],
    dependencies=[Depends(require(Perm.BID_READ))],
)
def amendment_impacts(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[AmendmentImpactOut]:
    return [
        AmendmentImpactOut.model_validate(row)
        for row in amendments.for_bid(session, ctx, bid_id=bid_id)
    ]


@router.get(
    "/bids/{bid_id}/structure",
    response_model=StructureOut,
    dependencies=[Depends(require(Perm.PACKAGE_READ))],
)
def get_structure(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> StructureOut:
    return _structure_out(
        session, ctx, release_service.structure_for_bid(session, ctx, bid_id=bid_id)
    )


@router.post(
    "/bids/{bid_id}/structure/derive",
    response_model=StructureOut,
    dependencies=[Depends(require(Perm.PACKAGE_ASSEMBLE))],
)
def derive_structure(
    bid_id: uuid.UUID,
    payload: DeriveStructureIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> StructureOut:
    row = release_service.derive_structure(
        session,
        ctx,
        bid_id=bid_id,
        source_document_version_id=payload.source_document_version_id,
    )
    return _structure_out(session, ctx, row)


@router.post(
    "/sections/{section_id}/artifacts",
    response_model=dict,
    dependencies=[Depends(require(Perm.PACKAGE_ASSEMBLE))],
)
def map_artifact(
    section_id: uuid.UUID,
    payload: MapArtifactIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    row = release_service.map_artifact(
        session,
        ctx,
        section_id=section_id,
        artifact_id=payload.artifact_id,
        order_index=payload.order_index,
        inclusion_mode=_enum(InclusionMode, payload.inclusion_mode, "inclusion mode"),
        excerpt_statement_ids=payload.excerpt_statement_ids or None,
    )
    return {
        "id": str(row.id),
        "section_id": str(row.section_id),
        "artifact_id": str(row.artifact_id),
    }


@router.get(
    "/bids/{bid_id}/structure/coverage",
    response_model=dict,
    dependencies=[Depends(require(Perm.PACKAGE_READ))],
)
def structure_coverage(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return release_service.coverage(session, ctx, bid_id=bid_id)


@router.post(
    "/bids/{bid_id}/assemble",
    response_model=list[GeneratedVersionOut],
    dependencies=[Depends(require(Perm.PACKAGE_ASSEMBLE))],
)
def assemble(
    bid_id: uuid.UUID,
    payload: AssembleIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[GeneratedVersionOut]:
    rows = release_service.assemble(
        session,
        ctx,
        bid_id=bid_id,
        output_mode=_enum(OutputMode, payload.output_mode, "output mode"),
        join=payload.join,
    )
    return [GeneratedVersionOut.model_validate(row) for row in rows]


@router.post(
    "/bids/{bid_id}/report/join",
    response_model=JoinReportOut,
    dependencies=[Depends(require(Perm.PACKAGE_ASSEMBLE))],
)
def join_report(
    bid_id: uuid.UUID,
    payload: JoinReportIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> JoinReportOut:
    """Rewrite approved answers as report prose, a few at a time, then the Executive Summary."""
    return JoinReportOut(
        **report_edit.edit_bid(
            session,
            ctx,
            bid_id=bid_id,
            limit=payload.limit,
            force=payload.force,
            retry_failed=payload.retry_failed,
        )
    )


@router.post(
    "/bids/{bid_id}/report/consolidate",
    response_model=ConsolidateOut,
    dependencies=[Depends(require(Perm.PACKAGE_ASSEMBLE))],
)
def consolidate_report(
    bid_id: uuid.UUID,
    payload: JoinReportIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ConsolidateOut:
    """Write each technical chapter once within a word budget, then the assumptions register."""
    return ConsolidateOut(
        **report_consolidate.consolidate_bid(
            session,
            ctx,
            bid_id=bid_id,
            limit=payload.limit,
            retry_failed=payload.retry_failed,
        )
    )


@router.post(
    "/bids/{bid_id}/report/extras",
    response_model=ExtrasOut,
    dependencies=[Depends(require(Perm.PACKAGE_ASSEMBLE))],
)
def draft_report_extras(
    bid_id: uuid.UUID,
    payload: ExtrasIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ExtrasOut:
    """Draft the financial proposal and the evaluation matrix from the approved answers."""
    return ExtrasOut(**report_extras.draft_extras(session, ctx, bid_id=bid_id, force=payload.force))


@router.get(
    "/bids/{bid_id}/generated-documents",
    response_model=list[dict],
    dependencies=[Depends(require(Perm.PACKAGE_READ))],
)
def generated_documents(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    rows = session.execute(
        select(GeneratedDocument).where(
            GeneratedDocument.org_id == ctx.org_id,
            GeneratedDocument.bid_id == bid_id,
        )
    ).scalars()
    return [
        {
            "id": str(row.id),
            "name": row.name,
            "filename": row.required_filename,
            "format": row.format.value,
            "stream": row.stream.value,
            "current_version_id": str(row.current_version_id) if row.current_version_id else None,
        }
        for row in rows
    ]


@router.get(
    "/generated-documents/{document_id}/versions/{version_no}/download",
    response_model=dict,
    dependencies=[Depends(require(Perm.PACKAGE_READ))],
)
def download_generated(
    document_id: uuid.UUID,
    version_no: int,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    row = session.execute(
        select(GeneratedDocumentVersion).where(
            GeneratedDocumentVersion.org_id == ctx.org_id,
            GeneratedDocumentVersion.generated_document_id == document_id,
            GeneratedDocumentVersion.version_no == version_no,
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("That generated document version does not exist.")
    return {"url": get_storage().presigned_url(row.storage_key), "sha256": row.sha256}


@router.post(
    "/bids/{bid_id}/packages",
    response_model=PackageOut,
    dependencies=[Depends(require(Perm.PACKAGE_ASSEMBLE))],
)
def create_package(
    bid_id: uuid.UUID,
    payload: AssembleIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> PackageOut:
    row = release_service.create_package(
        session,
        ctx,
        bid_id=bid_id,
        output_mode=_enum(OutputMode, payload.output_mode, "output mode"),
    )
    return PackageOut.model_validate(row)


@router.get(
    "/bids/{bid_id}/packages",
    response_model=list[PackageOut],
    dependencies=[Depends(require(Perm.PACKAGE_READ))],
)
def list_packages(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[PackageOut]:
    return [
        PackageOut.model_validate(row)
        for row in release_service.packages(session, ctx, bid_id=bid_id)
    ]


@router.post(
    "/packages/{package_id}/validate",
    response_model=ValidationRunOut,
    dependencies=[Depends(require(Perm.PACKAGE_VALIDATE))],
)
def validate_package(
    package_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ValidationRunOut:
    return ValidationRunOut.model_validate(
        release_service.validate_package(session, ctx, package_id=package_id)
    )


@router.get(
    "/packages/{package_id}/validation",
    response_model=dict,
    dependencies=[Depends(require(Perm.PACKAGE_READ))],
)
def validation(
    package_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    rows = release_service.validation_findings(session, ctx, package_id=package_id)
    return {
        "non_waivable": [
            FindingOut.model_validate(row).model_dump(mode="json")
            for row in rows
            if row.waivability.value == "NON_WAIVABLE"
        ],
        "waivable": [
            FindingOut.model_validate(row).model_dump(mode="json")
            for row in rows
            if row.waivability.value == "WAIVABLE"
        ],
    }


@router.get(
    "/bids/{bid_id}/clarification-register",
    response_model=dict,
    dependencies=[Depends(require(Perm.PACKAGE_READ))],
)
def clarification_register_route(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return clarification_register.export(session, ctx, bid_id)


@router.post(
    "/findings/{finding_id}/exception",
    response_model=ExceptionOut,
    dependencies=[Depends(require(Perm.EXCEPTION_REQUEST))],
)
def request_exception(
    finding_id: uuid.UUID,
    payload: ExceptionRequestIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ExceptionOut:
    return ExceptionOut.model_validate(
        release_service.request_exception(
            session, ctx, finding_id=finding_id, justification=payload.justification
        )
    )


@router.post(
    "/exceptions/{exception_id}/decide",
    response_model=ExceptionOut,
    dependencies=[Depends(require(Perm.EXCEPTION_APPROVE))],
)
def decide_exception(
    exception_id: uuid.UUID,
    payload: DecisionIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ExceptionOut:
    return ExceptionOut.model_validate(
        release_service.decide_exception(
            session,
            ctx,
            exception_id=exception_id,
            decision=_enum(ExceptionState, payload.decision, "exception decision"),
            comment=payload.comment,
        )
    )


@router.post(
    "/packages/{package_id}/submit-for-release",
    response_model=PackageOut,
    dependencies=[Depends(require(Perm.RELEASE_SUBMIT))],
)
def submit_release(
    package_id: uuid.UUID,
    payload: NoteIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> PackageOut:
    return PackageOut.model_validate(
        release_service.submit_for_release(session, ctx, package_id=package_id, note=payload.note)
    )


@router.post(
    "/packages/{package_id}/approve-release",
    response_model=PackageOut,
    dependencies=[Depends(require(Perm.RELEASE_APPROVE))],
)
def approve_release(
    package_id: uuid.UUID,
    payload: NoteIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> PackageOut:
    return PackageOut.model_validate(
        release_service.approve_release(session, ctx, package_id=package_id, comment=payload.note)
    )


@router.post(
    "/packages/{package_id}/archive",
    response_model=PackageOut,
    dependencies=[Depends(require(Perm.RELEASE_APPROVE))],
)
def archive_package(
    package_id: uuid.UUID,
    payload: ArchiveIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> PackageOut:
    return PackageOut.model_validate(
        release_service.archive(session, ctx, package_id=package_id, reason=payload.reason)
    )


@router.post(
    "/bids/{bid_id}/outcome",
    response_model=OutcomeOut,
    dependencies=[Depends(require(Perm.BID_WRITE))],
)
def record_outcome(
    bid_id: uuid.UUID,
    payload: OutcomeIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> OutcomeOut:
    return OutcomeOut.model_validate(
        release_service.record_outcome(
            session,
            ctx,
            bid_id=bid_id,
            result=_enum(OutcomeResult, payload.result, "outcome result"),
            technical_score=payload.technical_score,
            commercial_score=payload.commercial_score,
            award_value=payload.award_value,
            competitor=payload.competitor,
            loss_reason=payload.loss_reason,
            evaluator_feedback=payload.evaluator_feedback,
        )
    )


@router.get(
    "/bids/{bid_id}/outcome",
    response_model=OutcomeOut,
    dependencies=[Depends(require(Perm.BID_READ))],
)
def get_outcome(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> OutcomeOut:
    row = session.execute(
        select(Outcome).where(Outcome.org_id == ctx.org_id, Outcome.bid_id == bid_id)
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("That bid has no recorded outcome.")
    return OutcomeOut.model_validate(row)


@router.get(
    "/knowledge/update-candidates",
    response_model=list[CandidateOut],
    dependencies=[Depends(require(Perm.KNOWLEDGE_APPROVE))],
)
def candidates(
    state: str | None = Query(default=None),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[CandidateOut]:
    stmt = select(KnowledgeUpdateCandidate).where(KnowledgeUpdateCandidate.org_id == ctx.org_id)
    if state:
        stmt = stmt.where(
            KnowledgeUpdateCandidate.state == _enum(CandidateState, state, "candidate state")
        )
    return [CandidateOut.model_validate(row) for row in session.execute(stmt).scalars()]


@router.post(
    "/knowledge/update-candidates/{candidate_id}/decide",
    response_model=CandidateOut,
    dependencies=[Depends(require(Perm.KNOWLEDGE_APPROVE))],
)
def decide_candidate(
    candidate_id: uuid.UUID,
    payload: DecisionIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> CandidateOut:
    return CandidateOut.model_validate(
        release_service.decide_candidate(
            session,
            ctx,
            candidate_id=candidate_id,
            decision=_enum(CandidateState, payload.decision, "candidate decision"),
            comment=payload.comment,
        )
    )
