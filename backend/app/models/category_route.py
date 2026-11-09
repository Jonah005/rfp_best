"""Which department a requirement category goes to, for one organization.

The defaults live in code (`DEPARTMENT_BY_CATEGORY`); a row here replaces the default
for that category. No row means the default applies.
"""

from __future__ import annotations

import uuid

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, tenant_fk, uuid_pk


class CategoryRoute(Base, TenantMixin, TimestampMixin):
    __tablename__ = "category_route"

    id: Mapped[uuid.UUID] = uuid_pk()
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    department_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("org_id", "category", name="uq_category_route_org_category"),
        tenant_fk("department_id", "department", name="fk_category_route_department_org"),
    )
