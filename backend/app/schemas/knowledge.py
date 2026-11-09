from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DocumentOut(ORMModel):
    id: uuid.UUID
    side: str
    title: str
    original_filename: str
    language: str
    confidentiality_class: str
    current_version_id: uuid.UUID | None = None
    # The kind this was filed as, and — for a response to a past tender — the
    # tender it answered. Both are needed to tell "what the customer asked"
    # from "what we offered"; a caller that cannot see the type cannot make
    # that distinction and neither can the person reading the list.
    document_type: str | None = None
    responds_to_document_id: uuid.UUID | None = None
    # Evidence governance: a person's decisions, UNREVIEWED / INTERNAL_ONLY until made.
    evidence_status: str = "UNREVIEWED"
    evidence_use_scope: str = "INTERNAL_ONLY"
    evidence_provenance: str | None = None
    evidence_decided_by: uuid.UUID | None = None
    evidence_decided_at: datetime | None = None
    evidence_reality: str = "UNVERIFIED"
    attested_by: uuid.UUID | None = None
    attested_at: datetime | None = None
    attestation_method: str | None = None
    created_at: datetime


class EvidenceDecisionRequest(BaseModel):
    """Approve, reject or clear a company document as bid evidence."""

    status: str
    use_scope: str = "INTERNAL_ONLY"
    provenance: str | None = None
    note: str | None = None
    # UNVERIFIED (default), GENUINE (needs attestation_method) or SYNTHETIC.
    reality: str = "UNVERIFIED"
    attestation_method: str | None = None


class ApproveBatchRequest(BaseModel):
    """A reviewed set, approved together.

    `evidence_ids` omitted means every currently pending item — deliberately
    explicit, so approving the whole backlog is something a caller asks for
    rather than something that happens by default.
    """

    evidence_ids: list[uuid.UUID] | None = None
    reason: str = Field(min_length=3, max_length=500)


class ClassifyDocumentRequest(BaseModel):
    """A human's filing decision for a document already in the system."""

    document_type: str
    # Optional, and only meaningful for a response to a past tender.
    responds_to_document_id: uuid.UUID | None = None


class DocumentVersionOut(ORMModel):
    id: uuid.UUID
    document_id: uuid.UUID
    version_no: int
    mime_type: str
    byte_size: int
    sha256: str
    state: str
    page_count: int | None = None
    block_count: int | None = None
    chunk_count: int | None = None
    parse_error: str | None = None
    issue_date: date | None = None


class BlockOut(ORMModel):
    id: uuid.UUID
    block_type: str
    ordinal: int
    text: str
    heading_path: list[str] | None = None
    clause_ref: str | None = None
    table_name: str | None = None
    row_index: int | None = None
    page_from: int | None = None
    page_to: int | None = None


class EntityCreate(BaseModel):
    entity_type: str
    canonical_name: str = Field(min_length=1, max_length=500)
    aliases: list[str] = Field(default_factory=list)
    classification: str = "INTERNAL"
    attributes: dict | None = None


class EntityOut(ORMModel):
    id: uuid.UUID
    entity_type: str
    canonical_name: str
    aliases: list[str] | None = None
    classification: str
    status: str
    approval_status: str
    merged_into_entity_id: uuid.UUID | None = None


class FactCreate(BaseModel):
    entity_id: uuid.UUID
    predicate: str = Field(min_length=1, max_length=128)
    value_text: str | None = None
    value_number: float | None = None
    value_date: date | None = None
    unit: str | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class FactOut(ORMModel):
    id: uuid.UUID
    entity_id: uuid.UUID
    predicate: str
    value_text: str | None = None
    value_number: float | None = None
    value_date: date | None = None
    unit: str | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    status: str
    approval_status: str


class RelationCreate(BaseModel):
    subject_entity_id: uuid.UUID
    predicate: str
    object_entity_id: uuid.UUID
    attributes: dict | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class RelationOut(ORMModel):
    id: uuid.UUID
    subject_entity_id: uuid.UUID
    predicate: str
    object_entity_id: uuid.UUID
    attributes: dict | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    status: str
    approval_status: str


class EvidenceOut(ORMModel):
    id: uuid.UUID
    source_document_version_id: uuid.UUID
    excerpt: str
    evidence_type: str
    confidentiality_class: str
    approval_status: str
    status: str
    effective_date: date | None = None
    expiry_date: date | None = None
    approved_for_reuse: bool
    source_locator: dict


class ReviewItemOut(ORMModel):
    id: uuid.UUID
    kind: str
    subject_type: str
    subject_id: uuid.UUID
    detail: dict | None = None
    candidate_ids: list[uuid.UUID] | None = None
    created_at: datetime


class ResolveReviewRequest(BaseModel):
    resolution: str = Field(min_length=3)
    merge_into_entity_id: uuid.UUID | None = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    entity_types: list[str] = Field(default_factory=list)
    sides: list[str] = Field(default_factory=lambda: ["COMPANY"])
    task_id: uuid.UUID | None = None
    # Required when searching TENDER text, and harmless otherwise. A tender
    # chunk means nothing without the bid it belongs to, and searching them all
    # at once mixes one customer's RFP into another's answer.
    bid_id: uuid.UUID | None = None
    limit: int = Field(default=10, ge=1, le=50)


class SearchHit(BaseModel):
    chunk_id: uuid.UUID
    document_version_id: uuid.UUID
    text: str
    score: float
    why_matched: list[str]
    source_locator: dict
    citation: str


class SearchResponse(BaseModel):
    hits: list[SearchHit]
    admissible_before_ranking: int
    filters_applied: dict
    out_of_scope: bool = False
    owning_departments: list[str] = Field(default_factory=list)
