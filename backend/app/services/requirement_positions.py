"""What to say about a requirement the company holds no evidence for.

The system, not the model, decides how much evidence exists. The model then offers a
few honest ways to handle the requirement; the person picks one (or says what they
would rather say), the model words it formally, and the person submits it. Or the
person sets the requirement aside. Nothing is decided by the model, and every
suggestion and every decision stays on the row, so it can be traced later.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.core.logging import get_logger
from app.gateway.registry import get as get_call_type
from app.models import RequirementAssessment, RequirementPosition, User
from app.services import audit, context_builder
from app.services import focus as focus_service
from app.services import tasks as task_service

log = get_logger("requirement_positions")

_FULL = {"CURRENT_VERIFIED", "HISTORICAL_VERIFIED"}
_ACTIVE = ("submitted", "ignored")


def _level(status: str | None) -> str:
    if status in _FULL:
        return "full"
    if status == "PARTIAL":
        return "partial"
    return "none"


def _rows(
    session: Session, ctx: ExecutionContext, task_id: uuid.UUID, ids: list[uuid.UUID]
) -> dict[uuid.UUID, RequirementPosition]:
    return {
        r.requirement_id: r
        for r in session.execute(
            select(RequirementPosition).where(
                RequirementPosition.org_id == ctx.org_id,
                RequirementPosition.task_id == task_id,
                RequirementPosition.requirement_id.in_(ids),
            )
        ).scalars()
    }


def _chosen(session, ctx, task_id, ids) -> list:
    on_task = {r.id: r for r in task_service.task_requirements(session, ctx, task_id=task_id)}
    if not ids:
        raise ValidationError("Pick at least one requirement.")
    if any(i not in on_task for i in ids):
        raise ValidationError("One of those requirements is not part of this task.")
    return [on_task[i] for i in dict.fromkeys(ids)]


def evidence_levels(
    session: Session, ctx: ExecutionContext, ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    found = {
        a.requirement_id: _level(a.evidence_status.value)
        for a in session.execute(
            select(RequirementAssessment).where(
                RequirementAssessment.org_id == ctx.org_id,
                RequirementAssessment.is_current.is_(True),
                RequirementAssessment.requirement_id.in_(ids),
            )
        ).scalars()
    }
    return {i: found.get(i, "none") for i in ids}


def propose(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    requirement_ids: list[uuid.UUID],
    gateway,
    embedder=None,
) -> dict:
    """Say what was found, and offer ways to handle the requirements. Decides nothing."""
    from app.gateway.registry import wrap_untrusted
    from app.schemas.requirement_positions import PositionOptions

    chosen = _chosen(session, ctx, task_id, requirement_ids)
    ids = [r.id for r in chosen]
    levels = evidence_levels(session, ctx, ids)
    focus = focus_service.requirements_focus(session, ctx, task_id, ids)
    built = context_builder.build(
        session, ctx, task_id=task_id, query=focus.hint[:600], embedder=embedder
    )
    overall = (
        "full"
        if all(v == "full" for v in levels.values())
        else "none"
        if all(v == "none" for v in levels.values())
        else "partial"
    )
    per = [{"code": r.code, "evidence_level": levels[r.id]} for r in chosen]
    prompt = (
        built.prompt_block()
        + "\n\n"
        + focus.block
        + "\n\nEVIDENCE LEVEL (decided by the system, not by you)\n"
        + wrap_untrusted(json.dumps(per))
    )
    summary, options, error = "", [], None
    try:
        result = gateway.generate_structured(
            "requirement_options", prompt, ctx=ctx, session=session
        )
        if not isinstance(result, PositionOptions):
            raise ValueError("unexpected result")
        summary = result.summary.strip()
        options = [o.model_dump() for o in result.options]
    except Exception:  # noqa: BLE001
        log.info("requirement_options_unavailable", task_id=str(task_id))
        error = "The AI could not suggest wording just now."
    if overall == "none":
        # With nothing approved behind it, anything offered is a promise.
        for o in options:
            o["is_commitment"] = True

    stamp = f"llm:requirement_options@{get_call_type('requirement_options').prompt_version}"
    group = uuid.uuid4()
    rows = _rows(session, ctx, task_id, ids)
    for r in chosen:
        row = rows.get(r.id)
        if row is None:
            row = RequirementPosition(
                org_id=ctx.org_id, task_id=task_id, requirement_id=r.id, status="proposed"
            )
            session.add(row)
        if row.status in ("proposed", "restored"):
            row.status = "proposed"
            row.group_id = group
        row.proposed = {"summary": summary, "options": options, "levels": per}
        row.proposed_by = stamp
    session.flush()
    return {
        "found": overall,
        "per_requirement": per,
        "summary": summary,
        "options": options,
        "error": error,
        "codes": [r.code for r in chosen],
    }


def formalise(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    requirement_ids: list[uuid.UUID],
    option: int | None,
    own_words: str | None,
    instruction: str | None,
    gateway,
) -> dict:
    """The chosen position (or the person's own idea) in the formal voice of a response."""
    from app.gateway.registry import wrap_untrusted
    from app.schemas.requirement_positions import FormalAnswer

    chosen = _chosen(session, ctx, task_id, requirement_ids)
    ids = [r.id for r in chosen]
    position = (own_words or "").strip()
    if not position:
        row = next(iter(_rows(session, ctx, task_id, ids).values()), None)
        options = ((row.proposed or {}).get("options") if row else None) or []
        if option is None or not (0 <= option < len(options)):
            raise ValidationError("Pick one of the suggestions, or say what you would like to say.")
        position = options[option]["text"]
    payload = {
        "requirements": [
            {"code": r.code, "text": t[:500]}
            for r, t in zip(chosen, _texts(session, ctx, chosen), strict=True)
        ],
        "position": position,
        "instruction": (instruction or "").strip() or None,
    }
    try:
        result = gateway.generate_structured(
            "requirement_formal_answer",
            wrap_untrusted(json.dumps(payload, ensure_ascii=False)),
            ctx=ctx,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001
        raise ValidationError("The AI could not word that just now. Try again.") from exc
    if not isinstance(result, FormalAnswer):
        raise ValidationError("The AI could not word that just now. Try again.")
    return {"text": result.text.strip(), "from_option": option if not own_words else None}


def _texts(session, ctx, chosen) -> list[str]:
    from app.models import RequirementVersion

    out = []
    for r in chosen:
        version = (
            session.get(RequirementVersion, r.current_version_id) if r.current_version_id else None
        )
        out.append(version.requirement_text if version else "")
    return out


def _decide(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    requirement_ids: list[uuid.UUID],
    status: str,
    text: str | None,
    source: str | None,
    reason: str | None,
    option: int | None,
) -> list[RequirementPosition]:
    chosen = _chosen(session, ctx, task_id, requirement_ids)
    task = task_service.get_task(session, ctx, task_id=task_id)
    ids = [r.id for r in chosen]
    rows = _rows(session, ctx, task_id, ids)
    group = uuid.uuid4()
    now = datetime.now(timezone.utc)
    out = []
    for r in chosen:
        row = rows.get(r.id)
        before = row.status if row else None
        if row is None:
            row = RequirementPosition(org_id=ctx.org_id, task_id=task_id, requirement_id=r.id)
            session.add(row)
        row.status = status
        row.group_id = group
        row.text = text
        row.source = source
        row.reason = (reason or None) if status == "ignored" else None
        row.chosen_option = option
        row.decided_by = ctx.actor_user_id
        row.decided_at = now
        session.flush()
        audit.record(
            session,
            ctx,
            action=f"requirement.position_{status}",
            entity_type="requirement_position",
            entity_id=row.id,
            bid_id=task.bid_id,
            before={"status": before},
            after={"status": status, "requirement": r.code},
            reason=reason,
        )
        out.append(row)
    from app.services import todo

    todo.mark_reviewed(session, ctx, task_id=task_id, requirement_ids=ids)
    return out


def submit(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    requirement_ids: list[uuid.UUID],
    text: str,
    source: str = "person",
    option: int | None = None,
) -> list[RequirementPosition]:
    """A person approves this wording as the company's answer to the requirements."""
    if not (text or "").strip():
        raise ValidationError("An answer needs its wording.")
    if source not in ("person", "model"):
        raise ValidationError("An answer comes from a person or the AI's wording.")
    return _decide(
        session,
        ctx,
        task_id=task_id,
        requirement_ids=requirement_ids,
        status="submitted",
        text=text.strip(),
        source=source,
        reason=None,
        option=option,
    )


def ignore(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    requirement_ids: list[uuid.UUID],
    reason: str | None,
) -> list[RequirementPosition]:
    """A person sets the requirements aside. They leave the working list, and the draft."""
    return _decide(
        session,
        ctx,
        task_id=task_id,
        requirement_ids=requirement_ids,
        status="ignored",
        text=None,
        source=None,
        reason=reason,
        option=None,
    )


def restore(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID, requirement_id: uuid.UUID
) -> None:
    """Undo a submitted answer or a set-aside; the record of it stays."""
    row = session.execute(
        select(RequirementPosition).where(
            RequirementPosition.org_id == ctx.org_id,
            RequirementPosition.task_id == task_id,
            RequirementPosition.requirement_id == requirement_id,
        )
    ).scalar_one_or_none()
    if row is None or row.status not in _ACTIVE:
        raise NotFound("There is nothing to restore for that requirement.")
    task = task_service.get_task(session, ctx, task_id=task_id)
    before = row.status
    row.status = "restored"
    row.decided_by = ctx.actor_user_id
    row.decided_at = datetime.now(timezone.utc)
    session.flush()
    audit.record(
        session,
        ctx,
        action="requirement.position_restored",
        entity_type="requirement_position",
        entity_id=row.id,
        bid_id=task.bid_id,
        before={"status": before},
        after={"status": "restored"},
    )


def decisions(session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID) -> list[dict]:
    """Every submitted answer and set-aside on the task, with who decided and when."""
    from app.models import Requirement

    rows = session.execute(
        select(RequirementPosition, Requirement)
        .join(Requirement, Requirement.id == RequirementPosition.requirement_id)
        .where(
            RequirementPosition.org_id == ctx.org_id,
            RequirementPosition.task_id == task_id,
            RequirementPosition.status.in_(_ACTIVE),
        )
        .order_by(Requirement.code)
    ).all()
    people = {
        u.id: u.display_name
        for u in session.execute(select(User).where(User.org_id == ctx.org_id)).scalars()
    }
    texts = {
        r.id: t
        for r, t in zip(
            [r for _, r in rows], _texts(session, ctx, [r for _, r in rows]), strict=True
        )
    }
    return [
        {
            "requirement_id": str(p.requirement_id),
            "code": r.code,
            "text": texts.get(r.id, "")[:300],
            "status": p.status,
            "answer": p.text,
            "source": p.source,
            "reason": p.reason,
            "decided_by": people.get(p.decided_by),
            "decided_at": p.decided_at.isoformat() if p.decided_at else None,
            "ai_suggested": [o["label"] for o in ((p.proposed or {}).get("options") or [])],
            "ai_prompt": p.proposed_by,
        }
        for p, r in rows
    ]


def active_for(
    session: Session, ctx: ExecutionContext, task_id: uuid.UUID
) -> dict[uuid.UUID, RequirementPosition]:
    """The submitted and ignored rows of a task, by requirement."""
    return {
        p.requirement_id: p
        for p in session.execute(
            select(RequirementPosition).where(
                RequirementPosition.org_id == ctx.org_id,
                RequirementPosition.task_id == task_id,
                RequirementPosition.status.in_(_ACTIVE),
            )
        ).scalars()
    }
