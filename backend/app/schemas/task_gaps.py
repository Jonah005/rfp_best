"""Structured contracts for the gaps in the tender that concern one task."""

from __future__ import annotations

from pydantic import BaseModel, Field


class GapJudgement(BaseModel):
    n: int = Field(ge=1)
    relevant: bool
    # What the RFP leaves out, in one formal sentence.
    missing: str = Field(default="", max_length=400)
    # Why this task needs it filled.
    why_needed: str = Field(default="", max_length=400)
    # The codes of the task's requirements this gap leaves a hole in. Empty when it
    # concerns the task as a whole rather than one requirement.
    requirement_codes: list[str] = Field(default_factory=list, max_length=12)
    # Which piece of that requirement, in a few words, taken from the requirement's own
    # wording. Empty when there is no single requirement.
    part: str = Field(default="", max_length=120)
    # When this gap says the same thing as an EARLIER gap in the list, that gap's number.
    same_as: int | None = Field(default=None, ge=1)


class GapRelevanceResult(BaseModel):
    items: list[GapJudgement] = Field(default_factory=list, max_length=60)


class GapAssumptionProposal(BaseModel):
    """A proposed wording for the assumption, and what it rests on."""

    assumption: str = Field(min_length=1, max_length=1200)
    # Which approved evidence (the [E#] refs) it rests on; empty when none.
    based_on: list[str] = Field(default_factory=list, max_length=10)
    # Plain statement of how sure this is and what a person should check.
    caution: str = Field(default="", max_length=400)


class GapRevision(GapAssumptionProposal):
    """The assumption after the person asked for a change, and what was changed."""

    what_changed: str = Field(default="", max_length=400)


class GapChatTurn(BaseModel):
    """One turn of the chat about a single missing part of the RFP."""

    reply: str = Field(min_length=1, max_length=1500)
    # The full wording of the assumption, set only when this turn drafts or changes it.
    assumption: str | None = Field(default=None, max_length=1200)
    based_on: list[str] = Field(default_factory=list, max_length=10)
    caution: str = Field(default="", max_length=400)


__all__ = [
    "GapAssumptionProposal",
    "GapChatTurn",
    "GapRevision",
    "GapJudgement",
    "GapRelevanceResult",
]
