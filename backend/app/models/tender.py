"""Opportunity, bid, and tender intelligence.

Everything here describes what the CUSTOMER asked for. Company knowledge lives
in ``app.models.knowledge`` and never appears in these tables: a requirement is
created only from tender-side text, and the separation is structural rather than
conventional.

The requirement model is deliberately thin. Identity, wording and applicability
live here; coverage and readiness live in the assessment, tasks and artifacts
that reference a requirement, so requirement state and task state cannot
contradict each other.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Computed,
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
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, tenant_fk, uuid_pk
from app.models.enums import (
    BidState,
    ClarificationIssueType,
    ClarificationSeverity,
    ClarificationStatus,
    DeliverableStatus,
    GeneratedFormat,
    ObligationOwner,
    PrecedenceMethod,
    RequirementApplicability,
    RequirementCategory,
    RequirementLifecycle,
    RiskLevel,
    ScopeConfidenceLevel,
)


def _pg_enum(python_enum, name: str) -> PGEnum:
    return PGEnum(
        python_enum,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        create_type=True,
    )


class Opportunity(Base, TenantMixin, TimestampMixin):
    """Pre-bid commercial context, kept separate from bid execution."""

    __tablename__ = "opportunity"

    id: Mapped[uuid.UUID] = uuid_pk()
    customer_name: Mapped[str] = mapped_column(String(500), nullable=False)
    tender_name: Mapped[str] = mapped_column(String(500), nullable=False)
    tender_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    bid_manager_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    account_manager_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    estimated_value: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_opportunity_id_org"),
        tenant_fk("bid_manager_user_id", "app_user", name="fk_opportunity_bid_manager_org"),
        tenant_fk("account_manager_user_id", "app_user", name="fk_opportunity_account_manager_org"),
        Index("ix_opportunity_deadline", "org_id", "deadline"),
    )


class Bid(Base, TenantMixin, TimestampMixin):
    __tablename__ = "bid"

    id: Mapped[uuid.UUID] = uuid_pk()
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, unique=True
    )
    state: Mapped[BidState] = mapped_column(_pg_enum(BidState, "bid_state"), nullable=False)
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)
    submission_instructions: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    tender_facts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    no_bid_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_bid_id_org"),
        tenant_fk("opportunity_id", "opportunity", ondelete="RESTRICT"),
        Index("ix_bid_state", "org_id", "state"),
    )


class ClauseResolution(Base, TenantMixin, TimestampMixin):
    """Which document version governs one clause, and how that was decided.

    Precedence is resolved rather than assumed, so a clarification issued after
    an amendment can win one clause while the amendment still governs another.
    """

    __tablename__ = "clause_resolution"

    id: Mapped[uuid.UUID] = uuid_pk()
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # 200, not 64: the reference is qualified by its section
    # ("Business Requirements / 3") because a bare number names three
    # different clauses in a document that restarts numbering. Real headings
    # push that past 64 characters. See migration 0018 and clause_identity.
    clause_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    winning_document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    winning_block_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    competing_version_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=True
    )
    method: Mapped[PrecedenceMethod] = mapped_column(
        _pg_enum(PrecedenceMethod, "precedence_method"), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    superseded_by_resolution_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_clause_resolution_id_org"),
        # Exactly one CURRENT resolution per clause per bid. Superseded rows are
        # retained, so a partial unique index is the right shape: history stays
        # queryable while the winner stays unambiguous.
        Index(
            "uq_clause_resolution_current",
            "bid_id",
            "clause_ref",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index("ix_clause_resolution_lookup", "org_id", "bid_id", "clause_ref"),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
        tenant_fk(
            "winning_document_version_id",
            "document_version",
            name="fk_clause_resolution_version_org",
        ),
        tenant_fk("resolved_by", "app_user", name="fk_clause_resolution_actor_org"),
    )


class BlockExtraction(Base, TenantMixin, TimestampMixin):
    """A record that one prompt version read one block. Kept whatever it found.

    Extraction used to infer this from the requirements themselves: a block was
    "already decomposed" if a requirement pointed at it carrying the current
    `extracted_by`. That inference fails in exactly the case the system is meant
    to handle well -- a block correctly judged to hold no obligation.

    The executive summary of a live tender showed both halves of the damage. v6
    read it, applied the new obligation test, and rightly raised nothing. With
    no requirement to carry the stamp:

      * the rerun guard saw no v6 row and re-decomposed the block on the next
        run, and every run after that, never converging;
      * the retire sweep could not tell "v6 read this and found nothing" from
        "v6 never read this", so it protected four v5 rows of prose that v6 had
        already judged not to be requirements.

    A record of the READING, separate from its yield, settles both. `found`
    carries how many requirements the pass produced, so a block that produced
    nothing is still distinguishable from one never attempted: the row exists
    either way.

    Scoped by bid as well as document version, because two bids may share a
    document and must not consume each other's extraction state.
    """

    __tablename__ = "block_extraction"

    id: Mapped[uuid.UUID] = uuid_pk()
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    document_version_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # The character span rather than the block id: it is the coordinate
    # requirement identity is keyed on, so the two agree by construction.
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    # "llm:<call type>@<prompt version>" -- the same string RequirementVersion
    # stores, so the two compare without either being parsed.
    extracted_by: Mapped[str] = mapped_column(String(128), nullable=False)
    found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_block_extraction_id_org"),
        # One row per (bid, block, prompt version). Re-reading a block with the
        # same prompt updates that row rather than accumulating a history
        # nothing queries.
        UniqueConstraint(
            "bid_id",
            "document_version_id",
            "char_start",
            "char_end",
            "extracted_by",
            name="uq_block_extraction_pass",
        ),
        Index(
            "ix_block_extraction_lookup",
            "org_id",
            "bid_id",
            "document_version_id",
            "extracted_by",
        ),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
        tenant_fk(
            "document_version_id",
            "document_version",
            name="fk_block_extraction_version_org",
        ),
    )


class Requirement(Base, TenantMixin, TimestampMixin):
    """Stable identity. Wording lives in RequirementVersion."""

    __tablename__ = "requirement"

    id: Mapped[uuid.UUID] = uuid_pk()
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[RequirementCategory] = mapped_column(
        _pg_enum(RequirementCategory, "requirement_category"), nullable=False
    )
    subcategory: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Who the clause binds. Defaults to BIDDER for every row that existed before
    # this column, which is the conservative reading: an unclassified obligation
    # stays the bidder's problem rather than being quietly excused.
    obligation_owner: Mapped[ObligationOwner] = mapped_column(
        _pg_enum(ObligationOwner, "obligation_owner"),
        nullable=False,
        server_default=ObligationOwner.BIDDER.value,
    )
    is_mandatory: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_scored: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    evaluation_weight: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    is_conditional: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # The tender asked for this to be priced on its own line ("Quote Separate").
    # Read from the clause's own words, never from the model: dropping it puts a
    # separately-quoted item inside the fixed price.
    priced_separately: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    condition_expression: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    applicability: Mapped[RequirementApplicability] = mapped_column(
        _pg_enum(RequirementApplicability, "requirement_applicability"), nullable=False
    )
    applicability_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    lifecycle_state: Mapped[RequirementLifecycle] = mapped_column(
        _pg_enum(RequirementLifecycle, "requirement_lifecycle"), nullable=False
    )
    # Who accepted that this is what the customer asked for, and when. See 0024.
    #
    # NULL means nobody has. That is the honest state for every requirement
    # extracted before this existed: the lifecycle value said CONFIRMED was
    # possible and nothing ever required it, so a machine reading of a clause
    # became a task, a draft and an approved artifact without a person ever
    # agreeing it was read correctly.
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set ONLY when this identity is replaced by a different one (a split or a
    # merge). A wording amendment creates a new version and leaves the
    # requirement CONFIRMED.
    superseded_by_requirement_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    risk: Mapped[RiskLevel] = mapped_column(_pg_enum(RiskLevel, "risk_level"), nullable=False)
    recommended_department_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_requirement_id_org"),
        UniqueConstraint("bid_id", "code", name="uq_requirement_bid_code"),
        CheckConstraint(
            "(is_conditional = false) OR (condition_expression IS NOT NULL)",
            name="conditional_requires_expression",
        ),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
        tenant_fk(
            "superseded_by_requirement_id", "requirement", name="fk_requirement_superseded_org"
        ),
        tenant_fk("recommended_department_id", "department", name="fk_requirement_department_org"),
        tenant_fk("confirmed_by", "app_user", name="fk_requirement_confirmed_by_org"),
        Index("ix_requirement_scope", "org_id", "bid_id", "category", "applicability"),
        Index("ix_requirement_mandatory", "org_id", "bid_id", "is_mandatory"),
    )


class RequirementVersion(Base, TenantMixin, TimestampMixin):
    __tablename__ = "requirement_version"

    id: Mapped[uuid.UUID] = uuid_pk()
    requirement_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    requirement_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_requirement: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_document_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    source_locator: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Qualified, matching clause_resolution.clause_ref. See migration 0018.
    clause_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Which precedence decision this wording came from, so a requirement can be
    # traced back to the clause that actually won.
    clause_resolution_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_by: Mapped[str] = mapped_column(String(128), nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # How many the clause demands, of what, and who read it. See migration 0023.
    #
    # NULL means "not judged", never "none required": a requirement nobody has
    # read for quantity must not pass as one the tender asks for zero of. A
    # count of 1 is never stored -- that is the ordinary case, and recording it
    # would make every requirement look like a counting obligation.
    required_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    counted_noun: Mapped[str | None] = mapped_column(String(200), nullable=True)
    quantity_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    text_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', requirement_text)", persisted=True)
    )

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_requirement_version_id_org"),
        UniqueConstraint("requirement_id", "version_no", name="uq_requirement_version_no"),
        tenant_fk("requirement_id", "requirement", ondelete="CASCADE"),
        tenant_fk(
            "source_document_version_id",
            "document_version",
            name="fk_requirement_version_source_org",
        ),
        tenant_fk(
            "clause_resolution_id", "clause_resolution", name="fk_requirement_version_res_org"
        ),
        Index("ix_requirement_version_tsv", "text_tsv", postgresql_using="gin"),
    )


class ClarificationIssue(Base, TenantMixin, TimestampMixin):
    """A defect in the tender that a human may need to ask the customer about."""

    __tablename__ = "clarification_issue"

    id: Mapped[uuid.UUID] = uuid_pk()
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    issue_type: Mapped[ClarificationIssueType] = mapped_column(
        _pg_enum(ClarificationIssueType, "clarification_issue_type"), nullable=False
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source_a: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source_b: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    severity: Mapped[ClarificationSeverity] = mapped_column(
        _pg_enum(ClarificationSeverity, "clarification_severity"), nullable=False
    )
    blocks_generation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    recommended_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ClarificationStatus] = mapped_column(
        _pg_enum(ClarificationStatus, "clarification_status"), nullable=False
    )
    client_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Why this finding is no longer open. A closure with no reason reads as the
    # system quietly dropping a question: the live bid closed 47 coverage gaps
    # and recorded nothing about any of them.
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detected_by: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)

    # Set when a model judged this to be another open question in other words.
    #
    # The fingerprint above dedupes on the model's own prose, so a clause
    # re-read in a different batch window produces a new row saying the same
    # thing: 234 open ambiguity findings on this tender carried 79 distinct
    # questions, and several of those 79 were still one question apart.
    #
    # The row stays. "Asked four ways" is worth seeing, and a merge a reviewer
    # disagrees with has to be visible to be argued with. What it loses is its
    # requirement links, so it neither crowds a matrix row nor gates it.
    #
    # See migration 0020. The stamp is the usual llm:<name>@<version>, so the
    # verdict can be traced to the prompt that made it, and re-judged when that
    # prompt changes.
    restates_clarification_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    equivalence_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    equivalence_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Whose question this is. The department is where the answer lives (legal
    # terms to Legal, deadlines to Bid Management); the owner is the person who
    # asks the customer and records the reply, the department's head until
    # someone passes it on. Both nullable: older rows are filled by the seed.
    department_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    # How the department was chosen: "category" (from the linked requirements),
    # "model" (the AI, with a reason), "rules" (a word list, when nothing else
    # applied) or "person" (someone moved it). A person's choice is never undone.
    routing_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    routing_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How the recorded assumption came about -- "person" (written or edited by
    # someone), "model" (the AI's wording, used as drafted) or "department" (a
    # colleague's written reply) -- and who recorded it.
    answer_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    answered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    # Whether the response may rest on this assumption. The person decides; only
    # what is included reaches the drafting.
    include_in_response: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_clarification_issue_id_org"),
        tenant_fk("answered_by_user_id", "app_user", name="fk_clarification_answered_by_org"),
        # Re-running analysis must not pile up duplicates of the same finding.
        UniqueConstraint("bid_id", "fingerprint", name="uq_clarification_fingerprint"),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
        tenant_fk(
            "restates_clarification_id",
            "clarification_issue",
            name="fk_clarification_restates_org",
        ),
        Index("ix_clarification_open", "org_id", "bid_id", "severity", "status"),
        tenant_fk("department_id", "department", name="fk_clarification_department_org"),
        tenant_fk("owner_user_id", "app_user", name="fk_clarification_owner_org"),
        Index("ix_clarification_owner", "org_id", "owner_user_id", "status"),
    )


class ClarificationRequirement(Base, TenantMixin):
    __tablename__ = "clarification_requirement"

    id: Mapped[uuid.UUID] = uuid_pk()
    clarification_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    requirement_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("clarification_id", "requirement_id", name="uq_clarification_requirement"),
        tenant_fk("clarification_id", "clarification_issue", ondelete="CASCADE"),
        tenant_fk("requirement_id", "requirement", ondelete="CASCADE"),
    )


class Deliverable(Base, TenantMixin, TimestampMixin):
    """Something the tender requires us to submit."""

    __tablename__ = "deliverable"

    id: Mapped[uuid.UUID] = uuid_pk()
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    deliverable_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    required_format: Mapped[GeneratedFormat | None] = mapped_column(
        _pg_enum(GeneratedFormat, "generated_format"), nullable=True
    )
    required_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[DeliverableStatus] = mapped_column(
        _pg_enum(DeliverableStatus, "deliverable_status"), nullable=False
    )
    source_locator: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    extracted_by: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)

    # Several clauses name the same submission with a little more detail each
    # time, and each is fingerprinted on its own wording, so the sheet carried
    # six financial proposals and four sets of terms and conditions.
    #
    # Same shape as `ClarificationIssue.restates_clarification_id` and
    # `Requirement.superseded_by_requirement_id`, for the same reason: a
    # duplicate is NOT "not applicable". The tender really does require that
    # submission; this row simply names it less completely. Retiring one by
    # status asserts the obligation went away, and left 15 rows reading
    # "mandatory" and "not applicable" at the same time. See migration 0022.
    duplicate_of_deliverable_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    # 'llm:deliverable_equivalence@vN' or 'human:<user_id>'.
    equivalence_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    equivalence_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_deliverable_id_org"),
        UniqueConstraint("bid_id", "fingerprint", name="uq_deliverable_fingerprint"),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
        tenant_fk("owner_user_id", "app_user", name="fk_deliverable_owner_org"),
        tenant_fk(
            "duplicate_of_deliverable_id",
            "deliverable",
            name="fk_deliverable_duplicate_of_org",
        ),
        Index("ix_deliverable_bid", "org_id", "bid_id", "status"),
        Index("ix_deliverable_duplicate_of", "org_id", "bid_id", "duplicate_of_deliverable_id"),
    )


class DeliverableRequirement(Base, TenantMixin):
    __tablename__ = "deliverable_requirement"

    id: Mapped[uuid.UUID] = uuid_pk()
    deliverable_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    requirement_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("deliverable_id", "requirement_id", name="uq_deliverable_requirement"),
        tenant_fk("deliverable_id", "deliverable", ondelete="CASCADE"),
        tenant_fk("requirement_id", "requirement", ondelete="CASCADE"),
    )


class ScopeConfidence(Base, TenantMixin, TimestampMixin):
    """How well we understand the TENDER. Never how well we can answer it.

    Company evidence availability is deliberately not an input: that is a
    question about us, not about the tender, and it belongs to bid readiness.
    Conflating the two would let a well-understood tender we are poorly
    positioned for masquerade as an unclear one.
    """

    __tablename__ = "scope_confidence"

    id: Mapped[uuid.UUID] = uuid_pk()
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, unique=True)
    level: Mapped[ScopeConfidenceLevel] = mapped_column(
        _pg_enum(ScopeConfidenceLevel, "scope_confidence_level"), nullable=False
    )
    per_category: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    signals: Mapped[dict] = mapped_column(JSONB, nullable=False)
    blocking_reasons: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    detailed_drafting_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    computed_by: Mapped[str] = mapped_column(String(128), nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_scope_confidence_id_org"),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
    )
