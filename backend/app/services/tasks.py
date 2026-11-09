"""Task creation, grouping, assignment and lifecycle.

A task is the unit of contributor work, and it is also the unit of context
isolation. Three things follow from that and are enforced here rather than left
to callers.

**Grouping is explained.** Requirements are grouped into tasks by department and
category, and every task records why its requirements are together. A grouping
nobody can explain is a grouping nobody can correct.

**Scope is computed at creation and re-evaluated at use.** `retrieval_scope` is
derived from the owning department plus the categories of the requirements the
task covers. It is a ceiling, not an entitlement: what the task may *ever* see.
What it sees on any given message is that ceiling narrowed by live permission
evaluation.

**State is a guarded machine.** `AI_READY_FOR_REVIEW` is not `COMPLETED`. Every
move is declared in one table and audited, so an illegal move fails identically
in a request, a worker or a script.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import IllegalTransition, NotFound, PermissionDenied, ValidationError
from app.core.logging import get_logger
from app.core.permissions import Perm
from app.models import (
    ArtifactRequirement,
    ArtifactStatement,
    ArtifactVersion,
    AuditEvent,
    CrossDepartmentRequest,
    Department,
    Requirement,
    RequirementAssessment,
    ResponseArtifact,
    Task,
    TaskConversation,
    TaskDependency,
    TaskEvidence,
    TaskParticipant,
    TaskRequirement,
    User,
    UserDepartment,
)
from app.models.enums import (
    BidReadiness,
    ConfidentialityClass,
    DepartmentCode,
    ParticipantRole,
    RequirementApplicability,
    RequirementCategory,
    TaskDependencyType,
    TaskPriority,
    TaskState,
)
from app.services import audit, category_routing, notifications
from app.services.assessment import DEPARTMENT_BY_CATEGORY  # noqa: F401 - re-exported
from app.services.requirement_scope import LIVE_REQUIREMENT

log = get_logger("tasks")

# from_state -> allowed to_states.
ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.NOT_STARTED: frozenset(
        {
            TaskState.AI_READY_FOR_REVIEW,
            TaskState.NEEDS_INPUT,
            TaskState.IN_PROGRESS,
            TaskState.BLOCKED,
        }
    ),
    # NEEDS_INPUT is reachable from AI_READY_FOR_REVIEW, and AI_READY_FOR_REVIEW
    # from both of the others, because re-assessment can move readiness in
    # either direction: evidence is uploaded and a blocked task becomes
    # draftable, or the evidence rule tightens and a ready one stops being
    # ready. Without these edges the correction is refused and the task keeps a
    # verdict the assessment no longer supports.
    TaskState.AI_READY_FOR_REVIEW: frozenset(
        {TaskState.IN_PROGRESS, TaskState.BLOCKED, TaskState.NEEDS_INPUT}
    ),
    TaskState.NEEDS_INPUT: frozenset(
        {TaskState.IN_PROGRESS, TaskState.BLOCKED, TaskState.AI_READY_FOR_REVIEW}
    ),
    TaskState.BLOCKED: frozenset(
        {TaskState.IN_PROGRESS, TaskState.NEEDS_INPUT, TaskState.AI_READY_FOR_REVIEW}
    ),
    TaskState.IN_PROGRESS: frozenset(
        {
            TaskState.WAITING_FOR_DEPENDENCY,
            TaskState.WAITING_FOR_ACCESS,
            TaskState.READY_FOR_APPROVAL,
            TaskState.BLOCKED,
            TaskState.NEEDS_INPUT,
        }
    ),
    TaskState.WAITING_FOR_DEPENDENCY: frozenset({TaskState.IN_PROGRESS, TaskState.BLOCKED}),
    # A rejected access request leaves the task blocked, not quietly in progress
    # with a gap in it.
    TaskState.WAITING_FOR_ACCESS: frozenset({TaskState.IN_PROGRESS, TaskState.BLOCKED}),
    TaskState.READY_FOR_APPROVAL: frozenset(
        {TaskState.APPROVED, TaskState.REWORK_REQUIRED, TaskState.IN_PROGRESS}
    ),
    TaskState.REWORK_REQUIRED: frozenset({TaskState.IN_PROGRESS}),
    TaskState.APPROVED: frozenset({TaskState.COMPLETED, TaskState.REVALIDATION_REQUIRED}),
    TaskState.COMPLETED: frozenset({TaskState.REVALIDATION_REQUIRED}),
    TaskState.REVALIDATION_REQUIRED: frozenset({TaskState.IN_PROGRESS}),
}

REASON_REQUIRED = frozenset({TaskState.BLOCKED, TaskState.REWORK_REQUIRED})

# Confidentiality ceiling by department. A task never sees above its owner's
# department ceiling, whatever a contributor's personal role allows elsewhere.
DEPARTMENT_CEILING: dict[DepartmentCode, ConfidentialityClass] = {
    DepartmentCode.HR: ConfidentialityClass.RESTRICTED,
    DepartmentCode.LEGAL: ConfidentialityClass.RESTRICTED,
    DepartmentCode.FINANCE: ConfidentialityClass.RESTRICTED,
    DepartmentCode.COMMERCIAL: ConfidentialityClass.RESTRICTED,
}
DEFAULT_CEILING = ConfidentialityClass.CONFIDENTIAL


def _code_for(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID, prefix: str) -> str:
    used = set(
        session.execute(select(Task.code).where(Task.org_id == ctx.org_id, Task.bid_id == bid_id))
        .scalars()
        .all()
    )
    n = 1
    while f"{prefix}-{n:02d}" in used:
        n += 1
    return f"{prefix}-{n:02d}"


def compute_retrieval_scope(
    *,
    department: Department,
    categories: list[RequirementCategory],
) -> dict:
    """What this task may ever see, before live permissions narrow it.

    The scope is a ceiling. Storing it does not entitle anyone to anything: the
    context builder re-evaluates every element against current grants and roles
    on every message, and a scope that says PRIVILEGED still yields nothing to a
    user with no grant.
    """
    return {
        "departments": [department.code.value],
        "categories": sorted({c.value for c in categories}),
        "entity_types": [],
        "max_confidentiality": DEPARTMENT_CEILING.get(department.code, DEFAULT_CEILING).value,
        "extra_resource_ids": [],
    }


def create_task(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    title: str,
    department_id: uuid.UUID,
    requirement_ids: list[uuid.UUID] | None = None,
    description: str | None = None,
    owner_user_id: uuid.UUID | None = None,
    priority: TaskPriority = TaskPriority.NORMAL,
    due_date: date | None = None,
    grouping_rationale: str | None = None,
    code: str | None = None,
) -> Task:
    if not (title or "").strip():
        raise ValidationError("A task needs a title.")
    department = session.get(Department, department_id)
    if department is None or department.org_id != ctx.org_id:
        raise NotFound("That department does not exist.")

    # Work goes to the department's head, who passes it on. Nobody is left
    # holding a task that no one was asked about.
    if owner_user_id is None:
        head = _head_of(session, department)
        if head is not None and can_work_tasks(session, head):
            owner_user_id = head.id

    requirement_ids = requirement_ids or []
    requirements = (
        session.execute(
            select(Requirement).where(
                Requirement.org_id == ctx.org_id,
                Requirement.bid_id == bid_id,
                Requirement.id.in_(requirement_ids),
                LIVE_REQUIREMENT,
            )
        )
        .scalars()
        .all()
        if requirement_ids
        else []
    )
    if len(requirements) != len(set(requirement_ids)):
        raise ValidationError("One or more requirements do not belong to this bid.")
    if not requirements:
        # A task with nothing to answer cannot be drafted and has no way to be closed:
        # it would sit in the package as an unresolved task for good.
        raise ValidationError(
            "A task must cover at least one requirement of this bid. Link the tender "
            "requirement it answers; a section the tender does not ask anything of needs "
            "no task."
        )

    task = Task(
        org_id=ctx.org_id,
        bid_id=bid_id,
        code=code or _code_for(session, ctx, bid_id=bid_id, prefix=department.code.value[:3]),
        title=title.strip(),
        description=description,
        department_id=department.id,
        owner_user_id=owner_user_id,
        priority=priority,
        due_date=due_date,
        state=TaskState.NOT_STARTED,
        retrieval_scope=compute_retrieval_scope(
            department=department, categories=[r.category for r in requirements]
        ),
        grouping_rationale=grouping_rationale,
        state_changed_at=datetime.now(timezone.utc),
        state_changed_by=ctx.actor_user_id,
    )
    session.add(task)
    session.flush()

    for requirement in requirements:
        session.add(
            TaskRequirement(org_id=ctx.org_id, task_id=task.id, requirement_id=requirement.id)
        )
    session.flush()

    if owner_user_id is not None:
        ensure_conversation(session, ctx, task_id=task.id, user_id=owner_user_id)

    # The task's working draft is its current DRAFT ArtifactVersion.  Creating
    # it here keeps task and artifact lifecycle one continuous object.
    from app.services import artifacts

    artifacts.ensure_for_task(session, ctx, task_id=task.id, title=task.title)

    audit.record(
        session,
        ctx,
        action="task.created",
        entity_type="task",
        entity_id=task.id,
        bid_id=bid_id,
        after={
            "code": task.code,
            "department": department.code.value,
            "requirements": len(requirements),
            "retrieval_scope": task.retrieval_scope,
        },
    )
    return task


# --- generating a task set from an assessed bid ------------------------------


def ungrouped_reason(category, department_code, configured) -> str | None:
    """Why this requirement cannot be grouped, or None when it can.

    Pulled out of `generate_tasks` so the decision can be tested without a
    database. The two reasons need different work from different people -- a
    taxonomy change versus an administrator adding a department -- so they are
    never merged into one count.
    """
    if department_code is None:
        return "no department owns this category"
    if department_code not in configured:
        return f"{department_code.value} is not configured for this organization"
    return None


def packages_from(
    proposed: list, members: list[Requirement]
) -> list[tuple[str, list[Requirement], str]]:
    """Turn the model's proposed packages into a partition of `members`.

    The model names; this decides. Each requirement goes to the first package
    that names it; a code it was not shown is ignored; an empty package is
    dropped; and anything no package claimed is kept in a leftover package
    rather than lost -- a requirement with no task is a question nobody answers.
    Returns [] when the proposal is unusable, so the caller falls back.
    """
    by_code = {r.code.strip().upper(): r for r in members}
    claimed: set[str] = set()
    packages: list[tuple[str, list[Requirement], str]] = []
    for package in proposed or []:
        chosen = []
        for code in getattr(package, "requirement_codes", []) or []:
            key = (code or "").strip().upper()
            if key in by_code and key not in claimed:
                claimed.add(key)
                chosen.append(by_code[key])
        title = (getattr(package, "title", "") or "").strip()
        if chosen and title:
            packages.append((title, chosen, (getattr(package, "rationale", "") or "").strip()))
    if not packages:
        return []
    leftover = [r for r in members if r.code.strip().upper() not in claimed]
    if leftover:
        packages.append(
            (
                "Other requirements",
                leftover,
                "Not placed in any proposed package; kept here so nothing is left unowned.",
            )
        )
    return packages


def _plan_work_packages(
    session: Session,
    ctx: ExecutionContext,
    *,
    department: Department,
    members: list[Requirement],
    gateway=None,
) -> list[tuple[str, list[Requirement], str]]:
    """Packages for one department, or [] to fall back to one task per category.

    A model failure is never a reason a bid cannot get tasks: the per-category
    grouping it replaces is still correct, just coarse.
    """
    if len(members) < 3:
        return []
    import json

    from app.gateway.gateway import build_gateway
    from app.gateway.registry import wrap_untrusted
    from app.models import RequirementVersion
    from app.schemas.task_grouping import TaskGroupingResult

    texts = {
        v.requirement_id: v.requirement_text
        for v in session.execute(
            select(RequirementVersion).where(
                RequirementVersion.org_id == ctx.org_id,
                RequirementVersion.id.in_([m.current_version_id for m in members]),
            )
        ).scalars()
    }
    payload = {
        "department": department.code.value,
        "requirements": [
            {
                "code": m.code,
                "category": m.category.value,
                "text": " ".join((texts.get(m.id) or "").split())[:400],
            }
            for m in members
        ],
    }
    try:
        model_gateway = gateway or build_gateway()
        result = model_gateway.generate_structured(
            "task_grouping",
            wrap_untrusted(json.dumps(payload, ensure_ascii=False)),
            ctx=ctx,
            session=session,
            cache=True,
        )
    except Exception as exc:  # noqa: BLE001 - grouping degrades, never fails
        log.info("task_grouping_unavailable", reason=type(exc).__name__)
        return []
    if not isinstance(result, TaskGroupingResult):
        return []
    return packages_from(result.packages, members)


def generate_tasks(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID, gateway=None
) -> list[Task]:
    """Group the bid's requirements into department tasks.

    Requirements already covered by a task are left alone, so this is safe to
    re-run after an amendment adds requirements. NOT_APPLICABLE requirements are
    never grouped: nobody should be asked to answer a question that does not
    apply.
    """
    departments = {
        d.code: d
        for d in session.execute(select(Department).where(Department.org_id == ctx.org_id))
        .scalars()
        .all()
    }
    covered = set(
        session.execute(
            select(TaskRequirement.requirement_id)
            .join(Task, Task.id == TaskRequirement.task_id)
            .where(TaskRequirement.org_id == ctx.org_id, Task.bid_id == bid_id)
        )
        .scalars()
        .all()
    )

    rows = (
        session.execute(
            select(Requirement)
            .where(
                Requirement.org_id == ctx.org_id,
                Requirement.bid_id == bid_id,
                Requirement.applicability != RequirementApplicability.NOT_APPLICABLE,
                # A clause the assessment found to bind only the customer is
                # nobody's task. Decided on the assessment, not on the label
                # alone: a customer row that hands something over still holds
                # work for the bidder and keeps its task.
                ~Requirement.id.in_(
                    select(RequirementAssessment.requirement_id).where(
                        RequirementAssessment.org_id == ctx.org_id,
                        RequirementAssessment.is_current.is_(True),
                        RequirementAssessment.bid_readiness == BidReadiness.CUSTOMER_OBLIGATION,
                    )
                ),
                LIVE_REQUIREMENT,
            )
            .order_by(Requirement.code)
        )
        .scalars()
        .all()
    )

    route_table = category_routing.routes(session, ctx)
    groups: dict[tuple[DepartmentCode, RequirementCategory], list[Requirement]] = {}
    ungrouped: dict[str, int] = {}
    for requirement in rows:
        if requirement.id in covered:
            continue
        code = route_table.get(requirement.category)
        reason = ungrouped_reason(requirement.category, code, departments)
        if reason is not None:
            # No department to own it. Better to leave it visibly ungrouped than
            # to file it somewhere plausible and lose it -- but VISIBLY is the
            # operative word, and it was not.
            #
            # An organization with no departments configured skips every
            # requirement here and reports tasks_created=0 with no reason. That
            # is what a fixture org did: assessment looked healthy, 17
            # requirements assessed, and every stage after it -- drafting,
            # artifacts, requests, assembly -- was silently unreachable because
            # nothing downstream exists without a task. The seeded demo org has
            # departments, so it never showed there.
            #
            # A count and a reason, on the same axis as the rest of the report.
            # This is the fifth bug in this codebase with the shape "an
            # optimisation that became a silent skip", and the fix is always
            # the same: say what was dropped and why.
            ungrouped[reason] = ungrouped.get(reason, 0) + 1
            continue
        if code is None:  # ungrouped_reason() names a reason whenever there is no code
            continue
        groups.setdefault((code, requirement.category), []).append(requirement)

    if ungrouped:
        log.warning(
            "requirements_left_ungrouped",
            bid_id=str(bid_id),
            total=sum(ungrouped.values()),
            reasons=ungrouped,
        )

    created: list[Task] = []

    # Work packages first, per department across its categories. Where the
    # model proposes a usable partition, those become the tasks; otherwise the
    # department's categories below each become one task, as before.
    by_department: dict[DepartmentCode, list[Requirement]] = {}
    for (dept_code, _category), members in groups.items():
        by_department.setdefault(dept_code, []).extend(members)
    packaged: set[DepartmentCode] = set()
    for dept_code in sorted(by_department, key=lambda c: c.value):
        department = departments[dept_code]
        members = sorted(by_department[dept_code], key=lambda r: r.code)
        packages = _plan_work_packages(
            session, ctx, department=department, members=members, gateway=gateway
        )
        if not packages:
            continue
        packaged.add(dept_code)
        for title, package_members, rationale in packages:
            created.append(
                create_task(
                    session,
                    ctx,
                    bid_id=bid_id,
                    title=title,
                    department_id=department.id,
                    requirement_ids=[r.id for r in package_members],
                    grouping_rationale=with_covers_note(
                        f"Work package for {dept_code.value}: {rationale}"
                        if rationale
                        else f"Work package of {len(package_members)} requirement(s) "
                        f"for {dept_code.value}.",
                        [r.code for r in package_members],
                    ),
                )
            )

    for (dept_code, category), members in sorted(
        groups.items(), key=lambda kv: (kv[0][0].value, kv[0][1].value)
    ):
        if dept_code in packaged:
            continue
        department = departments[dept_code]
        task = create_task(
            session,
            ctx,
            bid_id=bid_id,
            title=f"{category.value.replace('_', ' ').title()} requirements",
            department_id=department.id,
            requirement_ids=[r.id for r in members],
            grouping_rationale=with_covers_note(
                f"{len(members)} {category.value} requirement(s) grouped for "
                f"{dept_code.value} because that department owns this category.",
                [r.code for r in members],
            ),
        )
        created.append(task)

    if created:
        audit.record(
            session,
            ctx,
            action="task.generated",
            entity_type="bid",
            entity_id=bid_id,
            bid_id=bid_id,
            after={"tasks_created": len(created), "ungrouped": ungrouped},
        )
    return created


# States the system assigned and may therefore revise. Everything else --
# IN_PROGRESS, READY_FOR_APPROVAL, APPROVED, COMPLETED, the waiting states --
# belongs to a person who has picked the task up, and re-deriving those from
# evidence would overwrite work with an opinion.
SYSTEM_ASSIGNED_STATES = frozenset(
    {
        TaskState.NOT_STARTED,
        TaskState.AI_READY_FOR_REVIEW,
        TaskState.NEEDS_INPUT,
        TaskState.BLOCKED,
    }
)


_COVERS = re.compile(r"\s*Covers:\s*([^.]*)\.\s*$")


def with_covers_note(text: str, codes: list[str]) -> str:
    """Append the membership the explanation was written for.

    A grouping explanation is prose about a set of clauses. When the set later
    changes -- a customer clause is released from the task -- the prose keeps
    describing the old set, and the task reads as covering things it no longer
    holds. Recording the set beside the prose makes staleness a comparison
    instead of a guess.
    """
    base = _COVERS.sub("", text or "").rstrip()
    return f"{base} Covers: {', '.join(sorted(codes))}."


def refresh_stale_rationales(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID) -> int:
    """Replace grouping explanations that no longer describe the task.

    Only generated explanations of tasks nobody has taken over are touched: an
    owner's or a person's words are never rewritten, and neither are the marker
    tasks the consistency check opens. An explanation is stale when it records a
    different set of clauses than the task now holds, or records none (written
    before the set was recorded), in which case the model's prose cannot be
    trusted to match. It is then replaced by a plain statement of the current
    membership -- less to say, but true.
    """
    tasks = (
        session.execute(
            select(Task).where(
                Task.org_id == ctx.org_id,
                Task.bid_id == bid_id,
                Task.owner_user_id.is_(None),
                Task.state.in_(SYSTEM_ASSIGNED_STATES),
            )
        )
        .scalars()
        .all()
    )
    changed = 0
    for task in tasks:
        text = task.grouping_rationale or ""
        if not text or text.startswith("consistency:"):
            continue
        members = (
            session.execute(
                select(Requirement.code)
                .join(TaskRequirement, TaskRequirement.requirement_id == Requirement.id)
                .where(
                    TaskRequirement.org_id == ctx.org_id,
                    TaskRequirement.task_id == task.id,
                    # A withdrawn requirement keeps its join row but is no
                    # longer part of the bid: the matrix does not show it, so
                    # neither may the explanation count it.
                    LIVE_REQUIREMENT,
                )
            )
            .scalars()
            .all()
        )
        if not members:
            continue
        match = _COVERS.search(text)
        recorded = {c.strip() for c in match.group(1).split(",")} if match else None
        if recorded == set(members):
            continue
        department = session.get(Department, task.department_id)
        dept = department.code.value if department else "the owning department"
        task.grouping_rationale = with_covers_note(
            f"Work package for {dept}: {len(members)} requirement(s). The earlier "
            "explanation described a different set of clauses and was replaced.",
            list(members),
        )
        changed += 1
    if changed:
        session.flush()
        log.info("task_rationales_refreshed", count=changed)
    return changed


def release_customer_clauses(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID
) -> dict[str, int]:
    """Take clauses that bind the customer out of the work packages.

    Tasks are generated before the owner of every clause is known, and an
    assessment run after a re-analysis can change it. Eleven customer clauses
    were still sitting inside company tasks, four of those tasks held nothing
    else, and the matrix showed a task ID and a NEEDS_INPUT state against
    requirements the bidder cannot act on.

    Only membership that nobody has touched is changed. A task with an owner,
    a participant, evidence, a draft, a request or a dependency is somebody's
    work and keeps every link it has. Within an untouched task only the join
    row is removed; the requirement and its assessment are not altered. A task
    left covering nothing is deleted only when it has none of the things above,
    because deleting one that does would cascade into that work.
    """
    customer_clauses = set(
        session.execute(
            select(RequirementAssessment.requirement_id)
            .join(Requirement, Requirement.id == RequirementAssessment.requirement_id)
            .where(
                RequirementAssessment.org_id == ctx.org_id,
                RequirementAssessment.is_current.is_(True),
                RequirementAssessment.bid_readiness == BidReadiness.CUSTOMER_OBLIGATION,
                Requirement.bid_id == bid_id,
            )
        )
        .scalars()
        .all()
    )
    if not customer_clauses:
        return {"unlinked": 0, "tasks_removed": 0}

    def touched(task: Task) -> bool:
        if task.owner_user_id is not None or task.state not in SYSTEM_ASSIGNED_STATES:
            return True
        # Every task is given an empty working draft when it is created, so
        # the artifact existing says nothing. A statement in it does.
        if session.execute(
            select(func.count())
            .select_from(ArtifactStatement)
            .join(ArtifactVersion, ArtifactVersion.id == ArtifactStatement.artifact_version_id)
            .join(ResponseArtifact, ResponseArtifact.id == ArtifactVersion.artifact_id)
            .where(ArtifactStatement.org_id == ctx.org_id, ResponseArtifact.task_id == task.id)
        ).scalar_one():
            return True
        for model, column in (
            (TaskParticipant, TaskParticipant.task_id),
            (TaskEvidence, TaskEvidence.task_id),
            (TaskConversation, TaskConversation.task_id),
            (CrossDepartmentRequest, CrossDepartmentRequest.origin_task_id),
        ):
            if session.execute(
                select(func.count())
                .select_from(model)
                .where(model.org_id == ctx.org_id, column == task.id)
            ).scalar_one():
                return True
        return bool(
            session.execute(
                select(func.count())
                .select_from(TaskDependency)
                .where(
                    TaskDependency.org_id == ctx.org_id,
                    (TaskDependency.from_task_id == task.id)
                    | (TaskDependency.to_task_id == task.id),
                )
            ).scalar_one()
        )

    unlinked = 0
    removed = 0
    tasks = (
        session.execute(select(Task).where(Task.org_id == ctx.org_id, Task.bid_id == bid_id))
        .scalars()
        .all()
    )
    for task in tasks:
        links = (
            session.execute(
                select(TaskRequirement).where(
                    TaskRequirement.org_id == ctx.org_id, TaskRequirement.task_id == task.id
                )
            )
            .scalars()
            .all()
        )
        mine = [link for link in links if link.requirement_id in customer_clauses]
        if not mine or touched(task):
            continue
        for link in mine:
            session.delete(link)
            unlinked += 1
            # The empty draft answered this clause too; it stops doing so.
            for answer in session.execute(
                select(ArtifactRequirement)
                .join(ResponseArtifact, ResponseArtifact.id == ArtifactRequirement.artifact_id)
                .where(
                    ArtifactRequirement.org_id == ctx.org_id,
                    ResponseArtifact.task_id == task.id,
                    ArtifactRequirement.requirement_id == link.requirement_id,
                )
            ).scalars():
                session.delete(answer)
        session.flush()
        if len(mine) == len(links):
            audit.record(
                session,
                ctx,
                action="task.removed_customer_only",
                entity_type="task",
                entity_id=task.id,
                bid_id=task.bid_id,
                before={"code": task.code, "title": task.title},
            )
            session.delete(task)
            removed += 1
    session.flush()
    if unlinked:
        log.info("customer_clauses_released", unlinked=unlinked, tasks_removed=removed)
    return {"unlinked": unlinked, "tasks_removed": removed}


def retire_tasks_without_requirements(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID
) -> int:
    """Close the work packages whose every requirement was moved or withdrawn.

    A requirement folded into another one takes its task link with it, so a
    package can end up covering nothing. It then sat in the list BLOCKED or
    REWORK_REQUIRED with an empty "cannot be drafted" answer, counted as
    unresolved work at release, and its empty statements fed the contradiction
    check -- all for clauses that no longer exist in the bid. Nobody can fix
    such a task, because there is nothing left to answer.

    Only generated packages are retired (they carry a "Covers:" note); the
    marker tasks of the consistency check and tasks a person made by hand are
    left alone. The task is closed, its answer is set aside as SUPERSEDED so it
    is never released or compared, and the promises it proposed are withdrawn.
    """
    from app.models import Commitment
    from app.models.enums import ApprovalStatus, ArtifactState

    tasks = (
        session.execute(
            select(Task).where(
                Task.org_id == ctx.org_id,
                Task.bid_id == bid_id,
                Task.grouping_rationale.like("%Covers:%"),
                ~Task.grouping_rationale.like("consistency:%"),
                ~select(TaskRequirement.id)
                .join(Requirement, Requirement.id == TaskRequirement.requirement_id)
                .where(TaskRequirement.task_id == Task.id, LIVE_REQUIREMENT)
                .exists(),
            )
        )
        .scalars()
        .all()
    )
    retired = 0
    for task in tasks:
        artifact = session.execute(
            select(ResponseArtifact).where(
                ResponseArtifact.org_id == ctx.org_id, ResponseArtifact.task_id == task.id
            )
        ).scalar_one_or_none()
        already = task.state is TaskState.COMPLETED and (
            artifact is None or artifact.state is ArtifactState.SUPERSEDED
        )
        if already:
            continue
        if artifact is not None:
            artifact.state = ArtifactState.SUPERSEDED
            for statement in session.execute(
                select(ArtifactStatement)
                .join(ArtifactVersion, ArtifactVersion.id == ArtifactStatement.artifact_version_id)
                .where(ArtifactVersion.artifact_id == artifact.id)
            ).scalars():
                if statement.commitment_id:
                    commitment = session.get(Commitment, statement.commitment_id)
                    if commitment is not None and commitment.org_id == ctx.org_id:
                        commitment.approval_status = ApprovalStatus.WITHDRAWN
        before = task.state
        task.state = TaskState.COMPLETED
        task.state_changed_at = datetime.now(timezone.utc)
        task.state_changed_by = ctx.actor_user_id
        task.grouping_rationale = (
            "Retired: every requirement this package covered was moved into another "
            "requirement or withdrawn, so there is nothing left to answer here."
        )
        audit.record(
            session,
            ctx,
            action="task.retired_no_requirements",
            entity_type="task",
            entity_id=task.id,
            bid_id=task.bid_id,
            before={"state": before.value},
            after={"state": TaskState.COMPLETED.value},
        )
        retired += 1
    if retired:
        session.flush()
        log.info("tasks_retired_without_requirements", count=retired)
    return retired


def seed_initial_states(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID
) -> dict[str, int]:
    """Move tasks to the state their evidence justifies, and keep them there.

    A task whose requirements are all READY is worth a contributor's review; one
    with a mandatory blocker is not started, it is blocked. Nothing here claims
    the work is done -- `AI_READY_FOR_REVIEW` means a human has not looked yet.

    This used to consider only tasks still in NOT_STARTED, which made it a
    one-time seeding despite running after every assessment. Readiness then
    changed underneath it and the tasks kept a verdict from an earlier run: when
    the evidence rule tightened and 38 requirements moved to BLOCKED, PMO-01 and
    QUA-01 went on reading AI_READY_FOR_REVIEW -- "a draft can be proposed" --
    while every requirement behind them said there was nothing to draft from.
    Two values about the same work, disagreeing, with the stale one on the sheet
    a contributor actually opens.

    So the whole system-assigned set is revisited. A task a person has taken --
    IN_PROGRESS and everything beyond it -- is never touched: their state is a
    fact about what they are doing, not a conclusion to recompute.
    """
    release_customer_clauses(session, ctx, bid_id=bid_id)
    retire_tasks_without_requirements(session, ctx, bid_id=bid_id)
    refresh_stale_rationales(session, ctx, bid_id=bid_id)
    tasks = (
        session.execute(
            select(Task).where(
                Task.org_id == ctx.org_id,
                Task.bid_id == bid_id,
                Task.state.in_(SYSTEM_ASSIGNED_STATES),
            )
        )
        .scalars()
        .all()
    )
    moved: dict[str, int] = {}
    for task in tasks:
        readiness = (
            session.execute(
                select(RequirementAssessment.bid_readiness)
                .join(
                    TaskRequirement,
                    TaskRequirement.requirement_id == RequirementAssessment.requirement_id,
                )
                .where(
                    RequirementAssessment.org_id == ctx.org_id,
                    RequirementAssessment.is_current.is_(True),
                    TaskRequirement.task_id == task.id,
                )
            )
            .scalars()
            .all()
        )
        readiness = [r for r in readiness if r is not BidReadiness.CUSTOMER_OBLIGATION]
        if not readiness:
            continue
        if BidReadiness.BLOCKED in readiness:
            target, reason = (
                TaskState.BLOCKED,
                "A covered requirement is blocked: nothing attached proves it.",
            )
        elif all(r in (BidReadiness.READY, BidReadiness.DRAFTABLE) for r in readiness):
            target, reason = (
                TaskState.AI_READY_FOR_REVIEW,
                "Every covered requirement has usable evidence; a draft can be proposed.",
            )
        else:
            target, reason = (
                TaskState.NEEDS_INPUT,
                "Some covered requirements need refreshed evidence or confirmation.",
            )
        transition(session, ctx, task_id=task.id, to_state=target, reason=reason)
        moved[target.value] = moved.get(target.value, 0) + 1
    return moved


# --- lifecycle ---------------------------------------------------------------


def transition(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    to_state: TaskState,
    reason: str | None = None,
) -> Task:
    task = get_task(session, ctx, task_id=task_id)
    if to_state is task.state:
        return task
    allowed = ALLOWED_TRANSITIONS.get(task.state, frozenset())
    if to_state not in allowed:
        raise IllegalTransition(
            f"A task cannot move from {task.state.value} to {to_state.value}. "
            f"Allowed from here: {', '.join(sorted(s.value for s in allowed)) or 'nothing'}."
        )
    if to_state in REASON_REQUIRED and not (reason or "").strip():
        raise ValidationError(f"Moving a task to {to_state.value} requires a reason.")

    before = task.state
    task.state = to_state
    task.state_changed_at = datetime.now(timezone.utc)
    task.state_changed_by = ctx.actor_user_id
    if to_state is TaskState.BLOCKED:
        task.blocker_note = reason
    elif before is TaskState.BLOCKED:
        task.blocker_note = None
    session.flush()

    audit.record(
        session,
        ctx,
        action="task.state_changed",
        entity_type="task",
        entity_id=task.id,
        bid_id=task.bid_id,
        before={"state": before.value},
        after={"state": to_state.value},
        reason=reason,
    )
    # Only blocking is worth interrupting someone for. A task moving through
    # its ordinary states is progress the owner is already making; a task that
    # has stopped is the one thing they cannot discover by working.
    if to_state is TaskState.BLOCKED and task.owner_user_id is not None:
        notifications.notify(
            session,
            ctx,
            recipients=[task.owner_user_id],
            kind="task.blocked",
            title=f"{task.code} is blocked",
            body=reason,
            screen="tasks",
            bid_id=task.bid_id,
            focus_id=task.id,
        )
    return task


def get_task(session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID) -> Task:
    task = session.get(Task, task_id)
    if task is None or task.org_id != ctx.org_id:
        raise NotFound("That task does not exist.")
    return task


def can_work_tasks(session: Session, user: User | None) -> bool:
    """May this person actually open a task and write its draft?

    A department head is only a sensible default owner if they can do the work.
    Quality's head was the Knowledge Admin, whose role approves evidence and
    deliberately cannot read or write drafts (the person who approves the
    library should not also be the author citing it). QUA-01 was handed to them
    and nobody could open it. So the default owner is chosen from people who
    can work; if the head cannot, the task stays with the bid manager.
    """
    if user is None or not getattr(user, "is_active", True):
        return False
    from app.core.permissions import ROLE_PERMISSIONS, RoleCode

    # DEPARTMENT_HEAD only adds staffing authority; it says nothing either way
    # about whether the person can work, so it is not what we judge by.
    roles = [
        r for r in (getattr(user, "roles", None) or []) if r.code != RoleCode.DEPARTMENT_HEAD.value
    ]
    if not roles:
        # No role information to judge by: do not take a person's work away on a guess.
        return True
    held: set[Perm] = set()
    for role in roles:
        try:
            held |= ROLE_PERMISSIONS.get(RoleCode(role.code), set())
        except ValueError:
            continue
    return {Perm.TASK_WRITE, Perm.ARTIFACT_WRITE} <= held


def _head_of(session: Session, department: Department | None):
    """The named head of a department, as a User, or None.

    One lookup used by both the check and the answer it gives forwards, so the
    two cannot disagree about who is in charge of a department.
    """
    if department is None or department.head_user_id is None:
        return None
    return session.get(User, department.head_user_id)


def _authorise_assignment(
    session: Session, ctx: ExecutionContext, *, task: Task, owner_user_id: uuid.UUID
) -> None:
    """May this actor put this owner on this task?

    The check cannot live in a route dependency, because the answer depends on
    the task's department and the proposed owner's -- data the router does not
    have. A permission alone was never enough to express "assign, but only your
    own people".

    **The system routes work to a department; the department's head names the
    person.** That division is the whole design, and it means a headed
    department is the head's exclusively: not a place the bid manager may also
    reach. Sara decides what the bid needs and who is accountable for it; she
    does not decide which of Leila's people is free on Thursday, because she
    does not know.

    So there are two doors, and only one is open at a time:

    * A department WITH a head: only that head, holding
      `task:assign_in_department`, and only onto a member of that department.
      `task:assign` does not open this door -- being accountable for the bid is
      not the same as knowing the team.
    * A department with NO head, or a task belonging to no department: the bid
      manager's `task:assign`, any owner. This is the fallback that keeps work
      movable, not a general power. It closes the moment a head is named.

    The owner-membership rule is the half people skip, and it is the half that
    matters. A head who can assign outside their department can hand work to any
    account in the organization, which is a quiet way to route a bid's evidence
    to someone who was never meant to see it.
    """
    department = session.get(Department, task.department_id) if task.department_id else None
    authorise_for_department(session, ctx, department=department, owner_user_id=owner_user_id)


def authorise_for_department(
    session: Session,
    ctx: ExecutionContext,
    *,
    department: Department | None,
    owner_user_id: uuid.UUID,
) -> None:
    """The assignment rule for anything that belongs to a department.

    Tasks and clarifications share it, so a department's work is staffed by the
    same people under the same rule wherever it appears.
    """
    head = _head_of(session, department)
    is_manager = ctx.has_permission(Perm.TASK_ASSIGN.value)
    if head is not None and not can_work_tasks(session, head):
        # A head who cannot open tasks cannot staff them either; the bid
        # manager keeps the department moving (see can_work_tasks).
        head = None

    if head is None:
        # Nobody owns staffing here, so the bid manager keeps it moving. Without
        # this, naming no head would mean work that can never be assigned at all.
        if is_manager:
            return
        where = f"{department.name} has no head" if department else "This task has no department"
        raise PermissionDenied(
            f"{where}, so only a bid manager can assign it. "
            "Ask an administrator to name a head for it."
        )
    if department is None:  # a head implies a department; stated so the type agrees
        raise PermissionDenied("This task has no department, so only a bid manager can assign it.")

    is_head = head.id == ctx.actor_user_id
    if is_head and not is_manager and not ctx.has_permission(Perm.TASK_ASSIGN_IN_DEPARTMENT.value):
        raise PermissionDenied(
            f"You are named as head of {department.name} but do not hold "
            "task:assign_in_department. Ask an administrator for the DEPARTMENT_HEAD role."
        )
    if not is_head and not is_manager:
        raise PermissionDenied(
            f"{department.name} is headed by {head.display_name}, and staffing its work "
            "is the head's. Ask them, or ask an administrator to change the head."
        )

    # The bid manager and the head both choose from the same list: the people in
    # the department. Nobody hands a department's work to someone outside it.
    member = session.execute(
        select(UserDepartment).where(
            UserDepartment.org_id == ctx.org_id,
            UserDepartment.user_id == owner_user_id,
            UserDepartment.department_id == department.id,
        )
    ).scalar_one_or_none()
    if member is None:
        raise PermissionDenied(
            f"That person is not in {department.name}. Its work goes to the head "
            "or a team member of that department, and to nobody else."
        )


def request_targets(session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID) -> list[dict]:
    """Departments this task may raise a cross-department request to.

    Every department except its own -- asking yourself is not a request.

    This exists because a contributor cannot list departments: `/admin/departments`
    needs `org:read`, which is an administrator's. Without it the copilot could
    name three departments in a refusal and offer no way to reach any of them,
    which is what it did -- the request ladder was described in prose three
    times per refusal and could not be climbed from anywhere in the product.

    Returned to anyone who can read the task. Knowing that a Legal department
    exists is not a disclosure; what Legal HOLDS stays behind the same grants it
    always did, and asking is exactly the governed path for reaching it.
    """
    task = get_task(session, ctx, task_id=task_id)
    rows = (
        session.execute(
            select(Department).where(Department.org_id == ctx.org_id).order_by(Department.name)
        )
        .scalars()
        .all()
    )
    staffed = set(
        session.execute(
            select(UserDepartment.department_id).where(UserDepartment.org_id == ctx.org_id)
        ).scalars()
    )
    # A department nobody is in is not somewhere a request can go.
    return [
        {"id": str(d.id), "code": d.code.value, "name": d.name, "has_members": True}
        for d in rows
        if d.id != task.department_id and d.id in staffed
    ]


def _person(user: User) -> dict:
    """A candidate as the picker shows them: who they are, and where they sit.

    Names alone do not say who is in which department, and two people in different
    departments can look alike in a list, so the departments (and roles) travel with
    the name.
    """
    return {
        "id": str(user.id),
        "display_name": user.display_name,
        "email": user.email,
        "departments": sorted(d.name for d in getattr(user, "departments", None) or []),
        "positions": sorted(
            (
                {
                    "department": d.name,
                    "position": "Head" if d.head_user_id == user.id else "Team member",
                }
                for d in getattr(user, "departments", None) or []
            ),
            key=lambda x: x["department"],
        ),
        "roles": sorted(r.name for r in getattr(user, "roles", None) or []),
    }


def assignable_owners(session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID) -> dict:
    """Who may this actor put on this task, and under what authority.

    The same facts `_authorise_assignment` checks, asked forwards instead of
    backwards. A client that had to work this out for itself would need the
    task's department, that department's head and its membership -- three
    lookups, two of which a department head is not allowed to make (`org:read`
    is an administrator's). It would also be a second implementation of the
    rule, free to drift from the one that actually decides.

    So the server answers it. The list is advisory: it shapes what the screen
    offers, and `assign_owner` still refuses anything outside it. A client that
    ignores this is not a security problem, only a client that shows a control
    which then fails.
    """
    task = get_task(session, ctx, task_id=task_id)
    department = session.get(Department, task.department_id) if task.department_id else None
    return assignable_for_department(session, ctx, department=department)


def assignable_for_department(
    session: Session, ctx: ExecutionContext, *, department: Department | None
) -> dict:
    """Who this actor may put on work belonging to a department (see above)."""
    head = _head_of(session, department)
    if head is not None and not can_work_tasks(session, head):
        head = None  # a head who cannot open tasks cannot staff them (see can_work_tasks)
    name = department.name if department else None

    def everyone() -> list[dict]:
        rows = (
            session.execute(select(User).where(User.org_id == ctx.org_id, User.is_active.is_(True)))
            .scalars()
            .all()
        )
        return [_person(u) for u in rows if can_work_tasks(session, u)]

    if head is None:
        if ctx.has_permission(Perm.TASK_ASSIGN.value):
            return {
                "scope": "UNHEADED",
                "department": name,
                "reason": (
                    f"{name} has no head, so staffing it falls to you."
                    if name
                    else "This task has no department, so staffing it falls to you."
                ),
                "candidates": everyone(),
            }
        return {
            "scope": "NONE",
            "department": name,
            "reason": (
                f"{name} has no head. Only a bid manager can assign it."
                if name
                else "This task has no department. Only a bid manager can assign it."
            ),
            "candidates": [],
        }

    if department is None:  # a head implies a department; stated so the type agrees
        return {
            "scope": "NONE",
            "department": None,
            "reason": "This task has no department. Only a bid manager can assign it.",
            "candidates": [],
        }
    acts_as_head = head.id == ctx.actor_user_id and ctx.has_permission(
        Perm.TASK_ASSIGN_IN_DEPARTMENT.value
    )
    if not acts_as_head and not ctx.has_permission(Perm.TASK_ASSIGN.value):
        return {
            "scope": "NONE",
            "department": name,
            "reason": f"{name} is headed by {head.display_name}. Staffing its work is theirs.",
            "candidates": [],
        }

    rows = (
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
        )
        .scalars()
        .all()
    )
    people = sorted(
        (_person(u) for u in rows), key=lambda p: (p["id"] != str(head.id), p["display_name"])
    )
    return {
        "scope": "DEPARTMENT",
        "department": name,
        "reason": (
            f"You head {name}, so you name the person who does its work."
            if acts_as_head
            else f"{name} is headed by {head.display_name}. Choose from its people."
        ),
        "candidates": people,
    }


def assign_owner(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> Task:
    """One accountable owner. Reassignment does not remove the old owner's
    conversation -- their working thread is part of the record."""
    task = get_task(session, ctx, task_id=task_id)
    _authorise_assignment(session, ctx, task=task, owner_user_id=owner_user_id)
    before = task.owner_user_id
    task.owner_user_id = owner_user_id
    session.flush()
    ensure_conversation(session, ctx, task_id=task.id, user_id=owner_user_id)
    audit.record(
        session,
        ctx,
        action="task.owner_assigned",
        entity_type="task",
        entity_id=task.id,
        bid_id=task.bid_id,
        before={"owner_user_id": str(before) if before else None},
        after={"owner_user_id": str(owner_user_id)},
    )
    notifications.notify(
        session,
        ctx,
        recipients=[owner_user_id],
        kind="task.owner_assigned",
        title=f"{task.code} was assigned to you",
        body=task.title,
        screen="tasks",
        bid_id=task.bid_id,
        focus_id=task.id,
    )
    return task


def settle_task_owners(session: Session, ctx: ExecutionContext) -> dict[str, int]:
    """Put work that nobody has picked up with the head of the department it belongs to.

    Covers tasks with no owner, and not-yet-started tasks held by someone who is
    not in the task's department (a stopgap from when departments had no head).
    Work a person has started is never moved. Returns counts for the seed to print.
    """
    tasks = (
        session.execute(
            select(Task).where(
                Task.org_id == ctx.org_id,
                Task.department_id.is_not(None),
                Task.state.in_(SYSTEM_ASSIGNED_STATES),
            )
        )
        .scalars()
        .all()
    )
    filled = moved = 0
    for task in tasks:
        department = session.get(Department, task.department_id)
        head = _head_of(session, department)
        if department is None or head is None:
            continue
        if not can_work_tasks(session, head) or task.owner_user_id == head.id:
            continue
        if task.owner_user_id is not None:
            in_department = session.execute(
                select(UserDepartment).where(
                    UserDepartment.org_id == ctx.org_id,
                    UserDepartment.user_id == task.owner_user_id,
                    UserDepartment.department_id == department.id,
                )
            ).scalar_one_or_none()
            if in_department is not None:
                continue
        before = task.owner_user_id
        task.owner_user_id = head.id
        session.flush()
        ensure_conversation(session, ctx, task_id=task.id, user_id=head.id)
        audit.record(
            session,
            ctx,
            action="task.owner_defaulted",
            entity_type="task",
            entity_id=task.id,
            bid_id=task.bid_id,
            before={"owner_user_id": str(before) if before else None},
            after={"owner_user_id": str(head.id), "reason": "department head"},
        )
        if before is None:
            filled += 1
        else:
            moved += 1
    return {"filled": filled, "moved": moved}


# --- participants ------------------------------------------------------------


def add_participant(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    participant_role: ParticipantRole,
) -> TaskParticipant:
    task = get_task(session, ctx, task_id=task_id)
    existing = session.execute(
        select(TaskParticipant).where(
            TaskParticipant.org_id == ctx.org_id,
            TaskParticipant.task_id == task_id,
            TaskParticipant.user_id == user_id,
            TaskParticipant.participant_role == participant_role,
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Re-adding someone previously removed reinstates them rather than
        # creating a second row, so the unique constraint stays meaningful.
        if existing.removed_at is not None:
            existing.removed_at = None
            existing.removed_by = None
            session.flush()
        participant = existing
    else:
        participant = TaskParticipant(
            org_id=ctx.org_id,
            task_id=task_id,
            user_id=user_id,
            participant_role=participant_role,
            added_by=ctx.actor_user_id or user_id,
        )
        session.add(participant)
        session.flush()

    ensure_conversation(session, ctx, task_id=task_id, user_id=user_id)
    audit.record(
        session,
        ctx,
        action="task.participant_added",
        entity_type="task",
        entity_id=task_id,
        bid_id=task.bid_id,
        after={"user_id": str(user_id), "role": participant_role.value},
    )
    return participant


def remove_participant(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    participant_role: ParticipantRole,
) -> TaskParticipant:
    """Removal is recorded, never deleted.

    Who could see a task last month is a question an auditor is entitled to ask,
    and a deleted row cannot answer it.
    """
    task = get_task(session, ctx, task_id=task_id)
    participant = session.execute(
        select(TaskParticipant).where(
            TaskParticipant.org_id == ctx.org_id,
            TaskParticipant.task_id == task_id,
            TaskParticipant.user_id == user_id,
            TaskParticipant.participant_role == participant_role,
        )
    ).scalar_one_or_none()
    if participant is None:
        raise NotFound("That person is not on this task in that role.")
    participant.removed_at = datetime.now(timezone.utc)
    participant.removed_by = ctx.actor_user_id
    session.flush()
    audit.record(
        session,
        ctx,
        action="task.participant_removed",
        entity_type="task",
        entity_id=task_id,
        bid_id=task.bid_id,
        after={"user_id": str(user_id), "role": participant_role.value},
    )
    return participant


def participants(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    include_removed: bool = False,
) -> list[TaskParticipant]:
    query = select(TaskParticipant).where(
        TaskParticipant.org_id == ctx.org_id, TaskParticipant.task_id == task_id
    )
    if not include_removed:
        query = query.where(TaskParticipant.removed_at.is_(None))
    return list(session.execute(query.order_by(TaskParticipant.created_at)).scalars().all())


def is_on_task(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Owner or active participant. The membership question the copilot asks."""
    task = session.get(Task, task_id)
    if task is None or task.org_id != ctx.org_id:
        return False
    if task.owner_user_id == user_id:
        return True
    return (
        session.execute(
            select(func.count())
            .select_from(TaskParticipant)
            .where(
                TaskParticipant.org_id == ctx.org_id,
                TaskParticipant.task_id == task_id,
                TaskParticipant.user_id == user_id,
                TaskParticipant.removed_at.is_(None),
            )
        ).scalar_one()
        > 0
    )


# --- conversations -----------------------------------------------------------


def ensure_conversation(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID, user_id: uuid.UUID
) -> TaskConversation:
    """Create the (task, user) conversation if it does not exist.

    Called when someone joins a task -- never from a GET. A read that writes
    makes conversation counts meaningless and lets a permission check be
    sidestepped by simply looking.
    """
    existing = session.execute(
        select(TaskConversation).where(
            TaskConversation.org_id == ctx.org_id,
            TaskConversation.task_id == task_id,
            TaskConversation.user_id == user_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    conversation = TaskConversation(
        org_id=ctx.org_id, task_id=task_id, user_id=user_id, message_count=0
    )
    session.add(conversation)
    session.flush()
    return conversation


# --- dependencies ------------------------------------------------------------


def add_dependency(
    session: Session,
    ctx: ExecutionContext,
    *,
    from_task_id: uuid.UUID,
    to_task_id: uuid.UUID,
    dependency_type: TaskDependencyType,
    note: str | None = None,
) -> TaskDependency:
    if from_task_id == to_task_id:
        raise ValidationError("A task cannot depend on itself.")
    from_task = get_task(session, ctx, task_id=from_task_id)
    get_task(session, ctx, task_id=to_task_id)

    if _would_cycle(session, ctx, from_task_id=from_task_id, to_task_id=to_task_id):
        raise ValidationError(
            "That dependency would create a cycle: the tasks would each be waiting for the other."
        )

    existing = session.execute(
        select(TaskDependency).where(
            TaskDependency.org_id == ctx.org_id,
            TaskDependency.from_task_id == from_task_id,
            TaskDependency.to_task_id == to_task_id,
            TaskDependency.dependency_type == dependency_type,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    dependency = TaskDependency(
        org_id=ctx.org_id,
        from_task_id=from_task_id,
        to_task_id=to_task_id,
        dependency_type=dependency_type,
        note=note,
    )
    session.add(dependency)
    session.flush()
    audit.record(
        session,
        ctx,
        action="task.dependency_added",
        entity_type="task",
        entity_id=from_task_id,
        bid_id=from_task.bid_id,
        after={
            "to_task_id": str(to_task_id),
            "dependency_type": dependency_type.value,
        },
    )
    return dependency


def _would_cycle(
    session: Session,
    ctx: ExecutionContext,
    *,
    from_task_id: uuid.UUID,
    to_task_id: uuid.UUID,
) -> bool:
    """Walk the existing edges from `to_task_id` looking for `from_task_id`.

    A dependency cycle is not a theoretical concern: two contributors each
    waiting on the other is a bid that silently stops moving, and nobody
    notices until the deadline.
    """
    seen: set[uuid.UUID] = set()
    frontier = [to_task_id]
    while frontier:
        current = frontier.pop()
        if current == from_task_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(
            session.execute(
                select(TaskDependency.to_task_id).where(
                    TaskDependency.org_id == ctx.org_id,
                    TaskDependency.from_task_id == current,
                )
            )
            .scalars()
            .all()
        )
    return False


def dependencies(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID
) -> dict[str, list[TaskDependency]]:
    outgoing = list(
        session.execute(
            select(TaskDependency).where(
                TaskDependency.org_id == ctx.org_id, TaskDependency.from_task_id == task_id
            )
        )
        .scalars()
        .all()
    )
    incoming = list(
        session.execute(
            select(TaskDependency).where(
                TaskDependency.org_id == ctx.org_id, TaskDependency.to_task_id == task_id
            )
        )
        .scalars()
        .all()
    )
    return {"outgoing": outgoing, "incoming": incoming}


def resolve_dependency(
    session: Session, ctx: ExecutionContext, *, dependency_id: uuid.UUID
) -> TaskDependency:
    dependency = session.get(TaskDependency, dependency_id)
    if dependency is None or dependency.org_id != ctx.org_id:
        raise NotFound("That dependency does not exist.")
    dependency.resolved_at = datetime.now(timezone.utc)
    session.flush()
    return dependency


# --- reading -----------------------------------------------------------------


def list_tasks(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID | None = None,
    owner_user_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    state: TaskState | None = None,
) -> list[Task]:
    query = select(Task).where(Task.org_id == ctx.org_id)
    if bid_id is not None:
        query = query.where(Task.bid_id == bid_id)
    if owner_user_id is not None:
        query = query.where(Task.owner_user_id == owner_user_id)
    if department_id is not None:
        query = query.where(Task.department_id == department_id)
    if state is not None:
        query = query.where(Task.state == state)
    return list(session.execute(query.order_by(Task.code)).scalars().all())


def tasks_for_picker(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID) -> list[dict]:
    """The tasks one person should be choosing between.

    A bid manager runs the whole bid and sees every task. Anyone else sees the tasks of
    their own departments and any task that has been given to them, wherever it sits --
    the rest is other people's work and only makes the list long. A task a person
    handed over by name says who did: "assigned by Omar R.".
    """
    rows = list_tasks(session, ctx, bid_id=bid_id)
    if not ctx.has_permission(Perm.TASK_ASSIGN.value):
        mine = set(ctx.department_ids)
        rows = [t for t in rows if t.department_id in mine or t.owner_user_id == ctx.actor_user_id]
    given = [t.id for t in rows if ctx.actor_user_id and t.owner_user_id == ctx.actor_user_id]
    by_whom: dict[uuid.UUID, str] = {}
    if given:
        events = (
            session.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == ctx.org_id,
                    AuditEvent.action == "task.owner_assigned",
                    AuditEvent.entity_id.in_(given),
                )
                .order_by(AuditEvent.occurred_at)
            )
            .scalars()
            .all()
        )
        for event in events:  # oldest first, so the latest assignment wins
            if (event.after_state or {}).get("owner_user_id") == str(ctx.actor_user_id):
                if event.entity_id is None:
                    continue
                if event.actor_user_id and event.actor_user_id != ctx.actor_user_id:
                    user = session.get(User, event.actor_user_id)
                    if user is not None:
                        by_whom[event.entity_id] = user.display_name
                else:
                    by_whom.pop(event.entity_id, None)
    return [
        {
            "id": t.id,
            "code": t.code,
            "title": t.title,
            "state": t.state.value if hasattr(t.state, "value") else t.state,
            "owner_user_id": t.owner_user_id,
            "assigned_by": by_whom.get(t.id),
        }
        for t in rows
    ]


def task_requirements(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID
) -> list[Requirement]:
    return list(
        session.execute(
            select(Requirement)
            .join(TaskRequirement, TaskRequirement.requirement_id == Requirement.id)
            .where(
                TaskRequirement.org_id == ctx.org_id,
                TaskRequirement.task_id == task_id,
                LIVE_REQUIREMENT,
            )
            .order_by(Requirement.code)
        )
        .scalars()
        .all()
    )


def board(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID) -> dict:
    """The bid's task board: counts by state and by department.

    Deliberately a read of stored state rather than a recomputation. A board
    that disagrees with the task list is worse than no board.
    """
    rows = list_tasks(session, ctx, bid_id=bid_id)
    by_state: dict[str, int] = {}
    by_department: dict[str, int] = {}
    for task in rows:
        by_state[task.state.value] = by_state.get(task.state.value, 0) + 1
        by_department[str(task.department_id)] = by_department.get(str(task.department_id), 0) + 1
    blocked = [t for t in rows if t.state is TaskState.BLOCKED]
    waiting = [
        t
        for t in rows
        if t.state in (TaskState.WAITING_FOR_ACCESS, TaskState.WAITING_FOR_DEPENDENCY)
    ]
    return {
        "total": len(rows),
        "by_state": by_state,
        "by_department": by_department,
        "blocked": [
            {"id": str(t.id), "code": t.code, "title": t.title, "note": t.blocker_note}
            for t in blocked
        ],
        "waiting": [{"id": str(t.id), "code": t.code, "state": t.state.value} for t in waiting],
        "unassigned": [
            {"id": str(t.id), "code": t.code, "title": t.title}
            for t in rows
            if t.owner_user_id is None
        ],
    }


# --- a person's own work -------------------------------------------------------

DASHBOARD_BUCKETS: dict[str, frozenset[TaskState]] = {
    "ready_for_approval": frozenset({TaskState.AI_READY_FOR_REVIEW, TaskState.READY_FOR_APPROVAL}),
    "needs_my_input": frozenset(
        {
            TaskState.NOT_STARTED,
            TaskState.NEEDS_INPUT,
            TaskState.IN_PROGRESS,
            TaskState.REWORK_REQUIRED,
            TaskState.REVALIDATION_REQUIRED,
            TaskState.BLOCKED,
        }
    ),
    "waiting_on_another_department": frozenset(
        {TaskState.WAITING_FOR_DEPENDENCY, TaskState.WAITING_FOR_ACCESS}
    ),
    "completed": frozenset({TaskState.APPROVED, TaskState.COMPLETED}),
}


def bucket_for(state: TaskState) -> str:
    """Which dashboard column a task in this state belongs in. Every state has one."""
    for name, states in DASHBOARD_BUCKETS.items():
        if state in states:
            return name
    return "needs_my_input"


def my_work(session: Session, ctx: ExecutionContext) -> dict:
    """What is waiting on the signed-in person, counted the way they think of it.

    The flat `/tasks/mine` list answered "what am I on" and left the person to
    work out which of those needed them now. This groups their tasks -- owned or
    joined as a participant -- into the columns of the walkthrough, adds the
    approval steps their role must sign, and the open requests addressed to
    their departments.
    """
    from app.models import Approval, CrossDepartmentRequest
    from app.models.enums import ApprovalStepState
    from app.services.requests import OPEN_STATES

    if ctx.actor_user_id is None:
        return {"counts": {}, "tasks": [], "approvals": [], "incoming_requests": []}

    participant_task_ids = select(TaskParticipant.task_id).where(
        TaskParticipant.org_id == ctx.org_id,
        TaskParticipant.user_id == ctx.actor_user_id,
    )
    participant_task_ids = participant_task_ids.where(TaskParticipant.removed_at.is_(None))
    mine = (
        session.execute(
            select(Task)
            .where(
                Task.org_id == ctx.org_id,
                (Task.owner_user_id == ctx.actor_user_id) | Task.id.in_(participant_task_ids),
            )
            .order_by(Task.code)
        )
        .scalars()
        .all()
    )
    approvals = (
        session.execute(
            select(Approval).where(
                Approval.org_id == ctx.org_id,
                Approval.state == ApprovalStepState.PENDING,
                Approval.required_role.in_(list(ctx.role_codes) or [""]),
            )
        )
        .scalars()
        .all()
    )
    incoming = (
        session.execute(
            select(CrossDepartmentRequest).where(
                CrossDepartmentRequest.org_id == ctx.org_id,
                CrossDepartmentRequest.target_department_id.in_(
                    list(ctx.department_ids) or [uuid.UUID(int=0)]
                ),
                CrossDepartmentRequest.state.in_(list(OPEN_STATES)),
            )
        )
        .scalars()
        .all()
    )

    # Tasks span bids; without the bid on each row two "PMO-01"s from two
    # tenders look like one bid with a duplicated code.
    from app.models import Bid, Opportunity

    bid_labels = {
        bid_id: f"{customer} · {tender}"
        for bid_id, customer, tender in session.execute(
            select(Bid.id, Opportunity.customer_name, Opportunity.tender_name)
            .join(Opportunity, Opportunity.id == Bid.opportunity_id)
            .where(
                Bid.org_id == ctx.org_id,
                Bid.id.in_({t.bid_id for t in mine} or {uuid.UUID(int=0)}),
            )
        )
    }
    counts = {name: 0 for name in DASHBOARD_BUCKETS}
    rows = []
    for task in mine:
        bucket = bucket_for(task.state)
        counts[bucket] += 1
        rows.append(
            {
                "task_id": str(task.id),
                "bid_id": str(task.bid_id),
                "bid": bid_labels.get(task.bid_id, ""),
                "code": task.code,
                "title": task.title,
                "state": task.state.value,
                "bucket": bucket,
            }
        )
    counts["approvals_for_my_role"] = len(approvals)
    counts["incoming_requests"] = len(incoming)
    return {
        "counts": counts,
        "tasks": rows,
        "approvals": [
            {
                "approval_id": str(a.id),
                "entity_type": a.entity_type,
                "entity_id": str(a.entity_id),
                "required_role": a.required_role,
            }
            for a in approvals
        ],
        "incoming_requests": [
            {
                "request_id": str(r.id),
                "origin_task_id": str(r.origin_task_id) if r.origin_task_id else None,
                "state": getattr(r.state, "value", r.state),
            }
            for r in incoming
        ],
    }
