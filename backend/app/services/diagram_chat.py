"""Diagrams from the task chat: one conversation, not a second place to type.

Every message first goes past a router that decides whether it is about diagrams and, if
so, lays out the moves (draw, then restyle; review; drop a type ...). Anything that is not
about diagrams is answered by the copilot as before.

A person writes to the assistant as usual -- "draw the process flow", "add a retest
step", "make it black and white for print", "what would you improve?", "we don't
need a network diagram here". A model only labels which of those the message is.
Everything after that is the platform: whether the person may, what is drawn,
what changed, what could not be done, and what is said back. The reply is written
here from the result, not by a model, so it can only say what actually happened.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, PermissionDenied, StateConflict, ValidationError
from app.core.logging import get_logger
from app.core.permissions import Perm
from app.models.enums import DiagramType
from app.schemas.diagrams import DiagramIntentResult
from app.services import artifacts, writeup
from app.services import diagrams as dg

log = get_logger("diagram_chat")

# Cheap gate before a model is asked: most questions have nothing to do with pictures.
_WRITES = {
    "CREATE",
    "CHANGE",
    "RESTYLE",
    "REVIEW",
    "APPLY_SUGGESTIONS",
    "NOT_NEEDED",
    "RECORD_QUESTIONS",
    "REFRESH",
    "WHICH_NEEDED",
    "WRITE_UP",
    "WRITE_REQUIREMENTS",
    "LEAVE_OUT",
    "INCLUDE",
    "REMOVE",
}
_NAMES = {
    "FLOW": "process flow",
    "ARCHITECTURE": "solution architecture",
    "CUSTOMER_CONTEXT": "customer-side diagram",
    "DATA_FLOW": "data flow",
    "NETWORK": "network diagram",
    "ORG_CHART": "project team chart",
    "SEQUENCE": "sequence diagram",
}


def _name(kind: str | None) -> str:
    return _NAMES.get(kind or "", (kind or "diagram").replace("_", " ").lower())


def _reply(content: str, **diagram) -> dict:
    return {"content": content, "trace": {"outcome": "diagram", "diagram": diagram}}


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"- {line}" for line in lines)


_MERMAID = re.compile(
    r"^\s*(?:```(?:mermaid)?\s*)?(flowchart|graph|sequenceDiagram)\b", re.IGNORECASE | re.MULTILINE
)


def handle(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id,
    question: str,
    last_assistant,
    gateway,
    recent: list[dict] | None = None,
    can_write: bool | None = None,
    focus: dict | None = None,
) -> dict | None:
    """The reply to give when the message is about diagrams, else None.

    `can_write` is decided where the request arrived, because the worker that answers
    a chat message does not carry the caller's permissions.
    """
    if can_write is None:
        can_write = ctx.actor_kind == "SYSTEM" or ctx.has_permission(Perm.ARTIFACT_WRITE.value)

    # Pasted Mermaid is imported exactly; no model reads it.
    if _MERMAID.search(question or "") and can_write:
        try:
            text = re.sub(r"```(?:mermaid)?", "", question).strip()
            diagram = dg.import_mermaid(session, ctx, task_id=task_id, text=text)
        except (PermissionDenied, ValidationError, NotFound) as exc:
            return _reply(str(exc), error=True)
        version = dg.current_version(session, ctx, diagram)
        return _reply(
            f"Imported as “{diagram.title}” (version 1). Ask me to change it, restyle it or "
            "review it.",
            action="CREATE",
            diagram_id=str(diagram.id),
            version_no=version.version_no,
        )

    rows = dg.diagrams_for_task(session, ctx, task_id=task_id)
    refs = {f"D{i}": d for i, d in enumerate(rows, start=1)}
    current = {ref: dg.current_version(session, ctx, d) for ref, d in refs.items()}
    last_review = []
    for ref, v in current.items():
        notes = [n for n in (v.review_notes or []) if "text" in n]
        if notes:
            last_review.append(
                {
                    "diagram": ref,
                    "suggestions": [
                        {
                            "number": i,
                            "text": n["text"],
                            "needs_information": n["needs_information"],
                        }
                        for i, n in enumerate(notes, start=1)
                    ],
                }
            )
    prior = getattr(last_assistant, "retrieval_trace", None) or {}
    gathered = dg.gather(session, ctx, task_id=task_id)
    payload = {
        "message": question,
        "recent_conversation": recent or [],
        # What the person has picked on screen, when they have: "this", "these" and "it"
        # mean it.
        "person_is_looking_at": focus,
        "requirements": [
            {"code": v["code"], "text": v["text"][:200]} for v in gathered.requirements.values()
        ],
        "diagrams": [
            {
                "ref": r,
                "type": d.diagram_type,
                "title": d.title,
                "version": current[r].version_no,
                "parts": len((current[r].spec or {}).get("nodes", [])),
                "left_out_of_the_response": d.left_out,
            }
            for r, d in refs.items()
        ],
        "last_review": last_review,
        "previous_reply_left_missing_information": bool(
            (prior.get("diagram") or {}).get("missing")
        ),
    }
    try:
        plan = dg._ask(
            gateway,
            "diagram_intent",
            payload,
            ctx=ctx,
            session=session,
            expect=DiagramIntentResult,
        )
    except Exception:  # noqa: BLE001 - a failed router must never block an ordinary answer
        log.warning("diagram_router_failed")
        return None
    if not plan.wants_diagram or not plan.steps:
        return None
    if not can_write:
        return _reply(
            "You can look at this task's diagrams but not change them. "
            "Ask the bid manager, or someone on the task with edit rights."
        )

    texts: list[str] = []
    shown: list[dict] = []
    state = {"refs": refs, "current": current, "last": None}
    for step in plan.steps:
        try:
            text, info = _do(
                session, ctx, task_id=task_id, step=step, state=state, prior=prior, gateway=gateway
            )
        except (PermissionDenied, ValidationError, NotFound) as exc:
            texts.append(str(exc))
            break
        except StateConflict:
            texts.append(
                "Someone changed that diagram a moment ago. Look at the latest and ask again."
            )
            break
        texts.append(text)
        if info is not None:
            shown.append(info)
            state["last"] = info.get("diagram_id")
            _reload(session, ctx, task_id, state)
        if info is not None and info.get("failed"):
            break
    trace: dict[str, Any] = {
        "outcome": "diagram",
        "diagrams": [d for d in shown if d.get("diagram_id")],
    }
    if state.get("writeup"):
        trace["writeup"] = state["writeup"]
    if shown:
        trace["diagram"] = shown[-1]
    stale = _stale_note(session, ctx, state["refs"]) if shown else ""
    return {"content": "\n\n".join(t for t in texts if t) + stale, "trace": trace}


def _reload(session, ctx, task_id, state) -> None:
    rows = dg.diagrams_for_task(session, ctx, task_id=task_id)
    state["refs"] = {f"D{i}": d for i, d in enumerate(rows, start=1)}
    state["current"] = {r: dg.current_version(session, ctx, d) for r, d in state["refs"].items()}


def _pick(step, state):
    """Which diagram is meant, or a question to ask."""
    refs = state["refs"]
    if step.target == "LAST" and state["last"]:
        for d in refs.values():
            if str(d.id) == state["last"]:
                return d, None
    if step.target and step.target in refs:
        return refs[step.target], None
    if len(refs) == 1:
        return next(iter(refs.values())), None
    if not refs:
        return None, "No diagram on this task yet. Say which to draw, e.g. “draw the process flow”."
    listing = _bullets([_describe_one(r, d, state) for r, d in refs.items()])
    return None, f"Which diagram do you mean?\n{listing}"


def _describe_one(ref, diagram, state) -> str:
    """Enough to tell two diagrams with the same title apart."""
    version = state["current"][ref]
    parts = len((version.spec or {}).get("nodes", []))
    return (
        f"{ref}: {diagram.title} ({_name(diagram.diagram_type)}, version "
        f"{version.version_no}, {parts} parts"
        + (", left out of the response" if diagram.left_out else "")
        + ")"
    )


def _stale_note(session, ctx, refs) -> str:
    notes = []
    for d in refs.values():
        state = dg.staleness(session, ctx, d)
        if state["stale"]:
            codes = ", ".join(c["code"] for c in state["changed"])
            notes.append(f"“{d.title}” is out of date: {codes} changed since it was made")
    if not notes:
        return ""
    return "\n\n" + "; ".join(notes) + ". Say “update it” and I'll bring it up to date."


def _dense_hint(version) -> str:
    """Say so when a diagram is still crowded, and what to do about it."""
    from app.services import diagram_spec as ds

    report = ds.readability(ds.parse_spec(version.spec))
    if not report["issues"]:
        return ""
    return (
        f" It is still fairly dense ({report['nodes']} boxes). Say “simplify it”, or ask for "
        "part of it as a separate diagram."
    )


def _named(step, state) -> list:
    """The diagrams the person named in one step, in the order they named them."""
    refs = state["refs"]
    names = [*step.targets, *([step.target] if step.target else [])]
    out = []
    for name in names:
        if name == "LAST" and state["last"]:
            found = [d for d in refs.values() if str(d.id) == state["last"]]
        else:
            found = [refs[name]] if name in refs else []
        for d in found:
            if d not in out:
                out.append(d)
    if not out and step.diagram_type in {t.value for t in DiagramType}:
        out = [d for d in refs.values() if d.diagram_type == step.diagram_type]
    return out


def _listing(state, only=None) -> str:
    return _bullets(
        [_describe_one(r, d, state) for r, d in state["refs"].items() if only is None or d in only]
    )


def _do_writeup(session, ctx, *, task_id, step, state) -> tuple[str, dict | None]:
    """Write up some diagrams and the requirements, or decide which diagrams are in.

    Which diagrams appear in the written response is the person's call: naming them picks
    them, "all" picks everything not left out, and saying nothing when there is a choice
    to make gets a question and no work, because a section written for a duplicate or a
    sketch is a section someone has to delete.
    """
    kind = step.action
    refs = state["refs"]
    stop = {"action": kind, "failed": True}
    named = _named(step, state)

    if kind == "LEAVE_OUT":
        if not named:
            return (
                "Which diagram should I leave out of the written response? I won't guess.\n"
                + _listing(state)
            ), stop
        artifact = None
        try:
            artifact = artifacts.for_task(session, ctx, task_id=task_id)
        except NotFound:
            pass
        dropped = 0
        for d in named:
            dg.set_left_out(session, ctx, diagram_id=d.id, left_out=True)
            if artifact is not None:
                dropped += artifacts.drop_diagram_section(
                    session, ctx, artifact_id=artifact.id, diagram_id=d.id
                )
        titles = ", ".join(f"“{d.title}”" for d in named)
        text = f"Left out of the written response: {titles}. It stays on the task."
        if dropped:
            text += " Its section is gone from the Response draft."
        return text + " Say “include it” to put it back.", None

    if writeup.is_running(task_id):
        return (
            "I'm still writing the last one up. Its sections are appearing in the "
            "Response draft; ask again when it finishes."
        ), None

    requirements = kind == "WRITE_REQUIREMENTS"
    if kind == "INCLUDE" and not named:
        left = [d for d in refs.values() if d.left_out]
        if not left:
            return "Nothing is left out of the written response.", None
        return (
            "Which one should I put back in? I won't guess.\n" + _listing(state, only=left)
        ), stop

    if kind in ("WRITE_UP", "INCLUDE"):
        candidates = [d for d in refs.values() if not d.left_out]
        if named:
            chosen = named
        elif step.all_diagrams or len(candidates) == 1:
            chosen = candidates
            requirements = True
        elif not candidates:
            chosen = []
            requirements = True
        else:
            return (
                f"This task has {len(candidates)} diagrams. Which should I write up in the "
                "response?\n" + _listing(state, only=candidates) + "\n\nSay the ones you want, "
                "for example “D1 and D3”, or “all of them”. Any you don't want in the response "
                "can be left out; they stay on the task. Either way I'll also answer the "
                "tender's requirements in their own section."
            ), stop
        for d in chosen:
            if d.left_out:
                dg.set_left_out(session, ctx, diagram_id=d.id, left_out=False)
    else:
        chosen = []

    plan = writeup.plan_for(
        session,
        ctx,
        task_id=task_id,
        diagrams=chosen,
        instruction=step.instruction,
        requirements=requirements,
    )
    earlier = state.get("writeup")
    if earlier:
        plan = writeup.Plan.from_dict(earlier).merged(plan)
    state["writeup"] = plan.as_dict()
    names = [f"“{t}”" for t in plan.titles]
    if plan.requirements:
        names.append(f"“{writeup.REQUIREMENTS_TITLE}”")
    again = (
        " I'll rewrite only those sections and leave the rest as they are."
        if step.instruction
        else ""
    )
    return (
        f"Writing this up in the Response draft: {', '.join(names)}. Each section appears "
        "there as soon as it is written, with every sentence marked as a fact, a commitment "
        "or an assumption, so you can read it as it comes. Requirements that are blocked, "
        "need the customer to clarify them, or have not been assessed are answered with a "
        "condition and not a promise, and the draft shows which ones still have no answer. "
        "Tell me what to change in any part and I'll redo just that part." + again
    ), None


def _info(action, diagram, version_no, **extra) -> dict:
    return {"action": action, "diagram_id": str(diagram.id), "version_no": version_no, **extra}


def _do(session, ctx, *, task_id, step, state, prior, gateway) -> tuple[str, dict | None]:
    kind = step.action
    refs, current = state["refs"], state["current"]

    if kind == "WHICH_NEEDED":
        needs = dg.plan(session, ctx, task_id=task_id, gateway=gateway)
        if not needs:
            return "I don't think this task needs a diagram.", None
        lines = []
        for n in needs:
            status = (
                "not needed (you said so)"
                if n.status == "DECLINED"
                else (
                    "enough to draw now" if n.enough_information else "not enough information yet"
                )
            )
            lines.append(f"{_name(n.diagram_type).capitalize()} — {n.why or ''} ({status})")
        return (
            "For this task I would suggest:\n"
            + _bullets(lines)
            + "\n\nSay “draw the process flow” to make one, or tell me one isn't needed."
        ), None

    if kind == "CREATE":
        chosen = step.diagram_type if step.diagram_type in {t.value for t in DiagramType} else None
        if chosen is None:
            return (
                "Which kind? I can draw: " + ", ".join(_name(t.value) for t in DiagramType) + "."
            ), {"action": kind, "failed": True}
        out = dg.create(
            session,
            ctx,
            task_id=task_id,
            diagram_type=chosen,
            note=step.instruction,
            gateway=gateway,
        )
        if not out["created"]:
            missing = out.get("missing") or []
            lines = []
            for m in missing:
                asked = (
                    f"already asked for by {', '.join(m['requirement_codes'])}"
                    if m.get("requirement_codes")
                    else "no requirement asks for this yet"
                )
                lines.append(f"{m['what']} — {m.get('why') or ''} ({asked})".replace(" —  (", " ("))
            return (
                f"I can't draw the {_name(chosen)} yet: there isn't enough information, and I "
                "won't invent it. What's missing:\n"
                + _bullets(lines)
                + "\n\nSay “record those as open questions” and I'll add them for the bid manager."
            ), {"action": kind, "created": False, "missing": missing, "failed": True}
        diagram = dg.get_diagram(session, ctx, out["diagram_id"])
        version = dg.current_version(session, ctx, diagram)
        text = f"Here is the {_name(chosen)}: “{diagram.title}”."
        if version.unconfirmed:
            text += (
                " Dashed parts rest on an assumption, because nothing in the tender or "
                "approved evidence backs them: "
                + "; ".join(u["label"] for u in version.unconfirmed)
                + "."
            )
        if out.get("detail"):
            text += (
                " I kept it at overview level so it stays readable. Detail I can draw "
                "separately: " + "; ".join(out["detail"]) + "."
            )
        return text + _dense_hint(version), _info(kind, diagram, version.version_no)

    if kind == "RECORD_QUESTIONS":
        missing = (prior.get("diagram") or {}).get("missing") or []
        if not missing:
            return "There's nothing waiting to be recorded.", None
        for m in missing:
            dg.raise_question(
                session,
                ctx,
                task_id=task_id,
                what=m["what"],
                why=m.get("why"),
                question=m.get("suggested_question") or f"Please provide: {m['what']}.",
                requirement_codes=m.get("requirement_codes") or [],
            )
        return f"Recorded {len(missing)} open question(s) for the bid manager.", None

    if kind == "NOT_NEEDED":
        if step.diagram_type not in {t.value for t in DiagramType}:
            return "Which kind of diagram isn't needed?", None
        reason = (step.reason or step.instruction or "").strip() or "Not needed for this task."
        dg.decide_need(
            session,
            ctx,
            task_id=task_id,
            diagram_type=step.diagram_type,
            accept=False,
            reason=reason,
        )
        return (
            f"Noted: no {_name(step.diagram_type)} for this task. I won't suggest it again "
            "unless the requirements behind it change."
        ), None

    if kind in ("WRITE_UP", "WRITE_REQUIREMENTS", "LEAVE_OUT", "INCLUDE"):
        return _do_writeup(session, ctx, task_id=task_id, step=step, state=state)

    diagram, ask_which = _pick(step, state)
    if diagram is None:
        return ask_which or "Which diagram?", {"action": kind, "failed": True}

    if kind == "REMOVE":
        if not step.target and len(refs) > 1:
            listing = _bullets([_describe_one(r, d, state) for r, d in refs.items()])
            return (f"Which one should I remove? I won't guess.\n{listing}"), {
                "action": kind,
                "failed": True,
            }
        dg.archive(session, ctx, diagram_id=diagram.id)
        removed = 0
        try:
            artifact = artifacts.for_task(session, ctx, task_id=task_id)
            removed = artifacts.drop_diagram_section(
                session, ctx, artifact_id=artifact.id, diagram_id=diagram.id
            )
        except NotFound:
            pass
        text = f"Removed “{diagram.title}” from this task."
        if removed:
            text += " Its section is gone from the Response draft too."
        return text + " Its earlier versions are kept in the history.", None
    ref = next(r for r, d in refs.items() if d.id == diagram.id)
    version = current[ref]
    base = version.version_no

    if kind == "SHOW":
        return f"Here is “{diagram.title}” (version {base}).", _info(kind, diagram, base)

    if kind == "REVIEW":
        notes = dg.review(
            session, ctx, diagram_id=diagram.id, focus=step.instruction, gateway=gateway
        )
        items = [n for n in notes if "text" in n]
        verdict = next((n["verdict"] for n in notes if "verdict" in n), "")
        if not items:
            return (
                f"I reviewed “{diagram.title}” and have nothing to add. {verdict}".strip()
            ), _info(kind, diagram, base)
        lines = [
            f"{i}. [{n['severity'].lower()}] {n['text']}"
            + (
                "  (needs information that isn't in the sources yet)"
                if n["needs_information"]
                else ""
            )
            for i, n in enumerate(items, start=1)
        ]
        return (
            f"{verdict}\n\n"
            + "\n".join(lines)
            + "\n\nI haven't changed anything. Say “apply 1 and 3” or “apply all”."
        ), _info(kind, diagram, base)

    if kind == "APPLY_SUGGESTIONS":
        notes = [n for n in (version.review_notes or []) if "text" in n]
        if step.all_suggestions:
            picks = [i for i, n in enumerate(notes) if not n["needs_information"]]
        else:
            picks = [n - 1 for n in step.suggestion_numbers]
        out = dg.apply_suggestions(
            session,
            ctx,
            diagram_id=diagram.id,
            suggestion_indexes=picks,
            base_version_no=base,
            gateway=gateway,
        )
        return _changed(session, ctx, diagram, out, "Applied")

    if kind == "CHANGE":
        out = dg.revise(
            session,
            ctx,
            diagram_id=diagram.id,
            instruction=step.instruction or "",
            base_version_no=base,
            gateway=gateway,
        )
        return _changed(session, ctx, diagram, out, "Done")

    if kind == "RESTYLE":
        out = dg.restyle(
            session,
            ctx,
            diagram_id=diagram.id,
            wish=step.instruction or "",
            base_version_no=base,
            gateway=gateway,
        )
        if not out["changed"]:
            why = (
                "; ".join(out.get("could_not") or [])
                or "the look can't be described by the styles I have"
            )
            return f"I couldn't restyle it: {why}.", _info(kind, diagram, base, failed=True)
        extra = (
            " I couldn't do: " + "; ".join(out["could_not"]) + "." if out.get("could_not") else ""
        )
        return (
            f"{out.get('understood') or 'Restyled.'} (version {out['version_no']}).{extra}"
        ), _info(kind, diagram, out["version_no"])

    if kind == "REFRESH":
        out = dg.refresh(session, ctx, diagram_id=diagram.id, base_version_no=base, gateway=gateway)
        return _changed(session, ctx, diagram, out, "Updated")

    return "I didn't catch what to do with the diagram.", None


def _changed(session, ctx, diagram, out, verb) -> tuple[str, dict]:
    if not out.get("changed"):
        reasons = [f"{c['request']} — {c['reason']}" for c in out.get("could_not") or []]
        missing = out.get("missing") or []
        text = "I didn't change anything."
        if reasons:
            text += " I couldn't do:\n" + _bullets(reasons)
        if missing:
            text += "\nI'd need: " + "; ".join(m["what"] for m in missing) + "."
        return text, _info("CHANGE", diagram, out["version_no"], missing=missing, failed=True)
    version = dg.version_by_number(
        session, ctx, diagram_id=diagram.id, version_no=out["version_no"]
    )
    lines = [ln for ln in (version.change_summary or "").split("\n") if ln][:10]
    text = f"{verb} — version {out['version_no']} of “{diagram.title}”."
    if lines:
        text += " What changed:\n" + _bullets(lines)
    if out.get("could_not"):
        text += "\nI couldn't do:\n" + _bullets(
            [f"{c['request']} — {c['reason']}" for c in out["could_not"]]
        )
    return text + _dense_hint(version), _info("CHANGE", diagram, out["version_no"])


def picture_reply(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id,
    image: bytes,
    use_vision: bool,
    can_write: bool,
    gateway=None,
) -> dict:
    """What to say back when a picture is attached to the chat."""
    from app.core.errors import ProviderError

    if not can_write:
        return _reply("You can look at this task's diagrams but not change them.")
    if not use_vision:
        return _reply(
            "Reading a picture sends it to a vision model outside the platform, so I need your "
            "go-ahead for this picture. Attach it again with the box ticked, or paste the diagram "
            "as Mermaid text and I'll read it here without sending anything out."
        )
    try:
        out = dg.import_image(
            session, ctx, task_id=task_id, image=image, use_vision=True, gateway=gateway
        )
    except (PermissionDenied, ValidationError, NotFound, ProviderError) as exc:
        return _reply(str(exc), error=True)
    if not out["created"]:
        return _reply(out.get("reason", "That picture doesn't look like a diagram."), error=True)
    diagram = dg.get_diagram(session, ctx, out["diagram_id"])
    text = (
        f"I read your picture into “{diagram.title}”. Everything on it is drawn as yours, since "
        "I haven't checked it against the tender."
    )
    if out.get("unclear"):
        text += " I couldn't read these with confidence: " + "; ".join(out["unclear"]) + "."
    text += " Ask me to change it, restyle it, or review it."
    info = {"action": "CREATE", "diagram_id": str(diagram.id), "version_no": out["version_no"]}
    return {"content": text, "trace": {"outcome": "diagram", "diagram": info, "diagrams": [info]}}
