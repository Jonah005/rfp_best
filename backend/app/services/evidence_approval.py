"""A person's decision about a company document offered as proof in a bid.

Three facts, all recorded as the decider's own: whether the document is approved as
evidence, where it may be used, and where it came from. The system never decides any
of them. It only refuses decisions that cannot be true:

  * a document whose text says it is a test fixture cannot be approved or cleared for
    a customer, whoever asks;
  * clearing a document for a customer needs an approval and a provenance;
  * a rejected document goes back to internal use only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, StateConflict, ValidationError
from app.models import Document
from app.models.enums import DocumentEvidenceStatus, DocumentSide, UseScope
from app.services import audit, report_attachments


def decide(
    session: Session,
    ctx: ExecutionContext,
    *,
    document_id: uuid.UUID,
    status: DocumentEvidenceStatus,
    use_scope: UseScope,
    provenance: str | None,
    note: str | None = None,
    reality: str = "UNVERIFIED",
    attestation_method: str | None = None,
) -> Document:
    document = session.get(Document, document_id)
    if (
        document is None
        or document.org_id != ctx.org_id
        or document.side is not DocumentSide.COMPANY
    ):
        raise NotFound("Company document not found.")
    if document.withdrawn_at is not None:
        raise StateConflict("A withdrawn document cannot be approved as evidence.")
    provenance = (provenance or "").strip() or None
    if status is DocumentEvidenceStatus.REJECTED:
        use_scope = UseScope.INTERNAL_ONLY
    if status is DocumentEvidenceStatus.UNREVIEWED and use_scope is UseScope.EXTERNAL_BID:
        raise ValidationError("A document cannot be cleared for a customer before it is approved.")
    if status is DocumentEvidenceStatus.APPROVED:
        text = report_attachments.document_text(session, ctx, document)
        if report_attachments.is_fixture(text):
            raise StateConflict(
                "This document says it is a test fixture, so it cannot be approved as "
                "evidence. Upload the company's own document instead."
            )
        if provenance is None:
            raise ValidationError("Say where the document came from before approving it.")
    if use_scope is UseScope.EXTERNAL_BID and status is not DocumentEvidenceStatus.APPROVED:
        raise ValidationError("Only an approved document can be cleared for a customer.")

    reality = (reality or "UNVERIFIED").upper()
    if reality not in ("UNVERIFIED", "GENUINE", "SYNTHETIC"):
        raise ValidationError("Reality must be UNVERIFIED, GENUINE or SYNTHETIC.")
    method = (attestation_method or "").strip() or None
    version_hash = None
    if reality == "GENUINE":
        if status is not DocumentEvidenceStatus.APPROVED:
            raise ValidationError("Only an approved document can be attested as genuine.")
        if method is None:
            raise ValidationError(
                "Say how you checked it is genuine (for example: signed original seen)."
            )
        text = report_attachments.document_text(session, ctx, document)
        if report_attachments.is_test_data(text, provenance):
            raise StateConflict(
                "This document says it is test data, so it cannot be attested as genuine."
            )
        from app.models import DocumentVersion

        version = session.get(DocumentVersion, document.current_version_id)
        version_hash = getattr(version, "sha256", None)
        if not version_hash:
            raise StateConflict("The document has no stored version to attest.")
    elif reality == "UNVERIFIED" and report_attachments.is_test_data("", provenance):
        reality = "SYNTHETIC"

    before = {
        "reality": document.evidence_reality,
        "status": document.evidence_status.value,
        "use_scope": document.evidence_use_scope.value,
        "provenance": document.evidence_provenance,
    }
    document.evidence_status = status
    document.evidence_use_scope = use_scope
    document.evidence_provenance = provenance
    document.evidence_note = (note or "").strip() or None
    document.evidence_decided_by = ctx.actor_user_id
    document.evidence_decided_at = datetime.now(timezone.utc)
    document.evidence_reality = reality
    if reality == "GENUINE":
        document.attested_by = ctx.actor_user_id
        document.attested_at = datetime.now(timezone.utc)
        document.attestation_method = method
        document.attested_version_hash = version_hash
    else:
        document.attested_by = None
        document.attested_at = None
        document.attestation_method = None
        document.attested_version_hash = None
    session.flush()
    audit.record(
        session,
        ctx,
        action="document.evidence_decided",
        entity_type="document",
        entity_id=document.id,
        before=before,
        after={
            "status": status.value,
            "use_scope": use_scope.value,
            "provenance": provenance,
            "reality": reality,
            "attestation_method": method,
        },
    )
    return document
