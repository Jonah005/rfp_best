"""Structured contract for joining approved answers into report prose."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EditedParagraph(BaseModel):
    text: str = Field(min_length=1, max_length=6000)
    # BODY restates approved statements and must cite them. CONNECTIVE only
    # orients the reader (an opening line, a link to the next topic) and may
    # carry no figures, names or promises.
    kind: Literal["BODY", "CONNECTIVE"] = "BODY"
    source_refs: list[str] = Field(default_factory=list, max_length=120)


class ChapterEdit(BaseModel):
    paragraphs: list[EditedParagraph] = Field(min_length=1, max_length=80)


__all__ = ["ChapterEdit", "EditedParagraph"]
