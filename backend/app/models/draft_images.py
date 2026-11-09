"""Pictures in a response draft, and how the drawn diagrams are laid out in it.

Two kinds of row. An UPLOAD is a picture a person pasted or added (the bytes are kept
here). A DIAGRAM row holds nothing but the layout a person gave a drawn diagram in the
document -- how wide, which part is shown -- because the drawing itself is rebuilt from
its structure and is never a stored picture.

A picture belongs to a section (the same key the statements use: heading and diagram)
and sits after a paragraph of it, so rewriting the section's text does not lose it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, tenant_fk, uuid_pk


class DraftImage(Base, TenantMixin, TimestampMixin):
    __tablename__ = "draft_image"

    id: Mapped[uuid.UUID] = uuid_pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # UPLOAD | DIAGRAM
    section: Mapped[str | None] = mapped_column(String(200), nullable=True)
    diagram_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    # The picture comes after this paragraph of the section (paragraph numbers as the
    # statements carry them). Several pictures after one paragraph keep `position` order.
    after_paragraph: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    filename: Mapped[str | None] = mapped_column(String(200), nullable=True)
    natural_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    natural_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # How wide it is set in the document, as a share of the page width (10-100).
    width_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # The part shown, as fractions of the picture: {x, y, w, h}. Null shows all of it.
    crop: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    alt: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_draft_image_id_org"),
        tenant_fk("artifact_id", "response_artifact", ondelete="CASCADE"),
        Index("ix_draft_image_artifact", "org_id", "artifact_id"),
    )
