"""Which department a requirement category is routed to.

One answer for everything that routes by category -- the tasks a bid is grouped
into, the department recommended on a requirement, the clarifications raised
against it -- so changing the table changes all of them together. The default
table is `assessment.DEPARTMENT_BY_CATEGORY`; an organization overrides single
categories in `category_route`. Changing a route affects work created afterwards;
tasks already made keep their department.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.models import CategoryRoute, Department
from app.models.enums import DepartmentCode, RequirementCategory
from app.services import audit


def _defaults() -> dict[RequirementCategory, DepartmentCode]:
    from app.services.assessment import DEPARTMENT_BY_CATEGORY

    return DEPARTMENT_BY_CATEGORY


def routes(session: Session, ctx: ExecutionContext) -> dict[RequirementCategory, DepartmentCode]:
    """The routing in force: the defaults, with this organization's overrides on top."""
    table = dict(_defaults())
    rows = session.execute(
        select(CategoryRoute.category, Department.code)
        .join(
            Department,
            (Department.id == CategoryRoute.department_id)
            & (Department.org_id == CategoryRoute.org_id),
        )
        .where(CategoryRoute.org_id == ctx.org_id)
    ).all()
    for category, code in rows:
        try:
            table[RequirementCategory(category)] = code
        except ValueError:
            continue
    return table


def describe(session: Session, ctx: ExecutionContext) -> dict:
    """Every category with its department, whether that is the default, and the options."""
    in_force = routes(session, ctx)
    defaults = _defaults()
    departments = (
        session.execute(
            select(Department).where(Department.org_id == ctx.org_id).order_by(Department.name)
        )
        .scalars()
        .all()
    )
    by_code = {d.code: d for d in departments}
    rows = []
    for category in RequirementCategory:
        code = in_force.get(category)
        department = by_code.get(code) if code else None
        rows.append(
            {
                "category": category.value,
                "department_id": str(department.id) if department else None,
                "department": department.name if department else None,
                "is_default": in_force.get(category) == defaults.get(category),
                "default_department": (
                    by_code[defaults[category]].name if defaults.get(category) in by_code else None
                ),
            }
        )
    return {
        "routes": rows,
        "departments": [{"id": str(d.id), "name": d.name} for d in departments],
    }


def set_route(
    session: Session, ctx: ExecutionContext, *, category: str, department_id: uuid.UUID
) -> None:
    try:
        RequirementCategory(category)
    except ValueError as exc:
        raise ValidationError(f"{category} is not a requirement category.") from exc
    department = session.get(Department, department_id)
    if department is None or department.org_id != ctx.org_id:
        raise NotFound("That department does not exist.")
    existing = session.execute(
        select(CategoryRoute).where(
            CategoryRoute.org_id == ctx.org_id, CategoryRoute.category == category
        )
    ).scalar_one_or_none()
    before = existing.department_id if existing else None
    if existing is None:
        session.add(
            CategoryRoute(org_id=ctx.org_id, category=category, department_id=department.id)
        )
    else:
        existing.department_id = department.id
    session.flush()
    audit.record(
        session,
        ctx,
        action="routing.category_set",
        entity_type="DEPARTMENT",
        entity_id=department.id,
        before={"category": category, "department_id": str(before) if before else None},
        after={"category": category, "department_id": str(department.id)},
    )


def reset_route(session: Session, ctx: ExecutionContext, *, category: str) -> None:
    existing = session.execute(
        select(CategoryRoute).where(
            CategoryRoute.org_id == ctx.org_id, CategoryRoute.category == category
        )
    ).scalar_one_or_none()
    if existing is None:
        return
    session.delete(existing)
    session.flush()
    audit.record(
        session,
        ctx,
        action="routing.category_reset",
        entity_type="DEPARTMENT",
        entity_id=existing.department_id,
        before={"category": category},
        after={"category": category, "department_id": None},
    )
