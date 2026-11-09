"""Declarative base and the tenant-safety mixins.

Correction 1 of the final review: cross-tenant relationships must be impossible
at the database level, not merely discouraged in application code. Two
mechanisms implement that here:

  * every tenant table carries a redundant UNIQUE (id, org_id), and
  * every foreign key between tenant tables is COMPOSITE -- it references
    (id, org_id) and reuses the child's own org_id column.

Because a child row has exactly one org_id and both sides of the composite key
must match it, a row linking tenant A to tenant B cannot be written at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, MetaData, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )


class TenantMixin:
    """Adds org_id plus the UNIQUE (id, org_id) that composite FKs reference."""

    org_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organization.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    @classmethod
    def tenant_identity_constraint(cls) -> UniqueConstraint:
        table_name = cls.__dict__["__tablename__"]
        return UniqueConstraint("id", "org_id", name=f"uq_{table_name}_id_org")


def tenant_fk(
    column: str,
    target_table: str,
    *,
    ondelete: str = "RESTRICT",
    name: str | None = None,
) -> ForeignKeyConstraint:
    """A composite, tenant-aware foreign key.

    ``tenant_fk("user_id", "app_user")`` yields
    ``FOREIGN KEY (user_id, org_id) REFERENCES app_user (id, org_id)``.

    Both the parent and the child are matched on the SAME org_id column, so the
    reference cannot cross tenants.
    """
    return ForeignKeyConstraint(
        [column, "org_id"],
        [f"{target_table}.id", f"{target_table}.org_id"],
        ondelete=ondelete,
        name=name or f"fk_{column}_org",
    )
