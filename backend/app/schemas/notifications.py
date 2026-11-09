from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import ORMModel


class NotificationOut(ORMModel):
    id: uuid.UUID
    kind: str
    title: str
    body: str | None = None
    # Where clicking it goes. The screen key matches the UI's own route names.
    screen: str
    bid_id: uuid.UUID | None = None
    focus_id: uuid.UUID | None = None
    actor_user_id: uuid.UUID | None = None
    created_at: datetime
    read_at: datetime | None = None


class UnreadCount(BaseModel):
    unread: int
