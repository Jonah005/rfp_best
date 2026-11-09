"""A person's to-do list: what they must answer, review, finish or approve.

Notifications say that something happened; they are no way back to the exact part
of the work that needs a person. This builds the list from the records themselves
(requests, tasks, gaps, approvals) so it is always current, and gives every item a
link that opens the task on the very requirement or missing part concerned.

Nothing here is stored except one mark: a reply the person has dealt with.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.models import (
    Approval,
    Bid,
    ClarificationIssue,
    CrossDepartmentRequest,
    Department,
    Opportunity,
    RequirementAssessment,
    RequirementPosition,
    RequirementRequest,
    Task,
    TaskGap,
    TaskParticipant,
)
from app.models.enums import ApprovalStepState, ClarificationStatus, RequestState, TaskState
from app.services import tasks as task_service
from app.services.requests import OPEN_STATES

# A department has answered, shared, granted access or declined: the asker must act on it.
REPLY_STATES = frozenset(
    {
        RequestState.ANSWERED,
        RequestState.EVIDENCE_SHARED,
        RequestState.ACCESS_GRANTED,
        RequestState.REJECTED,
    }
)
_HAVE = {"CURRENT_VERIFIED", "HISTORICAL_VERIFIED", "PARTIAL"}
_LIVE_GAP = (ClarificationStatus.OPEN, ClarificationStatus.SENT_TO_CLIENT)
_DONE_TASK = {TaskState.COMPLETED}


def _department_name(departments: dict, request) -> str | None:
    """The requesting department's name, if the request names one."""
    if request.requester_department_id is None:
        return None
    return departments.get(request.requester_department_id)


def reply_text(request: CrossDepartmentRequest) -> str:
    if request.state is RequestState.REJECTED:
        return "Declined: " + (request.rejection_reason or "no reason given")
    if request.resolution_text:
        return request.resolution_text
    if request.state is RequestState.EVIDENCE_SHARED:
        return "Shared evidence for this task."
    return "Access was granted."


def mark_reviewed(
    session: Session,
    ctx: ExecutionContext,
    *,
    request_ids: list[uuid.UUID] | None = None,
    task_id: uuid.UUID | None = None,
    requirement_ids: list[uuid.UUID] | None = None,
    clarification_id: uuid.UUID | None = None,
) -> int:
    """The person who asked has dealt with these replies. Only the asker's own are touched."""
    query = select(CrossDepartmentRequest).where(
        CrossDepartmentRequest.org_id == ctx.org_id,
        CrossDepartmentRequest.requester_user_id == ctx.actor_user_id,
        CrossDepartmentRequest.state.in_(list(REPLY_STATES)),
        CrossDepartmentRequest.reviewed_at.is_(None),
    )
    if request_ids is not None:
        query = query.where(CrossDepartmentRequest.id.in_(request_ids or [uuid.UUID(int=0)]))
    if task_id is not None:
        query = query.where(CrossDepartmentRequest.origin_task_id == task_id)
    if requirement_ids is not None:
        linked = select(RequirementRequest.request_id).where(
            RequirementRequest.org_id == ctx.org_id,
            RequirementRequest.requirement_id.in_(requirement_ids or [uuid.UUID(int=0)]),
        )
        query = query.where(CrossDepartmentRequest.id.in_(linked))
    if clarification_id is not None:
        query = query.where(CrossDepartmentRequest.clarification_id == clarification_id)
    now = datetime.now(timezone.utc)
    rows = list(session.execute(query).scalars())
    for row in rows:
        row.reviewed_at = now
    session.flush()
    return len(rows)


def replies_by_requirement(
    session: Session, ctx: ExecutionContext, task_id: uuid.UUID
) -> dict[uuid.UUID, list[dict]]:
    """Unhandled replies on this task's requirements, for the requirement rows."""
    rows = session.execute(
        select(RequirementRequest.requirement_id, CrossDepartmentRequest, Department.name)
        .join(CrossDepartmentRequest, CrossDepartmentRequest.id == RequirementRequest.request_id)
        .join(Department, Department.id == CrossDepartmentRequest.target_department_id)
        .where(
            RequirementRequest.org_id == ctx.org_id,
            CrossDepartmentRequest.origin_task_id == task_id,
            CrossDepartmentRequest.requester_user_id == ctx.actor_user_id,
            CrossDepartmentRequest.state.in_(list(REPLY_STATES)),
            CrossDepartmentRequest.reviewed_at.is_(None),
        )
        .order_by(CrossDepartmentRequest.created_at)
    ).all()
    out: dict[uuid.UUID, list[dict]] = {}
    for requirement_id, request, department in rows:
        out.setdefault(requirement_id, []).append(
            {
                "request_id": request.id,
                "department": department,
                "state": request.state.value,
                "text": reply_text(request),
            }
        )
    return out


def _href(bid_id, task_id, target: str | None = None) -> str:
    base = f"/tasks?bid={bid_id}&focus={task_id}"
    return base + (f"&open={quote(target, safe=':,')}" if target else "")


def _snip(text: str | None, n: int = 140) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def for_user(session: Session, ctx: ExecutionContext) -> dict:
    """Everything waiting on the signed-in person, grouped by what they must do."""
    if ctx.actor_user_id is None:
        return {"groups": [], "counts": {}}
    me = ctx.actor_user_id

    participant = select(TaskParticipant.task_id).where(
        TaskParticipant.org_id == ctx.org_id,
        TaskParticipant.user_id == me,
        TaskParticipant.removed_at.is_(None),
    )
    mine = list(
        session.execute(
            select(Task).where(
                Task.org_id == ctx.org_id,
                (Task.owner_user_id == me) | Task.id.in_(participant),
            )
        ).scalars()
    )
    my_ids = {t.id for t in mine}

    asked_by_me = list(
        session.execute(
            select(CrossDepartmentRequest)
            .where(
                CrossDepartmentRequest.org_id == ctx.org_id,
                CrossDepartmentRequest.requester_user_id == me,
            )
            .order_by(CrossDepartmentRequest.created_at.desc())
        ).scalars()
    )
    incoming = list(
        session.execute(
            select(CrossDepartmentRequest)
            .where(
                CrossDepartmentRequest.org_id == ctx.org_id,
                CrossDepartmentRequest.target_department_id.in_(
                    list(ctx.department_ids) or [uuid.UUID(int=0)]
                ),
                CrossDepartmentRequest.state.in_(list(OPEN_STATES)),
            )
            .order_by(CrossDepartmentRequest.created_at)
        ).scalars()
    )

    # Names for everything shown: bids, tasks, departments.
    task_ids = (
        my_ids | {r.origin_task_id for r in asked_by_me} | {r.origin_task_id for r in incoming}
    )
    tasks = {
        t.id: t
        for t in session.execute(
            select(Task).where(
                Task.org_id == ctx.org_id, Task.id.in_(task_ids or {uuid.UUID(int=0)})
            )
        ).scalars()
    }
    bid_labels = {
        bid_id: f"{customer} · {tender}"
        for bid_id, customer, tender in session.execute(
            select(Bid.id, Opportunity.customer_name, Opportunity.tender_name)
            .join(Opportunity, Opportunity.id == Bid.opportunity_id)
            .where(
                Bid.org_id == ctx.org_id,
                Bid.id.in_({t.bid_id for t in tasks.values()} or {uuid.UUID(int=0)}),
            )
        ).all()
    }
    departments = {
        d.id: d.name
        for d in session.execute(
            select(Department).where(Department.org_id == ctx.org_id)
        ).scalars()
    }
    linked_reqs: dict[uuid.UUID, list[uuid.UUID]] = {}
    ids = [r.id for r in asked_by_me]
    if ids:
        for request_id, requirement_id in session.execute(
            select(RequirementRequest.request_id, RequirementRequest.requirement_id).where(
                RequirementRequest.org_id == ctx.org_id, RequirementRequest.request_id.in_(ids)
            )
        ).all():
            linked_reqs.setdefault(request_id, []).append(requirement_id)

    def base(task_id: uuid.UUID) -> dict:
        t = tasks.get(task_id)
        return {
            "task_id": str(task_id),
            "task_code": t.code if t else None,
            "bid_id": str(t.bid_id) if t else None,
            "bid": bid_labels.get(t.bid_id) if t else None,
        }

    def target_of(request: CrossDepartmentRequest) -> str | None:
        if request.clarification_id:
            return f"gap:{request.clarification_id}"
        reqs = linked_reqs.get(request.id)
        return ("req:" + ",".join(str(i) for i in reqs)) if reqs else None

    review, waiting, answer, do, approve = [], [], [], [], []

    for request in incoming:
        t = tasks.get(request.origin_task_id)
        answer.append(
            {
                "id": f"answer:{request.id}",
                "title": _snip(request.request_text),
                "detail": "From "
                + (_department_name(departments, request) or "another department")
                + (f" · {request.business_reason[:80]}" if request.business_reason else ""),
                "href": f"/requests?bid={t.bid_id if t else ''}&focus={request.id}",
                "at": request.created_at.isoformat(),
                **base(request.origin_task_id),
            }
        )

    for request in asked_by_me:
        dept = departments.get(request.target_department_id) or "A department"
        t = tasks.get(request.origin_task_id)
        if t is None:
            continue
        href = _href(t.bid_id, t.id, target_of(request))
        if request.state in REPLY_STATES and request.reviewed_at is None:
            word = "declined" if request.state is RequestState.REJECTED else "replied"
            review.append(
                {
                    "id": f"review:{request.id}",
                    "title": f"{dept} {word}: {_snip(reply_text(request), 120)}",
                    "detail": f"You asked: {_snip(request.request_text, 100)}",
                    "href": href,
                    "request_id": str(request.id),
                    "at": (request.resolved_at or request.created_at).isoformat(),
                    **base(request.origin_task_id),
                }
            )
        elif request.state in OPEN_STATES:
            waiting.append(
                {
                    "id": f"waiting:{request.id}",
                    "title": f"Waiting on {dept}: {_snip(request.request_text, 120)}",
                    "detail": "Nothing for you to do until they reply.",
                    "href": href,
                    "at": request.created_at.isoformat(),
                    **base(request.origin_task_id),
                }
            )

    # What is still left on each of my tasks.
    asked_open = {
        rid
        for (rid,) in session.execute(
            select(RequirementRequest.requirement_id)
            .join(
                CrossDepartmentRequest,
                CrossDepartmentRequest.id == RequirementRequest.request_id,
            )
            .where(
                RequirementRequest.org_id == ctx.org_id,
                CrossDepartmentRequest.origin_task_id.in_(my_ids or {uuid.UUID(int=0)}),
                CrossDepartmentRequest.state.in_(list(OPEN_STATES) + list(REPLY_STATES)),
                CrossDepartmentRequest.reviewed_at.is_(None),
            )
        ).all()
    }
    decided = {
        (tid, rid)
        for tid, rid in session.execute(
            select(RequirementPosition.task_id, RequirementPosition.requirement_id).where(
                RequirementPosition.org_id == ctx.org_id,
                RequirementPosition.task_id.in_(my_ids or {uuid.UUID(int=0)}),
                RequirementPosition.status.in_(["submitted", "ignored"]),
            )
        ).all()
    }
    for t in sorted(mine, key=lambda x: x.code):
        if t.state in _DONE_TASK:
            continue
        reqs = task_service.task_requirements(session, ctx, task_id=t.id)
        assessed = {
            a.requirement_id: a
            for a in session.execute(
                select(RequirementAssessment).where(
                    RequirementAssessment.org_id == ctx.org_id,
                    RequirementAssessment.is_current.is_(True),
                    RequirementAssessment.requirement_id.in_([r.id for r in reqs] or [None]),
                )
            ).scalars()
        }
        need = []
        for r in reqs:
            a = assessed.get(r.id)
            if (t.id, r.id) in decided or r.id in asked_open:
                continue
            if a and a.bid_readiness.value == "CUSTOMER_OBLIGATION":
                continue
            if a and a.evidence_status.value in _HAVE:
                continue
            need.append(r)
        if need:
            do.append(
                {
                    "id": f"do-req:{t.id}",
                    "title": f"{len(need)} requirement{'s' if len(need) != 1 else ''} still "
                    f"without an answer: {', '.join(r.code for r in need[:4])}"
                    + ("…" if len(need) > 4 else ""),
                    "detail": f"{t.code} · {t.title}",
                    "href": _href(t.bid_id, t.id, "req:" + ",".join(str(r.id) for r in need[:20])),
                    "count": len(need),
                    "at": None,
                    **base(t.id),
                }
            )
        open_gaps = (
            session.execute(
                select(ClarificationIssue)
                .join(
                    TaskGap,
                    TaskGap.clarification_id == ClarificationIssue.id,
                )
                .where(
                    TaskGap.org_id == ctx.org_id,
                    TaskGap.task_id == t.id,
                    TaskGap.attached.is_(True),
                    ClarificationIssue.status.in_(list(_LIVE_GAP)),
                )
            )
            .scalars()
            .all()
        )
        if open_gaps:
            do.append(
                {
                    "id": f"do-gap:{t.id}",
                    "title": f"{len(open_gaps)} missing part{'s' if len(open_gaps) != 1 else ''} "
                    "of the RFP still open",
                    "detail": f"{t.code} · {t.title}",
                    "href": _href(t.bid_id, t.id, "gaps"),
                    "count": len(open_gaps),
                    "at": None,
                    **base(t.id),
                }
            )

    for step in session.execute(
        select(Approval).where(
            Approval.org_id == ctx.org_id,
            Approval.state == ApprovalStepState.PENDING,
            Approval.required_role.in_(list(ctx.role_codes) or [""]),
        )
    ).scalars():
        approve.append(
            {
                "id": f"approve:{step.id}",
                "title": f"Approval waiting: {step.entity_type.replace('_', ' ')}",
                "detail": f"Step {step.step_no} needs the "
                + step.required_role.replace("_", " ").lower(),
                "href": "/approvals",
                "at": step.created_at.isoformat(),
                "task_id": None,
                "task_code": None,
                "bid_id": None,
                "bid": None,
            }
        )

    groups = [
        ("answer", "For you to answer", "Other departments are waiting on you.", answer),
        (
            "review",
            "Answered — for you to review",
            "A department replied. Use it or set it aside.",
            review,
        ),
        (
            "do",
            "Left to do on your tasks",
            "Requirements and missing parts nobody has answered yet.",
            do,
        ),
        ("approve", "Waiting for your approval", "Drafts that need your role's sign-off.", approve),
        ("waiting", "Waiting on others", "You asked; nothing to do until they reply.", waiting),
    ]
    return {
        "groups": [
            {"key": k, "title": title, "hint": hint, "items": items}
            for k, title, hint, items in groups
        ],
        "counts": {k: len(items) for k, _t, _h, items in groups},
    }
