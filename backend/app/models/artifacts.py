"""Response artifacts, typed statements, reviews and approvals.

**There is no separate draft model.** A task's working draft *is* a
`ResponseArtifact` whose newest `ArtifactVersion` is in state `DRAFT`. The
artifact is created with the task, so the lifecycle is one continuous object
from first keystroke to released package:

    task ──► response_artifact (DRAFT) ──► artifact_version v1 (DRAFT)
      editing writes artifact_statement rows against the current DRAFT version
      submit  ──► v1 SUBMITTED_FOR_REVIEW ──► IN_REVIEW ──► APPROVED
      later edit ──► a NEW version v2 (DRAFT); v1 stays immutable forever

A parallel "draft" table would have made the approved thing and the edited thing
two different objects, which is how an approved answer quietly diverges from
what actually ships.

**Statements are typed, and the type is enforced in the database.** A
`TENDER_DERIVED` statement without a source locator, a `PROPOSED_COMMITMENT`
without a commitment row, an `ASSUMPTION` without an assumption row, or human
input without an author are all rejected by CHECK constraints -- not by
application discipline, which is exactly what fails under deadline pressure.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
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
    ApprovalStepState,
    ArtifactState,
    ArtifactVersionState,
    AssumptionStatus,
    ReviewDecision,
    StatementApproval,
    StatementClass,
)


def _pg_enum(python_enum, name: str) -> PGEnum:
    return PGEnum(
        python_enum,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        create_type=True,
    )


class ResponseArtifact(Base, TenantMixin, TimestampMixin):
    __tablename__ = "response_artifact"

    id: Mapped[uuid.UUID] = uuid_pk()
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    state: Mapped[ArtifactState] = mapped_column(
        _pg_enum(ArtifactState, "artifact_state"), nullable=False
    )
    # The newest version, whatever its state.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    # The newest APPROVED version, if any. Kept separately because "what we are
    # editing" and "what has been signed off" are different questions and the
    # answer is often different rows.
    approved_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    revalidation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_response_artifact_id_org"),
        UniqueConstraint("task_id", name="uq_response_artifact_task"),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
        tenant_fk("task_id", "task", ondelete="CASCADE"),
        Index("ix_artifact_bid_state", "org_id", "bid_id", "state"),
    )


class ArtifactRequirement(Base, TenantMixin, TimestampMixin):
    """Which requirements an artifact answers."""

    __tablename__ = "artifact_requirement"

    id: Mapped[uuid.UUID] = uuid_pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    requirement_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_artifact_requirement_id_org"),
        UniqueConstraint("artifact_id", "requirement_id", name="uq_artifact_requirement"),
        tenant_fk("artifact_id", "response_artifact", ondelete="CASCADE"),
        tenant_fk("requirement_id", "requirement", ondelete="CASCADE"),
        Index("ix_artifact_requirement_reverse", "org_id", "requirement_id"),
    )


class ArtifactVersion(Base, TenantMixin, TimestampMixin):
    """Mutable only while DRAFT. The repository exposes no update path after."""

    __tablename__ = "artifact_version"

    id: Mapped[uuid.UUID] = uuid_pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[ArtifactVersionState] = mapped_column(
        _pg_enum(ArtifactVersionState, "artifact_version_state"), nullable=False
    )
    # Assembled from the statements, for display and for generation.
    rendered_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    supersedes_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    revalidation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_artifact_version_id_org"),
        UniqueConstraint("artifact_id", "version_no", name="uq_artifact_version_no"),
        tenant_fk("artifact_id", "response_artifact", ondelete="CASCADE"),
        tenant_fk("approved_by", "app_user", name="fk_artifact_version_approver_org"),
        Index("ix_artifact_version_state", "org_id", "artifact_id", "state"),
    )


class Commitment(Base, TenantMixin, TimestampMixin):
    """A promise the company would be making. Needs a named approver."""

    __tablename__ = "commitment"

    id: Mapped[uuid.UUID] = uuid_pk()
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    required_approver_role: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_status: Mapped[ApprovalStatus] = mapped_column(
        _pg_enum(ApprovalStatus, "approval_status"), nullable=False
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_commitment_id_org"),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
        tenant_fk("owner_user_id", "app_user", name="fk_commitment_owner_org"),
        Index("ix_commitment_pending", "org_id", "bid_id", "approval_status"),
    )


class Assumption(Base, TenantMixin, TimestampMixin):
    """Something the response takes for granted.

    Tracked as its own object because an assumption that survives to submission
    must appear in the proposal's assumptions register, and one that was
    silently resolved must not.
    """

    __tablename__ = "assumption"

    id: Mapped[uuid.UUID] = uuid_pk()
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    raised_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    status: Mapped[AssumptionStatus] = mapped_column(
        _pg_enum(AssumptionStatus, "assumption_status"), nullable=False
    )
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_assumption_id_org"),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
        tenant_fk("raised_by", "app_user", name="fk_assumption_raised_by_org"),
        Index("ix_assumption_open", "org_id", "bid_id", "status"),
    )


class ArtifactStatement(Base, TenantMixin, TimestampMixin):
    """One typed claim inside a draft version."""

    __tablename__ = "artifact_statement"

    id: Mapped[uuid.UUID] = uuid_pk()
    artifact_version_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    statement_class: Mapped[StatementClass] = mapped_column(
        _pg_enum(StatementClass, "statement_class"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source_locator: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    generated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approval_state: Mapped[StatementApproval] = mapped_column(
        _pg_enum(StatementApproval, "statement_approval"), nullable=False
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    commitment_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    assumption_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    # Evidence backing an EVIDENCE_BACKED_FACT. Support is verified by the
    # validation engine against evidence currentness, which no row-level CHECK
    # can express.
    evidence_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=True
    )
    # A write-up section: the heading these statements sit under, and -- when the
    # section explains a diagram -- which diagram and which version of it. The
    # version is what lets the screen say "written from an older diagram" without
    # asking a model.
    section: Mapped[str | None] = mapped_column(String(200), nullable=True)
    diagram_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    diagram_version_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Which paragraph of its section this sentence belongs to. Sentences stay separate
    # rows so each keeps its type; the number only says how they are set as prose.
    paragraph_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Codes of the tender requirements this sentence answers (["R-019"]). Set by the
    # write-up; the coverage check reads it. Only codes of this task's requirements.
    requirement_codes: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_artifact_statement_id_org"),
        tenant_fk("artifact_version_id", "artifact_version", ondelete="CASCADE"),
        tenant_fk("commitment_id", "commitment", name="fk_statement_commitment_org"),
        tenant_fk("assumption_id", "assumption", name="fk_statement_assumption_org"),
        tenant_fk("author_user_id", "app_user", name="fk_statement_author_org"),
        # The typing rules, enforced where they cannot be forgotten.
        CheckConstraint(
            "statement_class <> 'TENDER_DERIVED' OR source_locator IS NOT NULL",
            name="chk_tender_derived_has_locator",
        ),
        CheckConstraint(
            "statement_class <> 'PROPOSED_COMMITMENT' OR commitment_id IS NOT NULL",
            name="chk_commitment_linked",
        ),
        CheckConstraint(
            "statement_class <> 'ASSUMPTION' OR assumption_id IS NOT NULL",
            name="chk_assumption_linked",
        ),
        CheckConstraint(
            "statement_class <> 'HUMAN_PROVIDED_INPUT' OR author_user_id IS NOT NULL",
            name="chk_human_input_attributed",
        ),
        Index("ix_statement_order", "org_id", "artifact_version_id", "order_index"),
        Index("ix_statement_class", "org_id", "statement_class", "approval_state"),
    )


class Review(Base, TenantMixin, TimestampMixin):
    """A reviewer's decision on something, with the comment that explains it."""

    __tablename__ = "review"

    id: Mapped[uuid.UUID] = uuid_pk()
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    decision: Mapped[ReviewDecision] = mapped_column(
        _pg_enum(ReviewDecision, "review_decision"), nullable=False
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False
    )

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_review_id_org"),
        tenant_fk("reviewer_user_id", "app_user", name="fk_review_reviewer_org"),
        Index("ix_review_entity", "org_id", "entity_type", "entity_id", "reviewed_at"),
    )


class Approval(Base, TenantMixin, TimestampMixin):
    """One step in an ordered approval chain.

    Steps are rows rather than a status field because "who still has to sign
    this, and in what order" is a question the release gate asks constantly, and
    a single status column cannot answer it.
    """

    __tablename__ = "approval"

    id: Mapped[uuid.UUID] = uuid_pk()
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    required_role: Mapped[str] = mapped_column(String(64), nullable=False)
    required_department_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    approver_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    state: Mapped[ApprovalStepState] = mapped_column(
        _pg_enum(ApprovalStepState, "approval_step_state"), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_blocking: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_approval_id_org"),
        UniqueConstraint("entity_type", "entity_id", "step_no", name="uq_approval_step"),
        tenant_fk("approver_user_id", "app_user", name="fk_approval_approver_org"),
        tenant_fk("required_department_id", "department", name="fk_approval_department_org"),
        Index("ix_approval_queue", "org_id", "approver_user_id", "state"),
        Index("ix_approval_entity", "org_id", "entity_type", "entity_id", "step_no"),
    )


class WriteupFinalization(Base, TenantMixin, TimestampMixin):
    """A write-up someone declared settled, kept exactly as it was.

    The draft it came from can be edited or replaced afterwards (a save rewrites its
    statements), so this row holds a copy of the text and the diagram versions it
    explains rather than pointing at rows that may change. The final report reads it
    back instead of re-deriving anything, and `content_hash` says whether the live
    draft still matches.
    """

    __tablename__ = "writeup_finalization"

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    artifact_version_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    finalized_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    finalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # [{section, diagram_id, diagram_version_no, diagram_title, diagram_type,
    #   statements: [{statement_class, text, evidence_ids, ...}]}]
    sections: Mapped[list] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_writeup_finalization_id_org"),
        tenant_fk("task_id", "task", ondelete="CASCADE"),
        tenant_fk("artifact_version_id", "artifact_version", ondelete="CASCADE"),
        Index("ix_writeup_finalization_task", "org_id", "task_id", "finalized_at"),
    )
