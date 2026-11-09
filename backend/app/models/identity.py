"""Organization, user, department, role, permission, access grant, audit event."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TenantMixin, TimestampMixin, tenant_fk, uuid_pk
from app.models.enums import (
    ActorKind,
    DepartmentCode,
    GrantPermission,
    GrantResourceType,
    GrantState,
)


def _pg_enum(python_enum, name: str) -> PGEnum:
    return PGEnum(
        python_enum,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        create_type=True,
    )


class Organization(Base, TimestampMixin):
    __tablename__ = "organization"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    retention_days: Mapped[int] = mapped_column(Integer, default=2555, nullable=False)
    residency_region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    default_language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Department(Base, TenantMixin, TimestampMixin):
    __tablename__ = "department"

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[DepartmentCode] = mapped_column(
        _pg_enum(DepartmentCode, "department_code"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    # Who staffs this department's work. Nullable, and null means nobody — the
    # bid manager assigns as before. Naming a head is what turns the
    # DEPARTMENT_HEAD role from a capability into an authority over something
    # specific, which is why the authority lives here and not in the role.
    head_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    # What this department answers for, in a line an administrator writes:
    # "contracts, licences, NDAs, liability". Read when deciding who a missing
    # piece of information should be requested from. Null means the default for
    # the department's code (see app.services.routing.DEFAULT_RESPONSIBILITIES).
    responsibilities: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_department_id_org"),
        # SET NULL, not RESTRICT: removing a person must not be blocked by a
        # department pointing at them, and "no head" is the safe resting state —
        # it falls back to the bid manager rather than leaving a dangling head.
        tenant_fk("head_user_id", "app_user", ondelete="SET NULL", name="fk_department_head_org"),
        UniqueConstraint("org_id", "code", name="uq_department_org_code"),
        tenant_fk("parent_id", "department", name="fk_department_parent_org"),
    )


class User(Base, TenantMixin, TimestampMixin):
    __tablename__ = "app_user"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Dev-auth only. Null once an identity provider backs this user; nothing
    # outside app.core.security ever reads it.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_idp_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    mfa_enrolled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    roles: Mapped[list[Role]] = relationship(secondary="user_role", lazy="selectin", viewonly=True)
    departments: Mapped[list[Department]] = relationship(
        secondary="user_department", lazy="selectin", viewonly=True
    )

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_app_user_id_org"),
        # Email is unique GLOBALLY, not per tenant. Authentication resolves a
        # tenant FROM an email before any tenant context exists, so an address
        # that appears in two organizations would make that lookup ambiguous --
        # and "pick one" is not an acceptable answer to "who is signing in".
        # The identity model already gives each user exactly one Organization,
        # so nothing is lost. Enforced on lower(email) by a functional unique
        # index (see migration 0002) so casing cannot be used to duplicate.
        Index(
            "uq_app_user_email_lower",
            func.lower(email),
            unique=True,
        ),
        UniqueConstraint("org_id", "external_idp_subject", name="uq_app_user_org_idp"),
    )


class Role(Base, TenantMixin, TimestampMixin):
    __tablename__ = "role"

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    permissions: Mapped[list[Permission]] = relationship(
        secondary="role_permission", lazy="selectin", viewonly=True
    )

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_role_id_org"),
        UniqueConstraint("org_id", "code", name="uq_role_org_code"),
    )


class Permission(Base):
    """Global catalogue. Not tenant-scoped; role_permission binds it per tenant."""

    __tablename__ = "permission"

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    resource: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class RolePermission(Base, TenantMixin):
    __tablename__ = "role_permission"

    id: Mapped[uuid.UUID] = uuid_pk()
    role_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    permission_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("permission.id", ondelete="CASCADE"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permission"),
        tenant_fk("role_id", "role", ondelete="CASCADE"),
    )


class UserRole(Base, TenantMixin):
    __tablename__ = "user_role"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    role_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_user_role"),
        tenant_fk("user_id", "app_user", ondelete="CASCADE"),
        tenant_fk("role_id", "role", ondelete="CASCADE"),
    )


class UserDepartment(Base, TenantMixin):
    __tablename__ = "user_department"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    department_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "department_id", name="uq_user_department"),
        tenant_fk("user_id", "app_user", ondelete="CASCADE"),
        tenant_fk("department_id", "department", ondelete="CASCADE"),
    )


class AccessGrant(Base, TenantMixin, TimestampMixin):
    """A task-scoped, time-bounded, revocable delegation of read access.

    Correction 2: effective state is DERIVED from expires_at/revoked_at on every
    check. `materialized_state` is a convenience for listing screens only and is
    never consulted by the authorization path -- see `effective_state`.
    """

    __tablename__ = "access_grant"

    id: Mapped[uuid.UUID] = uuid_pk()
    grantee_user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # Always task-scoped. There is no blanket user-level grant in this product.
    task_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    resource_type: Mapped[GrantResourceType] = mapped_column(
        _pg_enum(GrantResourceType, "grant_resource_type"), nullable=False
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    permission: Mapped[GrantPermission] = mapped_column(
        _pg_enum(GrantPermission, "grant_permission"), nullable=False
    )
    origin_request_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    granted_by: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    materialized_state: Mapped[GrantState | None] = mapped_column(
        _pg_enum(GrantState, "grant_state"), nullable=True
    )
    materialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_access_grant_id_org"),
        tenant_fk("grantee_user_id", "app_user"),
        tenant_fk("granted_by", "app_user", name="fk_access_grant_granted_by_org"),
        Index("ix_access_grant_lookup", "org_id", "grantee_user_id", "task_id"),
        Index("ix_access_grant_resource", "org_id", "resource_type", "resource_id"),
        Index("ix_access_grant_expiry", "org_id", "expires_at"),
    )

    def effective_state(self, *, now: datetime | None = None) -> GrantState:
        moment = now or datetime.now(timezone.utc)
        if self.revoked_at is not None:
            return GrantState.REVOKED
        if self.expires_at <= moment:
            return GrantState.EXPIRED
        return GrantState.ACTIVE

    def is_active(self, *, now: datetime | None = None) -> bool:
        return self.effective_state(now=now) is GrantState.ACTIVE


class AuditEvent(Base, TenantMixin):
    """Append-only. The application role holds no UPDATE or DELETE grant here."""

    __tablename__ = "audit_event"

    id: Mapped[uuid.UUID] = uuid_pk()
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    actor_kind: Mapped[ActorKind] = mapped_column(_pg_enum(ActorKind, "actor_kind"), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    bid_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    before_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (
        Index("ix_audit_event_bid", "org_id", "bid_id", "occurred_at"),
        Index("ix_audit_event_entity", "org_id", "entity_type", "entity_id", "occurred_at"),
    )


class Notification(Base, TenantMixin):
    """One thing one person needs to know about, and whether they have seen it.

    Deliberately not derived from `audit_event`. Audit answers "what happened"
    for someone entitled to see everything, and it is append-only precisely so
    that nothing can be marked, dismissed or tidied. A notification answers a
    different question -- "what is waiting on YOU" -- which needs a named
    recipient and a mutable read state, neither of which belongs in an audit
    record. The two are written from the same moments and stay independent:
    deleting every notification in the org loses nothing an auditor needs.

    `kind` is a plain string rather than a database enum. Enumerating it would
    mean a migration every time a new moment becomes worth telling somebody
    about, and the cost of an unrecognised kind here is a generic icon, not a
    corrupt row. The audit action names it mirrors are already strings for the
    same reason.

    A notification carries where to go, not what to render: `screen` plus an
    optional `focus_id` is enough for the UI to open the right place with the
    right record selected. It stores no evidence text and no document content,
    so it can never become a side channel around a permission check -- what a
    person is told exists is not the same as being shown it.
    """

    __tablename__ = "notification"

    id: Mapped[uuid.UUID] = uuid_pk()
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # Who caused it. Null for something the system did on its own, such as a
    # request falling overdue.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Where clicking it should land.
    screen: Mapped[str] = mapped_column(String(32), nullable=False)
    bid_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    focus_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # The unread badge is the most frequent read in the product: it runs on
        # a timer for every signed-in person. Order the index the way that query
        # filters -- recipient, then read state -- so it stays an index scan.
        Index(
            "ix_notification_recipient",
            "org_id",
            "recipient_user_id",
            "read_at",
            "created_at",
        ),
    )


class ModelUsage(Base, TenantMixin):
    """Per-call accounting for the model gateway. No prompt text is stored here."""

    __tablename__ = "model_usage"

    id: Mapped[uuid.UUID] = uuid_pk()
    call_type: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (Index("ix_model_usage_lookup", "org_id", "call_type", "occurred_at"),)


class ModelResultCache(Base, TenantMixin):
    """A structured model reply, keyed by exactly what was asked.

    Assessment is re-run constantly — approve three more documents, run it
    again — and every run currently re-pays for every requirement. The inputs
    have not changed for most of them, and neither has the answer: the call
    types that use this cache are deterministic reads at temperature zero.

    What makes reuse safe is the key. `input_sha256` covers the entire rendered
    prompt, so a changed requirement, a changed shortlist or a changed excerpt
    is a different key and gets a fresh call. `prompt_version` is separate and
    explicit, so editing a prompt invalidates every entry made under the old one
    rather than silently serving answers to a question no longer being asked.

    Tenant-scoped like everything else, which matters here more than usual: the
    key is a hash of that organization's own tender and evidence text.
    """

    __tablename__ = "model_result_cache"

    id: Mapped[uuid.UUID] = uuid_pk()
    call_type: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "call_type",
            "prompt_version",
            "input_sha256",
            name="uq_model_result_cache_key",
        ),
    )


# Tables that carry tenant data and therefore receive RLS policies.
TENANT_TABLES: tuple[str, ...] = (
    "department",
    "app_user",
    "role",
    "role_permission",
    "user_role",
    "user_department",
    "access_grant",
    "audit_event",
    "model_usage",
    "model_result_cache",
    "task",
    "task_participant",
)
