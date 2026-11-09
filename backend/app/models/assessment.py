"""Requirement assessment: where tender requirements meet company evidence.

This is the first place the two sides of the product touch, and the direction of
travel matters. An assessment reads company knowledge to judge whether a
requirement can be answered. It must never let that knowledge alter, extend or
reinterpret the requirement itself — the requirement is what the customer asked
for, and no amount of company material changes that.

The verdict lives on the assessment, not on any evidence pair: a requirement
satisfied by six evidence items has one assessment with six links.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, tenant_fk, uuid_pk
from app.models.enums import (
    AssessmentEvidenceRole,
    AssessmentState,
    BidNoBidResult,
    BidReadiness,
    EvidenceStatus,
)


def _pg_enum(python_enum, name: str) -> PGEnum:
    return PGEnum(
        python_enum,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        create_type=True,
    )


class RequirementAssessment(Base, TenantMixin, TimestampMixin):
    __tablename__ = "requirement_assessment"

    id: Mapped[uuid.UUID] = uuid_pk()
    requirement_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    requirement_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    evidence_status: Mapped[EvidenceStatus] = mapped_column(
        _pg_enum(EvidenceStatus, "evidence_status"), nullable=False
    )
    bid_readiness: Mapped[BidReadiness] = mapped_column(
        _pg_enum(BidReadiness, "bid_readiness"), nullable=False
    )
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    assessment_state: Mapped[AssessmentState] = mapped_column(
        _pg_enum(AssessmentState, "assessment_state"), nullable=False
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    computed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    # Kept so a verdict can be traced back to the retrieval and reasoning that
    # produced it, not just to its inputs.
    reasoning_trace: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    human_adjusted_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    human_adjustment_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_requirement_assessment_id_org"),
        UniqueConstraint("requirement_id", "version", name="uq_assessment_version"),
        tenant_fk("requirement_id", "requirement", ondelete="CASCADE"),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
        tenant_fk(
            "requirement_version_id",
            "requirement_version",
            name="fk_assessment_req_version_org",
        ),
        tenant_fk("human_adjusted_by", "app_user", name="fk_assessment_adjuster_org"),
        # Exactly one CURRENT assessment per requirement; superseded versions
        # are retained so a verdict's history stays queryable.
        Index(
            "uq_assessment_current",
            "requirement_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index("ix_assessment_lookup", "org_id", "requirement_id", "is_current"),
        Index("ix_assessment_readiness", "org_id", "bid_id", "bid_readiness"),
    )


class AssessmentEvidence(Base, TenantMixin, TimestampMixin):
    __tablename__ = "assessment_evidence"

    id: Mapped[uuid.UUID] = uuid_pk()
    assessment_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    evidence_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    role: Mapped[AssessmentEvidenceRole] = mapped_column(
        _pg_enum(AssessmentEvidenceRole, "assessment_evidence_role"), nullable=False
    )
    weight: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    contribution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieval_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rerank_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)

    __table_args__ = (
        UniqueConstraint("assessment_id", "evidence_id", "role", name="uq_assessment_evidence"),
        tenant_fk("assessment_id", "requirement_assessment", ondelete="CASCADE"),
        tenant_fk("evidence_id", "evidence", ondelete="CASCADE"),
        Index("ix_assessment_evidence_evidence", "org_id", "evidence_id"),
    )


class BidNoBidDecision(Base, TenantMixin, TimestampMixin):
    """Management's decision to proceed, with the factors it rested on."""

    __tablename__ = "bid_no_bid_decision"

    id: Mapped[uuid.UUID] = uuid_pk()
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    result: Mapped[BidNoBidResult] = mapped_column(
        _pg_enum(BidNoBidResult, "bid_no_bid_result"), nullable=False
    )
    factors: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_result: Mapped[BidNoBidResult | None] = mapped_column(
        _pg_enum(BidNoBidResult, "bid_no_bid_result"), nullable=True
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    blocking_factors: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_bid_no_bid_id_org"),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
        tenant_fk("decided_by", "app_user", name="fk_bid_no_bid_actor_org"),
        Index("ix_bid_no_bid_current", "org_id", "bid_id", "is_current"),
    )
