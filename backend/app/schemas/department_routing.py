"""Structured contract for "does this need another department, and which one".

The model names; `routing.route` decides. A department code the company does
not have is dropped, the asking department is never suggested to itself, and a
failed or empty call falls back to the rules that existed before.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RoutedDepartment(BaseModel):
    code: str = Field(min_length=1, max_length=60)
    question: str = Field(default="", max_length=600)
    reason: str = Field(default="", max_length=300)
    # Why THIS task needs the answer, in a sentence the colleague will read.
    need: str = Field(default="", max_length=400)


class DepartmentRoutingResult(BaseModel):
    needs_other_department: bool = False
    departments: list[RoutedDepartment] = Field(default_factory=list, max_length=5)


__all__ = ["RoutedDepartment", "DepartmentRoutingResult"]
