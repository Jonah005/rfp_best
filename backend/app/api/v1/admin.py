from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_context, get_db, require, require_any
from app.core.context import ExecutionContext
from app.core.errors import NotFound
from app.core.permissions import Perm
from app.models import Department, Organization, Role, User
from app.schemas.common import (
    DepartmentOut,
    DepartmentResponsibilitiesIn,
    RoleOut,
    UserCreate,
    UserOut,
)
from app.services import audit, category_routing, identity, routing

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=list[UserOut], dependencies=[Depends(require(Perm.USER_READ))])
def list_users(
    session: Session = Depends(get_db), ctx: ExecutionContext = Depends(get_current_context)
) -> list[UserOut]:
    rows = session.execute(select(User).where(User.org_id == ctx.org_id)).scalars().all()
    return [UserOut.model_validate(u) for u in rows]


@router.post(
    "/users",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Perm.USER_MANAGE))],
)
def create_user(
    payload: UserCreate,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> UserOut:
    user = identity.create_user(
        session,
        ctx,
        email=payload.email,
        display_name=payload.display_name,
        password=payload.password,
        role_codes_to_assign=payload.roles,
        department_ids_to_assign=payload.departments,
    )
    return UserOut.model_validate(user)


@router.get(
    "/users/{user_id}",
    response_model=UserOut,
    dependencies=[Depends(require(Perm.USER_READ))],
)
def get_user(
    user_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> UserOut:
    user = session.get(User, user_id)
    # RLS already hides other tenants' rows; this is the belt to that braces.
    if user is None or user.org_id != ctx.org_id:
        raise NotFound("User not found.")
    return UserOut.model_validate(user)


@router.get(
    "/departments",
    response_model=list[DepartmentOut],
    dependencies=[Depends(require(Perm.ORG_READ))],
)
def list_departments(
    session: Session = Depends(get_db), ctx: ExecutionContext = Depends(get_current_context)
) -> list[DepartmentOut]:
    rows = (
        session.execute(
            select(Department).where(Department.org_id == ctx.org_id).order_by(Department.name)
        )
        .scalars()
        .all()
    )
    return [_department_out(d) for d in rows]


def _department_out(department: Department) -> DepartmentOut:
    out = DepartmentOut.model_validate(department)
    out.responsibilities = routing.responsibilities_of(department) or None
    return out


@router.patch(
    "/departments/{department_id}/responsibilities",
    response_model=DepartmentOut,
    dependencies=[Depends(require(Perm.ORG_MANAGE))],
)
def set_responsibilities(
    department_id: uuid.UUID,
    payload: DepartmentResponsibilitiesIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> DepartmentOut:
    """Write what a department answers for; blank restores the default."""
    department = session.get(Department, department_id)
    if department is None or department.org_id != ctx.org_id:
        raise NotFound("Department not found.")
    before = department.responsibilities
    department.responsibilities = " ".join(payload.responsibilities.split()) or None
    session.flush()
    audit.record(
        session,
        ctx,
        action="department.responsibilities_set",
        entity_type="DEPARTMENT",
        entity_id=department.id,
        before={"responsibilities": before},
        after={"responsibilities": department.responsibilities},
    )
    return _department_out(department)


@router.get("/roles", response_model=list[RoleOut], dependencies=[Depends(require(Perm.ORG_READ))])
def list_roles(
    session: Session = Depends(get_db), ctx: ExecutionContext = Depends(get_current_context)
) -> list[RoleOut]:
    rows = session.execute(select(Role).where(Role.org_id == ctx.org_id)).scalars().all()
    return [RoleOut.model_validate(r) for r in rows]


@router.get("/organization", dependencies=[Depends(require(Perm.ORG_READ))])
def get_organization(
    session: Session = Depends(get_db), ctx: ExecutionContext = Depends(get_current_context)
) -> dict:
    org = session.get(Organization, ctx.org_id)
    if org is None:
        raise NotFound("Organization not found.")
    return {
        "id": str(org.id),
        "name": org.name,
        "slug": org.slug,
        "retention_days": org.retention_days,
        "residency_region": org.residency_region,
        "default_language": org.default_language,
    }


# --- which department each requirement category goes to ----------------------


class CategoryRouteIn(BaseModel):
    department_id: uuid.UUID


@router.get(
    "/category-routing",
    response_model=dict,
    dependencies=[Depends(require(Perm.BID_READ))],
)
def get_category_routing(
    session: Session = Depends(get_db), ctx: ExecutionContext = Depends(get_current_context)
) -> dict:
    """The routing table in force, so anyone working a bid can see how work is sent."""
    out = category_routing.describe(session, ctx)
    out["can_edit"] = ctx.has_permission(Perm.ORG_MANAGE.value) or ctx.has_permission(
        Perm.TASK_ASSIGN.value
    )
    return out


@router.put(
    "/category-routing/{category}",
    response_model=dict,
    dependencies=[Depends(require_any(Perm.ORG_MANAGE, Perm.TASK_ASSIGN))],
)
def set_category_routing(
    category: str,
    payload: CategoryRouteIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Send a category to a different department. Applies to work created afterwards."""
    category_routing.set_route(
        session, ctx, category=category.upper(), department_id=payload.department_id
    )
    return category_routing.describe(session, ctx)


@router.delete(
    "/category-routing/{category}",
    response_model=dict,
    dependencies=[Depends(require_any(Perm.ORG_MANAGE, Perm.TASK_ASSIGN))],
)
def reset_category_routing(
    category: str,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Go back to the built-in department for this category."""
    category_routing.reset_route(session, ctx, category=category.upper())
    return category_routing.describe(session, ctx)
