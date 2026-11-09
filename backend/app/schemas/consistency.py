"""Structured contract for checking approved answers against each other."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Conflict(BaseModel):
    statement_refs: list[str] = Field(min_length=2, max_length=6)
    kind: Literal["TIMELINE", "QUANTITY", "SCOPE", "STAFFING", "NAMING", "OTHER"] = "OTHER"
    explanation: str = Field(min_length=1, max_length=400)
    # The two different values given for the one fact, quoted. The caller drops
    # a "conflict" whose two values are the same, or that the model itself says
    # can both be true.
    value_a: str = Field(default="", max_length=200)
    value_b: str = Field(default="", max_length=200)
    cannot_both_be_true: bool = True


class ConsistencyResult(BaseModel):
    conflicts: list[Conflict] = Field(default_factory=list, max_length=20)


__all__ = ["Conflict", "ConsistencyResult"]
