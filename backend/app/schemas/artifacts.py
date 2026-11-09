from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class StatementIn(BaseModel):
    statement_class: str
    text: str = Field(min_length=1)
    source_locator: dict[str, Any] | None = None
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)
    commitment_owner_user_id: uuid.UUID | None = None
    required_approver_role: str | None = None
    # Carried through an edit so a write-up statement stays in its section.
    section: str | None = Field(default=None, max_length=200)
    diagram_id: uuid.UUID | None = None
    diagram_version_no: int | None = None
    paragraph_no: int | None = None
    requirement_codes: list[str] | None = None

    @model_validator(mode="after")
    def validate_backing_shape(self) -> StatementIn:
        if self.statement_class == "EVIDENCE_BACKED_FACT" and not self.evidence_ids:
            raise ValueError("EVIDENCE_BACKED_FACT requires at least one evidence id")
        if self.statement_class == "TENDER_DERIVED" and self.source_locator is None:
            raise ValueError("TENDER_DERIVED requires a source locator")
        if self.statement_class == "PROPOSED_COMMITMENT" and not self.required_approver_role:
            raise ValueError("PROPOSED_COMMITMENT requires an approver role")
        return self


class ArtifactCreate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)


class VersionCreate(BaseModel):
    statements: list[StatementIn] = Field(min_length=1)
    change_reason: str | None = None


class GenerateDraftIn(BaseModel):
    instruction: str | None = Field(default=None, max_length=4000)


class SubmitArtifact(BaseModel):
    approval_roles: list[str] = Field(default_factory=lambda: ["REVIEWER"], min_length=1)
    note: str | None = None


class ApprovalDecisionIn(BaseModel):
    decision: str
    comment: str | None = None


class ReviewIn(BaseModel):
    entity_type: str = Field(min_length=1, max_length=64)
    entity_id: uuid.UUID
    decision: str
    comment: str | None = None


class CommitmentDecisionIn(BaseModel):
    decision: str = "APPROVED"
    comment: str | None = None


class AssumptionUpdate(BaseModel):
    status: str
    resolution: str | None = None


class StatementOut(ORMModel):
    id: uuid.UUID
    artifact_version_id: uuid.UUID
    order_index: int
    statement_class: str
    text: str
    source_locator: dict[str, Any] | None = None
    author_user_id: uuid.UUID | None = None
    generated_by: str | None = None
    approval_state: str
    approved_by: uuid.UUID | None = None
    approved_at: datetime | None = None
    commitment_id: uuid.UUID | None = None
    assumption_id: uuid.UUID | None = None
    evidence_ids: list[uuid.UUID] | None = None
    # Carried so an editor can save a commitment statement back unchanged.
    required_approver_role: str | None = None
    # A write-up section: its heading, the diagram it explains and which version of it
    # it was written from. `out_of_date` is worked out on every read, never stored.
    section: str | None = None
    diagram_id: uuid.UUID | None = None
    diagram_version_no: int | None = None
    paragraph_no: int | None = None
    requirement_codes: list[str] | None = None
    out_of_date: bool = False
    out_of_date_reason: str | None = None


class VersionOut(ORMModel):
    id: uuid.UUID
    artifact_id: uuid.UUID
    version_no: int
    state: str
    rendered_text: str | None = None
    approved_by: uuid.UUID | None = None
    approved_at: datetime | None = None
    supersedes_version_id: uuid.UUID | None = None
    revalidation_reason: str | None = None
    created_by: uuid.UUID | None = None
    created_at: datetime
    statements: list[StatementOut] = Field(default_factory=list)


class ArtifactOut(ORMModel):
    id: uuid.UUID
    bid_id: uuid.UUID
    task_id: uuid.UUID
    title: str
    state: str
    current_version_id: uuid.UUID | None = None
    approved_version_id: uuid.UUID | None = None
    revalidation_reason: str | None = None
    created_at: datetime
    current_version: VersionOut | None = None


class ApprovalOut(ORMModel):
    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    step_no: int
    required_role: str
    required_department_id: uuid.UUID | None = None
    approver_user_id: uuid.UUID | None = None
    state: str
    decided_at: datetime | None = None
    comment: str | None = None
    is_blocking: bool


class ReviewOut(ORMModel):
    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    reviewer_user_id: uuid.UUID
    decision: str
    comment: str | None = None
    reviewed_at: datetime


class CommitmentOut(ORMModel):
    id: uuid.UUID
    bid_id: uuid.UUID
    text: str
    owner_user_id: uuid.UUID | None = None
    required_approver_role: str
    approval_status: str
    approved_by: uuid.UUID | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None


class AssumptionOut(ORMModel):
    id: uuid.UUID
    bid_id: uuid.UUID
    text: str
    raised_by: uuid.UUID | None = None
    status: str
    resolution: str | None = None
    resolved_by: uuid.UUID | None = None
    resolved_at: datetime | None = None
