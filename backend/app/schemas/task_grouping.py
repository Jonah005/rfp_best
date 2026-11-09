"""Structured contract for grouping one department's requirements into work packages.

The model proposes packages; `tasks.packages_from` decides. Every requirement
lands in exactly one package, codes the model was not shown are ignored, and
anything it forgot is kept in a leftover package rather than dropped -- a
requirement with no task is a question nobody will answer.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class WorkPackage(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    requirement_codes: list[str] = Field(default_factory=list, max_length=80)
    rationale: str = Field(default="", max_length=300)


class TaskGroupingResult(BaseModel):
    packages: list[WorkPackage] = Field(default_factory=list, max_length=12)


__all__ = ["WorkPackage", "TaskGroupingResult"]
