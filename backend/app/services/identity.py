"""User, department and role services, plus the login path."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import AuthenticationError, NotFound, ValidationError
from app.core.permissions import ROLE_PERMISSIONS, RoleCode
from app.core.security import hash_password, verify_password
from app.models import (
    Department,
    Permission,
    Role,
    RolePermission,
    User,
    UserDepartment,
    UserRole,
)
from app.services import audit


@dataclass(frozen=True)
class LoginIdentity:
    user_id: uuid.UUID
    org_id: uuid.UUID
    password_hash: str | None
    is_active: bool


def lookup_login_identity(session: Session, *, email: str) -> LoginIdentity | None:
    """Resolve an email to a tenant BEFORE any tenant context exists.

    RLS is FORCEd on app_user, so an ordinary query here correctly returns
    nothing -- there is no tenant yet to scope it to. The single sanctioned
    escape hatch is auth_lookup_user(), a SECURITY DEFINER function with a fixed
    minimal projection, owned by a NOLOGIN role and executable only by the
    application role. Nothing else in the application reads app_user without a
    tenant.
    """
    row = session.execute(
        text("SELECT user_id, org_id, password_hash, is_active FROM auth_lookup_user(:email)"),
        {"email": email.lower().strip()},
    ).first()
    if row is None:
        return None
    return LoginIdentity(
        user_id=row.user_id,
        org_id=row.org_id,
        password_hash=row.password_hash,
        is_active=row.is_active,
    )


def authenticate(session: Session, *, email: str, password: str) -> LoginIdentity:
    """Verify credentials. The failure message never distinguishes the cases."""
    identity_row = lookup_login_identity(session, email=email)
    if identity_row is None or not identity_row.is_active:
        # Spend the same work on a miss so response time does not disclose
        # whether the address exists.
        verify_password(password, None)
        raise AuthenticationError("Invalid email or password.")
    if not verify_password(password, identity_row.password_hash):
        raise AuthenticationError("Invalid email or password.")
    return identity_row


def record_login(session: Session, ctx: ExecutionContext, *, user_id: uuid.UUID) -> None:
    """Runs inside the user's own tenant context, after authentication."""
    user = session.get(User, user_id)
    if user is None:
        raise AuthenticationError("Invalid email or password.")
    user.last_login_at = datetime.now(timezone.utc)
    session.flush()
    audit.record(session, ctx, action="auth.login", entity_type="app_user", entity_id=user_id)


def effective_permissions(session: Session, user: User) -> set[str]:
    rows = (
        session.execute(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(UserRole, UserRole.role_id == RolePermission.role_id)
            .where(UserRole.user_id == user.id, UserRole.org_id == user.org_id)
        )
        .scalars()
        .all()
    )
    return set(rows)


def role_codes(session: Session, user: User) -> set[str]:
    rows = (
        session.execute(
            select(Role.code)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user.id, UserRole.org_id == user.org_id)
        )
        .scalars()
        .all()
    )
    return set(rows)


def department_ids(session: Session, user: User) -> set[uuid.UUID]:
    rows = (
        session.execute(
            select(UserDepartment.department_id).where(
                UserDepartment.user_id == user.id, UserDepartment.org_id == user.org_id
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


def create_user(
    session: Session,
    ctx: ExecutionContext,
    *,
    email: str,
    display_name: str,
    password: str | None = None,
    role_codes_to_assign: list[str] | None = None,
    department_ids_to_assign: list[uuid.UUID] | None = None,
) -> User:
    email = email.lower().strip()

    user = User(
        org_id=ctx.org_id,
        email=email,
        display_name=display_name,
        password_hash=hash_password(password) if password else None,
    )
    session.add(user)
    try:
        # Email is globally unique, so an in-tenant pre-check would be
        # insufficient -- and RLS means this session cannot see another
        # tenant's rows to check against anyway. The database is the only
        # component that can answer the question, so let it.
        with session.begin_nested():
            session.flush()
    except IntegrityError as exc:
        if "uq_app_user_email_lower" not in str(exc.orig):
            raise
        # Deliberately does not reveal WHICH organization holds the address:
        # that would let an outsider enumerate a competitor's staff list.
        raise ValidationError("That email address is already registered.") from exc

    for code in role_codes_to_assign or []:
        assign_role(session, ctx, user_id=user.id, role_code=code)
    for dept_id in department_ids_to_assign or []:
        assign_department(session, ctx, user_id=user.id, department_id=dept_id)

    audit.record(
        session,
        ctx,
        action="user.created",
        entity_type="app_user",
        entity_id=user.id,
        after={"email": email, "display_name": display_name},
    )
    return user


def assign_role(
    session: Session, ctx: ExecutionContext, *, user_id: uuid.UUID, role_code: str
) -> UserRole:
    role = session.execute(
        select(Role).where(Role.org_id == ctx.org_id, Role.code == role_code)
    ).scalar_one_or_none()
    if role is None:
        raise NotFound(f"Role {role_code} does not exist in this organization.")
    existing = session.execute(
        select(UserRole).where(
            UserRole.org_id == ctx.org_id,
            UserRole.user_id == user_id,
            UserRole.role_id == role.id,
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    link = UserRole(org_id=ctx.org_id, user_id=user_id, role_id=role.id)
    session.add(link)
    session.flush()
    audit.record(
        session,
        ctx,
        action="user.role_assigned",
        entity_type="app_user",
        entity_id=user_id,
        after={"role": role_code},
    )
    return link


def assign_department(
    session: Session,
    ctx: ExecutionContext,
    *,
    user_id: uuid.UUID,
    department_id: uuid.UUID,
    is_primary: bool = False,
) -> UserDepartment:
    existing = session.execute(
        select(UserDepartment).where(
            UserDepartment.org_id == ctx.org_id,
            UserDepartment.user_id == user_id,
            UserDepartment.department_id == department_id,
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    link = UserDepartment(
        org_id=ctx.org_id,
        user_id=user_id,
        department_id=department_id,
        is_primary=is_primary,
    )
    session.add(link)
    session.flush()
    audit.record(
        session,
        ctx,
        action="user.department_assigned",
        entity_type="app_user",
        entity_id=user_id,
        after={"department_id": str(department_id)},
    )
    # A department with people always has a head. The first person in is it;
    # anyone who joins later is a team member until the head is changed.
    department = session.get(Department, department_id)
    if department is not None and department.head_user_id is None:
        set_department_head(session, ctx, department_id=department_id, user_id=user_id)
    return link


def provision_roles(session: Session, org_id: uuid.UUID) -> dict[str, Role]:
    """Create the role catalogue and bind permissions for one organization."""
    perms: dict[str, Permission] = {
        code: perm for code, perm in session.execute(select(Permission.code, Permission)).all()
    }
    created: dict[str, Role] = {}
    for role_code, permissions in ROLE_PERMISSIONS.items():
        role = session.execute(
            select(Role).where(Role.org_id == org_id, Role.code == role_code.value)
        ).scalar_one_or_none()
        if role is None:
            role = Role(
                org_id=org_id,
                code=role_code.value,
                name=role_code.value.replace("_", " ").title(),
            )
            session.add(role)
            session.flush()
        for perm in permissions:
            permission = perms.get(perm.value)
            if permission is None:
                continue
            exists = session.execute(
                select(RolePermission).where(
                    RolePermission.org_id == org_id,
                    RolePermission.role_id == role.id,
                    RolePermission.permission_id == permission.id,
                )
            ).scalar_one_or_none()
            if exists is None:
                session.add(
                    RolePermission(org_id=org_id, role_id=role.id, permission_id=permission.id)
                )
        created[role_code.value] = role
    session.flush()
    return created


def sync_permission_catalogue(session: Session) -> int:
    """Upsert the global permission catalogue. Idempotent."""
    from app.core.permissions import ALL_PERMISSIONS

    existing = set(session.execute(select(Permission.code)).scalars().all())
    added = 0
    for perm in ALL_PERMISSIONS:
        if perm.value in existing:
            continue
        resource, _, action = perm.value.partition(":")
        session.add(
            Permission(code=perm.value, resource=resource, action=action, description=perm.name)
        )
        added += 1
    session.flush()
    return added


__all__ = [
    "authenticate",
    "lookup_login_identity",
    "record_login",
    "LoginIdentity",
    "effective_permissions",
    "role_codes",
    "department_ids",
    "create_user",
    "assign_role",
    "assign_department",
    "provision_roles",
    "sync_permission_catalogue",
    "RoleCode",
]


# How a person sits in a department. Two words, used everywhere a person is
# shown next to a department, so a bid manager choosing an owner reads the same
# thing on every screen.
HEAD = "Head"
TEAM_MEMBER = "Team member"

# Roles that make a poor default head: they exist to oversee or administer, not
# to do a department's work.
_NOT_A_NATURAL_HEAD = {
    RoleCode.AUDITOR.value,
    RoleCode.ORG_ADMIN.value,
    RoleCode.EXECUTIVE_APPROVER.value,
    # Approves evidence for reuse and cannot open a task or write a draft.
    RoleCode.KNOWLEDGE_ADMIN.value,
}


def position_in(department: Department, user_id: uuid.UUID) -> str:
    """ "Head" if this person heads the department, otherwise "Team member"."""
    return HEAD if department.head_user_id == user_id else TEAM_MEMBER


def set_department_head(
    session: Session, ctx: ExecutionContext, *, department_id: uuid.UUID, user_id: uuid.UUID
) -> Department:
    """Make a member of the department its head, and move the head role with it."""
    department = session.get(Department, department_id)
    if department is None or department.org_id != ctx.org_id:
        raise NotFound("That department does not exist.")
    member = session.execute(
        select(UserDepartment).where(
            UserDepartment.org_id == ctx.org_id,
            UserDepartment.user_id == user_id,
            UserDepartment.department_id == department_id,
        )
    ).scalar_one_or_none()
    if member is None:
        raise ValidationError(f"That person is not in {department.name}, so cannot head it.")
    previous = department.head_user_id
    if previous != user_id:
        department.head_user_id = user_id
        session.flush()
    try:
        assign_role(session, ctx, user_id=user_id, role_code=RoleCode.DEPARTMENT_HEAD.value)
    except NotFound:
        # An organization whose roles were never provisioned still gets a head;
        # provisioning adds the role to them later.
        pass
    if previous == user_id:
        return department
    if previous is not None:
        _drop_head_role_if_unused(session, ctx, user_id=previous)
    audit.record(
        session,
        ctx,
        action="department.head_set",
        entity_type="DEPARTMENT",
        entity_id=department.id,
        before={"head_user_id": str(previous) if previous else None},
        after={"head_user_id": str(user_id)},
    )
    return department


def _drop_head_role_if_unused(session: Session, ctx: ExecutionContext, *, user_id) -> None:
    still = session.execute(
        select(Department.id).where(
            Department.org_id == ctx.org_id, Department.head_user_id == user_id
        )
    ).first()
    if still:
        return
    role = session.execute(
        select(Role).where(Role.org_id == ctx.org_id, Role.code == RoleCode.DEPARTMENT_HEAD.value)
    ).scalar_one_or_none()
    if role is None:
        return
    link = session.execute(
        select(UserRole).where(
            UserRole.org_id == ctx.org_id, UserRole.user_id == user_id, UserRole.role_id == role.id
        )
    ).scalar_one_or_none()
    if link is not None:
        session.delete(link)
        session.flush()


def settle_department_heads(session: Session, ctx: ExecutionContext) -> int:
    """Give every department that has people a head. Returns how many were named.

    Also repairs a head who has no head role, or who has left the department.
    A department nobody is in is left alone: there is nobody to name.
    """
    named = 0
    departments = (
        session.execute(select(Department).where(Department.org_id == ctx.org_id)).scalars().all()
    )
    for department in departments:
        members = (
            session.execute(
                select(User)
                .join(
                    UserDepartment,
                    (UserDepartment.user_id == User.id) & (UserDepartment.org_id == User.org_id),
                )
                .where(
                    User.org_id == ctx.org_id,
                    User.is_active.is_(True),
                    UserDepartment.department_id == department.id,
                )
                .order_by(User.created_at, User.display_name)
            )
            .scalars()
            .all()
        )
        if not members:
            continue
        current = next((m for m in members if m.id == department.head_user_id), None)
        if current is None:
            natural = [m for m in members if not ({r.code for r in m.roles} <= _NOT_A_NATURAL_HEAD)]
            current = (natural or members)[0]
            named += 1
        set_department_head(session, ctx, department_id=department.id, user_id=current.id)
    return named
