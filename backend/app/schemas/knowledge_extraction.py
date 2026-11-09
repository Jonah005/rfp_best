"""Structured contract for company-document knowledge extraction.

The model proposes graph material; it never writes it. Local ``ref`` values let
facts and relations refer to entities within one extraction batch without
inventing database ids. Every proposal names at least one source chunk ordinal
so the deterministic persistence service can create stable evidence provenance.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.models.enums import EntityType, EvidenceLinkRole, RelationPredicate


class ExtractedEntity(BaseModel):
    ref: str = Field(min_length=1, max_length=128)
    entity_type: EntityType
    canonical_name: str = Field(min_length=1, max_length=500)
    aliases: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] | None = None
    source_chunk_ordinals: list[int] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class ExtractedFact(BaseModel):
    entity_ref: str = Field(min_length=1, max_length=128)
    predicate: str = Field(min_length=1, max_length=128)
    value_text: str | None = None
    value_number: float | None = None
    value_date: date | None = None
    value_json: dict[str, Any] | None = None
    unit: str | None = Field(default=None, max_length=32)
    valid_from: date | None = None
    valid_to: date | None = None
    source_chunk_ordinals: list[int] = Field(min_length=1)
    link_role: EvidenceLinkRole = EvidenceLinkRole.SUPPORTS
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def exactly_one_value(self) -> ExtractedFact:
        values = (self.value_text, self.value_number, self.value_date, self.value_json)
        if sum(value is not None for value in values) != 1:
            raise ValueError("An extracted fact must carry exactly one value.")
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to cannot precede valid_from.")
        return self


class ExtractedRelation(BaseModel):
    subject_ref: str = Field(min_length=1, max_length=128)
    predicate: RelationPredicate
    object_ref: str = Field(min_length=1, max_length=128)
    attributes: dict[str, Any] | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    source_chunk_ordinals: list[int] = Field(min_length=1)
    link_role: EvidenceLinkRole = EvidenceLinkRole.SUPPORTS
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def valid_window(self) -> ExtractedRelation:
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to cannot precede valid_from.")
        return self


class KnowledgeExtractionResult(BaseModel):
    entities: list[ExtractedEntity] = Field(default_factory=list)
    facts: list[ExtractedFact] = Field(default_factory=list)
    relations: list[ExtractedRelation] = Field(default_factory=list)

    @model_validator(mode="after")
    def references_are_local_and_unambiguous(self) -> KnowledgeExtractionResult:
        refs = [entity.ref for entity in self.entities]
        if len(refs) != len(set(refs)):
            raise ValueError("Entity refs must be unique within an extraction batch.")
        known = set(refs)
        missing = {fact.entity_ref for fact in self.facts if fact.entity_ref not in known} | {
            ref
            for relation in self.relations
            for ref in (relation.subject_ref, relation.object_ref)
            if ref not in known
        }
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(f"Facts or relations reference unknown entity refs: {names}")
        return self
