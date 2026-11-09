from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models.enums import StatementClass


class DraftStatementProposal(BaseModel):
    statement_class: StatementClass
    text: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    tender_source_ref: str | None = None
    required_approver_role: str | None = None
    # For a write-up: which paragraph of the section this sentence belongs to (1, 2, ...).
    paragraph: int | None = Field(default=None, ge=1, le=40)
    # For a write-up: codes (R-019 ...) of the tender requirements this sentence answers.
    answers: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def backing_matches_type(self) -> DraftStatementProposal:
        if self.statement_class is StatementClass.EVIDENCE_BACKED_FACT:
            if not self.evidence_refs:
                raise ValueError("EVIDENCE_BACKED_FACT requires evidence_refs")
            if self.tender_source_ref:
                raise ValueError("a company fact cannot use a tender source as evidence")
        elif self.statement_class is StatementClass.TENDER_DERIVED:
            if not self.tender_source_ref:
                raise ValueError("TENDER_DERIVED requires tender_source_ref")
            if self.evidence_refs:
                raise ValueError("a tender-derived statement cannot cite company evidence")
        elif self.statement_class is StatementClass.PROPOSED_COMMITMENT:
            if not self.required_approver_role:
                raise ValueError("PROPOSED_COMMITMENT requires required_approver_role")
        elif self.statement_class is StatementClass.DIAGRAM_DESCRIPTION:
            if self.evidence_refs or self.tender_source_ref:
                raise ValueError("a diagram description cites nothing; it describes the picture")
        elif self.statement_class is StatementClass.HUMAN_PROVIDED_INPUT:
            raise ValueError("the model cannot propose HUMAN_PROVIDED_INPUT")
        return self


class DraftStatementResult(BaseModel):
    statements: list[DraftStatementProposal] = Field(min_length=1, max_length=100)


class FormParagraph(BaseModel):
    text: str = Field(min_length=1, max_length=1500)
    # True when the paragraph promises something the company must stand behind.
    commitment: bool = False
    required_approver_role: str | None = None


class FormDocumentResult(BaseModel):
    """A prescribed document (letter, declaration, undertaking, list) written out in full."""

    paragraphs: list[FormParagraph] = Field(min_length=1, max_length=30)


class ConsistencyProposal(BaseModel):
    kind: Literal["CONTRADICTION", "TERMS_DEVIATION"]
    first_quote: str = Field(min_length=12, max_length=400)
    second_quote: str = Field(min_length=12, max_length=400)
    issue: str = Field(min_length=1, max_length=500)


class ConsistencyResult(BaseModel):
    findings: list[ConsistencyProposal] = Field(default_factory=list, max_length=30)


class AdjudicatedFinding(BaseModel):
    id: int
    verdict: Literal["CONFIRMED", "AMBIGUOUS", "REJECTED"]
    reason: str = Field(default="", max_length=300)


class AdjudicationResult(BaseModel):
    verdicts: list[AdjudicatedFinding] = Field(default_factory=list, max_length=40)


CATEGORIES = Literal[
    "SOLUTION_DESIGN", "SECURITY_COMPLIANCE", "PROJECT_DELIVERY", "STAFFING", "COMMERCIAL",
    "LEGAL_CONTRACTUAL", "ELIGIBILITY", "SUBMISSION_ADMIN",
]


class DesignGap(BaseModel):
    question: str = Field(min_length=10, max_length=500)
    category: CATEGORIES
    # What the answer is needed for. CLARITY: helpful, but a compliant offer can be made without it.
    blocks: Literal["DESIGN", "PRICING", "SUBMISSION", "CLARITY"]
    # One specific sentence: what goes wrong for the bid without the answer.
    impact: str = Field(min_length=5, max_length=300)
    # Words (with synonyms) that would appear in the tender if it answered this.
    topic_terms: list[str] = Field(min_length=1, max_length=8)
    # The tender wording that is blank or too vague, copied exactly; null when the tender is silent.
    basis_quote: str | None = Field(default=None, max_length=400)


class DesignGapResult(BaseModel):
    gaps: list[DesignGap] = Field(default_factory=list, max_length=30)


class ScopeInterpretation(BaseModel):
    label: str = Field(min_length=3, max_length=160)
    quote: str = Field(min_length=12, max_length=400)


class ScopeResult(BaseModel):
    interpretations: list[ScopeInterpretation] = Field(default_factory=list, max_length=4)


class EntailmentVerdict(BaseModel):
    id: int
    answers_fully: bool
    missing_part: str | None = Field(default=None, max_length=300)


class EntailmentResult(BaseModel):
    verdicts: list[EntailmentVerdict] = Field(default_factory=list, max_length=120)


class TriageVerdict(BaseModel):
    id: int
    verdict: Literal["KEEP", "ANSWERED_IN_TENDER", "BIDDER_DECISION", "INTERNAL_CHECK"]
    quote: str | None = Field(default=None, max_length=400)


class TriageResult(BaseModel):
    verdicts: list[TriageVerdict] = Field(default_factory=list, max_length=120)


class WorkPackage(BaseModel):
    name: str = Field(min_length=3, max_length=160)
    items: list[int] = Field(min_length=1, max_length=40)


class WorkPackageResult(BaseModel):
    packages: list[WorkPackage] = Field(default_factory=list, max_length=60)


class QuestionAtoms(BaseModel):
    id: int
    # One entry per separate thing asked; a question that asks one thing returns itself.
    parts: list[str] = Field(min_length=1, max_length=6)


class AtomResult(BaseModel):
    items: list[QuestionAtoms] = Field(default_factory=list, max_length=120)
