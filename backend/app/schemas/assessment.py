from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AssessmentOut(ORMModel):
    id: uuid.UUID
    requirement_id: uuid.UUID
    bid_id: uuid.UUID
    evidence_status: str
    bid_readiness: str
    rationale: str
    assessment_state: str
    version: int
    is_current: bool
    computed_at: datetime
    computed_by: str
    human_adjustment_reason: str | None = None
    stale_reason: str | None = None
    reasoning_trace: dict[str, Any] | None = None


class AssessmentEvidenceOut(BaseModel):
    evidence_id: uuid.UUID
    role: str
    weight: float | None = None
    contribution_note: str | None = None
    retrieval_rank: int | None = None
    excerpt: str
    evidence_status: str
    approval_status: str
    expiry_date: str | None = None
    source_locator: dict[str, Any]


class AssessmentDetail(BaseModel):
    assessment: AssessmentOut
    evidence: list[AssessmentEvidenceOut] = Field(default_factory=list)


class AssessmentAdjust(BaseModel):
    evidence_status: str | None = None
    bid_readiness: str | None = None
    reason: str = Field(min_length=3)


class AttachEvidence(BaseModel):
    evidence_id: uuid.UUID
    role: str = "SUPPORTING"
    note: str | None = None


class ReadinessOut(BaseModel):
    total_requirements: int
    applicable_requirements: int
    not_applicable: int
    assessed: int
    by_readiness: dict[str, int]
    by_evidence_status: dict[str, int]
    requirement_coverage_pct: float
    evidence_readiness_pct: float
    critical_blockers: int
    open_clarifications: int
    outstanding_actions: int
    mandatory_unready: int
    answered_by_commitment: int = 0
    deliverables_total: int


class BidNoBidOut(ORMModel):
    id: uuid.UUID
    bid_id: uuid.UUID
    result: str
    recommended_result: str | None = None
    factors: dict[str, Any]
    rationale: str
    blocking_factors: list[str] | None = None
    decided_by: uuid.UUID | None = None
    decided_at: datetime
    is_current: bool


class BidNoBidRecommendationOut(BaseModel):
    result: str
    factors: dict[str, Any]
    blocking_factors: list[str]


class BidNoBidRecord(BaseModel):
    result: str
    rationale: str = Field(min_length=3)
