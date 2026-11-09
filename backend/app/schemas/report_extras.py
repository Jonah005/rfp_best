"""Structured contracts for the parts of a proposal no single answer writes."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PriceLine(BaseModel):
    phase: str = Field(min_length=1, max_length=80)
    role: str = Field(min_length=1, max_length=80)
    days: int = Field(ge=1, le=400)
    basis: str = Field(min_length=1, max_length=300)


class SeparateLine(BaseModel):
    """Effort for an item the tender asks to have quoted on its own line."""

    ref: str = Field(min_length=1, max_length=32)
    item: str = Field(min_length=1, max_length=60)
    role: str = Field(min_length=1, max_length=80)
    days: int = Field(ge=1, le=300)
    basis: str = Field(min_length=1, max_length=300)


class FinancialDraft(BaseModel):
    lines: list[PriceLine] = Field(min_length=3, max_length=40)
    separate: list[SeparateLine] = Field(default_factory=list, max_length=12)
    assumptions: list[str] = Field(default_factory=list, max_length=10)
    exclusions: list[str] = Field(default_factory=list, max_length=10)


class MatrixRow(BaseModel):
    factor_ref: str = Field(min_length=1, max_length=8)
    chapters: list[str] = Field(min_length=1, max_length=8)
    response: str = Field(min_length=1, max_length=600)


class EvaluationFactors(BaseModel):
    factors: list[str] = Field(min_length=2, max_length=12)


class EvaluationMatrix(BaseModel):
    rows: list[MatrixRow] = Field(min_length=1, max_length=20)


__all__ = [
    "EvaluationFactors",
    "EvaluationMatrix",
    "FinancialDraft",
    "MatrixRow",
    "PriceLine",
    "SeparateLine",
]
