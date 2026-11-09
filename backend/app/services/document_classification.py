"""What kind of company document this is, and therefore what it can prove.

Every excerpt inherits its document's type (`knowledge_extraction` stamps
`evidence_type` from it), and nothing ever set that type unless a person picked
one at upload. So 610 excerpts in the live corpus read `OTHER`: a blank CV form
and a signed reference letter were indistinguishable, and the copilot could not
apply the rule it is given -- that a template shows the SHAPE of an answer and
not that the company has done anything.

This proposes the type when no person stated one. A human classification always
wins: `classified_by` starting with `human:` is never overwritten. The model
names a type; this module checks that name against the enum and falls back to
OTHER, so an invented type cannot enter the taxonomy.
"""

from __future__ import annotations

import json

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.core.logging import get_logger
from app.models import (
    Document,
    DocumentChunk,
    DocumentClassification,
    DocumentVersion,
    Evidence,
)
from app.models.enums import DocumentSide, DocumentType
from app.services import audit

log = get_logger("document_classification")

# The tender side is the customer's material and is never proposed here: a
# company file mistyped as an RFP would be read as a wish list, and one mistyped
# the other way would let a customer's demands be mined as our capability.
TENDER_ONLY = {
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
}
COMPANY_TYPES = [t for t in DocumentType if t not in TENDER_ONLY]

MAX_CHARS = 6000


def _sample(session: Session, ctx: ExecutionContext, *, version_id) -> str:
    """The opening of the document: what a filing clerk would read to file it."""
    chunks = (
        session.execute(
            select(DocumentChunk)
            .where(
                DocumentChunk.org_id == ctx.org_id,
                DocumentChunk.document_version_id == version_id,
            )
            .order_by(DocumentChunk.ordinal)
            .limit(12)
        )
        .scalars()
        .all()
    )
    text = "\n".join(c.text for c in chunks)
    return text[:MAX_CHARS]


def _type_for(name: str) -> DocumentType:
    wanted = (name or "").strip().upper().replace(" ", "_").replace("-", "_")
    for candidate in COMPANY_TYPES:
        if candidate.value == wanted:
            return candidate
    return DocumentType.OTHER


def stamp_evidence(session: Session, ctx: ExecutionContext, *, version_id, document_type) -> int:
    """Give this version's excerpts the document's type.

    Only excerpts still carrying OTHER are touched: anything a person or an
    earlier, better-informed pass typed stays as it is.

    Stamping OTHER onto excerpts that already read OTHER is not work, and
    counting it as work is how the screen came to report "54 excerpts now carry
    a type" on a pass that had changed nothing.
    """
    if document_type is DocumentType.OTHER:
        return 0
    rows = (
        session.execute(
            select(Evidence).where(
                Evidence.org_id == ctx.org_id,
                Evidence.source_document_version_id == version_id,
                Evidence.evidence_type == DocumentType.OTHER,
            )
        )
        .scalars()
        .all()
    )
    for evidence in rows:
        evidence.evidence_type = document_type
    if rows:
        session.flush()
    return len(rows)


def classify_version(
    session: Session,
    ctx: ExecutionContext,
    *,
    version_id,
    gateway=None,
    force: bool = False,
) -> dict:
    """Propose a type for one company document version, and stamp its excerpts."""
    from app.gateway.gateway import build_gateway
    from app.gateway.registry import wrap_untrusted
    from app.schemas.document_classification import DocumentClassificationResult

    version = session.get(DocumentVersion, version_id)
    if version is None or version.org_id != ctx.org_id:
        raise NotFound("Document version not found.")
    document = session.get(Document, version.document_id)
    if document is None or document.org_id != ctx.org_id:
        raise NotFound("Document not found.")
    if document.side is not DocumentSide.COMPANY:
        raise ValidationError("Only company documents are classified here.")

    existing = session.execute(
        select(DocumentClassification).where(
            DocumentClassification.org_id == ctx.org_id,
            DocumentClassification.document_version_id == version.id,
        )
    ).scalar_one_or_none()
    if existing is not None and existing.classified_by.startswith("human:") and not force:
        stamped = stamp_evidence(
            session, ctx, version_id=version.id, document_type=existing.document_type
        )
        return {
            "document_type": existing.document_type.value,
            "source": "human",
            "evidence_restamped": stamped,
        }
    if existing is not None and existing.document_type is not DocumentType.OTHER and not force:
        stamped = stamp_evidence(
            session, ctx, version_id=version.id, document_type=existing.document_type
        )
        return {
            "document_type": existing.document_type.value,
            "source": "existing",
            "evidence_restamped": stamped,
        }

    sample = _sample(session, ctx, version_id=version.id)
    if not sample.strip():
        return {"document_type": None, "source": "no_text", "evidence_restamped": 0}

    payload = {
        "filename": document.original_filename,
        "allowed_types": [t.value for t in COMPANY_TYPES],
        "opening_text": sample,
    }
    try:
        model_gateway = gateway or build_gateway()
        result = model_gateway.generate_structured(
            "document_classification",
            wrap_untrusted(json.dumps(payload, ensure_ascii=False)),
            ctx=ctx,
            session=session,
            cache=True,
        )
    except Exception as exc:  # noqa: BLE001 - classification degrades, never fails ingestion
        log.info("document_classification_unavailable", reason=type(exc).__name__)
        return {"document_type": None, "source": "unavailable", "evidence_restamped": 0}
    if not isinstance(result, DocumentClassificationResult):
        return {"document_type": None, "source": "unusable", "evidence_restamped": 0}

    document_type = _type_for(result.document_type)
    from app.gateway.registry import get as get_call_type

    call_type = get_call_type("document_classification")
    classified_by = f"llm:{call_type.name}@{call_type.prompt_version}"
    if existing is None:
        existing = DocumentClassification(
            org_id=ctx.org_id,
            document_version_id=version.id,
            document_type=document_type,
            confidence=round(float(result.confidence), 3),
            classified_by=classified_by,
        )
        session.add(existing)
    else:
        existing.document_type = document_type
        existing.confidence = round(float(result.confidence), 3)
        existing.classified_by = classified_by
    session.flush()
    stamped = stamp_evidence(session, ctx, version_id=version.id, document_type=document_type)
    audit.record(
        session,
        ctx,
        action="document.classified",
        entity_type="document_version",
        entity_id=version.id,
        after={
            "document_type": document_type.value,
            "confidence": float(result.confidence),
            "evidence_restamped": stamped,
        },
        reason=result.reason[:400] or None,
    )
    log.info(
        "document_classified",
        document_type=document_type.value,
        proposed=result.document_type,
        evidence_restamped=stamped,
    )
    return {
        "document_type": document_type.value,
        "source": "model",
        "confidence": float(result.confidence),
        "reason": result.reason,
        "evidence_restamped": stamped,
    }


def classify_unclassified(
    session: Session, ctx: ExecutionContext, *, limit: int = 50, gateway=None
) -> dict:
    """Work through the company documents nobody has typed.

    Written as a bounded batch rather than one sweeping job so a large corpus
    can be worked through by calling it until `remaining` reaches zero.

    A decision is recorded even when the answer is OTHER, and this skips what
    the CURRENT prompt has already decided. Selecting on the type instead made
    the button non-terminating: four documents the model could not place were
    picked up by every pass, re-read, filed as OTHER again, and reported as
    four freshly filed documents for ever. A later prompt version is a new
    question, so those four are offered to it -- but the same question is not
    asked twice.
    """
    from app.gateway.registry import get as get_call_type

    call_type = get_call_type("document_classification")
    current = f"llm:{call_type.name}@{call_type.prompt_version}"
    typed = select(DocumentClassification.document_version_id).where(
        DocumentClassification.org_id == ctx.org_id,
        or_(
            DocumentClassification.document_type != DocumentType.OTHER,
            DocumentClassification.classified_by.startswith("human:"),
            DocumentClassification.classified_by == current,
        ),
    )
    stmt = (
        select(DocumentVersion.id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            DocumentVersion.org_id == ctx.org_id,
            Document.side == DocumentSide.COMPANY,
            Document.withdrawn_at.is_(None),
            DocumentVersion.id.not_in(typed),
        )
        .order_by(DocumentVersion.created_at)
    )
    pending = list(session.execute(stmt).scalars().all())
    done: dict[str, int] = {}
    restamped = 0
    filed = 0
    unplaced = 0
    for version_id in pending[:limit]:
        outcome = classify_version(session, ctx, version_id=version_id, gateway=gateway)
        key = outcome.get("document_type") or outcome["source"]
        done[key] = done.get(key, 0) + 1
        restamped += outcome.get("evidence_restamped", 0)
        # "Filed" means the document came out of this with a real type. A
        # document read and left OTHER, or one with no text to read, was
        # attempted, and saying so is the difference between a screen that
        # reports progress and one that reports effort.
        if outcome.get("document_type") and outcome["document_type"] != DocumentType.OTHER.value:
            filed += 1
        else:
            unplaced += 1
    return {
        "classified": filed,
        "attempted": len(pending[:limit]),
        "left_untyped": unplaced,
        "remaining": max(0, len(pending) - limit),
        "by_type": done,
        "evidence_restamped": restamped,
    }


__all__ = ["COMPANY_TYPES", "classify_unclassified", "classify_version", "stamp_evidence"]
