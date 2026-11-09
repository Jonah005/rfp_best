from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TaskOut(ORMModel):
    id: uuid.UUID
    bid_id: uuid.UUID
    code: str
    title: str
    description: str | None = None
    department_id: uuid.UUID
    owner_user_id: uuid.UUID | None = None
    priority: str
    due_date: date | None = None
    state: str
    retrieval_scope: dict[str, Any]
    blocker_note: str | None = None
    grouping_rationale: str | None = None
    state_changed_at: datetime | None = None


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    department_id: uuid.UUID
    requirement_ids: list[uuid.UUID] = Field(default_factory=list)
    description: str | None = None
    owner_user_id: uuid.UUID | None = None
    priority: str = "NORMAL"
    due_date: date | None = None
    grouping_rationale: str | None = None


class TaskTransition(BaseModel):
    to_state: str
    reason: str | None = None


class TaskAssign(BaseModel):
    owner_user_id: uuid.UUID


class ParticipantIn(BaseModel):
    user_id: uuid.UUID
    participant_role: str


class ParticipantOut(ORMModel):
    id: uuid.UUID
    task_id: uuid.UUID
    user_id: uuid.UUID
    participant_role: str
    added_by: uuid.UUID
    created_at: datetime
    removed_at: datetime | None = None


class DependencyIn(BaseModel):
    to_task_id: uuid.UUID
    dependency_type: str = "WAITS_FOR"
    note: str | None = None


class DependencyOut(ORMModel):
    id: uuid.UUID
    from_task_id: uuid.UUID
    to_task_id: uuid.UUID
    dependency_type: str
    note: str | None = None
    resolved_at: datetime | None = None


class ReplyOut(BaseModel):
    """A department's reply the person who asked has not dealt with yet."""

    request_id: uuid.UUID
    department: str | None = None
    state: str
    text: str


class TaskRequirementOut(BaseModel):
    requirement_id: uuid.UUID
    code: str
    category: str
    is_mandatory: bool
    applicability: str
    text: str | None = None
    bid_readiness: str | None = None
    evidence_status: str | None = None
    # Departments this task has asked about the requirement and not had a reply from.
    asked_departments: list[str] = Field(default_factory=list)
    # "submitted" (an answer was approved) or "ignored" (set aside), else None.
    position_status: str | None = None
    position_text: str | None = None
    # Replies from other departments waiting for the person to deal with them.
    replies: list[ReplyOut] = Field(default_factory=list)


class TaskDetail(BaseModel):
    task: TaskOut
    requirements: list[TaskRequirementOut] = Field(default_factory=list)
    participants: list[ParticipantOut] = Field(default_factory=list)
    dependencies_outgoing: list[DependencyOut] = Field(default_factory=list)
    dependencies_incoming: list[DependencyOut] = Field(default_factory=list)
    open_requests: int = 0


# --- cross-department requests ----------------------------------------------


class RequestCreate(BaseModel):
    origin_task_id: uuid.UUID
    target_department_id: uuid.UUID
    request_type: str
    request_text: str = Field(min_length=1)
    business_reason: str = Field(min_length=1)
    target_user_id: uuid.UUID | None = None
    requested_resource_type: str | None = None
    requested_resource_id: uuid.UUID | None = None
    requirement_ids: list[uuid.UUID] = Field(default_factory=list)
    due_at: datetime | None = None
    # The gap in the tender this request is about, when it was raised from one.
    clarification_id: uuid.UUID | None = None
    # The part of the response draft that is waiting on the answer, when asked from one.
    section: str | None = None


class RequestOut(ORMModel):
    id: uuid.UUID
    bid_id: uuid.UUID
    origin_task_id: uuid.UUID
    requester_user_id: uuid.UUID
    requester_department_id: uuid.UUID | None = None
    target_department_id: uuid.UUID
    target_user_id: uuid.UUID | None = None
    request_type: str
    request_text: str
    business_reason: str
    state: str
    resolution_text: str | None = None
    rejection_reason: str | None = None
    shared_evidence_ids: list[uuid.UUID] | None = None
    access_grant_id: uuid.UUID | None = None
    due_at: datetime | None = None
    resolved_at: datetime | None = None
    created_at: datetime


class AnswerIn(BaseModel):
    resolution_text: str = Field(min_length=1)
    # Evidence that backs the answer. Optional: an answer in words is still an
    # answer. Each item becomes a narrow grant scoped to the asking task.
    evidence_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)


class ShareEvidenceIn(BaseModel):
    evidence_ids: list[uuid.UUID] = Field(min_length=1)
    reason: str = Field(min_length=3)
    expires_at: datetime | None = None


class GrantAccessIn(BaseModel):
    resource_type: str
    resource_id: uuid.UUID
    reason: str = Field(min_length=3)
    permission: str = "READ"
    expires_at: datetime | None = None


class RejectIn(BaseModel):
    reason: str = Field(min_length=3)
