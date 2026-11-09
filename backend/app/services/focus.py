"""What the person is talking about, resolved on the server.

The chat used to work out which requirement or which part of the draft a question
meant from the conversation alone, and a model reading a long mixed thread guesses.
Now the screen says it: the requirements ticked, or the section clicked. This module
checks that claim against the task and builds the material the model is given about
it -- fresh, from the database, every time -- plus a key that names the thread, so
the turns about one thing are never shown to the model while it answers about another.

Nothing here is trusted from the client except the ids, and every id is checked
against the task.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import ValidationError
from app.models import (
    ClarificationIssue,
    ClarificationRequirement,
    CrossDepartmentRequest,
    Department,
    RequirementAssessment,
    RequirementRequest,
    RequirementVersion,
    TaskGap,
)
from app.models.enums import ClarificationStatus
from app.services import tasks as task_service

MAX_REQUIREMENTS = 12


@dataclass
class Focus:
    key: str
    kind: str  # "requirements" | "section" | "gaps"
    label: str
    block: str  # what the model is told about it
    hint: str  # words to retrieve evidence with
    data: dict = field(default_factory=dict)  # what is stored on the message
    requirement_ids: list[uuid.UUID] = field(default_factory=list)


def _texts(session: Session, ctx: ExecutionContext, requirements: list) -> dict[uuid.UUID, str]:
    ids = [r.current_version_id for r in requirements if r.current_version_id]
    if not ids:
        return {}
    by_version = {
        v.id: v.requirement_text
        for v in session.execute(
            select(RequirementVersion).where(
                RequirementVersion.org_id == ctx.org_id, RequirementVersion.id.in_(ids)
            )
        ).scalars()
    }
    return {
        r.id: by_version[r.current_version_id]
        for r in requirements
        if r.current_version_id in by_version
    }


def _gap_lines(
    session: Session,
    ctx: ExecutionContext,
    task_id: uuid.UUID,
    requirement_ids: list[uuid.UUID] | None,
) -> list[str]:
    """The gaps in the tender that bear on this focus, with what was decided about each."""
    query = (
        select(ClarificationIssue)
        .join(TaskGap, TaskGap.clarification_id == ClarificationIssue.id)
        .where(
            TaskGap.org_id == ctx.org_id,
            TaskGap.task_id == task_id,
            TaskGap.attached.is_(True),
            ClarificationIssue.status.in_(
                [
                    ClarificationStatus.OPEN,
                    ClarificationStatus.SENT_TO_CLIENT,
                    ClarificationStatus.ANSWERED,
                ]
            ),
        )
    )
    if requirement_ids:
        # A gap is shown against a requirement only when the tender ties them
        # together; a task-wide gap is shown with the section view instead.
        linked = select(ClarificationRequirement.clarification_id).where(
            ClarificationRequirement.org_id == ctx.org_id,
            ClarificationRequirement.requirement_id.in_(requirement_ids),
        )
        query = query.where(ClarificationIssue.id.in_(linked))
    lines = []
    for issue in session.execute(query).scalars():
        if issue.status is ClarificationStatus.ANSWERED and issue.client_response:
            used = (
                "the response uses it" if issue.include_in_response else "not used in the response"
            )
            lines.append(
                f"- RFP gap: {issue.description[:300]} -> OUR ASSUMPTION ({used}): "
                f"{issue.client_response[:400]}"
            )
        else:
            lines.append(f"- RFP gap, no assumption recorded yet: {issue.description[:300]}")
    return lines


def requirements_focus(
    session: Session, ctx: ExecutionContext, task_id: uuid.UUID, ids: list[uuid.UUID]
) -> Focus:
    if not ids:
        raise ValidationError("Pick at least one requirement.")
    on_task = {r.id: r for r in task_service.task_requirements(session, ctx, task_id=task_id)}
    unknown = [i for i in ids if i not in on_task]
    if unknown:
        raise ValidationError("One of those requirements is not part of this task.")
    chosen = sorted((on_task[i] for i in dict.fromkeys(ids)), key=lambda r: r.code)
    if len(chosen) > MAX_REQUIREMENTS:
        raise ValidationError(f"Pick at most {MAX_REQUIREMENTS} requirements at a time.")
    texts = _texts(session, ctx, chosen)
    assessed = {
        a.requirement_id: a
        for a in session.execute(
            select(RequirementAssessment).where(
                RequirementAssessment.org_id == ctx.org_id,
                RequirementAssessment.is_current.is_(True),
                RequirementAssessment.requirement_id.in_([r.id for r in chosen]),
            )
        ).scalars()
    }
    departments = {
        d.id: d.name
        for d in session.execute(
            select(Department).where(Department.org_id == ctx.org_id)
        ).scalars()
    }
    asked: dict[uuid.UUID, list[CrossDepartmentRequest]] = {}
    for req_id, request in session.execute(
        select(RequirementRequest.requirement_id, CrossDepartmentRequest)
        .join(CrossDepartmentRequest, CrossDepartmentRequest.id == RequirementRequest.request_id)
        .where(
            RequirementRequest.org_id == ctx.org_id,
            RequirementRequest.requirement_id.in_([r.id for r in chosen]),
            CrossDepartmentRequest.origin_task_id == task_id,
        )
    ).all():
        asked.setdefault(req_id, []).append(request)

    from app.services import requirement_positions

    decisions = requirement_positions.active_for(session, ctx, task_id)
    parts = []
    for r in chosen:
        lines = [f"[{r.code}] {texts.get(r.id, '')[:600]}"]
        a = assessed.get(r.id)
        if a is not None:
            lines.append(
                f"  Company evidence: {a.evidence_status.value}; readiness: "
                f"{a.bid_readiness.value}. {a.rationale[:300]}"
            )
        else:
            lines.append("  Company evidence: not assessed yet.")
        decision = decisions.get(r.id)
        if decision is not None and decision.status == "submitted":
            lines.append(f"  The company's answer (approved by a person): {decision.text}")
        elif decision is not None and decision.status == "ignored":
            lines.append("  A person set this requirement aside; it is not being answered.")
        for request in asked.get(r.id, []):
            where = departments.get(request.target_department_id, "a department")
            if request.resolution_text:
                lines.append(
                    f"  Asked {where}; their written reply: {request.resolution_text[:400]}"
                )
            else:
                lines.append(f"  Asked {where}: {request.state.value.lower()}, no reply yet.")
        parts.append("\n".join(lines))
    gaps = _gap_lines(session, ctx, task_id, [r.id for r in chosen])
    codes = [r.code for r in chosen]
    block = (
        "FOCUS: the person has picked these requirements and is talking about ONLY them. "
        "'this', 'these' and 'it' mean them.\n" + "\n".join(parts)
    )
    if gaps:
        block += "\nRFP GAPS THAT BEAR ON THEM\n" + "\n".join(gaps)
    return Focus(
        key="req:" + ",".join(str(i) for i in sorted(r.id for r in chosen)),
        kind="requirements",
        label="Requirements " + ", ".join(codes),
        block=block,
        hint=" ".join(texts.get(r.id, "")[:300] for r in chosen),
        data={"kind": "requirements", "codes": codes},
        requirement_ids=[r.id for r in chosen],
    )


def section_focus(
    session: Session, ctx: ExecutionContext, task_id: uuid.UUID, section: str
) -> Focus:
    from app.services import writeup

    _, _, rows = writeup._current_rows(session, ctx, task_id)
    mine = [r for r in rows if (r.section or "") == (section or "")]
    if not section or not mine:
        raise ValidationError("That section is not in the draft any more.")
    text = "\n".join(r.text for r in mine)
    codes = sorted({c for r in mine for c in (r.requirement_codes or [])})
    on_task = task_service.task_requirements(session, ctx, task_id=task_id)
    covered = [r for r in on_task if r.code in codes]
    texts = _texts(session, ctx, covered)
    req_lines = [f"[{r.code}] {texts.get(r.id, '')[:400]}" for r in covered]
    diagram_ids = {r.diagram_id for r in mine if r.diagram_id}
    block = (
        "FOCUS: the person has opened this section of the response draft and is talking "
        f"about ONLY it. 'this', 'it' and 'the section' mean it.\nSECTION: {section}\n"
        f"ITS CURRENT TEXT:\n{text[:6000]}"
    )
    if diagram_ids:
        block += f"\nIt explains {len(diagram_ids)} diagram(s) on this task."
    if req_lines:
        block += "\nREQUIREMENTS THIS SECTION ANSWERS\n" + "\n".join(req_lines)
    gaps = _gap_lines(session, ctx, task_id, [r.id for r in covered] or None)
    if gaps:
        block += "\nRFP GAPS THAT BEAR ON IT\n" + "\n".join(gaps)
    return Focus(
        key="sec:" + section[:280],
        kind="section",
        label=f"Section: {section}",
        block=block,
        hint=(section + " " + text[:500]),
        data={"kind": "section", "section": section},
        requirement_ids=[r.id for r in covered],
    )


def gaps_focus(session: Session, ctx: ExecutionContext, task_id: uuid.UUID) -> Focus:
    """Everything the tender leaves open for this task, and what was decided about each."""
    from app.services import task_gaps

    items = task_gaps.list_for_task(session, ctx, task_id=task_id)["items"]
    lines = []
    for n, g in enumerate(items, 1):
        line = f"{n}. MISSING: {g['missing']}"
        if g.get("why_needed"):
            line += f" | WHY WE NEED IT: {g['why_needed']}"
        if g["status"] == "ANSWERED" and g.get("assumption"):
            used = (
                "used in the response" if g["include_in_response"] else "not used in the response"
            )
            line += f" | OUR ASSUMPTION ({used}): {g['assumption'][:400]}"
        else:
            line += " | no assumption recorded yet"
        for r in g["requests"]:
            reply = f"replied: {r['reply'][:300]}" if r.get("reply") else "no reply yet"
            line += f" | ASKED {r['department'] or 'a department'} ({reply})"
        lines.append(line)
    block = (
        "FOCUS: the person is working on the parts the tender leaves out (RFP missing "
        "parts) for this task, and is talking ONLY about them. Help them decide what the "
        "response should assume for each, or whom to ask. Never invent what the client "
        "intends; an assumption is ours and must be said as ours.\n"
        + (
            "MISSING PARTS\n" + "\n".join(lines)
            if lines
            else "No missing parts are attached to this task."
        )
    )
    return Focus(
        key="gaps",
        kind="gaps",
        label="RFP missing parts",
        block=block,
        hint=" ".join(g["missing"][:200] for g in items),
        data={"kind": "gaps"},
    )


def resolve(
    session: Session, ctx: ExecutionContext, task_id: uuid.UUID, raw: dict | None
) -> Focus | None:
    """The focus a message names, checked against the task; None for the task as a whole."""
    if not raw or raw.get("kind") in (None, "", "task"):
        return None
    kind = raw.get("kind")
    if kind == "requirements":
        try:
            ids = [uuid.UUID(str(i)) for i in raw.get("requirement_ids") or []]
        except ValueError as exc:
            raise ValidationError("That is not a requirement.") from exc
        return requirements_focus(session, ctx, task_id, ids)
    if kind == "section":
        return section_focus(session, ctx, task_id, str(raw.get("section") or ""))
    if kind == "gaps":
        return gaps_focus(session, ctx, task_id)
    raise ValidationError(
        "The chat can be about the task, some requirements, a section, or the missing parts."
    )
