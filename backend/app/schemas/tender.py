from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class OpportunityCreate(BaseModel):
    customer_name: str = Field(min_length=1, max_length=500)
    tender_name: str = Field(min_length=1, max_length=500)
    tender_reference: str | None = Field(default=None, max_length=200)
    bid_manager_user_id: uuid.UUID | None = None
    account_manager_user_id: uuid.UUID | None = None
    deadline: datetime | None = None
    currency: str | None = Field(default=None, max_length=3)
    region: str | None = Field(default=None, max_length=64)
    estimated_value: float | None = Field(default=None, ge=0)
    source: str | None = Field(default=None, max_length=64)


class OpportunityCorrection(BaseModel):
    """Fix what was typed when the opportunity was created. Only what is sent changes."""

    customer_name: str | None = Field(default=None, min_length=1, max_length=500)
    tender_name: str | None = Field(default=None, min_length=1, max_length=500)
    tender_reference: str | None = Field(default=None, max_length=200)
    deadline: datetime | None = None


class OpportunityOut(ORMModel):
    id: uuid.UUID
    customer_name: str
    tender_name: str
    tender_reference: str | None = None
    bid_manager_user_id: uuid.UUID | None = None
    account_manager_user_id: uuid.UUID | None = None
    deadline: datetime | None = None
    currency: str | None = None
    region: str | None = None
    estimated_value: float | None = None
    created_at: datetime


class BidOut(ORMModel):
    id: uuid.UUID
    opportunity_id: uuid.UUID
    state: str
    language: str
    tender_facts: dict[str, Any] | None = None
    submission_instructions: dict[str, Any] | None = None
    no_bid_reason: str | None = None
    created_at: datetime


class BidStateChange(BaseModel):
    to_state: str
    reason: str | None = None


class ClauseResolutionOut(ORMModel):
    id: uuid.UUID
    clause_ref: str
    winning_document_version_id: uuid.UUID | None = None
    winning_block_id: uuid.UUID | None = None
    competing_version_ids: list[uuid.UUID] | None = None
    method: str
    confidence: float
    rationale: str
    resolved_by: uuid.UUID | None = None
    resolved_at: datetime
    is_current: bool


class PrecedenceOverride(BaseModel):
    winning_document_version_id: uuid.UUID
    reason: str = Field(min_length=3)


class RequirementVersionOut(ORMModel):
    id: uuid.UUID
    version_no: int
    requirement_text: str
    normalized_requirement: str | None = None
    source_document_version_id: uuid.UUID
    source_locator: dict[str, Any]
    clause_ref: str | None = None
    clause_resolution_id: uuid.UUID | None = None
    change_reason: str | None = None
    superseded_at: datetime | None = None


class RequirementOut(ORMModel):
    id: uuid.UUID
    bid_id: uuid.UUID
    code: str
    category: str
    subcategory: str | None = None
    is_mandatory: bool
    is_scored: bool
    evaluation_weight: float | None = None
    is_conditional: bool
    # The tender asked for this item to be quoted on its own line.
    priced_separately: bool = False
    condition_expression: dict[str, Any] | None = None
    applicability: str
    applicability_reason: str | None = None
    lifecycle_state: str
    risk: str
    current_version_id: uuid.UUID | None = None


class RequirementDetail(BaseModel):
    requirement: RequirementOut
    current_version: RequirementVersionOut | None = None
    versions: list[RequirementVersionOut] = Field(default_factory=list)


class ApplicabilityChange(BaseModel):
    applicability: str
    reason: str = Field(min_length=3)


class ConditionVariables(BaseModel):
    variables: dict[str, Any] = Field(default_factory=dict)


class ClarificationOut(ORMModel):
    id: uuid.UUID
    bid_id: uuid.UUID
    issue_type: str
    description: str
    severity: str
    blocks_generation: bool
    recommended_question: str | None = None
    status: str
    source_a: dict[str, Any] | None = None
    source_b: dict[str, Any] | None = None
    client_response: str | None = None
    # Why it is no longer open. A closure with no reason is indistinguishable
    # from a question quietly dropped.
    resolution_note: str | None = None
    created_at: datetime
    # Whose it is: the department that answers for it and the person who asks
    # the customer. `owner_position` is "Head" or "Team member" in that department.
    department_id: uuid.UUID | None = None
    department: str | None = None
    owner_user_id: uuid.UUID | None = None
    owner_name: str | None = None
    owner_position: str | None = None
    # How the department was chosen ("category", "model", "rules", "person") and,
    # when the AI chose, its one-line reason.
    routing_source: str | None = None
    routing_reason: str | None = None


class ClarificationAssign(BaseModel):
    owner_user_id: uuid.UUID


class ClarificationMove(BaseModel):
    department_id: uuid.UUID


class ClarificationStatusChange(BaseModel):
    status: str
    client_response: str | None = None
    reason: str | None = None


class DeliverableOut(ORMModel):
    id: uuid.UUID
    bid_id: uuid.UUID
    name: str
    deliverable_type: str | None = None
    required_format: str | None = None
    required_filename: str | None = None
    is_mandatory: bool
    status: str
    deadline: datetime | None = None
    source_locator: dict[str, Any] | None = None


class ScopeConfidenceOut(ORMModel):
    id: uuid.UUID
    bid_id: uuid.UUID
    level: str
    signals: dict[str, Any]
    blocking_reasons: list[str] | None = None
    detailed_drafting_allowed: bool
    computed_at: datetime
    computed_by: str


class AnalysisOut(BaseModel):
    precedence: dict[str, Any]
    requirements: dict[str, Any]
    deliverables: int
    ambiguities: int
    scope_confidence: str
    detailed_drafting_allowed: bool
    has_facts: bool
