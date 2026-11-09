"""Gaps in the tender, as they concern one task.

The tender is silent or unclear in places (a clarification issue). Not every gap
matters to every task, and a keyword match would attach the wrong ones. So the
model reads the task's requirements next to the candidate gaps and says which
ones the task's work would depend on, and why -- and code decides whether to
believe it: only gaps that exist, in this bid, are ever attached, and a
person's choice, either way, is never overridden.

The answer to a gap is an assumption the company records. The model may propose
its wording from the task's approved evidence; it never records anything.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.core.logging import get_logger
from app.models import (
    ClarificationIssue,
    ClarificationRequirement,
    Department,
    Task,
    TaskGap,
    User,
)
from app.models.enums import ClarificationStatus
from app.services import context_builder
from app.services import tasks as task_service
from app.services.clarifications import update_status

log = get_logger("task_gaps")

BATCH = 30
CANDIDATE_CAP = 60
_LIVE = [ClarificationStatus.OPEN, ClarificationStatus.SENT_TO_CLIENT, ClarificationStatus.ANSWERED]


def _task(session: Session, ctx: ExecutionContext, task_id: uuid.UUID) -> Task:
    return task_service.get_task(session, ctx, task_id=task_id)


def _requirement_texts(session: Session, ctx: ExecutionContext, task_id: uuid.UUID) -> list[dict]:
    from app.models import RequirementVersion

    out = []
    for req in task_service.task_requirements(session, ctx, task_id=task_id)[:60]:
        version = (
            session.get(RequirementVersion, req.current_version_id)
            if req.current_version_id
            else None
        )
        out.append({"code": req.code, "text": (version.requirement_text if version else "")[:1200]})
    return out


def candidates(
    session: Session, ctx: ExecutionContext, task: Task, *, skip: set[uuid.UUID]
) -> list[ClarificationIssue]:
    """Live gaps in the task's bid worth putting to the model.

    The ones tied to the task's own requirements come first, then the ones in the
    task's department. Anything a person or an earlier check already decided is
    left out.
    """
    req_ids = [r.id for r in task_service.task_requirements(session, ctx, task_id=task.id)]
    linked: set[uuid.UUID] = set()
    if req_ids:
        linked = set(
            session.execute(
                select(ClarificationRequirement.clarification_id).where(
                    ClarificationRequirement.org_id == ctx.org_id,
                    ClarificationRequirement.requirement_id.in_(req_ids),
                )
            )
            .scalars()
            .all()
        )
    rows = (
        session.execute(
            select(ClarificationIssue)
            .where(
                ClarificationIssue.org_id == ctx.org_id,
                ClarificationIssue.bid_id == task.bid_id,
                ClarificationIssue.status.in_(_LIVE),
            )
            .order_by(ClarificationIssue.created_at)
        )
        .scalars()
        .all()
    )
    rows = [r for r in rows if r.id not in skip]
    first = [r for r in rows if r.id in linked]
    second = [r for r in rows if r.id not in linked and r.department_id == task.department_id]
    return (first + second)[:CANDIDATE_CAP]


def _ask(
    session: Session, ctx: ExecutionContext, task: Task, chunk: list[ClarificationIssue], gateway
) -> dict[int, dict] | None:
    """{n: judgement}; None when the model could not answer."""
    from app.gateway.registry import wrap_untrusted
    from app.schemas.task_gaps import GapRelevanceResult

    payload = {
        "TASK": {
            "code": task.code,
            "title": task.title,
            "description": (task.description or "")[:500],
            "requirements": _requirement_texts(session, ctx, task.id),
        },
        "GAPS": [
            {
                "n": i,
                "problem": (g.description or "")[:700],
                "question": (g.recommended_question or "")[:300],
            }
            for i, g in enumerate(chunk, start=1)
        ],
    }
    try:
        result = gateway.generate_structured(
            "task_gap_relevance",
            wrap_untrusted(json.dumps(payload, ensure_ascii=False)),
            ctx=ctx,
            session=session,
        )
    except Exception:  # noqa: BLE001
        log.info("task_gap_relevance_unavailable", task_id=str(task.id))
        return None
    if not isinstance(result, GapRelevanceResult):
        return None
    out: dict[int, dict] = {}
    for item in result.items:
        if 1 <= item.n <= len(chunk):
            out[item.n] = {
                "relevant": item.relevant,
                "missing": item.missing.strip(),
                "why": item.why_needed.strip(),
                "codes": list(item.requirement_codes),
                "part": item.part.strip(),
                "same_as": item.same_as,
            }
    return out


def _link_requirements(session: Session, ctx: ExecutionContext, issue, requirements: list) -> None:
    """Record which requirements a gap leaves a hole in. Code checks the codes are real."""
    have = set(
        session.execute(
            select(ClarificationRequirement.requirement_id).where(
                ClarificationRequirement.org_id == ctx.org_id,
                ClarificationRequirement.clarification_id == issue.id,
            )
        ).scalars()
    )
    for req in requirements:
        if req.id not in have:
            session.add(
                ClarificationRequirement(
                    org_id=ctx.org_id, clarification_id=issue.id, requirement_id=req.id
                )
            )


def _repeat_of(answer: dict, n: int, chunk: list, answers: dict, row) -> uuid.UUID | None:
    """The gap this one repeats, when the model said so and the code agrees.

    The model names an earlier gap by number. Code checks that the number is an earlier
    one in this list, that the model judged it relevant too, that it is not itself a
    repeat (so a repeat always points at the first, never in a chain), and that a person
    has not said these are separate. Anything else is ignored: two gaps stay two.
    """
    if row.keep_separate:
        return None
    target = answer.get("same_as")
    if not isinstance(target, int) or not 1 <= target < n:
        return None
    other = answers.get(target)
    if other is None or not other["relevant"] or other.get("same_as"):
        return None
    return chunk[target - 1].id


def _current_version() -> str:
    """The version of the check now in force: that of the prompt that does the judging."""
    from app.gateway.registry import get as call_type

    return call_type("task_gap_relevance").prompt_version


def check(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID, gateway, force: bool = False
) -> dict:
    """Have the model decide which gaps concern this task. Cached on the task.

    `force` asks again about the gaps the model itself judged before; a gap a
    person attached or removed is never re-judged.
    """
    task = _task(session, ctx, task_id)
    existing = {
        g.clarification_id: g
        for g in session.execute(
            select(TaskGap).where(TaskGap.org_id == ctx.org_id, TaskGap.task_id == task.id)
        ).scalars()
    }
    # A task checked with an older version of the check is checked again, once.
    if task.gaps_checked_at is not None and task.gaps_check_version != _current_version():
        force = True
    if task.gaps_checked_at is not None and not force:
        return {"asked": 0, "attached": 0, "skipped": "already checked"}
    skip = {cid for cid, g in existing.items() if g.source == "person" or not force}
    pool = candidates(session, ctx, task, skip=skip)
    attached = asked = 0
    answered_any = not pool
    by_code = {r.code: r for r in task_service.task_requirements(session, ctx, task_id=task.id)}
    for start in range(0, len(pool), BATCH):
        chunk = pool[start : start + BATCH]
        answers = _ask(session, ctx, task, chunk, gateway)
        if answers is None:
            continue
        answered_any = True
        for i, issue in enumerate(chunk, start=1):
            if i not in answers:
                continue
            asked += 1
            answer = answers[i]
            relevant = bool(answer["relevant"])
            row = existing.get(issue.id)
            if row is None:
                row = TaskGap(
                    org_id=ctx.org_id, task_id=task.id, clarification_id=issue.id, source="model"
                )
                session.add(row)
                existing[issue.id] = row
            row.attached = relevant
            row.missing = (answer["missing"] or None) if relevant else None
            row.why_needed = (answer["why"] or None) if relevant else None
            row.part = (answer["part"][:120] or None) if relevant else None
            row.duplicate_of = _repeat_of(answer, i, chunk, answers, row) if relevant else None
            attached += 1 if relevant else 0
            if relevant:
                _link_requirements(
                    session, ctx, issue, [by_code[c] for c in answer["codes"] if c in by_code]
                )
    if answered_any:
        task.gaps_checked_at = datetime.now(timezone.utc)
        task.gaps_check_version = _current_version()
    session.flush()
    return {"asked": asked, "attached": attached}


def list_for_task(session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID) -> dict:
    """The gaps attached to a task, with their state and who owns each."""
    task = _task(session, ctx, task_id)
    rows = session.execute(
        select(TaskGap, ClarificationIssue)
        .join(ClarificationIssue, ClarificationIssue.id == TaskGap.clarification_id)
        .where(
            TaskGap.org_id == ctx.org_id,
            TaskGap.task_id == task.id,
            TaskGap.attached.is_(True),
            # Closed (no impact) and withdrawn (not real) gaps leave the task's list.
            ClarificationIssue.status.in_(_LIVE),
        )
        .order_by(ClarificationIssue.created_at)
    ).all()
    departments = {
        d.id: d
        for d in session.execute(
            select(Department).where(Department.org_id == ctx.org_id)
        ).scalars()
    }
    owner_ids = {i.owner_user_id for _, i in rows if i.owner_user_id}
    owners = (
        {
            u.id: u
            for u in session.execute(
                select(User).where(User.org_id == ctx.org_id, User.id.in_(owner_ids))
            ).scalars()
        }
        if owner_ids
        else {}
    )
    from app.models import CrossDepartmentRequest

    gap_ids = [i.id for _, i in rows]
    asked: dict[uuid.UUID, list[CrossDepartmentRequest]] = {}
    if gap_ids:
        for req in session.execute(
            select(CrossDepartmentRequest)
            .where(
                CrossDepartmentRequest.org_id == ctx.org_id,
                CrossDepartmentRequest.clarification_id.in_(gap_ids),
            )
            .order_by(CrossDepartmentRequest.created_at)
        ).scalars():
            if req.clarification_id is not None:
                asked.setdefault(req.clarification_id, []).append(req)
    answerer_ids = {i.answered_by_user_id for _, i in rows if i.answered_by_user_id}
    answerers = (
        {
            u.id: u.display_name
            for u in session.execute(
                select(User).where(User.org_id == ctx.org_id, User.id.in_(answerer_ids))
            ).scalars()
        }
        if answerer_ids
        else {}
    )
    linked = _related_codes(session, ctx, task_id=task.id, issues=[i for _, i in rows])
    # A gap the AI judged to repeat another one is folded under that one -- when the other
    # is on the list -- instead of standing as a second card. It is still there, and a
    # person can say the two are different.
    shown = {i.id for _, i in rows}
    folded: dict[uuid.UUID, list[dict]] = {}
    for gap, issue in rows:
        if gap.duplicate_of in shown and not gap.keep_separate:
            folded.setdefault(gap.duplicate_of, []).append(
                {
                    "id": str(issue.id),
                    "missing": gap.missing or (issue.description or "")[:400],
                    "why_needed": gap.why_needed,
                    "part": gap.part,
                }
            )
    items = []
    for gap, issue in rows:
        if gap.duplicate_of in shown and not gap.keep_separate:
            continue
        dept = departments.get(issue.department_id) if issue.department_id else None
        owner = owners.get(issue.owner_user_id) if issue.owner_user_id else None
        items.append(
            {
                "id": str(issue.id),
                "missing": gap.missing or (issue.description or "")[:400],
                "why_needed": gap.why_needed,
                "requirement_codes": linked.get(issue.id, []),
                "part": gap.part,
                "similar": folded.get(issue.id, []),
                "status": issue.status.value,
                "assumption": issue.client_response,
                "department": dept.name if dept else None,
                "owner_name": owner.display_name if owner else None,
                "attached_by": gap.source,
                "include_in_response": issue.include_in_response,
                "answer_source": issue.answer_source,
                "answered_by": answerers.get(issue.answered_by_user_id),
                "answered_at": issue.answered_at.isoformat() if issue.answered_at else None,
                "requests": [
                    {
                        "id": str(r.id),
                        "department": departments[r.target_department_id].name
                        if r.target_department_id in departments
                        else None,
                        "state": r.state.value,
                        "question": r.request_text,
                        "reply": r.resolution_text,
                        "reviewed": r.reviewed_at is not None,
                    }
                    for r in asked.get(issue.id, [])
                ],
            }
        )
    return {
        "checked": task.gaps_checked_at is not None
        and task.gaps_check_version == _current_version(),
        "items": items,
    }


def attach(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID, clarification_id: uuid.UUID
) -> None:
    """A person says this gap concerns the task."""
    _set(session, ctx, task_id, clarification_id, attached=True)


def detach(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID, clarification_id: uuid.UUID
) -> None:
    """A person says it does not. The AI will not put it back."""
    _set(session, ctx, task_id, clarification_id, attached=False)


def separate(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID, clarification_id: uuid.UUID
) -> None:
    """A person says a folded gap is not the same as the one it sits under.

    It becomes its own card again, and the AI does not fold it a second time.
    """
    task = _task(session, ctx, task_id)
    row = session.execute(
        select(TaskGap).where(
            TaskGap.org_id == ctx.org_id,
            TaskGap.task_id == task.id,
            TaskGap.clarification_id == clarification_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("That gap is not part of this task.")
    row.keep_separate = True
    row.duplicate_of = None
    session.flush()


def _set(
    session: Session,
    ctx: ExecutionContext,
    task_id: uuid.UUID,
    clarification_id: uuid.UUID,
    *,
    attached: bool,
) -> None:
    task = _task(session, ctx, task_id)
    issue = session.get(ClarificationIssue, clarification_id)
    if issue is None or issue.org_id != ctx.org_id or issue.bid_id != task.bid_id:
        raise NotFound("That gap is not part of this task's bid.")
    row = session.execute(
        select(TaskGap).where(
            TaskGap.org_id == ctx.org_id,
            TaskGap.task_id == task.id,
            TaskGap.clarification_id == clarification_id,
        )
    ).scalar_one_or_none()
    if row is None:
        row = TaskGap(
            org_id=ctx.org_id, task_id=task.id, clarification_id=clarification_id, source="person"
        )
        session.add(row)
    row.attached = attached
    row.source = "person"
    session.flush()


def settle(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    clarification_id: uuid.UUID,
    status: ClarificationStatus,
    assumption: str | None,
    answer_source: str = "person",
    include: bool = True,
) -> ClarificationIssue:
    """Record the assumption, or close the gap, from the task screen."""
    task = _task(session, ctx, task_id)
    issue = session.get(ClarificationIssue, clarification_id)
    if issue is None or issue.org_id != ctx.org_id or issue.bid_id != task.bid_id:
        raise NotFound("That gap is not part of this task's bid.")
    if status not in (
        ClarificationStatus.ANSWERED,
        ClarificationStatus.CLOSED,
        ClarificationStatus.WITHDRAWN,
    ):
        raise ValidationError("A gap is settled by an assumption, no impact, or not a real gap.")
    if answer_source not in ("person", "model", "department"):
        raise ValidationError("An answer comes from a person, the AI or a department.")
    issue = update_status(
        session,
        ctx,
        issue_id=clarification_id,
        status=status,
        client_response=assumption if status is ClarificationStatus.ANSWERED else None,
        reason="Settled from the task screen.",
    )
    from app.services import todo

    todo.mark_reviewed(session, ctx, clarification_id=clarification_id)
    if status is ClarificationStatus.ANSWERED:
        issue.answer_source = answer_source
        issue.answered_by_user_id = ctx.actor_user_id
        issue.include_in_response = bool(include)
        session.flush()
    return issue


def set_include(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    clarification_id: uuid.UUID,
    include: bool,
) -> None:
    """Whether the response may rest on this gap's recorded assumption."""
    task = _task(session, ctx, task_id)
    issue = session.get(ClarificationIssue, clarification_id)
    if issue is None or issue.org_id != ctx.org_id or issue.bid_id != task.bid_id:
        raise NotFound("That gap is not part of this task's bid.")
    if issue.status is not ClarificationStatus.ANSWERED:
        raise ValidationError("Only a recorded assumption can be included in the response.")
    issue.include_in_response = bool(include)
    session.flush()


def propose_assumption(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    clarification_id: uuid.UUID,
    gateway,
    embedder=None,
) -> dict:
    """Draft the wording of an assumption from what this task is allowed to see.

    Nothing is recorded: the person reads it, edits it and records it.
    """
    from app.gateway.registry import wrap_untrusted
    from app.schemas.task_gaps import GapAssumptionProposal

    task = _task(session, ctx, task_id)
    issue = session.get(ClarificationIssue, clarification_id)
    if issue is None or issue.org_id != ctx.org_id or issue.bid_id != task.bid_id:
        raise NotFound("That gap is not part of this task's bid.")
    built = context_builder.build(
        session,
        ctx,
        task_id=task.id,
        query=(issue.description or "")[:400],
        embedder=embedder,
    )
    gap_text = json.dumps(
        {
            "gap": (issue.description or "")[:900],
            "question": (issue.recommended_question or "")[:400],
        },
        ensure_ascii=False,
    )
    prompt = built.prompt_block() + "\n\nGAP TO SETTLE\n" + wrap_untrusted(gap_text)
    try:
        result = gateway.generate_structured("gap_assumption", prompt, ctx=ctx, session=session)
    except Exception as exc:  # noqa: BLE001
        log.info("gap_assumption_unavailable", task_id=str(task.id))
        raise ValidationError("The AI could not draft an assumption just now. Try again.") from exc
    if not isinstance(result, GapAssumptionProposal):
        raise ValidationError("The AI could not draft an assumption just now. Try again.")
    known = {s.ref: s for s in built.sources}
    used = []
    for ref in result.based_on:
        key = ref.strip().strip("[]")
        source = known.get(key)
        if source is not None:
            used.append({"ref": key, "kind": source.kind, "text": source.text[:160]})
    return {
        "assumption": result.assumption.strip(),
        "based_on": used,
        "caution": result.caution.strip(),
    }


def revise_assumption(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    clarification_id: uuid.UUID,
    current: str,
    instruction: str,
    gateway,
    conversation_id: uuid.UUID | None = None,
    embedder=None,
) -> dict:
    """Change the drafted wording the way the person asked, in their own words.

    Nothing is recorded as the assumption. The exchange is filed in the missing-parts
    thread so the chat shows what was asked and what changed.
    """
    from app.gateway.registry import wrap_untrusted
    from app.models.enums import ChatRole
    from app.schemas.task_gaps import GapRevision
    from app.services import copilot

    if not (instruction or "").strip():
        raise ValidationError("Say what to change.")
    task = _task(session, ctx, task_id)
    issue = session.get(ClarificationIssue, clarification_id)
    if issue is None or issue.org_id != ctx.org_id or issue.bid_id != task.bid_id:
        raise NotFound("That gap is not part of this task's bid.")
    built = context_builder.build(
        session,
        ctx,
        task_id=task.id,
        query=((issue.description or "")[:300] + " " + instruction)[:600],
        embedder=embedder,
    )
    material = json.dumps(
        {
            "gap": (issue.description or "")[:900],
            "current_wording": (current or "")[:1500],
            "person_asks": instruction.strip()[:800],
        },
        ensure_ascii=False,
    )
    prompt = (
        built.prompt_block() + "\n\nGAP, CURRENT WORDING AND REQUEST\n" + wrap_untrusted(material)
    )
    try:
        result = gateway.generate_structured(
            "gap_assumption_revise", prompt, ctx=ctx, session=session
        )
    except Exception as exc:  # noqa: BLE001
        log.info("gap_revise_unavailable", task_id=str(task.id))
        raise ValidationError("The AI could not change the wording just now. Try again.") from exc
    if not isinstance(result, GapRevision):
        raise ValidationError("The AI could not change the wording just now. Try again.")
    known = {s.ref: s for s in built.sources}
    used = []
    for ref in result.based_on:
        key = ref.strip().strip("[]")
        source = known.get(key)
        if source is not None:
            used.append({"ref": key, "kind": source.kind, "text": source.text[:160]})
    out = {
        "assumption": result.assumption.strip(),
        "based_on": used,
        "caution": result.caution.strip(),
        "what_changed": result.what_changed.strip(),
    }
    if conversation_id is not None:
        conversation = copilot.get_conversation(session, ctx, conversation_id=conversation_id)
        if conversation.task_id == task.id:
            stored = {"kind": "gaps"}
            copilot._append(
                session,
                ctx,
                conversation=conversation,
                role=ChatRole.USER,
                content=instruction.strip(),
                focus_key="gaps",
                focus=stored,
            )
            reply = "I changed the wording above."
            if out["what_changed"]:
                reply = f"I changed the wording above: {out['what_changed']}"
            if out["caution"]:
                reply += f"\nCheck first: {out['caution']}"
            copilot._append(
                session,
                ctx,
                conversation=conversation,
                role=ChatRole.ASSISTANT,
                content=reply,
                focus_key="gaps",
                focus=stored,
                prompt_version="gap_assumption_revise.v1",
            )
    return out


def _related_codes(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID, issues: list
) -> dict[uuid.UUID, list[str]]:
    """Per missing part: the codes of this task's requirements it is linked to."""
    on_task = task_service.task_requirements(session, ctx, task_id=task_id)
    code_of = {r.id: r.code for r in on_task}
    out: dict[uuid.UUID, list[str]] = {i.id: [] for i in issues}
    ids = [i.id for i in issues]
    if ids and code_of:
        for cid, rid in session.execute(
            select(
                ClarificationRequirement.clarification_id,
                ClarificationRequirement.requirement_id,
            ).where(
                ClarificationRequirement.org_id == ctx.org_id,
                ClarificationRequirement.clarification_id.in_(ids),
            )
        ):
            if rid in code_of:
                out[cid].append(code_of[rid])
    return {k: sorted(v) for k, v in out.items()}


def _scope_requirements(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID, issue, built
) -> list[str]:
    """Narrow the requirement context to the ones this missing part is tied to.

    The tender ties a gap to the requirements it affects. Those stay in full; every
    other requirement on the task shrinks to a code and a few words, so the copilot
    still knows they exist (and can say when one truly depends on this part) but does
    not wander through them. A gap with no recorded link keeps the whole list: better
    too much context than a copilot that cannot see what the part is about.
    Returns the codes that were kept in full.
    """
    codes = _related_codes(session, ctx, task_id=task_id, issues=[issue])[issue.id]
    if not codes or not built.requirement_block:
        return []
    kept: list[str] = []
    for line in built.requirement_block.split("\n"):
        code = line[1 : line.find("]")] if line.startswith("[") and "]" in line else ""
        if code and code not in codes:
            tail = line[line.find(")") + 2 :] if ")" in line else ""
            kept.append(f"[{code}] (not tied to this part) {tail[:70]}...")
        else:
            kept.append(line)
    built.requirement_block = "\n".join(kept)
    return codes


def gap_chat(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    clarification_id: uuid.UUID,
    message: str,
    current: str,
    conversation_id: uuid.UUID,
    gateway,
    embedder=None,
) -> dict:
    """One turn of the chat about a single missing part.

    The thread belongs to that missing part alone: only its own earlier turns are shown
    to the model, and nothing here is recorded as the assumption -- a person does that.
    """
    from app.gateway.registry import wrap_untrusted
    from app.models.enums import ChatRole
    from app.schemas.task_gaps import GapChatTurn
    from app.services import copilot

    if not (message or "").strip():
        raise ValidationError("Say something first.")
    task = _task(session, ctx, task_id)
    issue = session.get(ClarificationIssue, clarification_id)
    if issue is None or issue.org_id != ctx.org_id or issue.bid_id != task.bid_id:
        raise NotFound("That gap is not part of this task's bid.")
    conversation = copilot.get_conversation(session, ctx, conversation_id=conversation_id)
    if conversation.task_id != task.id:
        raise NotFound("That conversation is not about this task.")

    key = f"gap:{issue.id}"
    item = next(
        (
            i
            for i in list_for_task(session, ctx, task_id=task.id)["items"]
            if i["id"] == str(issue.id)
        ),
        None,
    )
    history = [
        {
            "who": "person" if m.role is ChatRole.USER else "copilot",
            "said": m.content[: 700 if m.role is ChatRole.USER else 350],
        }
        for m in copilot.messages(
            session, ctx, conversation_id=conversation.id, limit=10, focus_key=key
        )
    ]
    built = context_builder.build(
        session,
        ctx,
        task_id=task.id,
        query=((issue.description or "")[:300] + " " + message)[:600],
        embedder=embedder,
    )
    related = _scope_requirements(session, ctx, task_id=task.id, issue=issue, built=built)
    all_codes = {r.code for r in task_service.task_requirements(session, ctx, task_id=task.id)}
    stray = (all_codes - set(related)) if related else set()
    if stray:
        # An earlier reply written before the chat was scoped may talk about other
        # requirements. Left in, the model copies it forward, so those sentences go.
        for turn in history:
            if turn["who"] == "copilot":
                sentences = re.split(r"(?<=[.!?])\s+", turn["said"])
                turn["said"] = " ".join(
                    x for x in sentences if not any(re.search(rf"\b{c}\b", x) for c in stray)
                )
    material = json.dumps(
        {
            "related_requirements": related,
            "missing_part": (issue.description or "")[:900],
            "which_piece_of_the_requirement": (item or {}).get("part"),
            "same_point_worded_elsewhere": [x["missing"] for x in (item or {}).get("similar", [])],
            "why_this_task_needs_it": (item or {}).get("why_needed"),
            "recorded_assumption": (item or {}).get("assumption"),
            "department_replies": [
                {"department": r["department"], "question": r["question"], "reply": r["reply"]}
                for r in (item or {}).get("requests", [])
                if r.get("reply")
            ],
            "current_draft": (current or "")[:1500] or None,
            "conversation_so_far": history,
            "person_says": message.strip()[:1500],
        },
        ensure_ascii=False,
    )
    prompt = (
        built.prompt_block() + "\n\nTHIS MISSING PART AND THE CHAT\n" + wrap_untrusted(material)
    )
    try:
        result = gateway.generate_structured("gap_chat", prompt, ctx=ctx, session=session)
    except Exception as exc:  # noqa: BLE001
        log.info("gap_chat_unavailable", task_id=str(task.id))
        raise ValidationError("The AI could not answer just now. Try again.") from exc
    if not isinstance(result, GapChatTurn):
        raise ValidationError("The AI could not answer just now. Try again.")

    known = {s.ref: s for s in built.sources}
    used = []
    for ref in result.based_on:
        source = known.get(ref.strip().strip("[]"))
        if source is not None:
            used.append({"ref": source.ref, "kind": source.kind, "text": source.text[:160]})
    assumption = (result.assumption or "").strip() or None

    stored = {"kind": "gap", "gap_id": str(issue.id)}
    copilot._append(
        session,
        ctx,
        conversation=conversation,
        role=ChatRole.USER,
        content=message.strip(),
        focus_key=key,
        focus=stored,
    )
    reply = result.reply.strip()
    if assumption:
        reply += (
            "\n\nI put the wording in the box above. Change it by telling me, or edit it there."
        )
    copilot._append(
        session,
        ctx,
        conversation=conversation,
        role=ChatRole.ASSISTANT,
        content=reply,
        focus_key=key,
        focus=stored,
        prompt_version="gap_chat.v1",
    )
    return {
        "reply": reply,
        "assumption": assumption,
        "based_on": used,
        "caution": result.caution.strip(),
    }


# --- checking every task of a bid in one go ----------------------------------
#
# Opening a task never asks the AI anything: it only loads what was saved. One button
# on the bid asks the AI about every task, in the background, one task at a time, each
# committed on its own so the work done stays done if a later task fails.

_bid_lock = threading.Lock()
_bid_runs: dict[uuid.UUID, dict] = {}


def bid_check_status(bid_id: uuid.UUID, org_id: uuid.UUID) -> dict:
    """What the bid-wide check is doing now, or how the last one ended."""
    with _bid_lock:
        run = _bid_runs.get(bid_id)
        if run is None or run["org_id"] != org_id:
            return {"running": False, "total": 0, "done": 0, "failed": 0, "current": None}
        return {k: v for k, v in run.items() if k != "org_id"}


def run_bid_check(
    bid_id: uuid.UUID, ctx: ExecutionContext, plan: list, *, factory, gateway
) -> None:
    """Check each task in its own transaction; a task that fails does not stop the others."""
    from app.db.session import apply_tenant_context

    for task_id, code in plan:
        with _bid_lock:
            _bid_runs[bid_id]["current"] = code
        worker = factory()
        try:
            worker.begin()
            apply_tenant_context(worker, ctx)
            check(worker, ctx, task_id=task_id, gateway=gateway, force=True)
            worker.commit()
            ok = True
        except Exception as exc:  # noqa: BLE001
            worker.rollback()
            log.info("bid_gap_check_task_failed", task=code, error=str(exc)[:200])
            ok = False
        finally:
            worker.close()
        with _bid_lock:
            _bid_runs[bid_id]["done" if ok else "failed"] += 1
    with _bid_lock:
        _bid_runs[bid_id].update(
            running=False, current=None, finished_at=datetime.now(timezone.utc).isoformat()
        )


def start_bid_check(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID, gateway_factory=None
) -> dict:
    """Start checking every task of the bid; False if one is already running."""
    from app.db.session import get_session_factory
    from app.gateway.gateway import build_gateway

    tasks = task_service.list_tasks(session, ctx, bid_id=bid_id)
    if not tasks:
        return {"started": False, "reason": "This bid has no tasks yet."}
    with _bid_lock:
        current = _bid_runs.get(bid_id)
        if current is not None and current["running"]:
            return {"started": False, "reason": "A check is already running."}
        _bid_runs[bid_id] = {
            "org_id": ctx.org_id,
            "running": True,
            "total": len(tasks),
            "done": 0,
            "failed": 0,
            "current": None,
            "finished_at": None,
        }
    plan = [(t.id, t.code) for t in tasks]
    session.commit()  # the worker reads committed rows

    def work() -> None:
        try:
            run_bid_check(
                bid_id,
                ctx,
                plan,
                factory=get_session_factory(),
                gateway=(gateway_factory or build_gateway)(),
            )
        except Exception as exc:  # noqa: BLE001 - never leave the bid looking busy forever
            log.warning("bid_gap_check_crashed", error=str(exc)[:200])
            with _bid_lock:
                _bid_runs[bid_id].update(running=False, current=None)

    threading.Thread(target=work, daemon=True).start()
    return {"started": True, "total": len(plan)}
