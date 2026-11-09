"""Structured contracts for turning approved answers into one edited chapter."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChapterParagraph(BaseModel):
    kind: Literal["BODY", "SUBHEADING"] = "BODY"
    text: str = Field(min_length=1, max_length=12000)
    source_refs: list[str] = Field(default_factory=list, max_length=400)


class ChapterAssumption(BaseModel):
    text: str = Field(min_length=1, max_length=3000)
    source_refs: list[str] = Field(default_factory=list, max_length=200)


class ConsolidatedChapter(BaseModel):
    paragraphs: list[ChapterParagraph] = Field(min_length=1, max_length=2000)
    assumptions: list[ChapterAssumption] = Field(default_factory=list, max_length=300)


RegisterCategory = Literal[
    "CUSTOMER_DEPENDENCY", "COMMERCIAL", "TECHNICAL_DECISION", "INTERNAL_DRAFTING"
]


class RegisterItem(BaseModel):
    text: str = Field(min_length=1, max_length=3000)
    category: RegisterCategory = "TECHNICAL_DECISION"
    source_refs: list[str] = Field(default_factory=list, max_length=400)


class AssumptionRegister(BaseModel):
    items: list[RegisterItem] = Field(min_length=1, max_length=400)


__all__ = [
    "AssumptionRegister",
    "ChapterAssumption",
    "ChapterParagraph",
    "ConsolidatedChapter",
    "RegisterItem",
]
