"""Structured contract for the customer's required proposal structure.

The model reads the tender's submission instructions and says which sections
the customer wants, in which envelope, and which approved answers belong in
each. `release.sections_from` decides: a section keeps only answers it was
shown, no answer lands in two sections, and an answer no section claimed is
still placed rather than silently left out of the proposal.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ProposedSection(BaseModel):
    number_label: str | None = Field(default=None, max_length=32)
    title: str = Field(min_length=1, max_length=300)
    stream: Literal["TECHNICAL", "COMMERCIAL"] = "TECHNICAL"
    is_mandatory: bool = True
    artifact_refs: list[str] = Field(default_factory=list, max_length=40)


class ProposalStructureResult(BaseModel):
    sections: list[ProposedSection] = Field(default_factory=list, max_length=40)


__all__ = ["ProposedSection", "ProposalStructureResult"]
