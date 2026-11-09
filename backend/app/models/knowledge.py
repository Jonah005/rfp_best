"""The company knowledge graph.

Not a document store with embeddings on top. Company knowledge is modelled as
typed entities with attribute facts and typed relations between them:

    Ahmed --HAS_CERTIFICATION--> OSCP   {certificate_number, issue_date, expiry}
    Ahmed --WORKED_ON--> Ministry VAPT  {role: "Lead Tester"}

Every fact and relation carries temporal validity, currentness, approval,
supersession and provenance (through EvidenceLink -> Evidence -> a document
location). That is what makes "is this still true?" answerable, and what makes
an expiry on a certificate automatically invalidate a claim that relies on it.

Held in PostgreSQL. No graph database for the MVP.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    Computed,
    Date,
    DateTime,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, tenant_fk, uuid_pk

# Shared with the embedding column on document chunks: both must resolve the
# same configured backend, and one helper is how that stays true.
from app.models.documents import _vector_column
from app.models.enums import (
    ApprovalStatus,
    ConfidentialityClass,
    DocumentType,
    EntityType,
    EvidenceLinkRole,
    EvidenceLinkTarget,
    KnowledgeStatus,
    PersonLinkStatus,
    RelationPredicate,
    ReviewQueueKind,
)


def _pg_enum(python_enum, name: str) -> PGEnum:
    return PGEnum(
        python_enum,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        create_type=True,
    )


class KnowledgeEntity(Base, TenantMixin, TimestampMixin):
    __tablename__ = "knowledge_entity"

    id: Mapped[uuid.UUID] = uuid_pk()
    entity_type: Mapped[EntityType] = mapped_column(
        _pg_enum(EntityType, "entity_type"), nullable=False
    )
    canonical_name: Mapped[str] = mapped_column(String(500), nullable=False)
    aliases: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    classification: Mapped[ConfidentialityClass] = mapped_column(
        _pg_enum(ConfidentialityClass, "confidentiality_class"), nullable=False
    )
    status: Mapped[KnowledgeStatus] = mapped_column(
        _pg_enum(KnowledgeStatus, "knowledge_status"), nullable=False
    )
    approval_status: Mapped[ApprovalStatus] = mapped_column(
        _pg_enum(ApprovalStatus, "approval_status"), nullable=False
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    attributes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Set when this entity was merged into another; old references still resolve.
    merged_into_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    name_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', canonical_name)", persisted=True)
    )

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_knowledge_entity_id_org"),
        tenant_fk("owner_user_id", "app_user", name="fk_entity_owner_org"),
        tenant_fk("merged_into_entity_id", "knowledge_entity", name="fk_entity_merged_org"),
        Index("ix_knowledge_entity_type", "org_id", "entity_type", "status"),
        Index("ix_knowledge_entity_name_tsv", "name_tsv", postgresql_using="gin"),
        Index("ix_knowledge_entity_aliases", "aliases", postgresql_using="gin"),
    )


class KnowledgeFact(Base, TenantMixin, TimestampMixin):
    """An attribute assertion about one entity: (entity, predicate, value)."""

    __tablename__ = "knowledge_fact"

    id: Mapped[uuid.UUID] = uuid_pk()
    entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    predicate: Mapped[str] = mapped_column(String(128), nullable=False)

    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_number: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    value_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    value_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)

    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    status: Mapped[KnowledgeStatus] = mapped_column(
        _pg_enum(KnowledgeStatus, "knowledge_status"), nullable=False
    )
    approval_status: Mapped[ApprovalStatus] = mapped_column(
        _pg_enum(ApprovalStatus, "approval_status"), nullable=False
    )
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    extracted_by: Mapped[str] = mapped_column(String(128), nullable=False)
    supersedes_fact_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verified_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_knowledge_fact_id_org"),
        tenant_fk("entity_id", "knowledge_entity", ondelete="CASCADE"),
        tenant_fk("supersedes_fact_id", "knowledge_fact", name="fk_fact_supersedes_org"),
        tenant_fk("verified_by", "app_user", name="fk_fact_verifier_org"),
        Index("ix_knowledge_fact_lookup", "org_id", "entity_id", "predicate"),
        Index("ix_knowledge_fact_validity", "org_id", "status", "valid_to"),
    )


class KnowledgeRelation(Base, TenantMixin, TimestampMixin):
    """A typed edge between two entities, with attributes and validity."""

    __tablename__ = "knowledge_relation"

    id: Mapped[uuid.UUID] = uuid_pk()
    subject_entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    predicate: Mapped[RelationPredicate] = mapped_column(
        _pg_enum(RelationPredicate, "relation_predicate"), nullable=False
    )
    object_entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    attributes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    status: Mapped[KnowledgeStatus] = mapped_column(
        _pg_enum(KnowledgeStatus, "knowledge_status"), nullable=False
    )
    approval_status: Mapped[ApprovalStatus] = mapped_column(
        _pg_enum(ApprovalStatus, "approval_status"), nullable=False
    )
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    extracted_by: Mapped[str] = mapped_column(String(128), nullable=False)
    supersedes_relation_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verified_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_knowledge_relation_id_org"),
        tenant_fk("subject_entity_id", "knowledge_entity", ondelete="CASCADE"),
        tenant_fk("object_entity_id", "knowledge_entity", name="fk_relation_object_org"),
        tenant_fk(
            "supersedes_relation_id", "knowledge_relation", name="fk_relation_supersedes_org"
        ),
        tenant_fk("verified_by", "app_user", name="fk_relation_verifier_org"),
        Index("ix_knowledge_relation_subject", "org_id", "subject_entity_id", "predicate"),
        Index("ix_knowledge_relation_object", "org_id", "object_entity_id", "predicate"),
        Index("ix_knowledge_relation_validity", "org_id", "status", "valid_to"),
    )


class Evidence(Base, TenantMixin, TimestampMixin):
    """A citable proof unit: a region of a document version, plus governance."""

    __tablename__ = "evidence"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_document_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    source_locator: Mapped[dict] = mapped_column(JSONB, nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    # Generated in the database so evidence matching is an indexed query rather
    # than a scan in application memory. Without this, finding candidates for
    # one requirement means loading every approved evidence row in the tenant.
    excerpt_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', excerpt)", persisted=True)
    )
    # The excerpt's embedding, so a requirement can find evidence that means the
    # same thing without sharing its words.
    #
    # Lexical matching alone cannot do this job. A requirement reading "firms
    # must be Industry Recognized/Certified" shares no token with a certificate
    # reading "is certified to ISO/IEC 27001:2022" -- and `simple` does not even
    # stem, so `certified` and `certificate` are different words. The evidence
    # was not ranked low; it was never a candidate.
    #
    # Nullable because evidence extracted before this column existed has no
    # vector until it is backfilled, and a requirement must still be able to
    # match it lexically in the meantime.
    excerpt_vector: Mapped[list[float] | None] = mapped_column(_vector_column(), nullable=True)
    evidence_type: Mapped[DocumentType] = mapped_column(
        _pg_enum(DocumentType, "document_type"), nullable=False
    )

    # Whether this excerpt stands on its own as proof -- a different question
    # from what KIND of material it is. `NOT_SELF_EVIDENCING` downgrades whole
    # types (a blank form, a pricing template) and cannot reach a
    # certification-register row that is genuinely about ISO 27001 and reads
    # "Certificate no.: [REPLACE] | Evidence status: NOT EVIDENCED".
    #
    # NULL means nobody has judged this row, and is deliberately not a `true`
    # default: "assume proof until told otherwise" would make an unjudged row
    # indistinguishable from a judged-good one. See migration 0021.
    is_substantiated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    substantiation_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 'markers@v1', 'llm:evidence_classification@vN' or 'human:<user_id>'.
    classified_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    source_owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    approval_status: Mapped[ApprovalStatus] = mapped_column(
        _pg_enum(ApprovalStatus, "approval_status"), nullable=False
    )
    confidentiality_class: Mapped[ConfidentialityClass] = mapped_column(
        _pg_enum(ConfidentialityClass, "confidentiality_class"), nullable=False
    )

    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    applicable_region: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    applicable_product: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    applicable_service: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)

    version: Mapped[int] = mapped_column(default=1, nullable=False)
    status: Mapped[KnowledgeStatus] = mapped_column(
        _pg_enum(KnowledgeStatus, "knowledge_status"), nullable=False
    )
    approved_for_reuse: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    supersedes_evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_evidence_id_org"),
        tenant_fk("source_document_version_id", "document_version"),
        tenant_fk("source_owner_user_id", "app_user", name="fk_evidence_owner_org"),
        tenant_fk("supersedes_evidence_id", "evidence", name="fk_evidence_supersedes_org"),
        Index("ix_evidence_expiry", "org_id", "status", "expiry_date"),
        Index("ix_evidence_source", "org_id", "source_document_version_id"),
        Index("ix_evidence_class", "org_id", "confidentiality_class"),
        Index("ix_evidence_tsv", "excerpt_tsv", postgresql_using="gin"),
        # The candidate query filters on exactly these three, in this order.
        Index("ix_evidence_reusable", "org_id", "approval_status", "status"),
    )


class EvidenceLink(Base, TenantMixin, TimestampMixin):
    """Ties a piece of evidence to what it proves.

    Polymorphic by necessity: the target may be a fact, a relation, an entity,
    a requirement assessment or an artifact statement. A composite foreign key
    cannot be declared across a polymorphic target, so a trigger asserts the
    target's org_id matches (see migration 0003).
    """

    __tablename__ = "evidence_link"

    id: Mapped[uuid.UUID] = uuid_pk()
    evidence_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    target_type: Mapped[EvidenceLinkTarget] = mapped_column(
        _pg_enum(EvidenceLinkTarget, "evidence_link_target"), nullable=False
    )
    target_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    link_role: Mapped[EvidenceLinkRole] = mapped_column(
        _pg_enum(EvidenceLinkRole, "evidence_link_role"), nullable=False
    )
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "evidence_id", "target_type", "target_id", "link_role", name="uq_evidence_link"
        ),
        tenant_fk("evidence_id", "evidence", ondelete="CASCADE"),
        Index("ix_evidence_link_target", "org_id", "target_type", "target_id"),
    )


class KnowledgeReviewItem(Base, TenantMixin, TimestampMixin):
    """The Knowledge Administrator's queue.

    Entity resolution, conflicts and expiries all land here rather than being
    resolved automatically. Two people are never silently merged, and two
    contradictory facts are never silently ranked.
    """

    __tablename__ = "knowledge_review_item"

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[ReviewQueueKind] = mapped_column(
        _pg_enum(ReviewQueueKind, "review_queue_kind"), nullable=False
    )
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    candidate_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        tenant_fk("resolved_by", "app_user", name="fk_review_resolver_org"),
        Index("ix_knowledge_review_open", "org_id", "kind", "resolved_at"),
        Index("ix_knowledge_review_subject", "org_id", "subject_type", "subject_id"),
    )


class PersonLink(Base, TenantMixin, TimestampMixin):
    """A CV describes an employee; this says which account that employee holds.

    Two populations of "people" exist in this system and they are deliberately
    separate. `app_user` rows are identities: they sign in, hold roles, own
    tasks, and are created only by seeding or an org admin. `KnowledgeEntity`
    rows of type PERSON are evidence, extracted from company documents, and can
    do none of those things.

    Keeping them apart is a security property, not an oversight. Uploaded
    documents are untrusted input, so if extraction could create or match
    accounts, uploading a CV would mint an identity.

    This table is the bridge, and it is built to carry as little weight as
    possible:

    * **A link grants nothing.** No role, no permission, no access to any
      evidence. It records a fact — "this CV is about this person" — so a
      staffing answer can name a real employee. Authorization continues to come
      from roles and access grants alone, and nothing in the permission
      evaluator reads this table.
    * **Only a person confirms one.** The matcher proposes; a knowledge
      administrator decides. A PROPOSED row is a suggestion and is treated as
      absent by everything except the review list.
    * **A rejection is remembered.** Otherwise the next run proposes the same
      wrong pair, and a reviewer's decision decays into a chore.
    """

    __tablename__ = "person_link"

    id: Mapped[uuid.UUID] = uuid_pk()
    entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # Null on a REJECTED row: the reviewer said "not this account", and which
    # account was wrongly proposed is kept in `proposed_user_id` instead.
    user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    proposed_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    status: Mapped[PersonLinkStatus] = mapped_column(
        _pg_enum(PersonLinkStatus, "person_link_status"), nullable=False
    )
    #: How the pair was found — an exact name, a surname and initial, an email
    #: local part, or a person choosing it. Shown to the reviewer, because
    #: "matched on a surname" deserves more scrutiny than "matched on an email".
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # One live decision per extracted person. Re-running the matcher updates
        # the row rather than stacking proposals nobody asked for twice.
        UniqueConstraint("org_id", "entity_id", name="uq_person_link_entity"),
        tenant_fk("entity_id", "knowledge_entity", name="fk_person_link_entity_org"),
        tenant_fk("user_id", "app_user", name="fk_person_link_user_org"),
        Index("ix_person_link_status", "org_id", "status"),
    )
