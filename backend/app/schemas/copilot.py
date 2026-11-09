from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CreateConversationIn(BaseModel):
    title: str | None = None


class ConversationOut(ORMModel):
    id: uuid.UUID
    task_id: uuid.UUID
    user_id: uuid.UUID
    title: str | None = None
    message_count: int
    last_message_at: datetime | None = None


class FocusIn(BaseModel):
    """What the person is looking at: ticked requirements or a clicked section."""

    kind: str = "task"
    requirement_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    section: str | None = Field(default=None, max_length=300)


class AskIn(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    focus: FocusIn | None = None


class MessageOut(ORMModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    cited_chunk_ids: list[uuid.UUID] | None = None
    cited_evidence_ids: list[uuid.UUID] | None = None
    cited_request_ids: list[uuid.UUID] | None = None
    model_id: str | None = None
    created_at: datetime
    retrieval_trace: dict[str, Any] | None = None
    focus_key: str | None = None
    focus: dict[str, Any] | None = None
