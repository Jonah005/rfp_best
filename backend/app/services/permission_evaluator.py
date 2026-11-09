"""The one place authorization is decided.

The API, exports and (from Phase H onward) AI retrieval all call this. There is
no parallel implementation, so "what the assistant can see" cannot drift from
"what the user can see".

Three layers, in order:
  1. role-based permissions,
  2. object-level classification, and
  3. task-scoped AccessGrants.

Grant state is evaluated LIVE from expires_at/revoked_at (correction 2). No
stored state column is ever consulted here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import PermissionDenied
from app.core.permissions import CLASS_PERMISSION, Perm
from app.models import AccessGrant
from app.models.enums import GrantResourceType


@dataclass(frozen=True)
class ResourceRef:
    resource_type: GrantResourceType
    resource_id: uuid.UUID
    classification: str | None = None
    owning_department_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    basis: str  # ROLE | GRANT | CLASSIFICATION_OPEN | DENIED_*
    grant_id: uuid.UUID | None = None
    owning_department_id: uuid.UUID | None = None

    def raise_if_denied(self, detail: str, remediation: dict | None = None) -> None:
        if not self.allowed:
            raise PermissionDenied(detail, remediation=remediation)


def active_grant_clause(now: datetime | None = None):
    """SQL predicate for a live grant. Reused by retrieval so filtering happens
    inside the query rather than after ranking."""
    moment = now or datetime.now(timezone.utc)
    return and_(AccessGrant.revoked_at.is_(None), AccessGrant.expires_at > moment)


class PermissionEvaluator:
    def __init__(self, session: Session, ctx: ExecutionContext) -> None:
        self.session = session
        self.ctx = ctx

    # -- role layer ---------------------------------------------------------
    def has(self, permission: Perm) -> bool:
        return permission.value in self.ctx.permissions

    def require(self, permission: Perm, *, detail: str | None = None) -> None:
        if not self.has(permission):
            raise PermissionDenied(
                detail or f"This action requires the '{permission.value}' permission."
            )

    # -- object + grant layers ---------------------------------------------
    def can_read_resource(
        self,
        resource: ResourceRef,
        *,
        task_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> Decision:
        """Read access to one restricted object.

        A classification that maps to a permission the caller holds opens the
        object outright. Otherwise the caller needs a live, task-scoped grant.
        """
        cls = resource.classification
        if cls is None or cls in ("PUBLIC", "INTERNAL"):
            if self.has(Perm.EVIDENCE_READ) or self.has(Perm.KNOWLEDGE_READ):
                return Decision(True, "CLASSIFICATION_OPEN")

        needed = CLASS_PERMISSION.get(cls or "")
        if needed and self.has(needed):
            return Decision(True, "ROLE")

        grant = self._find_grant(resource, task_id=task_id, now=now)
        if grant is not None:
            return Decision(True, "GRANT", grant_id=grant.id)

        return Decision(
            False,
            "DENIED_NO_GRANT",
            owning_department_id=resource.owning_department_id,
        )

    def _find_grant(
        self,
        resource: ResourceRef,
        *,
        task_id: uuid.UUID | None,
        now: datetime | None = None,
    ) -> AccessGrant | None:
        if self.ctx.actor_user_id is None:
            return None
        stmt: Select = select(AccessGrant).where(
            AccessGrant.org_id == self.ctx.org_id,
            AccessGrant.grantee_user_id == self.ctx.actor_user_id,
            AccessGrant.resource_type == resource.resource_type,
            AccessGrant.resource_id == resource.resource_id,
            active_grant_clause(now),
        )
        # Grants are always task-scoped; a caller acting outside a task context
        # cannot rely on one.
        if task_id is None:
            return None
        stmt = stmt.where(AccessGrant.task_id == task_id)
        return self.session.execute(stmt.limit(1)).scalar_one_or_none()

    # -- query filtering ----------------------------------------------------
    def readable_resource_ids(
        self,
        resource_type: GrantResourceType,
        *,
        task_id: uuid.UUID,
        now: datetime | None = None,
    ) -> Select:
        """Subquery of resource ids this caller may read via a live grant.

        Retrieval composes this into its candidate query so permission filtering
        happens in SQL, never after ranking.
        """
        return select(AccessGrant.resource_id).where(
            AccessGrant.org_id == self.ctx.org_id,
            AccessGrant.grantee_user_id == self.ctx.actor_user_id,
            AccessGrant.resource_type == resource_type,
            AccessGrant.task_id == task_id,
            active_grant_clause(now),
        )

    def denial_remediation(self, resource: ResourceRef) -> dict:
        """The payload that lets the Copilot offer the request flow rather than
        returning a dead end."""
        return {
            "action": "cross_department_request",
            "resource_type": resource.resource_type.value,
            "owning_department_id": (
                str(resource.owning_department_id) if resource.owning_department_id else None
            ),
            "request_types": ["REQUEST_ANSWER", "REQUEST_EVIDENCE", "REQUEST_ACCESS"],
            "preferred": "REQUEST_ANSWER",
        }


__all__ = [
    "PermissionEvaluator",
    "ResourceRef",
    "Decision",
    "active_grant_clause",
    "or_",
]
