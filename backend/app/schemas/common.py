from __future__ import annotations

import uuid
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    total_estimate: int | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth scheme name, not a credential
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str


class DepartmentOut(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    # Who staffs this department's work, or null for nobody. A client needs it
    # to know whether to offer the assign control to the person looking at the
    # screen -- it is a fact about the department, not a grant to the reader.
    head_user_id: uuid.UUID | None = None
    # What the department answers for -- the stored line, or the default for
    # its code when none was written. Read when routing a missing answer.
    responsibilities: str | None = None


class DepartmentResponsibilitiesIn(BaseModel):
    responsibilities: str = Field(default="", max_length=1000)


class RoleOut(ORMModel):
    id: uuid.UUID
    code: str
    name: str


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    display_name: str
    is_active: bool
    mfa_enrolled: bool
    last_login_at: datetime | None = None


class MeOut(BaseModel):
    user: UserOut
    org_id: uuid.UUID
    roles: list[str]
    departments: list[uuid.UUID]
    permissions: list[str]


class UserCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)
    password: str | None = Field(default=None, min_length=12)
    roles: list[str] = Field(default_factory=list)
    departments: list[uuid.UUID] = Field(default_factory=list)


class GrantOut(ORMModel):
    id: uuid.UUID
    grantee_user_id: uuid.UUID
    task_id: uuid.UUID
    resource_type: str
    resource_id: uuid.UUID
    permission: str
    reason: str
    granted_by: uuid.UUID
    granted_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    effective_state: str


class GrantCreate(BaseModel):
    grantee_user_id: uuid.UUID
    task_id: uuid.UUID
    resource_type: str
    resource_id: uuid.UUID
    permission: str = "READ"
    reason: str = Field(min_length=3)
    expires_at: datetime | None = None


class ShareEvidenceRequest(BaseModel):
    grantee_user_id: uuid.UUID
    task_id: uuid.UUID
    evidence_ids: list[uuid.UUID] = Field(min_length=1)
    reason: str = Field(min_length=3)
    expires_at: datetime | None = None


class RevokeRequest(BaseModel):
    reason: str = Field(min_length=3)


class AuditOut(ORMModel):
    id: uuid.UUID
    actor_user_id: uuid.UUID | None
    actor_kind: str
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    reason: str | None
    occurred_at: datetime


class HealthOut(BaseModel):
    status: str
    checks: dict[str, str]
