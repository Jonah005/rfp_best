from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DeriveStructureIn(BaseModel):
    source_document_version_id: uuid.UUID | None = None


class MapArtifactIn(BaseModel):
    artifact_id: uuid.UUID
    order_index: int = 1
    inclusion_mode: str = "FULL"
    excerpt_statement_ids: list[uuid.UUID] = Field(default_factory=list)


class AssembleIn(BaseModel):
    output_mode: str = "REVIEW"
    # Print the rewritten report prose where a checked rewrite exists, and the
    # approved wording everywhere else.
    join: bool = False


class JoinReportIn(BaseModel):
    limit: int = Field(default=1, ge=1, le=5)
    force: bool = False
    retry_failed: bool = False


class JoinReportOut(BaseModel):
    edited: int
    failed: int
    remaining: int
    summary: str
    reasons: list[str] = Field(default_factory=list)


class ConsolidateOut(BaseModel):
    consolidated: int
    failed: int
    remaining: int
    assumptions_register: str
    reasons: list[str] = Field(default_factory=list)


class ExtrasIn(BaseModel):
    force: bool = False


class ExtrasOut(BaseModel):
    financial: str
    matrix: str
    reasons: list[str] = Field(default_factory=list)


class ExceptionRequestIn(BaseModel):
    justification: str = Field(min_length=3)


class DecisionIn(BaseModel):
    decision: str
    comment: str | None = None


class NoteIn(BaseModel):
    note: str | None = None


class ArchiveIn(BaseModel):
    reason: str = Field(min_length=3)


class OutcomeIn(BaseModel):
    result: str
    technical_score: float | None = None
    commercial_score: float | None = None
    award_value: float | None = None
    competitor: str | None = None
    loss_reason: str | None = None
    evaluator_feedback: str | None = None


class AmendmentImpactIn(BaseModel):
    amendment_document_version_id: uuid.UUID
    affected_requirement_ids: list[uuid.UUID] = Field(min_length=1)
    reason: str = Field(min_length=3)


class SectionOut(ORMModel):
    id: uuid.UUID
    structure_id: uuid.UUID
    parent_section_id: uuid.UUID | None = None
    order_index: int
    number_label: str | None = None
    title: str
    required_format: str | None = None
    is_mandatory: bool
    guidance: str | None = None
    source_locator: dict[str, Any] | None = None


class StructureOut(ORMModel):
    id: uuid.UUID
    bid_id: uuid.UUID
    derived_from_document_version_id: uuid.UUID | None = None
    derivation_method: str
    sections: list[SectionOut] = Field(default_factory=list)


class GeneratedVersionOut(ORMModel):
    id: uuid.UUID
    generated_document_id: uuid.UUID
    version_no: int
    output_mode: str
    storage_key: str
    sha256: str
    byte_size: int
    generated_at: datetime
    generation_trace: dict[str, Any]


class PackageOut(ORMModel):
    id: uuid.UUID
    bid_id: uuid.UUID
    version_no: int
    state: str
    output_mode: str
    supersedes_package_id: uuid.UUID | None = None
    released_at: datetime | None = None
    released_by: uuid.UUID | None = None
    submitted_by: uuid.UUID | None = None
    submitted_at: datetime | None = None
    immutable: bool


class ValidationRunOut(ORMModel):
    id: uuid.UUID
    package_id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    ruleset_version: str


class FindingOut(ORMModel):
    id: uuid.UUID
    validation_run_id: uuid.UUID
    check_code: str
    severity: str
    waivability: str
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    message: str
    detail: dict[str, Any]
    detected_at: datetime
    resolved_at: datetime | None = None


class ExceptionOut(ORMModel):
    id: uuid.UUID
    finding_id: uuid.UUID
    requested_by: uuid.UUID
    justification: str
    requested_at: datetime
    state: str
    approved_by: uuid.UUID | None = None
    approved_at: datetime | None = None
    decision_comment: str | None = None


class OutcomeOut(ORMModel):
    id: uuid.UUID
    bid_id: uuid.UUID
    result: str
    technical_score: float | None = None
    commercial_score: float | None = None
    award_value: float | None = None
    competitor: str | None = None
    loss_reason: str | None = None
    evaluator_feedback: str | None = None
    recorded_by: uuid.UUID | None = None
    recorded_at: datetime


class CandidateOut(ORMModel):
    id: uuid.UUID
    origin_type: str
    origin_entity_id: uuid.UUID
    target_type: str
    target_id: uuid.UUID | None = None
    proposed_payload: dict[str, Any]
    rationale: str
    state: str
    reviewed_by: uuid.UUID | None = None
    reviewed_at: datetime | None = None
    review_comment: str | None = None


class AmendmentImpactOut(ORMModel):
    id: uuid.UUID
    bid_id: uuid.UUID
    amendment_document_version_id: uuid.UUID
    affected_requirement_ids: list[uuid.UUID]
    affected_assessment_ids: list[uuid.UUID]
    affected_task_ids: list[uuid.UUID]
    affected_artifact_ids: list[uuid.UUID]
    analyzed_at: datetime
    analyzed_by: uuid.UUID | None = None
    trace: dict[str, Any]
