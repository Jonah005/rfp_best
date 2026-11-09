"""Task and cross-department request routes.

The read routes matter as much as the action routes. A task's detail response
carries its requirements with their current readiness, its participants, its
dependencies in both directions and its open request count, because the question
a contributor actually asks is "why can't I finish this?" -- and that answer is
spread across four tables.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_context, get_db, require, require_any
from app.core.context import ExecutionContext
from app.core.errors import ValidationError
from app.core.permissions import Perm
from app.models import (
    CrossDepartmentRequest,
    Department,
    RequirementAssessment,
    RequirementRequest,
    RequirementVersion,
)
from app.models.enums import (
    GrantPermission,
    GrantResourceType,
    ParticipantRole,
    RequestType,
    TaskDependencyType,
    TaskPriority,
    TaskState,
)
from app.schemas.tasks import (
    AnswerIn,
    DependencyIn,
    DependencyOut,
    GrantAccessIn,
    ParticipantIn,
    ParticipantOut,
    RejectIn,
    ReplyOut,
    RequestCreate,
    RequestOut,
    ShareEvidenceIn,
    TaskAssign,
    TaskCreate,
    TaskDetail,
    TaskOut,
    TaskRequirementOut,
    TaskTransition,
)
from app.services import requests as request_service
from app.services import requirement_positions as position_service
from app.services import task_gaps as task_gap_service
from app.services import tasks as task_service
from app.services import todo as todo_service

router = APIRouter(tags=["tasks"])


def _enum(enum_cls, value: str, label: str):
    try:
        return enum_cls(value)
    except ValueError as exc:
        allowed = ", ".join(sorted(m.value for m in enum_cls))
        raise ValidationError(f"Unknown {label} '{value}'. Allowed: {allowed}.") from exc


# --- tasks -------------------------------------------------------------------


@router.post(
    "/bids/{bid_id}/tasks",
    response_model=TaskOut,
    dependencies=[Depends(require(Perm.TASK_WRITE))],
)
def create_task(
    bid_id: uuid.UUID,
    payload: TaskCreate,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> TaskOut:
    task = task_service.create_task(
        session,
        ctx,
        bid_id=bid_id,
        title=payload.title,
        department_id=payload.department_id,
        requirement_ids=payload.requirement_ids,
        description=payload.description,
        owner_user_id=payload.owner_user_id,
        priority=_enum(TaskPriority, payload.priority, "priority"),
        due_date=payload.due_date,
        grouping_rationale=payload.grouping_rationale,
    )
    return TaskOut.model_validate(task)


@router.post(
    "/bids/{bid_id}/tasks/generate",
    response_model=dict,
    dependencies=[Depends(require(Perm.TASK_WRITE))],
)
def generate_tasks(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Group the bid's requirements into department tasks, then seed states."""
    created = task_service.generate_tasks(session, ctx, bid_id=bid_id)
    seeded = task_service.seed_initial_states(session, ctx, bid_id=bid_id)
    return {
        "created": len(created),
        "codes": [t.code for t in created],
        "initial_states": seeded,
    }


@router.get(
    "/bids/{bid_id}/tasks/picker",
    response_model=list[dict],
    dependencies=[Depends(require(Perm.TASK_READ))],
)
def task_picker(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    """The tasks this person chooses between: their departments', and any given to them."""
    return jsonable_encoder(task_service.tasks_for_picker(session, ctx, bid_id=bid_id))


@router.get(
    "/bids/{bid_id}/tasks",
    response_model=list[TaskOut],
    dependencies=[Depends(require(Perm.TASK_READ))],
)
def list_bid_tasks(
    bid_id: uuid.UUID,
    state: str | None = Query(default=None),
    department_id: uuid.UUID | None = Query(default=None),
    owner_user_id: uuid.UUID | None = Query(default=None),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[TaskOut]:
    rows = task_service.list_tasks(
        session,
        ctx,
        bid_id=bid_id,
        state=_enum(TaskState, state, "task state") if state else None,
        department_id=department_id,
        owner_user_id=owner_user_id,
    )
    return [TaskOut.model_validate(t) for t in rows]


@router.get(
    "/bids/{bid_id}/tasks/board",
    response_model=dict,
    dependencies=[Depends(require(Perm.TASK_READ))],
)
def task_board(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Counts by state and department, plus what is blocked, waiting or unowned."""
    return task_service.board(session, ctx, bid_id=bid_id)


@router.get(
    "/tasks/mine",
    response_model=list[TaskOut],
    dependencies=[Depends(require(Perm.TASK_READ))],
)
def my_tasks(
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[TaskOut]:
    if ctx.actor_user_id is None:
        return []
    rows = task_service.list_tasks(session, ctx, owner_user_id=ctx.actor_user_id)
    return [TaskOut.model_validate(t) for t in rows]


@router.get("/me/todo", dependencies=[Depends(require(Perm.TASK_READ))])
def my_todo(
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """What the signed-in person must answer, review, finish or approve, each with a link."""
    return todo_service.for_user(session, ctx)


class ReviewedIn(BaseModel):
    request_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)


@router.post("/me/todo/reviewed", dependencies=[Depends(require(Perm.TASK_READ))])
def replies_reviewed(
    payload: ReviewedIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """The person has dealt with these replies; they leave the to-do list."""
    return {"marked": todo_service.mark_reviewed(session, ctx, request_ids=payload.request_ids)}


@router.get("/me/dashboard", dependencies=[Depends(require(Perm.TASK_READ))])
def my_dashboard(
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """The signed-in person's work, in the columns they think in."""
    return task_service.my_work(session, ctx)


@router.get(
    "/tasks/{task_id}",
    response_model=TaskDetail,
    dependencies=[Depends(require(Perm.TASK_READ))],
)
def get_task(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> TaskDetail:
    """Everything needed to answer 'why can I not finish this task?'."""
    task = task_service.get_task(session, ctx, task_id=task_id)
    requirements = task_service.task_requirements(session, ctx, task_id=task_id)

    rows = session.execute(
        select(RequirementAssessment, RequirementVersion)
        .outerjoin(
            RequirementVersion,
            RequirementVersion.requirement_id == RequirementAssessment.requirement_id,
        )
        .where(
            RequirementAssessment.org_id == ctx.org_id,
            RequirementAssessment.is_current.is_(True),
            RequirementAssessment.requirement_id.in_([r.id for r in requirements] or [None]),
        )
    ).all()
    assessed = {a.requirement_id: a for a, _ in rows}
    # One query for every requirement's current wording, not one per requirement.
    version_ids = [r.current_version_id for r in requirements if r.current_version_id]
    by_version: dict[uuid.UUID, str] = {}
    if version_ids:
        by_version = {
            version.id: version.requirement_text
            for version in session.execute(
                select(RequirementVersion).where(
                    RequirementVersion.org_id == ctx.org_id,
                    RequirementVersion.id.in_(version_ids),
                )
            )
            .scalars()
            .all()
        }
    texts: dict[uuid.UUID, str] = {
        r.id: by_version[r.current_version_id]
        for r in requirements
        if r.current_version_id and r.current_version_id in by_version
    }

    positions = position_service.active_for(session, ctx, task_id)
    replies = todo_service.replies_by_requirement(session, ctx, task_id)
    # Which requirements are waiting on another department, for the screen's colours.
    waiting: dict[uuid.UUID, list[str]] = {}
    for req_id, department in session.execute(
        select(RequirementRequest.requirement_id, Department.name)
        .join(
            CrossDepartmentRequest,
            CrossDepartmentRequest.id == RequirementRequest.request_id,
        )
        .join(Department, Department.id == CrossDepartmentRequest.target_department_id)
        .where(
            RequirementRequest.org_id == ctx.org_id,
            RequirementRequest.requirement_id.in_([r.id for r in requirements] or [None]),
            CrossDepartmentRequest.origin_task_id == task_id,
            CrossDepartmentRequest.state.in_(list(request_service.OPEN_STATES)),
        )
    ).all():
        waiting.setdefault(req_id, []).append(department)

    deps = task_service.dependencies(session, ctx, task_id=task_id)
    open_requests = session.execute(
        select(func.count())
        .select_from(CrossDepartmentRequest)
        .where(
            CrossDepartmentRequest.org_id == ctx.org_id,
            CrossDepartmentRequest.origin_task_id == task_id,
            CrossDepartmentRequest.state.in_(list(request_service.OPEN_STATES)),
        )
    ).scalar_one()

    return TaskDetail(
        task=TaskOut.model_validate(task),
        requirements=[
            TaskRequirementOut(
                requirement_id=r.id,
                code=r.code,
                category=r.category.value,
                is_mandatory=r.is_mandatory,
                applicability=r.applicability.value,
                text=texts.get(r.id),
                bid_readiness=(assessed[r.id].bid_readiness.value if r.id in assessed else None),
                evidence_status=(
                    assessed[r.id].evidence_status.value if r.id in assessed else None
                ),
                asked_departments=sorted(set(waiting.get(r.id, []))),
                position_status=(positions[r.id].status if r.id in positions else None),
                position_text=(positions[r.id].text if r.id in positions else None),
                replies=[ReplyOut(**x) for x in replies.get(r.id, [])],
            )
            for r in requirements
        ],
        participants=[
            ParticipantOut.model_validate(p)
            for p in task_service.participants(session, ctx, task_id=task_id)
        ],
        dependencies_outgoing=[DependencyOut.model_validate(d) for d in deps["outgoing"]],
        dependencies_incoming=[DependencyOut.model_validate(d) for d in deps["incoming"]],
        open_requests=open_requests,
    )


@router.post(
    "/tasks/{task_id}/transition",
    response_model=TaskOut,
    dependencies=[Depends(require(Perm.TASK_WRITE))],
)
def transition_task(
    task_id: uuid.UUID,
    payload: TaskTransition,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> TaskOut:
    task = task_service.transition(
        session,
        ctx,
        task_id=task_id,
        to_state=_enum(TaskState, payload.to_state, "task state"),
        reason=payload.reason,
    )
    return TaskOut.model_validate(task)


@router.get(
    "/tasks/{task_id}/transitions",
    response_model=dict,
    dependencies=[Depends(require(Perm.TASK_READ))],
)
def allowed_transitions(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """What this task may do next, and which moves need a reason.

    Exposed so a caller never has to guess the state machine, and so a UI can
    disable a button rather than offer a move that will be refused.
    """
    task = task_service.get_task(session, ctx, task_id=task_id)
    allowed = task_service.ALLOWED_TRANSITIONS.get(task.state, frozenset())
    return {
        "state": task.state.value,
        "allowed": sorted(s.value for s in allowed),
        "reason_required": sorted(s.value for s in allowed if s in task_service.REASON_REQUIRED),
    }


@router.get(
    "/tasks/{task_id}/request-targets",
    response_model=list[dict],
    dependencies=[Depends(require(Perm.TASK_READ))],
)
def request_targets(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    """Departments this task can ask. Anyone who can read the task can ask one."""
    return task_service.request_targets(session, ctx, task_id=task_id)


@router.get(
    "/tasks/{task_id}/assignable",
    response_model=dict,
    dependencies=[Depends(require(Perm.TASK_READ))],
)
def assignable_owners(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Who the caller may put on this task, and why.

    Anyone who can read the task can ask. An answer of no candidates is a
    normal answer, not a refusal -- it is how a screen knows to show the owner
    as read-only rather than offering a control that would 403.
    """
    return task_service.assignable_owners(session, ctx, task_id=task_id)


@router.post(
    "/tasks/{task_id}/owner",
    response_model=TaskOut,
    # Two permissions open this route and they are not equivalent: the bid
    # manager's is unscoped, a department head's is bounded by their own
    # department and by the proposed owner's membership of it. Only the service
    # can tell them apart -- it needs the task's department, which the router
    # does not have -- so the router admits both and
    # tasks._authorise_assignment decides.
    dependencies=[Depends(require_any(Perm.TASK_ASSIGN, Perm.TASK_ASSIGN_IN_DEPARTMENT))],
)
def assign_owner(
    task_id: uuid.UUID,
    payload: TaskAssign,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> TaskOut:
    task = task_service.assign_owner(
        session, ctx, task_id=task_id, owner_user_id=payload.owner_user_id
    )
    return TaskOut.model_validate(task)


# --- gaps in the tender that concern this task --------------------------------


class GapSettleIn(BaseModel):
    status: str
    assumption: str | None = None
    # "person", "model" (the AI's wording as drafted) or "department" (a reply).
    answer_source: str = "person"
    include_in_response: bool = True


class GapIncludeIn(BaseModel):
    include: bool


@router.get(
    "/tasks/{task_id}/gaps",
    response_model=dict,
    dependencies=[Depends(require(Perm.TASK_READ))],
)
def task_gaps(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """What the RFP leaves out that this task depends on. Never asks the model."""
    return task_gap_service.list_for_task(session, ctx, task_id=task_id)


@router.post(
    "/tasks/{task_id}/gaps/check",
    response_model=dict,
    dependencies=[Depends(require(Perm.TASK_READ))],
)
def check_task_gaps(
    task_id: uuid.UUID,
    force: bool = Query(default=False),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Have the AI decide which gaps concern the task (cached; `force` asks again)."""
    from app.gateway.gateway import build_gateway

    return task_gap_service.check(
        session, ctx, task_id=task_id, gateway=build_gateway(), force=force
    )


@router.post(
    "/tasks/{task_id}/gaps/{clarification_id}/attach",
    response_model=dict,
    dependencies=[Depends(require_any(Perm.TASK_WRITE, Perm.TASK_ASSIGN))],
)
def attach_task_gap(
    task_id: uuid.UUID,
    clarification_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    task_gap_service.attach(session, ctx, task_id=task_id, clarification_id=clarification_id)
    return task_gap_service.list_for_task(session, ctx, task_id=task_id)


@router.post(
    "/tasks/{task_id}/gaps/{clarification_id}/detach",
    response_model=dict,
    dependencies=[Depends(require_any(Perm.TASK_WRITE, Perm.TASK_ASSIGN))],
)
def detach_task_gap(
    task_id: uuid.UUID,
    clarification_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    task_gap_service.detach(session, ctx, task_id=task_id, clarification_id=clarification_id)
    return task_gap_service.list_for_task(session, ctx, task_id=task_id)


@router.post(
    "/bids/{bid_id}/gaps/check-all",
    response_model=dict,
    status_code=202,
    dependencies=[Depends(require(Perm.TASK_ASSIGN))],
)
def check_all_task_gaps(
    bid_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Ask the AI which of the tender's gaps concern each task of this bid, all in one go."""
    return task_gap_service.start_bid_check(session, ctx, bid_id=bid_id)


@router.get(
    "/bids/{bid_id}/gaps/check-status",
    response_model=dict,
    dependencies=[Depends(require(Perm.TASK_READ))],
)
def check_all_task_gaps_status(
    bid_id: uuid.UUID,
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return task_gap_service.bid_check_status(bid_id, ctx.org_id)


@router.post(
    "/tasks/{task_id}/gaps/{clarification_id}/separate",
    response_model=dict,
    dependencies=[Depends(require_any(Perm.TASK_WRITE, Perm.TASK_ASSIGN))],
)
def separate_task_gap(
    task_id: uuid.UUID,
    clarification_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """These two are not the same missing part: show this one on its own again."""
    task_gap_service.separate(session, ctx, task_id=task_id, clarification_id=clarification_id)
    return task_gap_service.list_for_task(session, ctx, task_id=task_id)


class GapChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=1500)
    current: str = Field(default="", max_length=3000)
    conversation_id: uuid.UUID


@router.post(
    "/tasks/{task_id}/gaps/{clarification_id}/chat",
    response_model=dict,
    dependencies=[Depends(require(Perm.CHAT_USE))],
)
def chat_about_gap(
    task_id: uuid.UUID,
    clarification_id: uuid.UUID,
    payload: GapChatIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """One turn of the chat about a single missing part. Records no assumption."""
    from app.gateway.gateway import build_gateway

    return task_gap_service.gap_chat(
        session,
        ctx,
        task_id=task_id,
        clarification_id=clarification_id,
        message=payload.message,
        current=payload.current,
        conversation_id=payload.conversation_id,
        gateway=build_gateway(),
    )


class ReviseIn(BaseModel):
    current: str = Field(default="", max_length=3000)
    instruction: str = Field(min_length=1, max_length=1500)
    conversation_id: uuid.UUID | None = None


@router.post(
    "/tasks/{task_id}/gaps/{clarification_id}/revise",
    response_model=dict,
    dependencies=[Depends(require(Perm.CHAT_USE))],
)
def revise_gap_assumption(
    task_id: uuid.UUID,
    clarification_id: uuid.UUID,
    payload: ReviseIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Change the drafted wording the way the person asked. Records no assumption."""
    from app.gateway.gateway import build_gateway

    return task_gap_service.revise_assumption(
        session,
        ctx,
        task_id=task_id,
        clarification_id=clarification_id,
        current=payload.current,
        instruction=payload.instruction,
        conversation_id=payload.conversation_id,
        gateway=build_gateway(),
    )


@router.post(
    "/tasks/{task_id}/gaps/{clarification_id}/propose",
    response_model=dict,
    dependencies=[Depends(require(Perm.CHAT_USE))],
)
def propose_gap_assumption(
    task_id: uuid.UUID,
    clarification_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Draft an assumption from this task's approved evidence. Records nothing."""
    from app.gateway.gateway import build_gateway

    return task_gap_service.propose_assumption(
        session,
        ctx,
        task_id=task_id,
        clarification_id=clarification_id,
        gateway=build_gateway(),
    )


@router.post(
    "/tasks/{task_id}/gaps/{clarification_id}/settle",
    response_model=dict,
    dependencies=[Depends(require_any(Perm.TASK_WRITE, Perm.TASK_ASSIGN))],
)
def settle_task_gap(
    task_id: uuid.UUID,
    clarification_id: uuid.UUID,
    payload: GapSettleIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Record the company's assumption, or close the gap as no impact / not real."""
    from app.models.enums import ClarificationStatus

    try:
        status = ClarificationStatus(payload.status)
    except ValueError as exc:
        raise ValidationError("Unknown status.") from exc
    task_gap_service.settle(
        session,
        ctx,
        task_id=task_id,
        clarification_id=clarification_id,
        status=status,
        assumption=payload.assumption,
        answer_source=payload.answer_source,
        include=payload.include_in_response,
    )
    return task_gap_service.list_for_task(session, ctx, task_id=task_id)


@router.post(
    "/tasks/{task_id}/gaps/{clarification_id}/include",
    response_model=dict,
    dependencies=[Depends(require_any(Perm.TASK_WRITE, Perm.TASK_ASSIGN))],
)
def include_task_gap(
    task_id: uuid.UUID,
    clarification_id: uuid.UUID,
    payload: GapIncludeIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Choose whether the response rests on this recorded assumption."""
    task_gap_service.set_include(
        session,
        ctx,
        task_id=task_id,
        clarification_id=clarification_id,
        include=payload.include,
    )
    return task_gap_service.list_for_task(session, ctx, task_id=task_id)


# --- what to say about a requirement with no evidence --------------------------


class PositionIds(BaseModel):
    requirement_ids: list[uuid.UUID] = Field(min_length=1, max_length=12)


class PositionFormalise(PositionIds):
    option: int | None = None
    own_words: str | None = Field(default=None, max_length=1500)
    instruction: str | None = Field(default=None, max_length=500)


class PositionSubmit(PositionIds):
    text: str = Field(min_length=1, max_length=1500)
    source: str = "person"
    option: int | None = None


class PositionIgnore(PositionIds):
    reason: str | None = Field(default=None, max_length=500)


_DECIDE = [Depends(require_any(Perm.TASK_WRITE, Perm.TASK_ASSIGN))]


@router.get(
    "/tasks/{task_id}/positions",
    response_model=list[dict],
    dependencies=[Depends(require(Perm.TASK_READ))],
)
def list_positions(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    """Every answer submitted and every requirement set aside, with who decided and when."""
    return position_service.decisions(session, ctx, task_id=task_id)


@router.post(
    "/tasks/{task_id}/positions/propose",
    response_model=dict,
    dependencies=[Depends(require(Perm.CHAT_USE))],
)
def propose_positions(
    task_id: uuid.UUID,
    payload: PositionIds,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """What was found for these requirements, and ways the response could handle them."""
    from app.gateway.gateway import build_gateway

    return position_service.propose(
        session,
        ctx,
        task_id=task_id,
        requirement_ids=payload.requirement_ids,
        gateway=build_gateway(),
    )


@router.post(
    "/tasks/{task_id}/positions/formalise",
    response_model=dict,
    dependencies=[Depends(require(Perm.CHAT_USE))],
)
def formalise_position(
    task_id: uuid.UUID,
    payload: PositionFormalise,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    from app.gateway.gateway import build_gateway

    return position_service.formalise(
        session,
        ctx,
        task_id=task_id,
        requirement_ids=payload.requirement_ids,
        option=payload.option,
        own_words=payload.own_words,
        instruction=payload.instruction,
        gateway=build_gateway(),
    )


@router.post(
    "/tasks/{task_id}/positions/submit",
    response_model=list[dict],
    dependencies=_DECIDE,
)
def submit_position(
    task_id: uuid.UUID,
    payload: PositionSubmit,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    """Approve this wording as the company's answer to the requirements."""
    position_service.submit(
        session,
        ctx,
        task_id=task_id,
        requirement_ids=payload.requirement_ids,
        text=payload.text,
        source=payload.source,
        option=payload.option,
    )
    return position_service.decisions(session, ctx, task_id=task_id)


@router.post(
    "/tasks/{task_id}/positions/ignore",
    response_model=list[dict],
    dependencies=_DECIDE,
)
def ignore_positions(
    task_id: uuid.UUID,
    payload: PositionIgnore,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    """Set these requirements aside. They leave the working list; the record stays."""
    position_service.ignore(
        session,
        ctx,
        task_id=task_id,
        requirement_ids=payload.requirement_ids,
        reason=payload.reason,
    )
    return position_service.decisions(session, ctx, task_id=task_id)


@router.post(
    "/tasks/{task_id}/positions/{requirement_id}/restore",
    response_model=list[dict],
    dependencies=_DECIDE,
)
def restore_position(
    task_id: uuid.UUID,
    requirement_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    position_service.restore(session, ctx, task_id=task_id, requirement_id=requirement_id)
    return position_service.decisions(session, ctx, task_id=task_id)


# --- participants ------------------------------------------------------------


@router.get(
    "/tasks/{task_id}/participants",
    response_model=list[ParticipantOut],
    dependencies=[Depends(require(Perm.TASK_READ))],
)
def list_participants(
    task_id: uuid.UUID,
    include_removed: bool = Query(default=False),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[ParticipantOut]:
    rows = task_service.participants(session, ctx, task_id=task_id, include_removed=include_removed)
    return [ParticipantOut.model_validate(p) for p in rows]


@router.post(
    "/tasks/{task_id}/participants",
    response_model=ParticipantOut,
    dependencies=[Depends(require(Perm.TASK_ASSIGN))],
)
def add_participant(
    task_id: uuid.UUID,
    payload: ParticipantIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ParticipantOut:
    participant = task_service.add_participant(
        session,
        ctx,
        task_id=task_id,
        user_id=payload.user_id,
        participant_role=_enum(ParticipantRole, payload.participant_role, "participant role"),
    )
    return ParticipantOut.model_validate(participant)


@router.delete(
    "/tasks/{task_id}/participants",
    response_model=ParticipantOut,
    dependencies=[Depends(require(Perm.TASK_ASSIGN))],
)
def remove_participant(
    task_id: uuid.UUID,
    payload: ParticipantIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ParticipantOut:
    participant = task_service.remove_participant(
        session,
        ctx,
        task_id=task_id,
        user_id=payload.user_id,
        participant_role=_enum(ParticipantRole, payload.participant_role, "participant role"),
    )
    return ParticipantOut.model_validate(participant)


# --- dependencies ------------------------------------------------------------


@router.post(
    "/tasks/{task_id}/dependencies",
    response_model=DependencyOut,
    dependencies=[Depends(require(Perm.TASK_WRITE))],
)
def add_dependency(
    task_id: uuid.UUID,
    payload: DependencyIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> DependencyOut:
    dependency = task_service.add_dependency(
        session,
        ctx,
        from_task_id=task_id,
        to_task_id=payload.to_task_id,
        dependency_type=_enum(TaskDependencyType, payload.dependency_type, "dependency type"),
        note=payload.note,
    )
    return DependencyOut.model_validate(dependency)


@router.get(
    "/tasks/{task_id}/dependencies",
    response_model=dict,
    dependencies=[Depends(require(Perm.TASK_READ))],
)
def list_dependencies(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    deps = task_service.dependencies(session, ctx, task_id=task_id)
    return {
        "outgoing": [DependencyOut.model_validate(d).model_dump() for d in deps["outgoing"]],
        "incoming": [DependencyOut.model_validate(d).model_dump() for d in deps["incoming"]],
    }


# --- cross-department requests -----------------------------------------------


@router.post(
    "/requests",
    response_model=RequestOut,
    dependencies=[Depends(require(Perm.REQUEST_CREATE))],
)
def create_request(
    payload: RequestCreate,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> RequestOut:
    request = request_service.create_request(
        session,
        ctx,
        origin_task_id=payload.origin_task_id,
        target_department_id=payload.target_department_id,
        request_type=_enum(RequestType, payload.request_type, "request type"),
        request_text=payload.request_text,
        business_reason=payload.business_reason,
        target_user_id=payload.target_user_id,
        requested_resource_type=payload.requested_resource_type,
        requested_resource_id=payload.requested_resource_id,
        requirement_ids=payload.requirement_ids,
        due_at=payload.due_at,
        clarification_id=payload.clarification_id,
        section=payload.section,
    )
    return RequestOut.model_validate(request)


@router.get(
    "/requests/inbox",
    response_model=list[RequestOut],
    dependencies=[Depends(require(Perm.TASK_READ))],
)
def request_inbox(
    department_id: uuid.UUID | None = Query(default=None),
    open_only: bool = Query(default=True),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[RequestOut]:
    """What a department has been asked for."""
    rows = request_service.inbox(session, ctx, department_id=department_id, open_only=open_only)
    return [RequestOut.model_validate(r) for r in rows]


@router.get(
    "/requests/outbox",
    response_model=list[RequestOut],
    dependencies=[Depends(require(Perm.TASK_READ))],
)
def request_outbox(
    origin_task_id: uuid.UUID | None = Query(default=None),
    bid_id: uuid.UUID | None = Query(default=None),
    open_only: bool = Query(default=False),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[RequestOut]:
    rows = request_service.outbox(
        session, ctx, origin_task_id=origin_task_id, bid_id=bid_id, open_only=open_only
    )
    return [RequestOut.model_validate(r) for r in rows]


@router.get(
    "/requests/{request_id}",
    response_model=RequestOut,
    dependencies=[Depends(require(Perm.TASK_READ))],
)
def get_request(
    request_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> RequestOut:
    return RequestOut.model_validate(
        request_service.get_request(session, ctx, request_id=request_id)
    )


@router.get(
    "/requests/{request_id}/resolution",
    response_model=dict,
    dependencies=[Depends(require(Perm.TASK_READ))],
)
def request_resolution(
    request_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """What was asked, what was decided, and exactly what access it created.

    The grant list is the point: 'resolved' says nothing about whether the
    department boundary held.
    """
    return request_service.resolution_summary(session, ctx, request_id=request_id)


@router.post(
    "/requests/{request_id}/acknowledge",
    response_model=RequestOut,
    dependencies=[Depends(require(Perm.REQUEST_RESOLVE))],
)
def acknowledge_request(
    request_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> RequestOut:
    return RequestOut.model_validate(
        request_service.acknowledge(session, ctx, request_id=request_id)
    )


@router.post(
    "/requests/{request_id}/answer",
    response_model=RequestOut,
    dependencies=[Depends(require(Perm.REQUEST_RESOLVE))],
)
def answer_request(
    request_id: uuid.UUID,
    payload: AnswerIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> RequestOut:
    """Reply to a request: the answer in words, plus any evidence that backs it.

    With no evidence this is the least-privilege resolution and creates no
    grant. Attaching evidence needs `grant:create`, exactly as sharing it
    separately always did, and each item is a narrow grant to the asking task.
    """
    if payload.evidence_ids and not ctx.has_permission(Perm.GRANT_CREATE):
        from app.core.errors import PermissionDenied

        raise PermissionDenied("Attaching evidence to an answer needs grant:create.")
    return RequestOut.model_validate(
        request_service.answer(
            session,
            ctx,
            request_id=request_id,
            resolution_text=payload.resolution_text,
            evidence_ids=payload.evidence_ids,
        )
    )


@router.post(
    "/requests/{request_id}/share-evidence",
    response_model=dict,
    dependencies=[Depends(require(Perm.GRANT_CREATE))],
)
def share_evidence(
    request_id: uuid.UUID,
    payload: ShareEvidenceIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Share Evidence: one narrow, task-scoped, expiring grant per item."""
    request, grants = request_service.share_evidence(
        session,
        ctx,
        request_id=request_id,
        evidence_ids=payload.evidence_ids,
        reason=payload.reason,
        expires_at=payload.expires_at,
    )
    return {
        "request": RequestOut.model_validate(request).model_dump(mode="json"),
        "grants": [
            {
                "id": str(g.id),
                "resource_id": str(g.resource_id),
                "task_id": str(g.task_id),
                "expires_at": g.expires_at.isoformat(),
            }
            for g in grants
        ],
    }


@router.post(
    "/requests/{request_id}/grant-access",
    response_model=dict,
    dependencies=[Depends(require(Perm.GRANT_CREATE))],
)
def grant_access(
    request_id: uuid.UUID,
    payload: GrantAccessIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    request, grant = request_service.grant_access(
        session,
        ctx,
        request_id=request_id,
        resource_type=_enum(GrantResourceType, payload.resource_type, "resource type"),
        resource_id=payload.resource_id,
        reason=payload.reason,
        permission=_enum(GrantPermission, payload.permission, "permission"),
        expires_at=payload.expires_at,
    )
    return {
        "request": RequestOut.model_validate(request).model_dump(mode="json"),
        "grant": {
            "id": str(grant.id),
            "task_id": str(grant.task_id),
            "resource_type": grant.resource_type.value,
            "resource_id": str(grant.resource_id),
            "expires_at": grant.expires_at.isoformat(),
        },
    }


@router.post(
    "/requests/{request_id}/reject",
    response_model=RequestOut,
    dependencies=[Depends(require(Perm.REQUEST_RESOLVE))],
)
def reject_request(
    request_id: uuid.UUID,
    payload: RejectIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> RequestOut:
    """Rejecting is a legitimate answer. The origin task becomes BLOCKED, not
    quietly in progress with a hole in it."""
    return RequestOut.model_validate(
        request_service.reject(session, ctx, request_id=request_id, reason=payload.reason)
    )


@router.post(
    "/requests/{request_id}/cancel",
    response_model=RequestOut,
    dependencies=[Depends(require(Perm.REQUEST_CREATE))],
)
def cancel_request(
    request_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> RequestOut:
    return RequestOut.model_validate(request_service.cancel(session, ctx, request_id=request_id))


# --- evidence added from inside a task -----------------------------------------


@router.post(
    "/tasks/{task_id}/evidence",
    status_code=201,
    dependencies=[Depends(require(Perm.TASK_WRITE))],
)
async def upload_task_evidence(
    task_id: uuid.UUID,
    file: UploadFile = File(...),
    confidentiality_class: str = Form("INTERNAL"),
    document_type: str | None = Form(None),
    title: str | None = Form(None),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Add a company document from inside a task.

    It becomes ordinary company knowledge and waits for a knowledge
    administrator; the task remembers it asked, and its owner is told when the
    excerpts are approved and usable in the draft.
    """
    from app.api.v1.knowledge import _company_document_type, _enqueue_document_index
    from app.db.post_commit import run_after_commit
    from app.models.enums import ConfidentialityClass
    from app.services import task_evidence

    data = await file.read()
    version = task_evidence.upload_to_task(
        session,
        ctx,
        task_id=task_id,
        data=data,
        filename=file.filename or "upload",
        confidentiality_class=ConfidentialityClass(confidentiality_class),
        document_type=_company_document_type(document_type),
        title=title,
        declared_mime=file.content_type,
    )
    version_id, org_id, actor = version.id, ctx.org_id, ctx.actor_user_id
    run_after_commit(
        session,
        lambda: _enqueue_document_index(version_id=version_id, org_id=org_id, actor_user_id=actor),
        label="parse_and_index_task_evidence",
    )
    return {"document_version_id": str(version.id), "state": "uploaded, awaiting parsing"}


@router.get("/tasks/{task_id}/evidence", dependencies=[Depends(require(Perm.TASK_READ))])
def task_evidence_status(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    """Where each document uploaded from this task stands."""
    from app.services import task_evidence

    return task_evidence.status(session, ctx, task_id=task_id)
