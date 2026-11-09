"""Company knowledge routes."""

from __future__ import annotations

import functools
import uuid

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_context, get_db, require
from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.core.permissions import Perm
from app.db.post_commit import run_after_commit
from app.ingestion import archives, folder_types
from app.models import (
    Document,
    DocumentBlock,
    DocumentClassification,
    DocumentVersion,
    Evidence,
    KnowledgeEntity,
    KnowledgeFact,
    KnowledgeRelation,
)
from app.models.enums import (
    ConfidentialityClass,
    DocumentEvidenceStatus,
    DocumentSide,
    DocumentType,
    EntityType,
    RelationPredicate,
    ReviewQueueKind,
    UseScope,
)
from app.schemas.knowledge import (
    ApproveBatchRequest,
    BlockOut,
    ClassifyDocumentRequest,
    DocumentOut,
    DocumentVersionOut,
    EntityCreate,
    EntityOut,
    EvidenceDecisionRequest,
    EvidenceOut,
    FactCreate,
    FactOut,
    RelationCreate,
    RelationOut,
    ResolveReviewRequest,
    ReviewItemOut,
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from app.schemas.source_locator import SourceLocator
from app.services import audit, evidence_approval, ingestion, knowledge, person_links
from app.services.retrieval import RetrievalScope, RetrievalService

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


# --- documents --------------------------------------------------------------


# Tender-side labels describe what a customer demanded. Accepting one here would
# file a customer's document as company knowledge, where it could be mined as
# proof of this company's capability — so the company upload refuses them by
# name rather than silently storing a wrong label.
TENDER_ONLY_TYPES = frozenset(
    {
        DocumentType.RFP,
        DocumentType.RFQ,
        DocumentType.AMENDMENT,
        DocumentType.CLARIFICATION,
        DocumentType.TECHNICAL_SPECIFICATION,
        DocumentType.BOQ,
        DocumentType.COMMERCIAL_TEMPLATE,
        DocumentType.RESPONSE_TEMPLATE,
        DocumentType.TERMS_AND_CONDITIONS,
        DocumentType.APPENDIX,
        DocumentType.FORM,
        DocumentType.QUESTIONNAIRE,
    }
)


def _company_document_type(value: str | None) -> DocumentType | None:
    """The uploader's filing decision, validated.

    `PREVIOUS_TENDER` is deliberately allowed: a past customer's RFP is company
    material once it is filed as history, and keeping it typed as such is what
    stops it being read as this company's own offer.
    """
    if not value:
        return None
    try:
        document_type = DocumentType(value)
    except ValueError:
        raise ValidationError(f"'{value}' is not a document type.") from None
    if document_type in TENDER_ONLY_TYPES:
        raise ValidationError(
            f"'{value}' describes a tender document. Upload it against a bid, or file "
            "a historical copy as PREVIOUS_TENDER."
        )
    return document_type


@router.post(
    "/documents",
    response_model=DocumentVersionOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Perm.DOCUMENT_UPLOAD))],
)
async def upload_document(
    file: UploadFile = File(...),
    confidentiality_class: str = Form("INTERNAL"),
    title: str | None = Form(None),
    language: str = Form("en"),
    document_id: uuid.UUID | None = Form(None),
    document_type: str | None = Form(None),
    responds_to_document_id: uuid.UUID | None = Form(None),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> DocumentVersionOut:
    """Validate, scan and store a company document.

    Synchronous on purpose: an unsafe file must never reach a parser, and the
    caller deserves an immediate answer rather than a job id. Parsing and
    embedding are then enqueued.
    """
    data = await file.read()
    version = ingestion.register_upload(
        session,
        ctx,
        data=data,
        filename=file.filename or "upload",
        side=DocumentSide.COMPANY,
        confidentiality_class=ConfidentialityClass(confidentiality_class),
        title=title,
        language=language,
        declared_mime=file.content_type,
        document_id=document_id,
        document_type=_company_document_type(document_type),
        responds_to_document_id=responds_to_document_id,
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
    *,
    version_id: uuid.UUID,
    org_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
) -> None:
    """Dispatch the durable parse/index/extract pipeline after upload commit."""
    from app.workers.tasks import parse_and_index_document

    parse_and_index_document.apply_async(
        kwargs={
            "version_id": str(version_id),
            "org_id": str(org_id),
            "actor_user_id": str(actor_user_id) if actor_user_id else None,
        }
    )


@router.post(
    "/documents/{version_id}/index",
    response_model=DocumentVersionOut,
    dependencies=[Depends(require(Perm.DOCUMENT_UPLOAD))],
)
def index_document(
    version_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> DocumentVersionOut:
    version = ingestion.parse_and_index(session, ctx, version_id=version_id)
    return DocumentVersionOut.model_validate(version)


@router.post(
    "/documents/bulk",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Perm.DOCUMENT_UPLOAD))],
)
async def upload_document_archive(
    file: UploadFile = File(...),
    confidentiality_class: str = Form("INTERNAL"),
    language: str = Form("en"),
    default_document_type: str | None = Form(None),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Ingest a ZIP of company documents, filed by its folder names.

    The archive's structure is the filing decision: `CVs/`, `Policies/`,
    `Previous Tenders/`. A folder that matches nothing leaves its documents
    unclassified rather than guessed at, and they appear on the filed list for a
    person to correct — a wrong kind is worse than a missing one, because the
    wrong one is invisible.

    One bad file does not sink the archive. Each entry is validated and scanned
    exactly as a single upload would be, and a rejection is reported against
    that filename while the rest proceed: an archive of eighty documents with
    two macro-enabled files should file seventy-eight, not fail.
    """
    raw = await file.read()
    filename = file.filename or "archive.zip"
    if not archives.looks_like_zip(raw, filename):
        raise ValidationError("Upload a .zip archive, or use the single-document upload.")

    fallback = _company_document_type(default_document_type)
    members = archives.extract_members(raw)

    filed: list[dict] = []
    skipped: list[dict] = []
    for member in members:
        document_type = folder_types.document_type_for_folder(member.folder) or fallback
        try:
            version = ingestion.register_upload(
                session,
                ctx,
                data=member.data,
                filename=member.filename,
                side=DocumentSide.COMPANY,
                confidentiality_class=ConfidentialityClass(confidentiality_class),
                title=member.filename,
                language=language,
                document_type=document_type,
            )
        except ValidationError as exc:
            skipped.append(
                {"file": f"{member.folder}/{member.filename}".lstrip("/"), "reason": str(exc)}
            )
            continue
        version_id, org_id, actor_user_id = version.id, ctx.org_id, ctx.actor_user_id
        run_after_commit(
            session,
            functools.partial(
                _enqueue_document_index,
                version_id=version_id,
                org_id=org_id,
                actor_user_id=actor_user_id,
            ),
            label="parse_and_index_document",
        )
        filed.append(
            {
                "file": f"{member.folder}/{member.filename}".lstrip("/"),
                "document_type": document_type.value if document_type else None,
            }
        )

    return {
        "filed": len(filed),
        "skipped": len(skipped),
        "unclassified": sum(1 for item in filed if item["document_type"] is None),
        "documents": filed,
        "rejected": skipped,
    }


@router.post(
    "/documents/{document_id}/classify",
    response_model=DocumentOut,
    dependencies=[Depends(require(Perm.DOCUMENT_UPLOAD))],
)
def classify_document(
    document_id: uuid.UUID,
    payload: ClassifyDocumentRequest,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> DocumentOut:
    """Set or correct the kind of a document that is already filed.

    A bulk upload whose folders matched nothing leaves its documents ingested
    but unclassified, and without this the only remedy is to upload every file
    again — which duplicates the corpus to fix a label. Correcting in place is
    also the honest path when a folder name led to the wrong kind.

    The classification is recorded as this user's decision, replacing whatever
    was there, because a human correction should outrank an earlier guess.
    """
    document = session.get(Document, document_id)
    if document is None or document.org_id != ctx.org_id:
        raise NotFound("Document not found.")
    if document.side is not DocumentSide.COMPANY:
        raise ValidationError("Only company documents are classified here.")
    if document.current_version_id is None:
        raise ValidationError("The document has no version to classify.")

    document_type = _company_document_type(payload.document_type)
    if document_type is None:
        raise ValidationError("A document type is required.")

    classification = session.execute(
        select(DocumentClassification).where(
            DocumentClassification.org_id == ctx.org_id,
            DocumentClassification.document_version_id == document.current_version_id,
        )
    ).scalar_one_or_none()
    before = classification.document_type.value if classification is not None else None
    if classification is None:
        classification = DocumentClassification(
            org_id=ctx.org_id,
            document_version_id=document.current_version_id,
            document_type=document_type,
            confidence=1.0,
            classified_by=f"human:{ctx.actor_user_id}",
        )
        session.add(classification)
    else:
        classification.document_type = document_type
        classification.confidence = 1.0
        classification.classified_by = f"human:{ctx.actor_user_id}"

    if payload.responds_to_document_id is not None:
        document.responds_to_document_id = payload.responds_to_document_id
    session.flush()
    audit.record(
        session,
        ctx,
        action="document.reclassified",
        entity_type="document",
        entity_id=document.id,
        before={"document_type": before},
        after={"document_type": document_type.value},
    )
    out = DocumentOut.model_validate(document)
    out.document_type = document_type.value
    return out


@router.post(
    "/documents/{document_id}/evidence",
    response_model=DocumentOut,
    dependencies=[Depends(require(Perm.EVIDENCE_APPROVE))],
)
def decide_document_evidence(
    document_id: uuid.UUID,
    payload: EvidenceDecisionRequest,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> DocumentOut:
    """Record a person's decision on a company document as bid evidence.

    A document that says it is a test fixture is refused whatever is asked.
    """
    try:
        status_value = DocumentEvidenceStatus(payload.status.upper())
        scope_value = UseScope(payload.use_scope.upper())
    except ValueError as exc:
        raise ValidationError("Unknown evidence status or use scope.") from exc
    document = evidence_approval.decide(
        session,
        ctx,
        document_id=document_id,
        status=status_value,
        use_scope=scope_value,
        provenance=payload.provenance,
        note=payload.note,
        reality=payload.reality,
        attestation_method=payload.attestation_method,
    )
    return DocumentOut.model_validate(document)


@router.get(
    "/documents",
    response_model=list[DocumentOut],
    dependencies=[Depends(require(Perm.DOCUMENT_READ))],
)
def list_documents(
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[DocumentOut]:
    # Outer-joined on the current version's classification: an unclassified
    # document still belongs in the list, it just has no kind yet.
    rows = session.execute(
        select(Document, DocumentClassification.document_type)
        .outerjoin(
            DocumentClassification,
            DocumentClassification.document_version_id == Document.current_version_id,
        )
        .where(
            Document.org_id == ctx.org_id,
            Document.side == DocumentSide.COMPANY,
            Document.withdrawn_at.is_(None),
        )
        .order_by(Document.created_at.desc())
    ).all()
    out: list[DocumentOut] = []
    for document, document_type in rows:
        item = DocumentOut.model_validate(document)
        item.document_type = document_type.value if document_type is not None else None
        out.append(item)
    return out


@router.get(
    "/documents/{document_id}/versions",
    response_model=list[DocumentVersionOut],
    dependencies=[Depends(require(Perm.DOCUMENT_READ))],
)
def list_versions(
    document_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[DocumentVersionOut]:
    rows = (
        session.execute(
            select(DocumentVersion)
            .where(
                DocumentVersion.org_id == ctx.org_id,
                DocumentVersion.document_id == document_id,
            )
            .order_by(DocumentVersion.version_no)
        )
        .scalars()
        .all()
    )
    if not rows:
        raise NotFound("Document not found.")
    return [DocumentVersionOut.model_validate(v) for v in rows]


@router.get(
    "/documents/versions/{version_id}/blocks",
    response_model=list[BlockOut],
    dependencies=[Depends(require(Perm.DOCUMENT_READ))],
)
def list_blocks(
    version_id: uuid.UUID,
    block_type: str | None = None,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[BlockOut]:
    stmt = (
        select(DocumentBlock)
        .where(
            DocumentBlock.org_id == ctx.org_id,
            DocumentBlock.document_version_id == version_id,
        )
        .order_by(DocumentBlock.ordinal)
    )
    if block_type:
        stmt = stmt.where(DocumentBlock.block_type == block_type)
    return [BlockOut.model_validate(b) for b in session.execute(stmt).scalars().all()]


# --- entities, facts, relations ---------------------------------------------


@router.post(
    "/entities",
    response_model=EntityOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Perm.KNOWLEDGE_WRITE))],
)
def create_entity(
    payload: EntityCreate,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> EntityOut:
    entity, _created = knowledge.resolve_or_create_entity(
        session,
        ctx,
        entity_type=EntityType(payload.entity_type),
        name=payload.canonical_name,
        classification=ConfidentialityClass(payload.classification),
        attributes=payload.attributes,
    )
    if payload.aliases:
        entity.aliases = sorted({*(entity.aliases or []), *payload.aliases})
    return EntityOut.model_validate(entity)


@router.get(
    "/entities",
    response_model=list[EntityOut],
    dependencies=[Depends(require(Perm.KNOWLEDGE_READ))],
)
def list_entities(
    entity_type: str | None = None,
    q: str | None = None,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[EntityOut]:
    stmt = select(KnowledgeEntity).where(KnowledgeEntity.org_id == ctx.org_id)
    if entity_type:
        stmt = stmt.where(KnowledgeEntity.entity_type == entity_type)
    if q:
        stmt = stmt.where(KnowledgeEntity.canonical_name.ilike(f"%{q}%"))
    return [EntityOut.model_validate(e) for e in session.execute(stmt).scalars().all()]


@router.get(
    "/entities/{entity_id}",
    dependencies=[Depends(require(Perm.KNOWLEDGE_READ))],
)
def get_entity(
    entity_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    entity = session.get(KnowledgeEntity, entity_id)
    if entity is None or entity.org_id != ctx.org_id:
        raise NotFound("Entity not found.")
    facts = (
        session.execute(
            select(KnowledgeFact).where(
                KnowledgeFact.org_id == ctx.org_id, KnowledgeFact.entity_id == entity_id
            )
        )
        .scalars()
        .all()
    )
    relations = (
        session.execute(
            select(KnowledgeRelation).where(
                KnowledgeRelation.org_id == ctx.org_id,
                KnowledgeRelation.subject_entity_id == entity_id,
            )
        )
        .scalars()
        .all()
    )
    return {
        "entity": EntityOut.model_validate(entity).model_dump(mode="json"),
        "facts": [FactOut.model_validate(f).model_dump(mode="json") for f in facts],
        "relations": [RelationOut.model_validate(r).model_dump(mode="json") for r in relations],
    }


@router.post(
    "/facts",
    response_model=FactOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Perm.KNOWLEDGE_WRITE))],
)
def create_fact(
    payload: FactCreate,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> FactOut:
    fact = knowledge.add_fact(
        session,
        ctx,
        entity_id=payload.entity_id,
        predicate=payload.predicate,
        value_text=payload.value_text,
        value_number=payload.value_number,
        value_date=payload.value_date,
        unit=payload.unit,
        valid_from=payload.valid_from,
        valid_to=payload.valid_to,
        evidence_ids=payload.evidence_ids,
        # A person who cannot approve knowledge cannot vouch for it by typing
        # it in; it waits in the review queue like an uploaded document.
        approved=ctx.has_permission(Perm.KNOWLEDGE_APPROVE),
    )
    return FactOut.model_validate(fact)


@router.post(
    "/relations",
    response_model=RelationOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Perm.KNOWLEDGE_WRITE))],
)
def create_relation(
    payload: RelationCreate,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> RelationOut:
    relation = knowledge.add_relation(
        session,
        ctx,
        subject_entity_id=payload.subject_entity_id,
        predicate=RelationPredicate(payload.predicate),
        object_entity_id=payload.object_entity_id,
        attributes=payload.attributes,
        valid_from=payload.valid_from,
        valid_to=payload.valid_to,
        evidence_ids=payload.evidence_ids,
    )
    return RelationOut.model_validate(relation)


# --- evidence ---------------------------------------------------------------


@router.get(
    "/evidence",
    response_model=list[EvidenceOut],
    dependencies=[Depends(require(Perm.EVIDENCE_READ))],
)
def list_evidence(
    expiring_within_days: int | None = Query(default=None, ge=1, le=730),
    q: str | None = Query(default=None, max_length=200),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[EvidenceOut]:
    if q and q.strip():
        # A search box for the person answering a request: approved items whose
        # text contains every word typed, newest documents first, capped.
        from app.models.enums import ApprovalStatus

        stmt = select(Evidence).where(
            Evidence.org_id == ctx.org_id,
            Evidence.approval_status == ApprovalStatus.APPROVED,
        )
        for word in q.split()[:6]:
            stmt = stmt.where(Evidence.excerpt.ilike(f"%{word}%"))
        rows = list(session.execute(stmt.limit(25)).scalars().all())
        return [EvidenceOut.model_validate(e) for e in rows]
    if expiring_within_days:
        rows = list(knowledge.find_expiring(session, ctx, within_days=expiring_within_days))
    else:
        rows = list(
            session.execute(select(Evidence).where(Evidence.org_id == ctx.org_id)).scalars().all()
        )
    return [EvidenceOut.model_validate(e) for e in rows]


@router.post(
    "/documents/classify-unclassified",
    dependencies=[Depends(require(Perm.KNOWLEDGE_WRITE))],
)
def classify_unclassified_documents(
    limit: int = Query(default=25, ge=1, le=200),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Type the company documents nobody filed, and restamp their excerpts.

    Bounded per call so a large corpus is worked through by calling it until
    `remaining` reaches zero, rather than by one request that has to survive
    the whole job.
    """
    from app.services import document_classification

    return document_classification.classify_unclassified(session, ctx, limit=limit)


@router.post(
    "/evidence/backfill-vectors",
    dependencies=[Depends(require(Perm.KNOWLEDGE_WRITE))],
)
def backfill_evidence_vectors(
    limit: int = Query(default=200, ge=1, le=2000),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Embed evidence that predates semantic matching.

    Returns how many were embedded and how many remain, so a large corpus can
    be worked through by calling this until `remaining` reaches zero rather
    than by one request that has to survive the whole job.
    """
    return knowledge.backfill_evidence_vectors(session, ctx, limit=limit)


@router.post(
    "/evidence/approve-batch",
    dependencies=[Depends(require(Perm.EVIDENCE_APPROVE))],
)
def approve_evidence_batch(
    payload: ApproveBatchRequest,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Approve a reviewed set of evidence in one decision.

    The reason is required and recorded against every item, so a batch is as
    accountable as a single approval — the audit trail shows who approved what
    and on what grounds, which is the property the gate exists to preserve.
    """
    return knowledge.approve_evidence_batch(
        session,
        ctx,
        evidence_ids=payload.evidence_ids,
        reason=payload.reason,
    )


@router.post(
    "/evidence/{evidence_id}/approve",
    response_model=EvidenceOut,
    dependencies=[Depends(require(Perm.EVIDENCE_APPROVE))],
)
def approve_evidence(
    evidence_id: uuid.UUID,
    reason: str = Query(min_length=3),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> EvidenceOut:
    evidence = knowledge.approve_evidence(session, ctx, evidence_id=evidence_id, reason=reason)
    return EvidenceOut.model_validate(evidence)


# --- review queue -----------------------------------------------------------


@router.get(
    "/review-queue",
    response_model=list[ReviewItemOut],
    dependencies=[Depends(require(Perm.KNOWLEDGE_APPROVE))],
)
def review_queue(
    kind: str | None = None,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[ReviewItemOut]:
    items = knowledge.open_reviews(session, ctx, kind=ReviewQueueKind(kind) if kind else None)
    return [ReviewItemOut.model_validate(i) for i in items]


@router.post(
    "/review-queue/{item_id}/resolve",
    response_model=ReviewItemOut,
    dependencies=[Depends(require(Perm.KNOWLEDGE_APPROVE))],
)
def resolve_review(
    item_id: uuid.UUID,
    payload: ResolveReviewRequest,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ReviewItemOut:
    item = knowledge.resolve_review(
        session,
        ctx,
        item_id=item_id,
        resolution=payload.resolution,
        merge_into_entity_id=payload.merge_into_entity_id,
    )
    return ReviewItemOut.model_validate(item)


# --- search -----------------------------------------------------------------


@router.post(
    "/search",
    response_model=SearchResponse,
    dependencies=[Depends(require(Perm.KNOWLEDGE_READ))],
)
def search(
    payload: SearchRequest,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> SearchResponse:
    """The human-facing view of the same retrieval the Copilot will use.

    Identical permission filters, so what a user can find here and what the
    assistant can cite are the same set by construction.
    """
    service = RetrievalService(session, ctx)
    sides = tuple(DocumentSide(s) for s in payload.sides)

    # Company knowledge is deliberately org-wide: reuse across bids is the
    # asset. Tender text is the opposite -- it belongs to one customer's RFP,
    # and searching every tender in the organization at once is how one bid's
    # clauses end up quoted into another's answer. So naming the bid is
    # required to read tender text at all.
    if DocumentSide.TENDER in sides and payload.bid_id is None:
        raise ValidationError(
            "Searching tender documents requires a bid_id. Tender text belongs to one "
            "bid, and searching across bids would mix one RFP into another."
        )

    scope = RetrievalScope(
        task_id=payload.task_id,
        sides=sides,
        bid_ids=(payload.bid_id,) if payload.bid_id else (),
    )
    result = service.search(payload.query, scope=scope, limit=payload.limit)
    return SearchResponse(
        hits=[
            SearchHit(
                chunk_id=c.chunk_id,
                document_version_id=c.document_version_id,
                text=c.text,
                score=round(c.score, 6),
                why_matched=c.why_matched,
                source_locator=c.source_locator(),
                citation=SourceLocator(
                    **{
                        **c.source_locator(),
                        "document_version_id": c.document_version_id,
                    }
                ).human_reference(),
            )
            for c in result.candidates
        ],
        admissible_before_ranking=result.admissible_before_ranking,
        filters_applied=result.filters_applied,
        out_of_scope=result.out_of_scope,
        owning_departments=result.owning_departments,
    )


# --- people: matching CVs to accounts ---------------------------------------
# Under knowledge:approve because this is knowledge governance, not user
# administration: the reviewer is asserting that a piece of extracted evidence
# describes a particular colleague. It creates no account and confers no
# permission — see app.services.person_links for why the boundary matters.


class PersonLinkDecision(BaseModel):
    confirm: bool
    #: Override the suggestion with a different account. This is the "or change
    #: it" case, and the reason the matcher is allowed to be imperfect.
    user_id: uuid.UUID | None = None


class BulkConfirm(BaseModel):
    #: No default. Bulk confirmation has to be an explicit statement about how
    #: sure a match must be before nobody looks at it.
    min_confidence: float = Field(ge=0.0, le=1.0)


@router.post("/people/match", dependencies=[Depends(require(Perm.KNOWLEDGE_APPROVE))])
def match_people(
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Propose an account for every person extracted from the company documents."""
    return person_links.propose_links(session, ctx)


@router.get("/people", dependencies=[Depends(require(Perm.KNOWLEDGE_READ))])
def list_people_links(
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    """The review list: one row per extracted person, with its suggestion."""
    return person_links.list_links(session, ctx)


@router.post("/people/{link_id}", dependencies=[Depends(require(Perm.KNOWLEDGE_APPROVE))])
def decide_person_link(
    link_id: uuid.UUID,
    body: PersonLinkDecision,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Confirm the suggestion, confirm a different account, or reject it."""
    link = person_links.decide(
        session, ctx, link_id=link_id, confirm=body.confirm, user_id=body.user_id
    )
    return {"link_id": str(link.id), "status": link.status.value}


@router.post("/people/confirm-all", dependencies=[Depends(require(Perm.KNOWLEDGE_APPROVE))])
def confirm_all_person_links(
    body: BulkConfirm,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Confirm every proposal at or above a confidence, in one act."""
    return {"confirmed": person_links.confirm_all(session, ctx, min_confidence=body.min_confidence)}
