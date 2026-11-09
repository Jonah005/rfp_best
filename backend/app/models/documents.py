"""Document ingestion and search layer.

One pipeline serves tender-side and company-side documents; `Document.side`
keeps them apart. The chain is:

    Document -> DocumentVersion -> DocumentBlock -> DocumentChunk -> Embedding

`DocumentBlock` preserves the document's structure (headings, tables, rows,
cells) because that structure is what makes retrieval work: block type, heading
path and table name are first-class ranking signals, not decoration. Losing them
at parse time is what makes a naive RAG system return a CV when asked for
customer references.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Computed,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import EMBEDDING_DIM, get_settings
from app.db.base import Base, TenantMixin, TimestampMixin, tenant_fk, uuid_pk
from app.models.enums import (
    BlockType,
    ConfidentialityClass,
    DocumentEvidenceStatus,
    DocumentRelationType,
    DocumentSide,
    DocumentType,
    DocumentVersionState,
    UseScope,
)


def _vector_column():
    """The storage type for an embedding, per the configured backend.

    Resolved once at import: the schema and the ORM must agree, and a column
    type that changed underneath a running process would be far worse than one
    chosen at startup.
    """
    if get_settings().vector_backend == "portable":
        from sqlalchemy import Float
        from sqlalchemy.dialects.postgresql import ARRAY

        return ARRAY(Float(precision=24))
    return Vector(EMBEDDING_DIM)


def _vector_indexes():
    """HNSW needs pgvector; the portable backend has no index to offer."""
    if get_settings().vector_backend == "portable":
        return ()
    return (
        Index(
            "ix_embedding_vector_hnsw",
            "vector",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"vector": "vector_cosine_ops"},
        ),
    )


def _pg_enum(python_enum, name: str) -> PGEnum:
    return PGEnum(
        python_enum,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        create_type=True,
    )


class Document(Base, TenantMixin, TimestampMixin):
    __tablename__ = "document"

    id: Mapped[uuid.UUID] = uuid_pk()
    side: Mapped[DocumentSide] = mapped_column(
        _pg_enum(DocumentSide, "document_side"), nullable=False
    )
    # Set only for tender-side documents. Company knowledge is org-wide.
    bid_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)
    confidentiality_class: Mapped[ConfidentialityClass] = mapped_column(
        _pg_enum(ConfidentialityClass, "confidentiality_class"), nullable=False
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    withdrawn_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Historical bid files come in pairs. A past tender says what a customer
    # asked for; only the response says what this company actually offered, so
    # a PREVIOUS_PROPOSAL points back at the PREVIOUS_TENDER it answered.
    # Without the link the two are unrelated files and the question "what did we
    # commit to last time" has no path from the document that asked it.
    responds_to_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    # Evidence governance. Where the document came from, whether a person has
    # approved it as proof, and whether it may leave the company. All three are
    # human decisions: the defaults are "not reviewed" and "internal only", and a
    # document that says it is a test fixture can never be approved (see
    # `report_attachments.FIXTURE`).
    evidence_status: Mapped[DocumentEvidenceStatus] = mapped_column(
        _pg_enum(DocumentEvidenceStatus, "document_evidence_status"),
        default=DocumentEvidenceStatus.UNREVIEWED,
        server_default=DocumentEvidenceStatus.UNREVIEWED.value,
        nullable=False,
    )
    evidence_use_scope: Mapped[UseScope] = mapped_column(
        _pg_enum(UseScope, "use_scope"),
        default=UseScope.INTERNAL_ONLY,
        server_default=UseScope.INTERNAL_ONLY.value,
        nullable=False,
    )
    evidence_provenance: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_decided_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    evidence_decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Attestation: UNVERIFIED (default) / GENUINE / SYNTHETIC. GENUINE needs a person, a method
    # and the hash of the version they vouched for; a new version voids it.
    evidence_reality: Mapped[str] = mapped_column(
        String(16), default="UNVERIFIED", server_default="UNVERIFIED", nullable=False
    )
    attested_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    attested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attestation_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    attested_version_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_document_id_org"),
        Index("ix_document_responds_to", "org_id", "responds_to_document_id"),
        tenant_fk("owner_user_id", "app_user", name="fk_document_owner_org"),
        Index("ix_document_scope", "org_id", "side", "bid_id"),
    )


class DocumentVersion(Base, TenantMixin, TimestampMixin):
    __tablename__ = "document_version"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    version_label: Mapped[str | None] = mapped_column(String(64), nullable=True)

    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # As stated by the customer. Drives precedence resolution in Phase G, which
    # is why it is captured at intake and editable by a human.
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    state: Mapped[DocumentVersionState] = mapped_column(
        _pg_enum(DocumentVersionState, "document_version_state"), nullable=False
    )
    scan_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    block_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_document_version_id_org"),
        UniqueConstraint("document_id", "version_no", name="uq_document_version_no"),
        tenant_fk("document_id", "document", ondelete="CASCADE"),
        Index("ix_document_version_state", "org_id", "state"),
        Index("ix_document_version_sha", "org_id", "sha256"),
    )


class DocumentClassification(Base, TenantMixin, TimestampMixin):
    __tablename__ = "document_classification"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, unique=True
    )
    document_type: Mapped[DocumentType] = mapped_column(
        _pg_enum(DocumentType, "document_type"), nullable=False
    )
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    # 'llm:<call_type>@<prompt_version>' or 'human:<user_id>'. Every derived
    # value records what produced it.
    classified_by: Mapped[str] = mapped_column(String(128), nullable=False)
    customer_stated_precedence: Mapped[str | None] = mapped_column(Text, nullable=True)
    affected_clause_refs: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        tenant_fk("document_version_id", "document_version", ondelete="CASCADE"),
        tenant_fk("reviewed_by", "app_user", name="fk_doc_class_reviewer_org"),
    )


class DocumentRelation(Base, TenantMixin, TimestampMixin):
    """Explicit supersession between document versions.

    Phase G's precedence resolver consumes these. Modelling the relation rather
    than hardcoding 'amendment beats original' is what lets a later
    clarification win over an earlier amendment on one clause.
    """

    __tablename__ = "document_relation"

    id: Mapped[uuid.UUID] = uuid_pk()
    from_version_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    to_version_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    relation_type: Mapped[DocumentRelationType] = mapped_column(
        _pg_enum(DocumentRelationType, "document_relation_type"), nullable=False
    )
    clause_refs: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    detected_by: Mapped[str] = mapped_column(String(128), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "from_version_id", "to_version_id", "relation_type", name="uq_document_relation"
        ),
        tenant_fk("from_version_id", "document_version", ondelete="CASCADE"),
        tenant_fk("to_version_id", "document_version", ondelete="CASCADE"),
    )


class DocumentBlock(Base, TenantMixin):
    """A structural unit of a parsed document, in document order."""

    __tablename__ = "document_block"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_version_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    block_type: Mapped[BlockType] = mapped_column(_pg_enum(BlockType, "block_type"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    # The ancestor heading chain, e.g. {'6 Scope', '6.1 Testing window'}.
    heading_path: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    # Normalized clause identifier when one is detectable, e.g. '6.1'.
    clause_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)

    table_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    row_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    col_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)
    parent_block_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    text_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', text)", persisted=True)
    )

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_document_block_id_org"),
        UniqueConstraint("document_version_id", "ordinal", name="uq_document_block_ordinal"),
        tenant_fk("document_version_id", "document_version", ondelete="CASCADE"),
        tenant_fk("parent_block_id", "document_block", name="fk_block_parent_org"),
        Index("ix_document_block_order", "org_id", "document_version_id", "ordinal"),
        Index("ix_document_block_clause", "org_id", "clause_ref"),
        Index("ix_document_block_tsv", "text_tsv", postgresql_using="gin"),
    )


class DocumentChunk(Base, TenantMixin):
    """The retrieval unit, assembled from one or more blocks.

    `confidentiality_class` and `side` are denormalized from the parent document
    ON PURPOSE. Retrieval must apply tenant, permission and scope filters inside
    the same statement that touches the vector index; a join to reach those
    values would work today and get refactored into post-ranking filtering
    later, which is exactly the leak this design exists to prevent.
    """

    __tablename__ = "document_chunk"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_version_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    block_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(PGUUID(as_uuid=True)), nullable=False)

    heading_path: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    clause_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    table_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    page_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)

    confidentiality_class: Mapped[ConfidentialityClass] = mapped_column(
        _pg_enum(ConfidentialityClass, "confidentiality_class"), nullable=False
    )
    side: Mapped[DocumentSide] = mapped_column(
        _pg_enum(DocumentSide, "document_side"), nullable=False
    )

    text_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', text)", persisted=True)
    )

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_document_chunk_id_org"),
        UniqueConstraint("document_version_id", "ordinal", name="uq_document_chunk_ordinal"),
        tenant_fk("document_version_id", "document_version", ondelete="CASCADE"),
        Index("ix_document_chunk_order", "org_id", "document_version_id", "ordinal"),
        Index("ix_document_chunk_filter", "org_id", "side", "confidentiality_class"),
        Index("ix_document_chunk_tsv", "text_tsv", postgresql_using="gin"),
    )


class Embedding(Base, TenantMixin):
    """One vector per chunk per model.

    `model_id` is recorded on every row so a model change is a detectable,
    migrated re-index rather than silent corruption: retrieval filters on the
    configured active model, so a partially backfilled corpus degrades to
    "fewer candidates", never to "nonsense distances".
    """

    __tablename__ = "embedding"

    id: Mapped[uuid.UUID] = uuid_pk()
    chunk_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[list[float]] = mapped_column(_vector_column(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("chunk_id", "model_id", name="uq_embedding_chunk_model"),
        tenant_fk("chunk_id", "document_chunk", ondelete="CASCADE"),
        *_vector_indexes(),
        Index("ix_embedding_model", "org_id", "model_id"),
    )
