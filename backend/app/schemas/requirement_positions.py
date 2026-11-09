"""Structured contracts for what to say about a requirement with no evidence."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PositionOption(BaseModel):
    # A few plain words naming the approach ("Commit to provide it after award").
    label: str = Field(min_length=1, max_length=120)
    # What the response would say, in a sentence or two.
    text: str = Field(min_length=1, max_length=700)
    # True when this promises something the company has not yet shown it can do.
    is_commitment: bool = True
    # What a person should check before using it.
    caution: str = Field(default="", max_length=300)


class PositionOptions(BaseModel):
    # One or two plain sentences: what was and was not found.
    summary: str = Field(default="", max_length=500)
    options: list[PositionOption] = Field(default_factory=list, max_length=3)


class FormalAnswer(BaseModel):
    text: str = Field(min_length=1, max_length=1500)


__all__ = ["FormalAnswer", "PositionOption", "PositionOptions"]
