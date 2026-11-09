"""Structured contract for "what kind of company document is this".

The model proposes one type from the list it is shown; `document_classification`
checks the name against the enum and falls back to OTHER rather than inventing a
type. A human filing decision always outranks this.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DocumentClassificationResult(BaseModel):
    document_type: str = Field(default="OTHER", max_length=64)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = Field(default="", max_length=400)


__all__ = ["DocumentClassificationResult"]
