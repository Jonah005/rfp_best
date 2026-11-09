"""Structured contract for "which department should handle this clarification"."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClarificationRoute(BaseModel):
    n: int = Field(ge=1)
    department: str = Field(min_length=1, max_length=60)
    reason: str = Field(default="", max_length=300)


class ClarificationRoutingResult(BaseModel):
    items: list[ClarificationRoute] = Field(default_factory=list, max_length=60)


__all__ = ["ClarificationRoute", "ClarificationRoutingResult"]
