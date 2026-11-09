"""Structured contracts for tender-side model calls.

Every one of these describes what the CUSTOMER asked for. None of them may be
given company-side content, and none of them writes anything: a deterministic
service accepts or rejects each proposal through the normal write path.

Source provenance is mandatory everywhere. A proposal that cannot say which
clause block it came from cannot be traced, and an untraceable requirement is
worse than a missing one because it looks answered.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.models.enums import (
    ClarificationIssueType,
    ClarificationSeverity,
    GeneratedFormat,
    ObligationOwner,
    RequirementCategory,
    RiskLevel,
)

# --- precedence -------------------------------------------------------------


class PrecedenceHint(BaseModel):
    """A precedence rule the tender states about itself."""

    rule_text: str = Field(min_length=1, max_length=2000)
    affected_clause_refs: list[str] = Field(default_factory=list)
    # Document types the rule elevates, most authoritative first.
    priority_document_types: list[str] = Field(default_factory=list)
    source_block_ordinal: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)


class PrecedenceHintResult(BaseModel):
    hints: list[PrecedenceHint] = Field(default_factory=list)
    affected_clause_refs: list[str] = Field(default_factory=list)


# --- tender facts -----------------------------------------------------------


class TenderContact(BaseModel):
    name: str | None = None
    role: str | None = None
    email: str | None = None
    phone: str | None = None
    purpose: str | None = Field(default=None, description="technical, commercial, general")
    source_block_ordinal: int = Field(ge=0)


class EvaluationCriterion(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    weight: float | None = Field(default=None, ge=0)
    weight_unit: Literal["PERCENT", "POINTS", "RATIO"] | None = None
    notes: str | None = None
    source_block_ordinal: int = Field(ge=0)


class SubmissionRule(BaseModel):
    rule_type: str = Field(min_length=1, max_length=64)
    detail: str = Field(min_length=1)
    source_block_ordinal: int = Field(ge=0)


class TenderFactsResult(BaseModel):
    customer_name: str | None = None
    tender_title: str | None = None
    tender_reference: str | None = None
    submission_deadline: datetime | None = None
    contacts: list[TenderContact] = Field(default_factory=list)
    evaluation_criteria: list[EvaluationCriterion] = Field(default_factory=list)
    submission_rules: list[SubmissionRule] = Field(default_factory=list)
    currency: str | None = Field(default=None, max_length=8)
    confidence: float = Field(default=0.0, ge=0, le=1)


# --- requirements -----------------------------------------------------------


class ExtractedRequirement(BaseModel):
    """One atomic obligation.

    ``no_requirement`` is the explicit alternative to silence: a clause block
    that genuinely imposes nothing must say so, because the coverage check
    cannot distinguish "nothing here" from "the model missed it".
    """

    requirement_text: str = Field(min_length=1)
    normalized_requirement: str | None = None
    category: RequirementCategory
    subcategory: str | None = Field(default=None, max_length=128)
    # Who the clause binds. Defaults to BIDDER so that a model which omits the
    # field, or is unsure, produces the conservative answer rather than excusing
    # the bidder from an obligation by silence.
    obligation_owner: ObligationOwner = ObligationOwner.BIDDER
    is_mandatory: bool
    is_scored: bool = False
    evaluation_weight: float | None = Field(default=None, ge=0)
    risk: RiskLevel = RiskLevel.MEDIUM
    is_conditional: bool = False
    condition_text: str | None = None
    source_block_ordinal: int = Field(ge=0)
    clause_ref: str | None = Field(default=None, max_length=64)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def conditional_needs_condition(self) -> ExtractedRequirement:
        if self.is_conditional and not (self.condition_text or "").strip():
            raise ValueError("A conditional requirement must state its condition.")
        return self


class RequirementDecompositionResult(BaseModel):
    requirements: list[ExtractedRequirement] = Field(default_factory=list)
    # Block ordinals the model inspected and judged to impose no obligation.
    no_requirement_block_ordinals: list[int] = Field(default_factory=list)


# --- conditions -------------------------------------------------------------


class ConditionExpression(BaseModel):
    """A machine-evaluable form of "if subcontractors are used, ...".

    Kept deliberately small: one variable, one operator, one value. Anything the
    model cannot express this simply stays unevaluated and pending rather than
    being guessed into a shape that looks decidable.
    """

    variable: str = Field(min_length=1, max_length=128)
    operator: Literal["EQUALS", "NOT_EQUALS", "GREATER_THAN", "LESS_THAN", "EXISTS"]
    value: Any = None
    description: str = Field(min_length=1)

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ConditionExtractionResult(BaseModel):
    expression: ConditionExpression | None = None
    unparseable_reason: str | None = None

    @model_validator(mode="after")
    def one_of_expression_or_reason(self) -> ConditionExtractionResult:
        if (self.expression is None) == (self.unparseable_reason is None):
            raise ValueError(
                "Return either a condition expression or a reason it cannot be expressed."
            )
        return self


# --- ambiguity --------------------------------------------------------------


class DetectedAmbiguity(BaseModel):
    issue_type: ClarificationIssueType
    description: str = Field(min_length=1)
    severity: ClarificationSeverity
    recommended_question: str | None = None
    source_block_ordinal: int = Field(ge=0)
    # The second half of a contradiction. A contradiction with one source is an
    # assertion, not a contradiction.
    conflicting_block_ordinal: int | None = Field(default=None, ge=0)
    # The requirement codes this issue is ABOUT, chosen from the requirements
    # shown under the cited blocks. Without it a question was linked to every
    # requirement from its paragraph, and a question about a third-party licence
    # held the "develop in C#" row hostage. Validated in code against what the
    # model was shown; the model names, the code bounds.
    concerns: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def contradictions_cite_both_sides(self) -> DetectedAmbiguity:
        """An uncited contradiction is downgraded, not rejected.

        The rule itself stands: a contradiction with one source is an assertion.
        But the model sees one batch of blocks at a time, and a tender's two
        halves of a contradiction are usually pages apart, so it regularly spots
        a conflict whose other side is outside its window and cannot be cited.

        Raising here rejected the whole batch — dozens of valid findings lost
        because of one — and the repair loop could not help, since the missing
        block was never visible to begin with. Each retry instead made the model
        emit more issues until it hit the token cap and returned truncated JSON.

        So the finding is kept at its observed strength: the description is
        preserved and flagged, and the type becomes INCOMPLETE_SCOPE, which is
        what an unverifiable conflict actually is. Nothing is silent — the note
        travels with the issue to whoever reviews it.
        """
        contradiction_types = {
            ClarificationIssueType.CONTRADICTORY_DATE,
            ClarificationIssueType.CONTRADICTORY_QUANTITY,
            ClarificationIssueType.CONFLICTING_INSTRUCTION,
        }
        if self.issue_type in contradiction_types and self.conflicting_block_ordinal is None:
            self.description = (
                f"{self.description} [Reported as {self.issue_type.value} but the "
                "conflicting clause was not cited, so the conflict is unverified.]"
            )
            self.issue_type = ClarificationIssueType.INCOMPLETE_SCOPE
        return self


class AmbiguityAnalysisResult(BaseModel):
    issues: list[DetectedAmbiguity] = Field(default_factory=list)


# --- deliverables -----------------------------------------------------------


class ExtractedDeliverable(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    deliverable_type: str | None = Field(default=None, max_length=64)
    required_format: GeneratedFormat | None = None
    required_filename: str | None = Field(default=None, max_length=500)
    is_mandatory: bool = True
    deadline: datetime | None = None
    source_block_ordinal: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)


class DeliverableExtractionResult(BaseModel):
    deliverables: list[ExtractedDeliverable] = Field(default_factory=list)


__all__ = [
    "PrecedenceHint",
    "PrecedenceHintResult",
    "TenderContact",
    "EvaluationCriterion",
    "SubmissionRule",
    "TenderFactsResult",
    "ExtractedRequirement",
    "RequirementDecompositionResult",
    "ConditionExpression",
    "ConditionExtractionResult",
    "DetectedAmbiguity",
    "AmbiguityAnalysisResult",
    "ExtractedDeliverable",
    "DeliverableExtractionResult",
    "date",
]


# --- obligation owner review ------------------------------------------------


class ObligationVerdict(BaseModel):
    """One clause, re-read on its own terms."""

    clause: int = Field(ge=1, description="1-based position in the supplied list")
    owner: ObligationOwner
    reason: str = Field(default="", max_length=200)


class ObligationReviewResult(BaseModel):
    verdicts: list[ObligationVerdict] = Field(default_factory=list, max_length=120)


# --- how many the tender asks for -------------------------------------------


class QuantityVerdict(BaseModel):
    """One clause, read for a number the bidder must SHOW.

    The interesting field is `is_evidence_count`, not `required_count`. Finding
    a numeral is easy; deciding what it counts is the judgement:

        "provide three comparable projects"      -> 3, and we must show three
        "at least two testers holding OSCP"      -> 2, and we must show two
        "penetration testing of 25 applications" -> a scope of work, not
                                                    twenty-five things to prove
        "within three years"                     -> an age, not a count
        "the eight-week delivery period"         -> a duration, not a count

    Only the first two are counting obligations. The rest carry numerals that a
    regex would happily extract and a shortfall rule would then block a bid on.
    """

    clause: int = Field(ge=1, description="1-based position in the supplied list")
    is_evidence_count: bool = Field(
        default=False,
        description=(
            "True only when the clause demands a NUMBER OF SEPARATE THINGS the bidder "
            "must produce or evidence. False for durations, ages, scope, money, "
            "percentages, versions and every other numeral."
        ),
    )
    required_count: int = Field(
        default=0, ge=0, le=1000, description="How many, when is_evidence_count is true."
    )
    counted_noun: str = Field(
        default="",
        max_length=200,
        description="What is being counted, in the clause's own words: 'comparable projects'.",
    )


class QuantityReviewResult(BaseModel):
    verdicts: list[QuantityVerdict] = Field(default_factory=list, max_length=120)
