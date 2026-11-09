"""Diagrams that answer a task's requirements.

A diagram is stored as a small STRUCTURE -- boxes, links, lanes -- and drawn by
the platform, never as a picture a model produced. That is what makes every
request in the walkthrough tractable: "change this", "make it look like that",
"is this consistent with the requirement that changed" are edits to and
comparisons of structure. A bitmap could only be re-generated and hoped over.

Versions are immutable. An edit, an AI revision and a restyle each write a new
version, so the person who asked for the change can compare it with what was
there and go back.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, tenant_fk, uuid_pk


class Diagram(Base, TenantMixin, TimestampMixin):
    __tablename__ = "diagram"

    id: Mapped[uuid.UUID] = uuid_pk()
    bid_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    diagram_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Kept on the task but not written up: a person decided it should not appear in
    # the response (a duplicate, a working sketch). The write-up skips it.
    left_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_diagram_id_org"),
        tenant_fk("bid_id", "bid", ondelete="CASCADE"),
        tenant_fk("task_id", "task", ondelete="CASCADE"),
        Index("ix_diagram_task", "org_id", "task_id"),
    )


class DiagramVersion(Base, TenantMixin, TimestampMixin):
    __tablename__ = "diagram_version"

    id: Mapped[uuid.UUID] = uuid_pk()
    diagram_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    style: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    # What the person asked for, and what the platform says was done about it.
    instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Things asked for that could not be done, and why -- shown, not hidden.
    could_not: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Elements resting on an assumption or a promise rather than on the tender
    # or on evidence. Recorded so a reviewer sees what the picture takes for granted.
    unconfirmed: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Suggestions from a review. Advice only: nothing here has been applied.
    review_notes: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # {requirement code: hash of its wording} at the time this version was made,
    # so a later change to any of them is a comparison, not a guess.
    basis: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_diagram_version_id_org"),
        UniqueConstraint("diagram_id", "version_no", name="uq_diagram_version_no"),
        tenant_fk("diagram_id", "diagram", ondelete="CASCADE"),
        Index("ix_diagram_version_diagram", "org_id", "diagram_id"),
    )


class DiagramRequirement(Base, TenantMixin, TimestampMixin):
    """Which requirements a diagram answers, so a change to one can find it."""

    __tablename__ = "diagram_requirement"

    id: Mapped[uuid.UUID] = uuid_pk()
    diagram_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    requirement_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("diagram_id", "requirement_id", name="uq_diagram_requirement"),
        tenant_fk("diagram_id", "diagram", ondelete="CASCADE"),
        tenant_fk("requirement_id", "requirement", ondelete="CASCADE"),
    )


class DiagramNeed(Base, TenantMixin, TimestampMixin):
    """Whether a task needs a kind of diagram, and what a person decided.

    The platform proposes; a person accepts or declines, and a decline is
    remembered with its reason so the next proposal does not ask again. A
    declined type comes back only when the requirements behind it change.
    """

    __tablename__ = "diagram_need"

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    diagram_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # AI | PERSON
    why: Mapped[str | None] = mapped_column(Text, nullable=True)
    enough_information: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # [{what, why, requirement_codes}] -- what is missing, when it is.
    missing: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    decline_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The requirement wording when it was declined; a change re-opens the question.
    decided_basis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_diagram_need_id_org"),
        UniqueConstraint("task_id", "diagram_type", name="uq_diagram_need"),
        tenant_fk("task_id", "task", ondelete="CASCADE"),
    )
