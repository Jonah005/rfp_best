"""Proposal assembly, validation, immutable release and governed learning."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, tenant_fk, uuid_pk
from app.models.enums import (
    ApprovalStatus,
    CandidateState,
    ConfidentialityClass,
    DocumentStream,
    ExceptionState,
    FindingSeverity,
    FindingWaivability,
    GeneratedFormat,
    InclusionMode,
    OutcomeResult,
    OutputMode,
    PackageState,
    StructureDerivationMethod,
    ValidationRunStatus,
)


def _pg_enum(python_enum, name: str) -> PGEnum:
    return PGEnum(
        python_enum,
        name=name,
        values_callable=lambda enum: [member.value for member in enum],
        create_type=True,
    )


class ProposalStructure(Base, TenantMixin, TimestampMixin):
    __tablename__ = "proposal_structure"

    id: Mapped[uuid.UUID] = uuid_pk()
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    derived_from_document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    derivation_method: Mapped[StructureDerivationMethod] = mapped_column(
        _pg_enum(StructureDerivationMethod, "structure_derivation_method"), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_proposal_structure_id_org"),
        UniqueConstraint("bid_id", name="uq_proposal_structure_bid"),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
        tenant_fk(
            "derived_from_document_version_id",
            "document_version",
            name="fk_structure_source_org",
        ),
        tenant_fk("created_by", "app_user", name="fk_structure_creator_org"),
    )


class ProposalSection(Base, TenantMixin, TimestampMixin):
    __tablename__ = "proposal_section"

    id: Mapped[uuid.UUID] = uuid_pk()
    structure_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    parent_section_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    number_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    target_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    required_format: Mapped[GeneratedFormat | None] = mapped_column(
        _pg_enum(GeneratedFormat, "generated_format"), nullable=True
    )
    is_mandatory: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    guidance: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_locator: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_proposal_section_id_org"),
        tenant_fk("structure_id", "proposal_structure", ondelete="CASCADE"),
        tenant_fk("parent_section_id", "proposal_section", name="fk_section_parent_org"),
        Index("ix_section_tree", "org_id", "structure_id", "parent_section_id", "order_index"),
    )


class SectionArtifactMap(Base, TenantMixin, TimestampMixin):
    __tablename__ = "section_artifact_map"

    id: Mapped[uuid.UUID] = uuid_pk()
    section_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    artifact_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    inclusion_mode: Mapped[InclusionMode] = mapped_column(
        _pg_enum(InclusionMode, "inclusion_mode"), nullable=False
    )
    excerpt_statement_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_section_artifact_map_id_org"),
        UniqueConstraint("section_id", "artifact_id", name="uq_section_artifact"),
        tenant_fk("section_id", "proposal_section", ondelete="CASCADE"),
        tenant_fk("artifact_id", "response_artifact", ondelete="CASCADE"),
        Index("ix_section_artifact_reverse", "org_id", "artifact_id"),
    )


class GeneratedDocument(Base, TenantMixin, TimestampMixin):
    __tablename__ = "generated_document"

    id: Mapped[uuid.UUID] = uuid_pk()
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    structure_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    required_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    format: Mapped[GeneratedFormat] = mapped_column(
        _pg_enum(GeneratedFormat, "generated_format"), nullable=False
    )
    stream: Mapped[DocumentStream] = mapped_column(
        _pg_enum(DocumentStream, "document_stream"), nullable=False
    )
    template_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_generated_document_id_org"),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
        tenant_fk("structure_id", "proposal_structure", ondelete="CASCADE"),
        tenant_fk("template_document_id", "document", name="fk_generated_template_org"),
        tenant_fk("created_by", "app_user", name="fk_generated_creator_org"),
    )


class GeneratedDocumentVersion(Base, TenantMixin, TimestampMixin):
    __tablename__ = "generated_document_version"

    id: Mapped[uuid.UUID] = uuid_pk()
    generated_document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    output_mode: Mapped[OutputMode] = mapped_column(
        _pg_enum(OutputMode, "output_mode"), nullable=False
    )
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generated_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    generation_trace: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_generated_doc_version_id_org"),
        UniqueConstraint(
            "generated_document_id",
            "version_no",
            "output_mode",
            name="uq_generated_document_version",
        ),
        tenant_fk("generated_document_id", "generated_document", ondelete="CASCADE"),
        tenant_fk("generated_by", "app_user", name="fk_generated_version_actor_org"),
    )


class Package(Base, TenantMixin, TimestampMixin):
    __tablename__ = "package"

    id: Mapped[uuid.UUID] = uuid_pk()
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[PackageState] = mapped_column(
        _pg_enum(PackageState, "package_state"), nullable=False
    )
    output_mode: Mapped[OutputMode] = mapped_column(
        _pg_enum(OutputMode, "output_mode"), nullable=False
    )
    supersedes_package_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    immutable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_package_id_org"),
        UniqueConstraint("bid_id", "version_no", name="uq_package_bid_version"),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
        tenant_fk("supersedes_package_id", "package", name="fk_package_supersedes_org"),
        tenant_fk("released_by", "app_user", name="fk_package_releaser_org"),
        tenant_fk("submitted_by", "app_user", name="fk_package_submitter_org"),
        tenant_fk("created_by", "app_user", name="fk_package_creator_org"),
    )


class PackageItem(Base, TenantMixin, TimestampMixin):
    __tablename__ = "package_item"

    id: Mapped[uuid.UUID] = uuid_pk()
    package_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    generated_document_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    destination: Mapped[str] = mapped_column(String(128), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_package_item_id_org"),
        UniqueConstraint(
            "package_id", "generated_document_version_id", name="uq_package_item"
        ),
        tenant_fk("package_id", "package", ondelete="CASCADE"),
        tenant_fk(
            "generated_document_version_id",
            "generated_document_version",
            ondelete="RESTRICT",
        ),
    )


class Manifest(Base, TenantMixin, TimestampMixin):
    __tablename__ = "manifest"

    id: Mapped[uuid.UUID] = uuid_pk()
    package_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generated_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_manifest_id_org"),
        UniqueConstraint("package_id", name="uq_manifest_package"),
        tenant_fk("package_id", "package", ondelete="CASCADE"),
        tenant_fk("generated_by", "app_user", name="fk_manifest_actor_org"),
    )


class ManifestEntry(Base, TenantMixin, TimestampMixin):
    __tablename__ = "manifest_entry"

    id: Mapped[uuid.UUID] = uuid_pk()
    manifest_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    confidentiality: Mapped[ConfidentialityClass] = mapped_column(
        _pg_enum(ConfidentialityClass, "confidentiality_class"), nullable=False
    )
    approval_status: Mapped[ApprovalStatus] = mapped_column(
        _pg_enum(ApprovalStatus, "approval_status"), nullable=False
    )
    destination: Mapped[str] = mapped_column(String(128), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_manifest_entry_id_org"),
        tenant_fk("manifest_id", "manifest", ondelete="CASCADE"),
    )


class ValidationRun(Base, TenantMixin, TimestampMixin):
    __tablename__ = "validation_run"

    id: Mapped[uuid.UUID] = uuid_pk()
    package_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[ValidationRunStatus] = mapped_column(
        _pg_enum(ValidationRunStatus, "validation_run_status"), nullable=False
    )
    ruleset_version: Mapped[str] = mapped_column(String(32), nullable=False)
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_validation_run_id_org"),
        tenant_fk("package_id", "package", ondelete="CASCADE"),
        tenant_fk("triggered_by", "app_user", name="fk_validation_actor_org"),
    )


class ValidationFinding(Base, TenantMixin, TimestampMixin):
    __tablename__ = "validation_finding"

    id: Mapped[uuid.UUID] = uuid_pk()
    validation_run_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    check_code: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[FindingSeverity] = mapped_column(
        _pg_enum(FindingSeverity, "finding_severity"), nullable=False
    )
    waivability: Mapped[FindingWaivability] = mapped_column(
        _pg_enum(FindingWaivability, "finding_waivability"), nullable=False
    )
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_validation_finding_id_org"),
        tenant_fk("validation_run_id", "validation_run", ondelete="CASCADE"),
        tenant_fk("resolved_by", "app_user", name="fk_finding_resolver_org"),
        Index("ix_finding_gate", "org_id", "validation_run_id", "severity", "waivability"),
    )


class ReleaseException(Base, TenantMixin, TimestampMixin):
    __tablename__ = "release_exception"

    id: Mapped[uuid.UUID] = uuid_pk()
    finding_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[ExceptionState] = mapped_column(
        _pg_enum(ExceptionState, "exception_state"), nullable=False
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str] = mapped_column(String(128), nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_release_exception_id_org"),
        UniqueConstraint("finding_id", name="uq_release_exception_finding"),
        tenant_fk("finding_id", "validation_finding", ondelete="CASCADE"),
        tenant_fk("requested_by", "app_user", name="fk_exception_requester_org"),
        tenant_fk("approved_by", "app_user", name="fk_exception_approver_org"),
    )


class Outcome(Base, TenantMixin, TimestampMixin):
    __tablename__ = "outcome"

    id: Mapped[uuid.UUID] = uuid_pk()
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    result: Mapped[OutcomeResult] = mapped_column(
        _pg_enum(OutcomeResult, "outcome_result"), nullable=False
    )
    technical_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    commercial_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    award_value: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    competitor: Mapped[str | None] = mapped_column(String(500), nullable=True)
    loss_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluator_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_outcome_id_org"),
        UniqueConstraint("bid_id", name="uq_outcome_bid"),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
        tenant_fk("recorded_by", "app_user", name="fk_outcome_recorder_org"),
    )


class KnowledgeUpdateCandidate(Base, TenantMixin, TimestampMixin):
    __tablename__ = "knowledge_update_candidate"

    id: Mapped[uuid.UUID] = uuid_pk()
    origin_type: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    proposed_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[CandidateState] = mapped_column(
        _pg_enum(CandidateState, "candidate_state"), nullable=False
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_knowledge_candidate_id_org"),
        tenant_fk("reviewed_by", "app_user", name="fk_candidate_reviewer_org"),
        tenant_fk("created_by", "app_user", name="fk_candidate_creator_org"),
        Index("ix_candidate_queue", "org_id", "state", "target_type"),
    )


class AmendmentImpact(Base, TenantMixin, TimestampMixin):
    __tablename__ = "amendment_impact"

    id: Mapped[uuid.UUID] = uuid_pk()
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    amendment_document_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    affected_requirement_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False
    )
    affected_assessment_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False
    )
    affected_task_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False
    )
    affected_artifact_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False
    )
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    analyzed_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    trace: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_amendment_impact_id_org"),
        UniqueConstraint(
            "amendment_document_version_id", name="uq_amendment_impact_version"
        ),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
        tenant_fk(
            "amendment_document_version_id",
            "document_version",
            name="fk_amendment_impact_version_org",
        ),
        tenant_fk("analyzed_by", "app_user", name="fk_amendment_impact_actor_org"),
    )
