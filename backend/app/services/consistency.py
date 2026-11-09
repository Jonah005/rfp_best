"""Checking approved answers against each other once they sit in one proposal.

Ahmed approved "testing will take six weeks"; Fatima approved "testing runs
weeks 2 to 5". Each was right on its own task and reviewed by its own owner,
and nothing ever put them side by side. This reads every approved statement of
a bid together and, for each contradiction the model finds, opens a
RESOLUTION task naming the statements involved and adding the owners of both
sides. The release gate already refuses a package while any task is open, so
an unresolved conflict blocks release without a rule of its own.

The model finds; code decides. A statement ref it was not shown is ignored,
and a conflict already turned into a task is not opened twice.

A conflict matters to a package only if the customer will read both sides of
it. When the package's own text is given, a conflict whose wording is not in
that text opens no task, and an open task that names such wording is retired
(closed as "superseded", with the reason on the audit trail), so the release
gate counts only what could reach the customer. Every task records where it
came from -- the statements and answer versions, both quotations, the check
run and prompt version -- on its audit event.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.core.logging import get_logger
from app.models import ArtifactStatement, Department, ResponseArtifact, Task, TaskRequirement
from app.models.enums import ArtifactState, DepartmentCode, ParticipantRole, TaskState
from app.services import audit
from app.services import tasks as task_service

log = get_logger(__name__)

RESOLUTION_MARK = "consistency:"


def _approved_statements(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID):
    rows = session.execute(
        select(ArtifactStatement, ResponseArtifact, Task)
        .join(
            ResponseArtifact,
            ResponseArtifact.approved_version_id == ArtifactStatement.artifact_version_id,
        )
        .join(Task, Task.id == ResponseArtifact.task_id)
        .where(
            ArtifactStatement.org_id == ctx.org_id,
            ResponseArtifact.bid_id == bid_id,
            ResponseArtifact.state == ArtifactState.APPROVED,
        )
        .order_by(Task.code, ArtifactStatement.order_index)
    ).all()
    return rows


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").casefold())


def statement_in_text(statement: str, package_text: str, *, floor: float = 0.35) -> bool:
    """Is this approved wording (or a light rewrite of it) in the package's documents?

    Chapters are joined and lightly edited before they are sent, so an exact match would
    miss reworded statements. Instead the five-word runs of the statement are looked for
    in the package; a statement that shares at least `floor` of them is in the package.
    """
    want = _tokens(statement)
    if not want:
        return True
    have = _tokens(package_text)
    if len(want) <= 5:
        return " ".join(want) in " ".join(have)
    wanted = {" ".join(want[i : i + 5]) for i in range(len(want) - 4)}
    held = {" ".join(have[i : i + 5]) for i in range(len(have) - 4)}
    return len(wanted & held) / len(wanted) >= floor


def fingerprint(statement_ids: list[uuid.UUID]) -> str:
    joined = "|".join(sorted(str(i) for i in statement_ids))
    return RESOLUTION_MARK + hashlib.sha256(joined.encode()).hexdigest()[:24]


_QUOTED = re.compile(r'^- ([A-Z]+-\d+): "(.*)"$', re.MULTILINE)


SUPERSEDED = "superseded"


def _close(
    session: Session,
    ctx: ExecutionContext,
    task: Task,
    *,
    reason: str,
    superseded: bool = False,
) -> None:
    """Finish a resolution task. These have no draft to approve, so they skip the machine.

    `superseded` marks a task the system retired because the conflict no longer applies to
    what will be sent. Unlike a task a person settled, it is not remembered: if the same
    conflict is found again where it does matter, a new task is opened.
    """
    before = task.state
    if superseded:
        task.blocker_note = f"{SUPERSEDED}: {reason}"
    task.state = TaskState.COMPLETED
    task.state_changed_at = datetime.now(timezone.utc)
    task.state_changed_by = ctx.actor_user_id
    session.flush()
    audit.record(
        session,
        ctx,
        action="consistency.conflict_closed",
        entity_type="task",
        entity_id=task.id,
        bid_id=task.bid_id,
        before={"state": before.value},
        after={"state": TaskState.COMPLETED.value},
        reason=reason,
    )


def retire_outside_package(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID, package_text: str
) -> int:
    """Retire open resolution tasks naming wording that is not in the package's documents."""
    retired = 0
    for task in _open_resolution_tasks(session, ctx, bid_id=bid_id):
        quoted = _QUOTED.findall(task.description or "")
        absent = sorted(
            {code for code, text in quoted if not statement_in_text(text, package_text)}
        )
        if quoted and absent:
            _close(
                session,
                ctx,
                task,
                reason=(
                    "The wording of " + ", ".join(absent) + " is not in this package's documents, "
                    "so the conflict does not reach the customer."
                ),
                superseded=True,
            )
            retired += 1
    return retired


def _resolution_home(
    session: Session, ctx: ExecutionContext, fallback: Task
) -> tuple[uuid.UUID, uuid.UUID | None]:
    """Where a resolution task belongs: with the bid manager, not with one of the two authors.

    A conflict between two approved answers is a decision about the offer as a whole. The bid
    manager owns it, and the authors of both answers join it as contributors. Only when the
    organisation has no bid-management department does it stay with the first answer's owner.
    """
    department = session.execute(
        select(Department).where(
            Department.org_id == ctx.org_id, Department.code == DepartmentCode.BID_MANAGEMENT
        )
    ).scalars().first()
    if department is None:
        return fallback.department_id, fallback.owner_user_id
    return department.id, None  # the department's head is chosen by the task service


def rehome_unstarted(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID) -> int:
    """Retire unstarted resolution tasks that sit outside bid management, so they reopen there.

    Tasks opened before resolution tasks belonged to the bid manager stay with whoever authored
    one of the answers. Nobody has begun them, so they are closed as superseded and the check that
    follows opens the same conflict again under the bid-management department. A task someone has
    started is left where it is.
    """
    home = session.execute(
        select(Department.id).where(
            Department.org_id == ctx.org_id, Department.code == DepartmentCode.BID_MANAGEMENT
        )
    ).scalar_one_or_none()
    if home is None:
        return 0
    moved = 0
    for task in _open_resolution_tasks(session, ctx, bid_id=bid_id):
        if task.state is TaskState.NOT_STARTED and task.department_id != home:
            _close(
                session,
                ctx,
                task,
                reason="Reopened under bid management, which owns conflicts between answers.",
                superseded=True,
            )
            moved += 1
    return moved


def retire_twins(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID) -> int:
    """Close open resolution tasks that repeat another open task's matter, keeping the oldest.

    Two tasks with the same title ask the same two answers the same question, so a person
    would answer it twice. The earlier one stays; later ones are superseded by it.
    """
    by_title: dict[str, list[Task]] = {}
    for task in _open_resolution_tasks(session, ctx, bid_id=bid_id):
        by_title.setdefault(task.title, []).append(task)
    retired = 0
    for twins in by_title.values():
        if len(twins) < 2:
            continue
        twins.sort(key=lambda t: (t.created_at, t.code or ""))
        keeper = twins[0]
        for twin in twins[1:]:
            _close(
                session,
                ctx,
                twin,
                reason=f"Repeats {keeper.code}, which asks the same question of the same answers.",
                superseded=True,
            )
            retired += 1
    return retired


def _open_resolution_tasks(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID):
    return (
        session.execute(
            select(Task).where(
                Task.org_id == ctx.org_id,
                Task.bid_id == bid_id,
                Task.grouping_rationale.like(RESOLUTION_MARK + "%"),
                Task.state != TaskState.COMPLETED,
            )
        )
        .scalars()
        .all()
    )


def close_stale(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID, rows) -> int:
    """Close resolution tasks whose statements are no longer in the approved answers.

    Editing an answer replaces its statements, so a conflict that named the old
    wording is either fixed or will be found again under the new wording. Left
    open it blocks release over text nobody is going to send.
    """
    current = {(task.code, statement.text) for statement, _, task in rows}
    open_tasks = _open_resolution_tasks(session, ctx, bid_id=bid_id)
    closed = 0
    for task in open_tasks:
        quoted = _QUOTED.findall(task.description or "")
        if quoted and any(pair not in current for pair in quoted):
            _close(
                session,
                ctx,
                task,
                reason="The wording it named has since been replaced in the approved answers.",
                superseded=True,
            )
            closed += 1
    return closed


def resolve(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    outcome: str,
    note: str,
) -> Task:
    """A person settles a conflict: it was fixed in the answers, or it was never one."""
    task = session.get(Task, task_id)
    if task is None or task.org_id != ctx.org_id:
        raise NotFound("That task does not exist.")
    if not (task.grouping_rationale or "").startswith(RESOLUTION_MARK):
        raise ValidationError("Only a conflict-resolution task can be resolved this way.")
    if outcome not in {"FIXED", "NOT_A_CONFLICT"}:
        raise ValidationError("The outcome is FIXED or NOT_A_CONFLICT.")
    if len((note or "").strip()) < 3:
        raise ValidationError("Say in a few words why, so the record shows the decision.")
    if task.state is not TaskState.COMPLETED:
        _close(session, ctx, task, reason=f"{outcome}: {note.strip()}")
    return task


def check_bid(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    gateway=None,
    package_text: str | None = None,
) -> dict:
    """Find contradictions between approved answers; open a resolution task for each.

    With `package_text` (the words of the documents being released), a conflict whose
    wording is not in it opens no task, and open tasks that name such wording are retired.

    Returns {"checked": statements, "conflicts": found, "tasks_opened": new, ...}.
    Raises whatever the gateway raises -- the caller decides whether an
    unavailable model blocks release (see `release.validate_package`).
    """
    from app.gateway.gateway import build_gateway
    from app.gateway.registry import get as get_call_type
    from app.gateway.registry import wrap_untrusted
    from app.schemas.consistency import ConsistencyResult

    rows = _approved_statements(session, ctx, bid_id=bid_id)
    close_stale(session, ctx, bid_id=bid_id, rows=rows)
    retired = retire_twins(session, ctx, bid_id=bid_id)
    rehome_unstarted(session, ctx, bid_id=bid_id)
    if package_text:
        retired += retire_outside_package(session, ctx, bid_id=bid_id, package_text=package_text)
    tasks_involved = {task.id for _, _, task in rows}
    if len(tasks_involved) < 2:
        return {"checked": len(rows), "conflicts": 0, "tasks_opened": 0}
    refs = {f"S{i}": row for i, row in enumerate(rows, start=1)}
    payload = {
        "statements": [
            {"ref": ref, "section": f"{task.code} {artifact.title}", "text": statement.text}
            for ref, (statement, artifact, task) in refs.items()
        ]
    }
    model_gateway = gateway or build_gateway()
    result = model_gateway.generate_structured(
        "cross_section_consistency",
        wrap_untrusted(json.dumps(payload, ensure_ascii=False)),
        ctx=ctx,
        session=session,
        cache=True,
    )
    if not isinstance(result, ConsistencyResult):
        return {"checked": len(rows), "conflicts": 0, "tasks_opened": 0}

    # Marks of conflicts already in front of a person (or settled by one). A task the system
    # retired as superseded is not remembered, so the conflict can come back where it matters.
    existing = {
        mark
        for mark, note in session.execute(
            select(Task.grouping_rationale, Task.blocker_note).where(
                Task.org_id == ctx.org_id,
                Task.bid_id == bid_id,
                Task.grouping_rationale.like(RESOLUTION_MARK + "%"),
            )
        ).all()
        if not (note or "").startswith(SUPERSEDED)
    }
    open_by_title = {t.title: t for t in _open_resolution_tasks(session, ctx, bid_id=bid_id)}
    run_id = uuid.uuid4()
    prompt_version = get_call_type("cross_section_consistency").prompt_version
    opened = found = outside = 0
    for conflict in result.conflicts:
        if not conflict.cannot_both_be_true:
            continue
        a, b = (
            re.sub(r"\W+", " ", v.casefold()).strip() for v in (conflict.value_a, conflict.value_b)
        )
        if a and b and a == b:
            continue  # the same value twice is agreement, not a conflict
        involved = [
            refs[r.strip().upper()] for r in conflict.statement_refs if r.strip().upper() in refs
        ]
        involved_tasks = {task.id: task for _, _, task in involved}
        if len(involved) < 2 or len(involved_tasks) < 2:
            continue  # a conflict inside one answer is its reviewer's business
        found += 1
        mark = fingerprint([statement.id for statement, _, _ in involved])
        if package_text and any(
            not statement_in_text(statement.text, package_text) for statement, _, _ in involved
        ):
            outside += 1  # a side of this conflict is not in what the customer will read
            continue
        if mark in existing:
            continue
        lead = next(iter(involved_tasks.values()))
        codes = " and ".join(sorted(t.code for t in involved_tasks.values()))
        quoted = "\n".join(f'- {task.code}: "{statement.text}"' for statement, _, task in involved)
        title = f"Resolve {conflict.kind.lower()} conflict between {codes}"
        same_matter = open_by_title.get(title)
        if same_matter is not None:
            # The same kind of conflict between the same two answers is one matter for one
            # person; the further wording goes on that task instead of opening a twin.
            if quoted not in (same_matter.description or ""):
                same_matter.description = (
                    f"{same_matter.description or ''}\n\nAlso found (check run {run_id}): "
                    f"{conflict.explanation}\n{quoted}"
                )
            existing.add(mark)
            continue
        # The resolution task answers what the conflicting answers answer: it covers their
        # requirements (a task with no requirement is refused by the task service).
        requirement_ids = list(
            dict.fromkeys(
                session.execute(
                    select(TaskRequirement.requirement_id).where(
                        TaskRequirement.org_id == ctx.org_id,
                        TaskRequirement.task_id.in_(list(involved_tasks)),
                    )
                ).scalars()
            )
        )
        if not requirement_ids:
            log.warning("consistency_conflict_without_requirement", tasks=codes)
            continue
        home_department, home_owner = _resolution_home(session, ctx, lead)
        task = task_service.create_task(
            session,
            ctx,
            bid_id=bid_id,
            requirement_ids=requirement_ids,
            title=title,
            department_id=home_department,
            description=(
                f"{conflict.explanation}\n\nStatements involved:\n{quoted}"
                f"\n\nFound by consistency check run {run_id} (prompt {prompt_version})."
            ),
            owner_user_id=home_owner,
            grouping_rationale=mark,
        )
        for other in involved_tasks.values():
            if other.owner_user_id and other.owner_user_id != task.owner_user_id:
                task_service.add_participant(
                    session,
                    ctx,
                    task_id=task.id,
                    user_id=other.owner_user_id,
                    participant_role=ParticipantRole.CONTRIBUTOR,
                )
        existing.add(mark)
        open_by_title[title] = task
        opened += 1
        audit.record(
            session,
            ctx,
            action="consistency.conflict_found",
            entity_type="task",
            entity_id=task.id,
            bid_id=bid_id,
            after={
                "kind": conflict.kind,
                "tasks": sorted(t.code for t in involved_tasks.values()),
                "check_run_id": str(run_id),
                "prompt_version": prompt_version,
                "values": [conflict.value_a, conflict.value_b],
                "statements": [
                    {
                        "task": task.code,
                        "statement_id": str(statement.id),
                        "artifact_version_id": str(statement.artifact_version_id),
                        "quote": statement.text,
                        "in_package": (
                            None
                            if not package_text
                            else statement_in_text(statement.text, package_text)
                        ),
                    }
                    for statement, _, task in involved
                ],
            },
        )
    log.info(
        "consistency_checked",
        bid_id=str(bid_id),
        statements=len(rows),
        conflicts=found,
        opened=opened,
        outside_package=outside,
        retired=retired,
    )
    return {
        "checked": len(rows),
        "conflicts": found,
        "tasks_opened": opened,
        "outside_package": outside,
        "retired": retired,
    }
