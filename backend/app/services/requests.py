"""Cross-department requests: asking across a boundary without dissolving it.

The problem this solves is real and specific. A Security contributor drafting a
staffing answer needs to know whether a named engineer is available. The naive
fixes are both wrong: giving Security read access to HR records dissolves the
boundary, and refusing outright stops the bid. So the ask is made explicit, and
the person who owns the information chooses how much to give.

The three resolutions are offered in least-privilege order, and they are
genuinely different:

* **Answer** creates no grant at all. The answer text enters the requester's
  task context; the underlying record does not.
* **Share evidence** creates one narrow, task-scoped, expiring READ grant per
  shared item.
* **Grant access** creates the broader task-scoped grant that was asked for.

A request row records what was asked and what was decided. It never carries
authority itself -- authority lives only in `access_grant`, which is time-bounded
and revocable. Rejecting is a first-class outcome and requires a reason, because
"no" is information the requester needs in order to find another route.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import IllegalTransition, NotFound, ValidationError
from app.core.logging import get_logger
from app.models import (
    AccessGrant,
    CrossDepartmentRequest,
    Department,
    RequirementRequest,
    Task,
)
from app.models.enums import (
    GrantPermission,
    GrantResourceType,
    RequestState,
    RequestType,
    TaskState,
)
from app.services import access_grants, audit, notifications, tasks

log = get_logger("requests")

OPEN_STATES = frozenset({RequestState.PENDING, RequestState.ACKNOWLEDGED})

RESOLVED_STATES = frozenset(
    {
        RequestState.ANSWERED,
        RequestState.EVIDENCE_SHARED,
        RequestState.ACCESS_GRANTED,
        RequestState.REJECTED,
        RequestState.CANCELLED,
        RequestState.EXPIRED,
    }
)

ALLOWED_TRANSITIONS: dict[RequestState, frozenset[RequestState]] = {
    RequestState.PENDING: frozenset(
        {
            RequestState.ACKNOWLEDGED,
            RequestState.ANSWERED,
            RequestState.EVIDENCE_SHARED,
            RequestState.ACCESS_GRANTED,
            RequestState.REJECTED,
            RequestState.CANCELLED,
            RequestState.EXPIRED,
        }
    ),
    RequestState.ACKNOWLEDGED: frozenset(
        {
            RequestState.ANSWERED,
            RequestState.EVIDENCE_SHARED,
            RequestState.ACCESS_GRANTED,
            RequestState.REJECTED,
            RequestState.CANCELLED,
            RequestState.EXPIRED,
        }
    ),
}

# What a task's state becomes while it waits, by request type.
WAITING_STATE = {
    RequestType.REQUEST_ANSWER: TaskState.WAITING_FOR_DEPENDENCY,
    RequestType.REQUEST_EVIDENCE: TaskState.WAITING_FOR_DEPENDENCY,
    RequestType.REQUEST_ACCESS: TaskState.WAITING_FOR_ACCESS,
}

DEFAULT_DUE = timedelta(days=2)


def create_request(
    session: Session,
    ctx: ExecutionContext,
    *,
    origin_task_id: uuid.UUID,
    target_department_id: uuid.UUID,
    request_type: RequestType,
    request_text: str,
    business_reason: str,
    target_user_id: uuid.UUID | None = None,
    requested_resource_type: str | None = None,
    requested_resource_id: uuid.UUID | None = None,
    requirement_ids: list[uuid.UUID] | None = None,
    due_at: datetime | None = None,
    clarification_id: uuid.UUID | None = None,
    section: str | None = None,
) -> CrossDepartmentRequest:
    if not (request_text or "").strip():
        raise ValidationError("A request needs to say what is being asked for.")
    if not (business_reason or "").strip():
        # The reason is not paperwork. It is what lets the person on the other
        # side decide how much to give, and what an auditor reads later.
        raise ValidationError("A request needs a business reason: what the answer is needed for.")
    if ctx.actor_user_id is None:
        raise ValidationError("A cross-department request must have a requester.")

    task = tasks.get_task(session, ctx, task_id=origin_task_id)
    target = session.get(Department, target_department_id)
    if target is None or target.org_id != ctx.org_id:
        raise NotFound("That department does not exist.")
    if target.id == task.department_id:
        raise ValidationError(
            "That department already owns this task; no cross-department request is needed."
        )
    if not notifications.department_members(session, ctx, department_id=target.id):
        # Saved-but-unseen is worse than refused: the task would sit "waiting" on
        # a room nobody is in.
        raise ValidationError(
            f"Nobody is in {target.name} yet, so nobody would see this request. "
            "Ask an administrator to add someone to the department, or pick another one."
        )
    if request_type is RequestType.REQUEST_ACCESS and requested_resource_id is None:
        raise ValidationError("An access request must name the resource it wants.")

    request = CrossDepartmentRequest(
        org_id=ctx.org_id,
        bid_id=task.bid_id,
        origin_task_id=task.id,
        requester_user_id=ctx.actor_user_id,
        requester_department_id=task.department_id,
        target_department_id=target.id,
        target_user_id=target_user_id,
        request_type=request_type,
        request_text=request_text.strip(),
        business_reason=business_reason.strip(),
        requested_resource_type=requested_resource_type,
        requested_resource_id=requested_resource_id,
        state=RequestState.PENDING,
        clarification_id=clarification_id,
        due_at=due_at or datetime.now(timezone.utc) + DEFAULT_DUE,
    )
    session.add(request)
    session.flush()

    for requirement_id in requirement_ids or []:
        session.add(
            RequirementRequest(
                org_id=ctx.org_id, request_id=request.id, requirement_id=requirement_id
            )
        )
    session.flush()

    # The parts of the response draft that rest on this answer are marked as waiting, so
    # whatever the writer puts there in the meantime is not mistaken for final.
    from app.models import ClarificationRequirement
    from app.services import draft_holds

    marked = list(requirement_ids or [])
    if clarification_id is not None:
        marked += list(
            session.execute(
                select(ClarificationRequirement.requirement_id).where(
                    ClarificationRequirement.org_id == ctx.org_id,
                    ClarificationRequirement.clarification_id == clarification_id,
                )
            ).scalars()
        )
    if (marked or section) and request_type is not RequestType.REQUEST_ACCESS:
        draft_holds.place(
            session,
            ctx,
            task_id=task.id,
            requirement_ids=list(dict.fromkeys(marked)),
            section=section,
            department_id=target.id,
            request_id=request.id,
            clarification_id=clarification_id,
            note=request_text.strip()[:300],
        )

    # The task is visibly waiting. A task that looks in-progress while it is
    # actually stuck is how a deadline arrives with nothing done.
    waiting = WAITING_STATE[request_type]
    if waiting in tasks.ALLOWED_TRANSITIONS.get(task.state, frozenset()):
        tasks.transition(
            session,
            ctx,
            task_id=task.id,
            to_state=waiting,
            reason=f"Waiting on a {request_type.value} to {target.code.value}.",
        )

    audit.record(
        session,
        ctx,
        action="request.created",
        entity_type="cross_department_request",
        entity_id=request.id,
        bid_id=task.bid_id,
        after={
            "type": request_type.value,
            "target_department": target.code.value,
            "origin_task_id": str(task.id),
        },
        reason=business_reason,
    )
    _notify_department(
        session,
        ctx,
        request,
        kind="request.created",
        title=f"{target.name} has a new {request_type.value.replace('_', ' ').lower()}",
    )
    return request


# --- telling people -----------------------------------------------------------
#
# Routing lives here rather than at each audit call so the six resolutions
# cannot drift apart. Every one of them notifies exactly one side: the
# department that was asked, or the person who asked. A notification carries
# the request's own words -- never the answer, never a shared item, never
# anything the recipient could not already open -- because the resolution is
# already governed by the request itself and duplicating it here would route
# material around the permission that guards it.


def _notify_department(
    session: Session,
    ctx: ExecutionContext,
    request: CrossDepartmentRequest,
    *,
    kind: str,
    title: str,
) -> None:
    notifications.notify(
        session,
        ctx,
        recipients=notifications.department_members(
            session, ctx, department_id=request.target_department_id
        ),
        kind=kind,
        title=title,
        body=request.request_text,
        screen="requests",
        bid_id=request.bid_id,
        focus_id=request.id,
    )


def _notify_requester(
    session: Session,
    ctx: ExecutionContext,
    request: CrossDepartmentRequest,
    *,
    kind: str,
    title: str,
) -> None:
    # Landing on the task, not on the request: what the requester wants next is
    # to use the answer, and the answer reaches them as authorized context on
    # the task that was blocked.
    notifications.notify(
        session,
        ctx,
        recipients=[request.requester_user_id],
        kind=kind,
        title=title,
        body=request.request_text,
        screen="tasks",
        bid_id=request.bid_id,
        focus_id=request.origin_task_id,
    )


def get_request(
    session: Session, ctx: ExecutionContext, *, request_id: uuid.UUID
) -> CrossDepartmentRequest:
    request = session.get(CrossDepartmentRequest, request_id)
    if request is None or request.org_id != ctx.org_id:
        raise NotFound("That request does not exist.")
    return request


def _move(
    session: Session,
    ctx: ExecutionContext,
    request: CrossDepartmentRequest,
    to_state: RequestState,
) -> None:
    allowed = ALLOWED_TRANSITIONS.get(request.state, frozenset())
    if to_state not in allowed:
        raise IllegalTransition(
            f"A request cannot move from {request.state.value} to {to_state.value}."
        )
    request.state = to_state
    if to_state in RESOLVED_STATES:
        request.resolved_at = datetime.now(timezone.utc)
        request.resolved_by = ctx.actor_user_id
    session.flush()


def acknowledge(
    session: Session, ctx: ExecutionContext, *, request_id: uuid.UUID
) -> CrossDepartmentRequest:
    """Seen, not yet answered. Worth its own state: the requester learns the
    difference between 'ignored' and 'being worked on'."""
    request = get_request(session, ctx, request_id=request_id)
    _move(session, ctx, request, RequestState.ACKNOWLEDGED)
    audit.record(
        session,
        ctx,
        action="request.acknowledged",
        entity_type="cross_department_request",
        entity_id=request.id,
        bid_id=request.bid_id,
    )
    _notify_requester(
        session,
        ctx,
        request,
        kind="request.acknowledged",
        title="Your request has been seen, not yet answered",
    )
    return request


def _release_origin_task(
    session: Session, ctx: ExecutionContext, request: CrossDepartmentRequest, *, reason: str
) -> None:
    task = session.get(Task, request.origin_task_id)
    if task is None:
        return
    if _has_other_open_requests(session, ctx, request):
        return
    if TaskState.IN_PROGRESS in tasks.ALLOWED_TRANSITIONS.get(task.state, frozenset()):
        tasks.transition(
            session, ctx, task_id=task.id, to_state=TaskState.IN_PROGRESS, reason=reason
        )


def _has_other_open_requests(
    session: Session, ctx: ExecutionContext, request: CrossDepartmentRequest
) -> bool:
    """A task blocked on three requests is not unblocked by resolving one."""
    rows = (
        session.execute(
            select(CrossDepartmentRequest.id).where(
                CrossDepartmentRequest.org_id == ctx.org_id,
                CrossDepartmentRequest.origin_task_id == request.origin_task_id,
                CrossDepartmentRequest.id != request.id,
                CrossDepartmentRequest.state.in_(list(OPEN_STATES)),
            )
        )
        .scalars()
        .all()
    )
    return bool(rows)


def answer(
    session: Session,
    ctx: ExecutionContext,
    *,
    request_id: uuid.UUID,
    resolution_text: str,
    evidence_ids: list[uuid.UUID] | None = None,
) -> CrossDepartmentRequest:
    """Provide Answer: the answer travels, and optionally the evidence behind it.

    Without evidence no grant is created -- the least-privilege resolution.
    With evidence, each item becomes a narrow grant scoped to the asking task,
    exactly as `share_evidence` makes them, so one reply can say "here is the
    answer, and here is the document that proves it".
    """
    if not (resolution_text or "").strip():
        raise ValidationError("An answer needs text.")
    request = get_request(session, ctx, request_id=request_id)
    request.resolution_text = resolution_text.strip()
    grants = []
    if evidence_ids:
        grants = access_grants.share_evidence(
            session,
            ctx,
            grantee_user_id=request.requester_user_id,
            task_id=request.origin_task_id,
            evidence_ids=list(evidence_ids),
            reason=f"Evidence behind the answer to request {request.id}.",
            origin_request_id=request.id,
        )
        request.shared_evidence_ids = list(evidence_ids)
    _move(session, ctx, request, RequestState.ANSWERED)
    _release_origin_task(session, ctx, request, reason="The requested answer was provided.")
    audit.record(
        session,
        ctx,
        action="request.answered",
        entity_type="cross_department_request",
        entity_id=request.id,
        bid_id=request.bid_id,
        after={"granted_access": False, "evidence_attached": len(grants)},
    )
    _notify_requester(
        session,
        ctx,
        request,
        kind="request.answered",
        title=(
            f"Your request was answered, with {len(grants)} evidence item(s)"
            if grants
            else "Your request was answered"
        ),
    )
    return request


def share_evidence(
    session: Session,
    ctx: ExecutionContext,
    *,
    request_id: uuid.UUID,
    evidence_ids: list[uuid.UUID],
    reason: str,
    expires_at: datetime | None = None,
) -> tuple[CrossDepartmentRequest, list[AccessGrant]]:
    """Share Evidence: one narrow, task-scoped, expiring grant per item.

    The grants are scoped to the origin task, not to the requester generally.
    When the task ends, so does the access -- which is the whole difference
    between sharing a document and handing over a filing cabinet key.
    """
    if not evidence_ids:
        raise ValidationError("Sharing evidence requires at least one evidence item.")
    request = get_request(session, ctx, request_id=request_id)
    grants = access_grants.share_evidence(
        session,
        ctx,
        grantee_user_id=request.requester_user_id,
        task_id=request.origin_task_id,
        evidence_ids=evidence_ids,
        reason=reason or f"Shared in response to request {request.id}.",
        expires_at=expires_at,
        origin_request_id=request.id,
    )
    request.shared_evidence_ids = list(evidence_ids)
    request.resolution_text = reason
    _move(session, ctx, request, RequestState.EVIDENCE_SHARED)
    _release_origin_task(session, ctx, request, reason="Evidence was shared.")
    audit.record(
        session,
        ctx,
        action="request.evidence_shared",
        entity_type="cross_department_request",
        entity_id=request.id,
        bid_id=request.bid_id,
        after={"evidence_count": len(grants), "task_scoped": True},
        reason=reason,
    )
    _notify_requester(
        session,
        ctx,
        request,
        kind="request.evidence_shared",
        title=f"{len(grants)} evidence item(s) shared with your task",
    )
    return request, grants


def grant_access(
    session: Session,
    ctx: ExecutionContext,
    *,
    request_id: uuid.UUID,
    resource_type: GrantResourceType,
    resource_id: uuid.UUID,
    reason: str,
    permission: GrantPermission = GrantPermission.READ,
    expires_at: datetime | None = None,
) -> tuple[CrossDepartmentRequest, AccessGrant]:
    """Grant Access: the broadest resolution, and still task-scoped and expiring."""
    request = get_request(session, ctx, request_id=request_id)
    grant = access_grants.create_grant(
        session,
        ctx,
        grantee_user_id=request.requester_user_id,
        task_id=request.origin_task_id,
        resource_type=resource_type,
        resource_id=resource_id,
        permission=permission,
        reason=reason or f"Granted in response to request {request.id}.",
        expires_at=expires_at,
        origin_request_id=request.id,
    )
    request.access_grant_id = grant.id
    request.resolution_text = reason
    _move(session, ctx, request, RequestState.ACCESS_GRANTED)
    _release_origin_task(session, ctx, request, reason="Access was granted.")
    audit.record(
        session,
        ctx,
        action="request.access_granted",
        entity_type="cross_department_request",
        entity_id=request.id,
        bid_id=request.bid_id,
        after={
            "grant_id": str(grant.id),
            "resource_type": resource_type.value,
            "expires_at": grant.expires_at.isoformat(),
        },
        reason=reason,
    )
    _notify_requester(
        session,
        ctx,
        request,
        kind="request.access_granted",
        title="Task-scoped access was granted to you",
    )
    return request, grant


def reject(
    session: Session, ctx: ExecutionContext, *, request_id: uuid.UUID, reason: str
) -> CrossDepartmentRequest:
    """Rejecting is a legitimate answer, and the reason is mandatory.

    The origin task goes to BLOCKED rather than back to IN_PROGRESS: the
    contributor still has a gap, and pretending otherwise is how an
    unsubstantiated claim reaches a customer.
    """
    if not (reason or "").strip():
        raise ValidationError("Rejecting a request requires a reason.")
    request = get_request(session, ctx, request_id=request_id)
    request.rejection_reason = reason.strip()
    _move(session, ctx, request, RequestState.REJECTED)

    task = session.get(Task, request.origin_task_id)
    if task is not None and TaskState.BLOCKED in tasks.ALLOWED_TRANSITIONS.get(
        task.state, frozenset()
    ):
        tasks.transition(
            session,
            ctx,
            task_id=task.id,
            to_state=TaskState.BLOCKED,
            reason=f"A cross-department request was rejected: {reason.strip()}",
        )

    audit.record(
        session,
        ctx,
        action="request.rejected",
        entity_type="cross_department_request",
        entity_id=request.id,
        bid_id=request.bid_id,
        reason=reason,
    )
    _notify_requester(
        session,
        ctx,
        request,
        kind="request.rejected",
        title="Your request was declined",
    )
    return request


def cancel(
    session: Session, ctx: ExecutionContext, *, request_id: uuid.UUID, reason: str | None = None
) -> CrossDepartmentRequest:
    request = get_request(session, ctx, request_id=request_id)
    request.resolution_text = reason
    _move(session, ctx, request, RequestState.CANCELLED)
    _release_origin_task(session, ctx, request, reason="The request was withdrawn.")
    audit.record(
        session,
        ctx,
        action="request.cancelled",
        entity_type="cross_department_request",
        entity_id=request.id,
        bid_id=request.bid_id,
        reason=reason,
    )
    return request


def expire_overdue(session: Session, ctx: ExecutionContext, *, now: datetime | None = None) -> int:
    """Close out requests nobody answered.

    An unanswered request is not the same as a rejected one, so it gets its own
    terminal state. The origin task is deliberately NOT released: silence
    resolved nothing.
    """
    moment = now or datetime.now(timezone.utc)
    rows = (
        session.execute(
            select(CrossDepartmentRequest).where(
                CrossDepartmentRequest.org_id == ctx.org_id,
                CrossDepartmentRequest.state.in_(list(OPEN_STATES)),
                CrossDepartmentRequest.due_at < moment,
            )
        )
        .scalars()
        .all()
    )
    for request in rows:
        _move(session, ctx, request, RequestState.EXPIRED)
        audit.record(
            session,
            ctx,
            action="request.expired",
            entity_type="cross_department_request",
            entity_id=request.id,
            bid_id=request.bid_id,
        )
    return len(rows)


# --- reading -----------------------------------------------------------------


def inbox(
    session: Session,
    ctx: ExecutionContext,
    *,
    department_id: uuid.UUID | None = None,
    open_only: bool = True,
) -> list[CrossDepartmentRequest]:
    """What a department has been asked for."""
    query = select(CrossDepartmentRequest).where(CrossDepartmentRequest.org_id == ctx.org_id)
    if department_id is not None:
        query = query.where(CrossDepartmentRequest.target_department_id == department_id)
    if open_only:
        query = query.where(CrossDepartmentRequest.state.in_(list(OPEN_STATES)))
    return list(
        session.execute(query.order_by(CrossDepartmentRequest.created_at.desc())).scalars().all()
    )


def outbox(
    session: Session,
    ctx: ExecutionContext,
    *,
    origin_task_id: uuid.UUID | None = None,
    bid_id: uuid.UUID | None = None,
    open_only: bool = False,
) -> list[CrossDepartmentRequest]:
    """What has been asked of others, from a task or across a bid."""
    query = select(CrossDepartmentRequest).where(CrossDepartmentRequest.org_id == ctx.org_id)
    if origin_task_id is not None:
        query = query.where(CrossDepartmentRequest.origin_task_id == origin_task_id)
    if bid_id is not None:
        query = query.where(CrossDepartmentRequest.bid_id == bid_id)
    if open_only:
        query = query.where(CrossDepartmentRequest.state.in_(list(OPEN_STATES)))
    return list(
        session.execute(query.order_by(CrossDepartmentRequest.created_at.desc())).scalars().all()
    )


def resolution_summary(session: Session, ctx: ExecutionContext, *, request_id: uuid.UUID) -> dict:
    """What was asked, what was decided, and exactly what access it created.

    The last part is the point. 'Resolved' tells a reviewer nothing about
    whether the boundary held; the grant list does.
    """
    request = get_request(session, ctx, request_id=request_id)
    grants = (
        session.execute(
            select(AccessGrant).where(
                AccessGrant.org_id == ctx.org_id, AccessGrant.origin_request_id == request.id
            )
        )
        .scalars()
        .all()
    )
    return {
        "request_id": str(request.id),
        "type": request.request_type.value,
        "state": request.state.value,
        "asked": request.request_text,
        "business_reason": request.business_reason,
        "resolution_text": request.resolution_text,
        "rejection_reason": request.rejection_reason,
        "grants_created": [
            {
                "id": str(g.id),
                "resource_type": g.resource_type.value,
                "resource_id": str(g.resource_id),
                "permission": g.permission.value,
                "task_id": str(g.task_id),
                "expires_at": g.expires_at.isoformat(),
                "revoked_at": g.revoked_at.isoformat() if g.revoked_at else None,
            }
            for g in grants
        ],
    }
