"""What the model is allowed to say about a retrieved evidence excerpt.

The relevance check is deliberately the narrowest call type in the registry. It
is shown one requirement and a numbered shortlist that retrieval already
produced, and it may say only which of those numbers actually answer the
requirement. It cannot name an excerpt that was not shown to it, cannot supply
text, and cannot say anything about approval, validity or readiness — those are
computed from the database and are not the model's business.

`candidate` is an index into the shortlist, not an identifier. Ids in a prompt
invite invention: a model that hallucinates a plausible UUID produces a verdict
that looks routable and points at evidence nobody retrieved. An out-of-range
integer, by contrast, is caught by the schema.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class RelevanceVerdict(str, Enum):
    """Three answers, because two would lose the useful middle.

    SUPPORTS   - this excerpt is proof of what the requirement asks for.
    PARTIAL    - related and worth showing a human, but not proof on its own.
    UNRELATED  - retrieval was wrong; this says nothing about the requirement.
    """

    SUPPORTS = "SUPPORTS"
    PARTIAL = "PARTIAL"
    UNRELATED = "UNRELATED"


class CandidateVerdict(BaseModel):
    candidate: int = Field(ge=1, description="1-based position in the supplied shortlist")
    verdict: RelevanceVerdict
    reason: str = Field(default="", max_length=300)


class Coverage(str, Enum):
    """How much of the requirement the shortlist covers, as a reader sees it.

    Deliberately NOT the `EvidenceStatus` vocabulary. EvidenceStatus includes
    EXPIRED and HISTORICAL_VERIFIED, which are conclusions about dates the model
    is not shown and could not check; letting it answer in those terms would
    invite a model opinion about currency to sit in a field the system computes
    from `expiry_date`. Coverage is the one question it can actually answer:
    does this material say what the tender is asking for?
    """

    FULLY_COVERED = "FULLY_COVERED"
    PARTIALLY_COVERED = "PARTIALLY_COVERED"
    NOT_COVERED = "NOT_COVERED"


class RequirementFit(BaseModel):
    """The side-by-side reading: what was asked, what the company has, the gap.

    This is the part a bid manager actually wants and no similarity score can
    produce. It is prose for a human, and it is treated as prose: nothing
    downstream parses it, and `coverage` never becomes a status by itself.
    """

    asked: str = Field(default="", max_length=400)
    offered: str = Field(default="", max_length=400)
    gap: str = Field(default="", max_length=400)
    coverage: Coverage = Coverage.NOT_COVERED


class EvidenceRelevanceResult(BaseModel):
    # Bounded by the shortlist the caller sends; the caller checks the exact
    # length, and this cap stops a runaway reply before it is parsed.
    verdicts: list[CandidateVerdict] = Field(default_factory=list, max_length=32)
    fit: RequirementFit | None = None
