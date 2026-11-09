"""Write-ups: the response draft explains the task's diagrams, one section at a time.

A person says, in the task chat, "write up the architecture and how we'll deliver it".
The router (see `diagram_chat`) turns that into a request; this module does it.

What the model is given, and what it is not:

* the diagram as data -- its boxes, links, lanes, and which parts are unconfirmed;
* the tender wording the task answers and the company's approved evidence, chosen
  by code, exactly as for a first draft (`drafting.gather_sources`);
* the team's chat on this task, as untrusted text;
* a one-line digest of the sections already written, so it does not repeat them.

What comes back is typed statements, and they pass through the same checks as a
first draft (`drafting.map_proposals`): a fact needs evidence that can prove it or it
is written as a commitment, a promise needs a named approver, a tender statement needs
its locator. On top of that, code adds what a model may forget: a section for a diagram
with unconfirmed parts always carries an assumption that lists them.

A section is committed the moment it is written, in its own transaction, so the
Response draft fills in while the rest is still being worked on and the person can
read each part, accept it or ask for it to be redone.
"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, StateConflict, ValidationError
from app.core.logging import get_logger
from app.db.session import apply_tenant_context
from app.gateway.gateway import ModelGateway
from app.gateway.registry import WRITEUP_MOST_PROMISES
from app.gateway.registry import get as get_call_type
from app.models import (
    Bid,
    ChatMessage,
    ClarificationIssue,
    Diagram,
    DiagramRequirement,
    Opportunity,
    Requirement,
    RequirementAssessment,
    RequirementVersion,
    TaskRequirement,
    WriteupFinalization,
)
from app.models.enums import ChatRole, StatementClass
from app.schemas.diagrams import WriteupResult
from app.services import artifacts, audit, draft_holds, drafting
from app.services import diagram_spec as ds
from app.services import diagrams as dg
from app.services.requirement_scope import LIVE_REQUIREMENT

log = get_logger("writeup")

_KIND_NAMES = {
    "FLOW": "Process flow",
    "ARCHITECTURE": "Solution architecture",
    "CUSTOMER_CONTEXT": "Customer-side context",
    "DATA_FLOW": "Data flow",
    "NETWORK": "Network design",
    "ORG_CHART": "Project team",
    "SEQUENCE": "Interaction sequence",
}

_UNCONFIRMED = ("ASSUMPTION", "PROPOSED")

# The part of the response that answers the tender's requirements one by one.
REQUIREMENTS_TITLE = "Response to the tender requirements"
_BATCH = 6  # requirements answered per model call
_NOT_OURS = ("CUSTOMER_OBLIGATION", "NOT_APPLICABLE")
_HOLD_READINESS = ("NEEDS_CLIENT_CLARIFICATION",)

# One write-up per task at a time: two would fight over the same sections.
#
# What is running, and how far it has got, lives here on the server and not in the
# page that asked for it, so a reload or a second tab finds it again by asking the
# task. The sections themselves are already in the database; this is only the
# "still working on it" part. It is per process, like the job store beside it.
_active: dict[uuid.UUID, dict] = {}
_last: dict[uuid.UUID, dict] = {}
_running_lock = threading.Lock()


def section_title(diagram: Diagram) -> str:
    """The heading for a diagram's section: its kind, then its own title if it adds anything."""
    kind = _KIND_NAMES.get(diagram.diagram_type, diagram.diagram_type.replace("_", " ").title())
    title = (diagram.title or "").strip()
    if not title or title.lower() == kind.lower():
        return kind
    return f"{kind}: {title}"[:200]


@dataclass
class Plan:
    """What a write-up will cover. Built in the chat turn, carried out afterwards."""

    task_id: uuid.UUID
    diagram_ids: list[uuid.UUID]
    titles: list[str]
    instruction: str | None = None
    conversation_id: uuid.UUID | None = None
    # Also answer the tender's requirements, in their own section.
    requirements: bool = False
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "task_id": str(self.task_id),
            "diagram_ids": [str(i) for i in self.diagram_ids],
            "titles": self.titles,
            "instruction": self.instruction,
            "conversation_id": str(self.conversation_id) if self.conversation_id else None,
            "requirements": self.requirements,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Plan:
        return cls(
            task_id=uuid.UUID(raw["task_id"]),
            diagram_ids=[uuid.UUID(i) for i in raw["diagram_ids"]],
            titles=list(raw["titles"]),
            instruction=raw.get("instruction"),
            conversation_id=(
                uuid.UUID(raw["conversation_id"]) if raw.get("conversation_id") else None
            ),
            requirements=bool(raw.get("requirements")),
        )

    def merged(self, other: Plan) -> Plan:
        """Two steps of one chat message asked for write-ups: do both, once each."""
        ids = list(dict.fromkeys([*self.diagram_ids, *other.diagram_ids]))
        titles = dict(zip(self.diagram_ids, self.titles, strict=True))
        titles.update(zip(other.diagram_ids, other.titles, strict=True))
        want = self.requirements or other.requirements
        return Plan(
            task_id=self.task_id,
            diagram_ids=ids,
            titles=[titles[i] for i in ids],
            instruction=other.instruction or self.instruction,
            conversation_id=self.conversation_id or other.conversation_id,
            requirements=want,
        )


def plan_for(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    diagrams: list[Diagram],
    instruction: str | None,
    requirements: bool = False,
) -> Plan:
    """Check that a write-up can happen and say what it will cover."""
    dg._may_edit(session, ctx, dg._task(session, ctx, task_id))
    if not diagrams and not requirements:
        raise ValidationError(
            "There is nothing to write up yet. Ask me to draw a diagram first, or to answer "
            "the requirements."
        )
    if requirements and not _task_requirements(session, ctx, task_id):
        if not diagrams:
            raise ValidationError("This task has no requirements to answer yet.")
        requirements = False
    artifact = artifacts.ensure_for_task(session, ctx, task_id=task_id)
    version = artifacts._current_version(session, ctx, artifact)
    if version.state.value != "DRAFT":
        raise ValidationError(
            "The response is already with reviewers, so I can't add to it. "
            "Once it is back in draft I can write these up."
        )
    return Plan(
        task_id=task_id,
        diagram_ids=[d.id for d in diagrams],
        titles=[section_title(d) for d in diagrams],
        instruction=(instruction or "").strip() or None,
        requirements=requirements,
    )


def describe(version) -> dict:
    """A diagram as the writer sees it: what is in it, in words."""
    spec = ds.parse_spec(version.spec)
    labels = {n.id: n.label for n in spec.nodes}
    unconfirmed = {u.get("label") for u in (version.unconfirmed or [])}
    lanes: dict[str, list[str]] = {}
    for node in spec.nodes:
        lanes.setdefault(node.lane or "(no lane)", []).append(node.label)
    return {
        "lanes": [{"lane": lane, "boxes": boxes} for lane, boxes in lanes.items()],
        "boxes": [
            {
                "label": n.label,
                "kind": n.kind,
                "lane": n.lane,
                "unconfirmed": n.label in unconfirmed or n.basis in _UNCONFIRMED,
                "basis": n.basis,
            }
            for n in spec.nodes
        ],
        "links": [
            {
                "from": labels[e.source],
                "to": labels[e.target],
                "label": e.label,
                "unconfirmed": e.basis in _UNCONFIRMED,
            }
            for e in spec.edges
        ],
        "unconfirmed": [
            {"label": u.get("label"), "why": u.get("why") or u.get("basis")}
            for u in (version.unconfirmed or [])
        ],
    }


def chat_notes(session: Session, ctx: ExecutionContext, *, conversation_id: uuid.UUID | None):
    """What the person has said in this conversation, oldest first -- their words only."""
    if conversation_id is None:
        return []
    rows = (
        session.execute(
            select(ChatMessage)
            .where(
                ChatMessage.org_id == ctx.org_id,
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.role == ChatRole.USER,
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(25)
        )
        .scalars()
        .all()
    )
    return [(m.content or "")[:600] for m in reversed(rows)]


def bid_context(session: Session, ctx: ExecutionContext, task_id: uuid.UUID) -> dict:
    """Who the response is for, and what the customer has already told us.

    The customer's replies to clarification questions are the settled answers a section can
    be written on; the count of questions still open says how much is still conditional.
    """
    task = dg._task(session, ctx, task_id)
    bid = session.get(Bid, task.bid_id)
    opportunity = session.get(Opportunity, bid.opportunity_id) if bid else None
    issues = (
        session.execute(
            select(ClarificationIssue).where(
                ClarificationIssue.org_id == ctx.org_id,
                ClarificationIssue.bid_id == task.bid_id,
            )
        )
        .scalars()
        .all()
    )
    answered = [
        {
            "question": (i.recommended_question or i.description)[:400],
            "answer": (i.client_response or "")[:600],
        }
        for i in issues
        if i.client_response
        and i.status.value in ("ANSWERED", "CLOSED")
        # The person chose whether the response rests on it.
        and i.include_in_response
    ]
    still_open = sum(1 for i in issues if i.status.value in ("OPEN", "SENT_TO_CLIENT"))
    return {
        "customer": opportunity.customer_name if opportunity else None,
        # What the company decided about each unclear point in the tender. Nobody
        # asks the issuer, so these are our assumptions, not the customer's words.
        "assumptions": answered,
        "open_questions": still_open,
    }


def _digest(session: Session, ctx: ExecutionContext, artifact, skip: uuid.UUID) -> list[dict]:
    version = artifacts._current_version(session, ctx, artifact)
    rows = artifacts.statements_for_version(session, ctx, version_id=version.id)
    seen: dict[str, dict] = {}
    for row in rows:
        if row.diagram_id == skip or not row.section:
            continue
        entry = seen.setdefault(row.section, {"section": row.section, "opens_with": row.text[:220]})
        entry["statements"] = entry.get("statements", 0) + 1
    return list(seen.values())


_DIAGRAM_REF = re.compile(r"\bD\d{1,2}\b")
# Talking about the section or the drawing from outside, instead of explaining the system.
_META = re.compile(
    r"\b(this (section|part) (explains|describes|covers|sets out|shows)|"
    r"(the|this) (diagram|figure|drawing) (is read|reads|is for|shows \d+)|"
    r"which together answer)",
    re.IGNORECASE,
)
# The writer marks every part of the diagram it names as [[Label]]. Only the code reads the
# brackets: it can then check each name against the drawing, and removes them from the text.
_PART = re.compile(r"\[\[(.+?)\]\]")
_COVERAGE = 0.6
_MOST_PROMISES = WRITEUP_MOST_PROMISES


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _uncovered(mapped: list[dict], version) -> list[str]:
    """Unconfirmed parts of the diagram that no assumption in the section mentions."""
    text = " ".join(
        m["text"].lower() for m in mapped if m["statement_class"] == StatementClass.ASSUMPTION.value
    )
    missing: list[str] = []
    for item in version.unconfirmed or []:
        label = item.get("label") or ""
        names = (
            [part.strip() for part in label.split("->")] if item.get("kind") == "link" else [label]
        )
        if label and not all(name.lower() in text for name in names if name):
            missing.append(label)
    return missing


def _assumption_for_unconfirmed(
    mapped: list[dict], version, customer: str | None = None
) -> dict | None:
    """Anything the picture takes for granted must be stated as an assumption.

    The model is told to. This does not rely on it: whichever unconfirmed parts none of
    its assumptions mention are listed in one added assumption.
    """
    labels = _uncovered(mapped, version)
    if not labels:
        return None
    return {
        "statement_class": StatementClass.ASSUMPTION.value,
        "text": (
            "We have assumed the following steps are as drawn, and we will settle any "
            "difference in the design phase: " + "; ".join(labels) + "."
        ),
    }


@dataclass
class Review:
    """What is wrong with a section, found by code and not by asking the writer."""

    bad: set[int] = field(default_factory=set)  # sentences to drop if they can't be fixed
    unknown: list[str] = field(default_factory=list)
    shorthand: bool = False
    meta: bool = False
    missing_parts: list[str] = field(default_factory=list)
    unnumbered: bool = False
    promises: int = 0

    def complaints(self, parts: int) -> list[str]:
        out = []
        if self.unknown:
            out.append(
                "These names in [[ ]] are not parts or lanes of the diagram: "
                + ", ".join(self.unknown)
                + ". Use the exact labels given, and only for things that are on the drawing."
            )
        if self.shorthand:
            out.append(
                "Do not call diagrams D1, D2 and so on: that is shorthand from the chat and "
                "means nothing to the customer. Name a diagram by its title or its kind."
            )
        if self.meta:
            out.append(
                "Do not describe the section or the diagram from outside ('This section "
                "explains the diagram', 'the diagram is read top to bottom'). Explain how the "
                "system works, using the diagram to walk through it."
            )
        if parts and 1 - len(self.missing_parts) / parts < _COVERAGE:
            out.append(
                "These parts of the diagram are never mentioned, so the section does not "
                "explain them: "
                + ", ".join(self.missing_parts)
                + ". Cover them in the walkthrough, each as [[Label]]."
            )
        if self.unnumbered:
            out.append("Give every sentence a paragraph number.")
        if self.promises > _MOST_PROMISES:
            out.append(
                f"{self.promises} promises is too many for one section. Say what the diagram "
                "shows as DIAGRAM_DESCRIPTION and keep PROPOSED_COMMITMENT for the real "
                f"undertakings, at most {_MOST_PROMISES}, each covering a whole piece of work."
            )
        return out


def _review(mapped: list[dict], version) -> Review:
    info = describe(version)
    parts = {_norm(b["label"]): b["label"] for b in info["boxes"]}
    lanes = {_norm(lane["lane"]) for lane in info["lanes"] if lane["lane"] != "(no lane)"}
    known = set(parts) | lanes
    found = Review()
    used: set[str] = set()
    for i, m in enumerate(mapped):
        text = m["text"]
        for name in _PART.findall(text):
            if _norm(name) in known:
                used.add(_norm(name))
            else:
                found.unknown.append(name)
                found.bad.add(i)
        if _DIAGRAM_REF.search(text):
            found.shorthand = True
            found.bad.add(i)
        if _META.search(text):
            found.meta = True
        lowered = _norm(text)
        used.update(k for k in parts if k in lowered)
        if not m.get("paragraph_no"):
            found.unnumbered = True
        if m["statement_class"] == StatementClass.PROPOSED_COMMITMENT.value:
            found.promises += 1
    found.missing_parts = [label for k, label in parts.items() if k not in used]
    return found


def _repair(mapped: list[dict], found: Review) -> list[dict]:
    """The second attempt still had problems: keep what is sound and drop what is not.

    A sentence naming something that is not on the drawing, or using the chat's shorthand,
    is removed; a sentence with no paragraph number joins the paragraph before it.
    """
    kept = [m for i, m in enumerate(mapped) if i not in found.bad]
    paragraph = 1
    for m in kept:
        if m.get("paragraph_no"):
            paragraph = m["paragraph_no"]
        else:
            m["paragraph_no"] = paragraph
    return kept


def _ask_for_statements(
    session: Session,
    ctx: ExecutionContext,
    gateway: ModelGateway,
    payload: dict,
    tender,
    evidence,
    version,
) -> list[dict]:
    """Ask the writer; if the answer breaks a rule, ask once more saying which, then repair."""
    fix: list[str] | None = None
    parts = len(describe(version)["boxes"])
    for attempt in (1, 2):
        try:
            result = dg._ask(
                gateway,
                "diagram_writeup",
                {**payload, "fix_these": fix, "asked_at": datetime.now(timezone.utc).isoformat()},
                ctx=ctx,
                session=session,
                expect=WriteupResult,
            )
            mapped = drafting.map_proposals(
                ctx, result.statements, tender, evidence, allow_descriptions=True
            )
            found = _review(mapped, version)
        except Exception as exc:  # noqa: BLE001 - the same failure is reported once, in words
            if attempt == 2:
                raise ValidationError(f"The writer's answer could not be used: {exc}") from exc
            fix = [f"Your last answer was rejected: {exc}"]
            continue
        complaints = found.complaints(parts)
        if not complaints:
            return _repair(mapped, found)
        if attempt == 2:
            kept = _repair(mapped, found)
            if not kept:
                raise ValidationError("The writer's answer broke the rules twice: " + complaints[0])
            return kept
        fix = complaints
    raise ValidationError("The writer gave no usable answer.")  # pragma: no cover


def _finish(
    written: list[dict], diagram: Diagram, version, customer: str | None = None
) -> list[dict]:
    """Take the brackets out, and add what code knows the writer left out."""
    statements = [{**m, "text": _PART.sub(r"\1", m["text"])} for m in written]
    last = max((m.get("paragraph_no") or 1) for m in statements)
    left_out = _review(written, version).missing_parts
    parts = len(describe(version)["boxes"])
    # A few boxes a good explanation does not need to name; a large share means it skipped
    # a piece of the design, and then the reader is told what else the drawing holds.
    if left_out and parts and len(left_out) / parts > 1 - _COVERAGE:
        last += 1
        statements.append(
            {
                "statement_class": StatementClass.DIAGRAM_DESCRIPTION.value,
                "text": "The drawing also includes " + ", ".join(left_out) + ".",
                "paragraph_no": last,
            }
        )
    extra = _assumption_for_unconfirmed(statements, version, customer)
    if extra is not None:
        statements.append({**extra, "paragraph_no": last + 1})
    return statements


def write_section(
    session: Session,
    ctx: ExecutionContext,
    *,
    artifact_id: uuid.UUID,
    diagram_id: uuid.UUID,
    instruction: str | None,
    notes: list[str],
    gateway: ModelGateway,
) -> dict:
    """Write, or rewrite, the section that explains one diagram."""
    artifact = artifacts.get_artifact(session, ctx, artifact_id=artifact_id)
    diagram = dg.get_diagram(session, ctx, diagram_id)
    if diagram.task_id != artifact.task_id:
        raise NotFound("That diagram is not on this task.")
    version = dg.current_version(session, ctx, diagram)
    title = section_title(diagram)

    tender, evidence, evidence_payload = drafting.gather_sources(
        session, ctx, task_id=artifact.task_id
    )
    stances = {r["code"]: r["stance"] for r in _task_requirements(session, ctx, artifact.task_id)}
    payload = {
        **bid_context(session, ctx, artifact.task_id),
        "section_title": title,
        "instruction": instruction,
        "diagram": {"type": diagram.diagram_type, "title": diagram.title, **describe(version)},
        "tender_sources": [
            {
                "ref": ref,
                "code": v["code"],
                "text": v["text"],
                "stance": stances.get(v["code"]),
            }
            for ref, v in tender.items()
        ],
        "evidence_sources": evidence_payload,
        "chat_notes": notes,
        "other_sections": _digest(session, ctx, artifact, diagram.id),
    }
    written = _ask_for_statements(session, ctx, gateway, payload, tender, evidence, version)
    mapped = _finish(written, diagram, version, payload.get("customer"))
    call = get_call_type("diagram_writeup")
    artifacts.write_section(
        session,
        ctx,
        artifact_id=artifact.id,
        section=title,
        statements=mapped,
        diagram_id=diagram.id,
        diagram_version_no=version.version_no,
        generated_by=f"llm:{call.name}@{call.prompt_version}",
        change_reason=instruction,
    )
    return {"section": title, "statements": len(mapped), "diagram_id": str(diagram.id)}


# --- answering the tender's requirements ------------------------------------------


def _task_requirements(session: Session, ctx: ExecutionContext, task_id: uuid.UUID) -> list[dict]:
    """The task's requirements with their current assessment, in code order."""
    rows = session.execute(
        select(Requirement, RequirementVersion)
        .join(TaskRequirement, TaskRequirement.requirement_id == Requirement.id)
        .join(RequirementVersion, RequirementVersion.id == Requirement.current_version_id)
        .where(
            TaskRequirement.org_id == ctx.org_id,
            TaskRequirement.task_id == task_id,
            Requirement.org_id == ctx.org_id,
            LIVE_REQUIREMENT,
        )
        .order_by(Requirement.code)
    ).all()
    ids = [r.id for r, _ in rows]
    assessed = {}
    if ids:
        assessed = {
            a.requirement_id: a
            for a in session.execute(
                select(RequirementAssessment).where(
                    RequirementAssessment.org_id == ctx.org_id,
                    RequirementAssessment.requirement_id.in_(ids),
                    RequirementAssessment.is_current.is_(True),
                )
            ).scalars()
        }
    from app.services import requirement_positions

    decided = requirement_positions.active_for(session, ctx, task_id)
    out = []
    for requirement, version in rows:
        a = assessed.get(requirement.id)
        readiness = a.bid_readiness.value if a else None
        evidence = a.evidence_status.value if a else None
        if readiness in _NOT_OURS:
            stance = "not_ours"
        elif readiness in _HOLD_READINESS:
            # Only a requirement the customer must clarify is on hold. "Blocked" and "not
            # assessed" mean we hold no approved evidence yet; the response can still say how
            # we will meet it, as a promise that needs an approver, never as a fact.
            stance = "hold"
        elif evidence == "CURRENT_VERIFIED" and readiness in ("READY", "DRAFTABLE"):
            stance = "can_state"
        else:
            stance = "commit_only"
        decision = decided.get(requirement.id)
        set_aside = decision is not None and decision.status == "ignored"
        position = (
            decision.text if decision is not None and decision.status == "submitted" else None
        )
        if set_aside:
            # A person set it aside: it is not answered and not chased.
            stance = "not_ours"
        elif position and stance in ("hold", "commit_only"):
            # A person approved this wording as the company's answer, so there is
            # something to write -- as a promise, never as proven fact.
            stance = "commit_only"
        out.append(
            {
                "set_aside": set_aside,
                "company_position": position,
                "id": requirement.id,
                "code": requirement.code,
                "text": version.requirement_text,
                "evidence_status": evidence,
                "bid_readiness": readiness,
                "why": ((a.rationale or "")[:300] if a else ""),
                "stance": stance,
            }
        )
    return out


def _needs(item: dict) -> str | None:
    """What stands between a requirement and an answer, in the words a person would use."""
    if item["bid_readiness"] is None:
        return "Not assessed yet"
    if item["bid_readiness"] == "BLOCKED":
        return "Blocked"
    if item["bid_readiness"] == "NEEDS_CLIENT_CLARIFICATION":
        return "Needs the customer to clarify it"
    return None


def _short(text: str, limit: int = 90) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def requirement_batches(session: Session, ctx: ExecutionContext, task_id: uuid.UUID):
    """The requirements to answer, in groups small enough for one careful model call."""
    _, _, rows = _current_rows(session, ctx, task_id)
    # What the diagram sections already answer is not answered a second time; whatever this
    # section itself said before is being rewritten, so it does not count.
    elsewhere = [r for r in rows if not (r.section == REQUIREMENTS_TITLE and r.diagram_id is None)]
    state = {
        c["code"]: c["state"] for c in coverage_for(session, ctx, task_id=task_id, rows=elsewhere)
    }
    ours = [
        r["code"]
        for r in _task_requirements(session, ctx, task_id)
        if r["stance"] != "not_ours" and state.get(r["code"]) != "answered"
    ]
    return [ours[i : i + _BATCH] for i in range(0, len(ours), _BATCH)]


@dataclass
class ReqReview:
    bad: set[int] = field(default_factory=set)
    unanswered: list[str] = field(default_factory=list)  # what they are about, in words
    unanswered_codes: list[str] = field(default_factory=list)
    hold_breaks: list[str] = field(default_factory=list)
    shorthand: bool = False
    unnumbered: bool = False

    def complaints(self) -> list[str]:
        out = []
        if self.unanswered:
            out.append(
                "These requirements have no real answer: "
                + ", ".join(self.unanswered)
                + ". Answer each with what we will do, tagged in `answers`; restating the "
                "requirement is not an answer."
            )
        if self.hold_breaks:
            out.append(
                "These requirements are on hold, so promise and claim nothing for them, and "
                "write only ASSUMPTION sentences saying what the answer depends on: "
                + ", ".join(self.hold_breaks)
                + "."
            )
        if self.shorthand:
            out.append("Do not write D1, D2 and so on; name a diagram by its title.")
        if self.unnumbered:
            out.append("Give every sentence a paragraph number.")
        return out


_ANSWERING = (
    StatementClass.PROPOSED_COMMITMENT.value,
    StatementClass.EVIDENCE_BACKED_FACT.value,
)


def _review_requirements(mapped: list[dict], batch: list[dict]) -> ReqReview:
    codes = {b["code"]: b for b in batch}
    found = ReqReview()
    answered: set[str] = set()
    broken: list[str] = []
    for i, m in enumerate(mapped):
        tagged = [c for c in m.get("requirement_codes", []) if c in codes]
        if _DIAGRAM_REF.search(m["text"]):
            found.shorthand = True
            found.bad.add(i)
        if not m.get("paragraph_no"):
            found.unnumbered = True
        if not tagged:
            continue
        if m["statement_class"] != StatementClass.ASSUMPTION.value and all(
            codes[c]["stance"] == "hold" for c in tagged
        ):
            found.bad.add(i)
            broken.extend(tagged)
            continue
        for c in tagged:
            if codes[c]["stance"] == "hold":
                if m["statement_class"] == StatementClass.ASSUMPTION.value:
                    answered.add(c)
            elif m["statement_class"] in _ANSWERING:
                answered.add(c)
    found.hold_breaks = [_short(codes[c]["text"], 60) for c in dict.fromkeys(broken)]
    left = [b for b in batch if b["code"] not in answered and b["stance"] != "hold"]
    found.unanswered_codes = [b["code"] for b in left]
    found.unanswered = [_short(b["text"], 60) for b in left]
    return found


def _fallback(item: dict) -> str:
    """The plain admission that goes in when the writer left a requirement without an answer."""
    subject = f"the requirement “{_short(item['text'])}”"
    if item["bid_readiness"] == "NEEDS_CLIENT_CLARIFICATION":
        return (
            f"Our response to {subject} depends on the customer clarifying what it requires; "
            "we will confirm our approach once they do."
        )
    return (
        f"How we will meet {subject} has not been settled yet and needs a decision from the "
        "bid team before this response is complete."
    )


def _finish_requirements(kept: list[dict], batch: list[dict], start: int) -> list[dict]:
    """Keep what is sound, offset the paragraphs, and admit any requirement left unanswered."""
    codes = {b["code"] for b in batch}
    out = []
    for m in kept:
        row = {**m, "paragraph_no": (m.get("paragraph_no") or 1) + start}
        row["requirement_codes"] = [c for c in m.get("requirement_codes", []) if c in codes]
        if not row["requirement_codes"]:
            row.pop("requirement_codes")
        out.append(row)
    last = max((r["paragraph_no"] for r in out), default=start)
    verdict = _review_requirements(out, batch)
    say = [b for b in batch if b["code"] in verdict.unanswered_codes]
    for b in batch:
        if b["stance"] == "hold" and not any(
            r["statement_class"] == StatementClass.ASSUMPTION.value
            and b["code"] in r.get("requirement_codes", [])
            for r in out
        ):
            say.append(b)
    if say:
        last += 1
        for b in say:
            out.append(
                {
                    "statement_class": StatementClass.ASSUMPTION.value,
                    "text": _fallback(b),
                    "paragraph_no": last,
                    "requirement_codes": [b["code"]],
                }
            )
    return out


def _repair_requirements(mapped: list[dict], found: ReqReview) -> list[dict]:
    kept = [m for i, m in enumerate(mapped) if i not in found.bad]
    paragraph = 1
    for m in kept:
        if m.get("paragraph_no"):
            paragraph = m["paragraph_no"]
        else:
            m["paragraph_no"] = paragraph
    return kept


def write_requirements(
    session: Session,
    ctx: ExecutionContext,
    *,
    artifact_id: uuid.UUID,
    codes: list[str],
    first: bool,
    instruction: str | None,
    notes: list[str],
    gateway: ModelGateway,
) -> dict:
    """Answer one group of the task's requirements in the section that answers them all.

    The first group replaces the section (a rewrite), later groups are added after it.
    """
    artifact = artifacts.get_artifact(session, ctx, artifact_id=artifact_id)
    tender, evidence, evidence_payload = drafting.gather_sources(
        session, ctx, task_id=artifact.task_id
    )
    by_code = {r["code"]: r for r in _task_requirements(session, ctx, artifact.task_id)}
    ref_of = {v["code"]: ref for ref, v in tender.items()}
    batch = [by_code[c] for c in codes if c in by_code and by_code[c]["stance"] != "not_ours"]
    if not batch:
        raise ValidationError("None of those requirements are ones this task has to answer.")

    _, _, rows = _current_rows(session, ctx, artifact.task_id)
    written = {row.diagram_id: row.section for row in rows if row.diagram_id}
    linked: dict[uuid.UUID, list[str]] = {}
    for link in session.execute(
        select(DiagramRequirement).where(DiagramRequirement.org_id == ctx.org_id)
    ).scalars():
        if link.diagram_id in written:
            linked.setdefault(link.requirement_id, []).append(written[link.diagram_id])
    start = 0
    if not first:
        start = max(
            (
                r.paragraph_no or 0
                for r in rows
                if r.section == REQUIREMENTS_TITLE and not r.diagram_id
            ),
            default=0,
        )

    facts = [
        {
            "ref": ref_of.get(b["code"]),
            "code": b["code"],
            "text": b["text"],
            "evidence_status": b["evidence_status"],
            "bid_readiness": b["bid_readiness"],
            "stance": b["stance"],
            # Wording a person approved as the company's answer, when they did.
            "company_position": b.get("company_position"),
            "why": b["why"],
            "shown_in": sorted(set(linked.get(b["id"], []))),
        }
        for b in batch
    ]
    wanted = {f["ref"] for f in facts}
    payload = {
        **bid_context(session, ctx, artifact.task_id),
        "section_title": REQUIREMENTS_TITLE,
        "instruction": instruction,
        "requirements": facts,
        "tender_sources": [
            {"ref": ref, "code": v["code"], "text": v["text"]}
            for ref, v in tender.items()
            if ref in wanted
        ],
        "evidence_sources": evidence_payload,
        "chat_notes": notes,
        "other_sections": [
            {"section": t, "opens_with": ""} for t in dict.fromkeys(written.values())
        ],
    }
    kept: list[dict] = []
    fix: list[str] | None = None
    for attempt in (1, 2):
        try:
            result = dg._ask(
                gateway,
                "requirements_writeup",
                {**payload, "fix_these": fix, "asked_at": datetime.now(timezone.utc).isoformat()},
                ctx=ctx,
                session=session,
                expect=WriteupResult,
            )
            mapped = drafting.map_proposals(ctx, result.statements, tender, evidence)
            found = _review_requirements(mapped, batch)
        except Exception as exc:  # noqa: BLE001 - reported once, in words
            if attempt == 2:
                raise ValidationError(f"The writer's answer could not be used: {exc}") from exc
            fix = [f"Your last answer was rejected: {exc}"]
            continue
        complaints = found.complaints()
        if not complaints or attempt == 2:
            kept = _repair_requirements(mapped, found)
            break
        fix = complaints
    if not kept:
        raise ValidationError("The writer gave no usable answer for these requirements.")

    statements = _finish_requirements(kept, batch, start)
    call = get_call_type("requirements_writeup")
    artifacts.write_section(
        session,
        ctx,
        artifact_id=artifact.id,
        section=REQUIREMENTS_TITLE,
        statements=statements,
        generated_by=f"llm:{call.name}@{call.prompt_version}",
        change_reason=instruction,
        append=not first,
    )
    return {"section": REQUIREMENTS_TITLE, "statements": len(statements), "diagram_id": None}


def coverage_for(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID, rows
) -> list[dict]:
    """For each requirement: is there a real answer in the response, and if not, why not.

    Counted from the draft itself, not from what a model says it wrote. A sentence answers a
    requirement when it is tagged with it and promises, or proves, something. A sentence that
    only restates the tender does not count, and neither does a condition on its own.
    """
    out = []
    for item in _task_requirements(session, ctx, task_id):
        tagged = [r for r in rows if item["code"] in (r.requirement_codes or [])]
        classes = {r.statement_class.value for r in tagged}
        needs = _needs(item)
        if item.get("set_aside"):
            state = "set_aside"
        elif item["stance"] == "not_ours":
            state = "not_ours"
        elif classes & (set(_ANSWERING) | {StatementClass.DIAGRAM_DESCRIPTION.value}):
            state = "conditional" if item["stance"] == "hold" else "answered"
        elif StatementClass.ASSUMPTION.value in classes:
            # A condition is the right answer for a requirement on hold; for any other it
            # only says the answer is missing.
            state = "conditional" if item["stance"] == "hold" else "unanswered"
        elif classes:
            state = "restated"
        else:
            state = "unanswered"
        out.append(
            {
                "code": item["code"],
                "text": item["text"][:240],
                "state": state,
                "needs": needs,
                "sections": sorted({r.section for r in tagged if r.section}),
                "diagram_ids": sorted({str(r.diagram_id) for r in tagged if r.diagram_id}),
            }
        )
    return out


def is_running(task_id: uuid.UUID) -> bool:
    with _running_lock:
        return task_id in _active


def begin(task_id: uuid.UUID, *, org_id: uuid.UUID, titles: list[str]) -> bool:
    """Claim the task for a write-up; False if one is already running."""
    with _running_lock:
        if task_id in _active:
            return False
        _last.pop(task_id, None)
        _active[task_id] = {
            "org_id": org_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "titles": list(titles),
            "progress": {"total": len(titles), "done": 0, "failed": 0, "position": 0},
        }
        return True


def note(task_id: uuid.UUID, data: dict) -> None:
    with _running_lock:
        if task_id in _active:
            _active[task_id]["progress"] = data


def end(task_id: uuid.UUID, result: dict | None = None) -> None:
    with _running_lock:
        _active.pop(task_id, None)
        if result is not None:
            _last[task_id] = {**result, "finished_at": datetime.now(timezone.utc).isoformat()}


def running_status(task_id: uuid.UUID, org_id: uuid.UUID) -> dict:
    """What the server is doing for this task right now, and how the last run ended."""
    with _running_lock:
        active = _active.get(task_id)
        last = _last.get(task_id)
        if active is not None and active["org_id"] == org_id:
            return {
                "running": True,
                "started_at": active["started_at"],
                "titles": active["titles"],
                "progress": active["progress"],
                "last": None,
            }
        return {
            "running": False,
            "started_at": None,
            "titles": [],
            "progress": None,
            "last": last if last and last.get("org_id") == org_id else None,
        }


def start_background(
    plan: Plan, *, org_id: uuid.UUID, user_id: uuid.UUID, request_id: uuid.UUID
) -> str | None:
    """Carry out a plan on a worker thread. Returns the job id, or None if one is running."""
    from app.db.session import get_session_factory
    from app.gateway.gateway import build_gateway
    from app.services.jobs import get_store

    shown = plan.titles + ([REQUIREMENTS_TITLE] if plan.requirements else [])
    if not begin(plan.task_id, org_id=org_id, titles=shown):
        return None
    store = get_store()
    job = store.create(org_id=org_id, user_id=user_id, kind="draft_writeup")

    def work() -> None:
        store.start(job)
        result: dict | None = None
        try:
            result = run(
                plan,
                session_factory=get_session_factory(),
                org_id=org_id,
                user_id=user_id,
                request_id=request_id,
                gateway=build_gateway(),
                progress=lambda data: store.emit(job, "progress", data),
            )
            store.finish(job, result)
        except Exception as exc:  # noqa: BLE001 - surfaced through the job and the status
            log.warning("writeup_failed", error=str(exc))
            result = {"written": [], "failed": [{"section": "all", "error": str(exc)}]}
            store.fail(job, f"{type(exc).__name__}: {exc}")
        finally:
            end(plan.task_id, {**(result or {}), "org_id": org_id})

    threading.Thread(target=work, daemon=True).start()
    return str(job.id)


def run(
    plan: Plan,
    *,
    session_factory: Callable[[], Session],
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    request_id: uuid.UUID,
    gateway: ModelGateway,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """Write every section in the plan, committing after each.

    Each section gets its own session and transaction, so what is written stays
    written if a later one fails, and a reader of the Response draft sees it at once.
    A failure is recorded against its section and the rest carry on.
    """
    done: list[dict] = []
    failed: list[dict] = []
    total = len(plan.diagram_ids) + (1 if plan.requirements else 0)

    def report(**data) -> None:
        state = {"total": total, "done": len(done), "failed": len(failed), **data}
        note(plan.task_id, state)
        if progress is not None:
            progress(state)

    def unit(work: Callable[[Session, ExecutionContext, uuid.UUID], object]):
        """Run one piece of work in its own transaction; commit it if it worked."""
        session = session_factory()
        try:
            session.begin()
            ctx = ExecutionContext(org_id=org_id, actor_user_id=user_id, request_id=request_id)
            apply_tenant_context(session, ctx)
            dg._may_edit(session, ctx, dg._task(session, ctx, plan.task_id))
            artifact = artifacts.ensure_for_task(session, ctx, task_id=plan.task_id)
            out = work(session, ctx, artifact.id)
            session.commit()
            return out
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def notes_of(session: Session, ctx: ExecutionContext) -> list[str]:
        return chat_notes(session, ctx, conversation_id=plan.conversation_id)

    for index, (diagram_id, title) in enumerate(
        zip(plan.diagram_ids, plan.titles, strict=True), start=1
    ):
        report(current=title, position=index)
        try:

            def one_section(
                session: Session,
                ctx: ExecutionContext,
                artifact_id: uuid.UUID,
                diagram_id: uuid.UUID = diagram_id,
            ) -> object:
                return write_section(
                    session,
                    ctx,
                    artifact_id=artifact_id,
                    diagram_id=diagram_id,
                    instruction=plan.instruction,
                    notes=notes_of(session, ctx),
                    gateway=gateway,
                )

            out = unit(one_section)
            done.append(out)
        except Exception as exc:  # noqa: BLE001 - one section failing must not lose the others
            log.warning("writeup_section_failed", section=title, error=str(exc))
            failed.append({"section": title, "error": f"{type(exc).__name__}: {exc}"})
        report(current=title, position=index)

    if plan.requirements:
        position = total
        report(current=REQUIREMENTS_TITLE, position=position)
        try:
            batches = unit(lambda session, ctx, _a: requirement_batches(session, ctx, plan.task_id))
            written = 0
            if not batches:
                # The diagram sections already answer everything: no section of its own.
                unit(
                    lambda session, ctx, artifact_id: artifacts.drop_section(
                        session, ctx, artifact_id=artifact_id, section=REQUIREMENTS_TITLE
                    )
                )
            for n, codes in enumerate(batches):
                report(
                    current=f"{REQUIREMENTS_TITLE} (part {n + 1} of {len(batches)})",
                    position=position,
                )

                def one_part(
                    session: Session,
                    ctx: ExecutionContext,
                    artifact_id: uuid.UUID,
                    codes: list[str] = codes,
                    n: int = n,
                ) -> dict:
                    return write_requirements(
                        session,
                        ctx,
                        artifact_id=artifact_id,
                        codes=codes,
                        first=(n == 0),
                        instruction=plan.instruction,
                        notes=notes_of(session, ctx),
                        gateway=gateway,
                    )

                out = unit(one_part)
                written += out["statements"]
            done.append({"section": REQUIREMENTS_TITLE, "statements": written, "diagram_id": None})
        except Exception as exc:  # noqa: BLE001 - the parts already written stay written
            log.warning("writeup_section_failed", section=REQUIREMENTS_TITLE, error=str(exc))
            failed.append({"section": REQUIREMENTS_TITLE, "error": f"{type(exc).__name__}: {exc}"})
        report(current=REQUIREMENTS_TITLE, position=position)
    return {"written": done, "failed": failed}


def diagram_version_no(
    session: Session, ctx: ExecutionContext, *, diagram_id: uuid.UUID
) -> int | None:
    """The version a diagram is on now, or None if it is gone."""
    diagram = session.get(Diagram, diagram_id)
    if diagram is None or diagram.org_id != ctx.org_id or diagram.archived:
        return None
    if diagram.current_version_id is None:
        return None
    return dg.current_version(session, ctx, diagram).version_no


def why_out_of_date(
    session: Session, ctx: ExecutionContext, *, diagram_id: uuid.UUID
) -> str | None:
    """Why a section written from this diagram no longer matches the tender, or None.

    Whether the *diagram* has moved on is a version comparison the caller makes; this
    is the other half: whether the requirements the diagram answers have been reworded
    or withdrawn since the diagram was made, which the diagram already knows.
    """
    diagram = session.get(Diagram, diagram_id)
    if diagram is None or diagram.org_id != ctx.org_id:
        return "the diagram no longer exists"
    if diagram.archived:
        return "the diagram was removed"
    state = dg.staleness(session, ctx, diagram)
    if state["stale"]:
        codes = ", ".join(c["code"] for c in state["changed"])
        return f"{codes} changed since the diagram was made"
    return None


def out_of_date_reason(
    session: Session,
    ctx: ExecutionContext,
    *,
    diagram_id: uuid.UUID,
    written_version_no: int | None,
) -> str | None:
    """Why a section no longer matches its diagram or the tender, or None if it does."""
    why = why_out_of_date(session, ctx, diagram_id=diagram_id)
    if why is not None:
        return why
    now = diagram_version_no(session, ctx, diagram_id=diagram_id)
    if now and written_version_no != now:
        return f"the diagram is now version {now}"
    return None


# --- what is in the draft, and finalizing it ----------------------------------


def _current_rows(session: Session, ctx: ExecutionContext, task_id: uuid.UUID):
    artifact = artifacts.ensure_for_task(session, ctx, task_id=task_id)
    version = artifacts._current_version(session, ctx, artifact)
    rows = artifacts.statements_for_version(session, ctx, version_id=version.id)
    return artifact, version, rows


def _content_hash(rows) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            "|".join(
                [
                    str(row.order_index),
                    row.statement_class.value,
                    row.text,
                    str(row.diagram_id or ""),
                    str(row.diagram_version_no or ""),
                    row.section or "",
                    str(row.paragraph_no or ""),
                    ",".join(row.requirement_codes or []),
                ]
            ).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _sections_of(rows) -> list[dict]:
    """The draft's statements grouped into its sections, in reading order."""
    out: list[dict] = []
    for row in rows:
        key = (row.section, row.diagram_id)
        if not out or out[-1]["_key"] != key:
            out.append(
                {
                    "_key": key,
                    "section": row.section,
                    "diagram_id": str(row.diagram_id) if row.diagram_id else None,
                    "diagram_version_no": row.diagram_version_no,
                    "statements": [],
                }
            )
        out[-1]["statements"].append(
            {
                "statement_class": row.statement_class.value,
                "text": row.text,
                "evidence_ids": [str(e) for e in (row.evidence_ids or [])],
                "source_locator": row.source_locator,
                "approval_state": row.approval_state.value,
                "paragraph_no": row.paragraph_no,
                "requirement_codes": row.requirement_codes or [],
            }
        )
    for item in out:
        item.pop("_key")
    return out


def status(session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID) -> dict:
    """Everything the screen needs to show about a task's write-up, in one call.

    Asked on every page load, so a reload finds a write-up that is still running,
    the diagrams that have no section yet, and whether the draft is finalized.
    """
    dg._task(session, ctx, task_id)
    live = running_status(task_id, ctx.org_id)
    if live["last"]:
        live["last"] = {k: v for k, v in live["last"].items() if k != "org_id"}
    _, version, rows = _current_rows(session, ctx, task_id)
    covered = {row.diagram_id for row in rows if row.diagram_id}
    drawn = dg.diagrams_for_task(session, ctx, task_id=task_id)
    requirements_written = any(
        row.section == REQUIREMENTS_TITLE and row.diagram_id is None for row in rows
    )
    diagram_states = [
        {
            "diagram_id": str(d.id),
            "title": section_title(d),
            "state": "left_out" if d.left_out else ("written" if d.id in covered else "pending"),
            "version_no": dg.current_version(session, ctx, d).version_no,
            "parts": len((dg.current_version(session, ctx, d).spec or {}).get("nodes", [])),
        }
        for d in drawn
    ]
    # Only worth saying once a write-up exists; before that the whole thing is "missing".
    missing = (
        [
            {"diagram_id": d["diagram_id"], "title": d["title"]}
            for d in diagram_states
            if d["state"] == "pending"
        ]
        if covered or requirements_written
        else []
    )
    coverage = coverage_for(session, ctx, task_id=task_id, rows=rows)
    counts: dict[str, int] = {}
    for item in coverage:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    final = session.execute(
        select(WriteupFinalization)
        .where(
            WriteupFinalization.org_id == ctx.org_id,
            WriteupFinalization.task_id == task_id,
            WriteupFinalization.artifact_version_id == version.id,
        )
        .order_by(WriteupFinalization.finalized_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return {
        **live,
        "has_writeup": bool(covered) or requirements_written,
        "missing": missing,
        "diagrams": diagram_states,
        "requirements_written": requirements_written,
        "coverage": coverage,
        "coverage_counts": counts,
        "holds": draft_holds.for_task(session, ctx, task_id=task_id),
        "finalized": (
            None
            if final is None
            else {
                "at": final.finalized_at.isoformat(),
                "by": str(final.finalized_by) if final.finalized_by else None,
                "unchanged": final.content_hash == _content_hash(rows),
            }
        ),
    }


def finalize(session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID) -> dict:
    """Keep this write-up exactly as it is, for the final report to use.

    Refused while it is still being written, and while any section describes a
    diagram (or requirement) that has since changed: finalizing something known to be
    out of date would put a stale picture in the report with a stamp saying it was checked.
    """
    dg._may_edit(session, ctx, dg._task(session, ctx, task_id))
    if is_running(task_id):
        raise StateConflict("The write-up is still being written. Finalize it when it finishes.")
    artifact, version, rows = _current_rows(session, ctx, task_id)
    if not any(row.section for row in rows):
        raise ValidationError(
            "There is no write-up to finalize yet. Ask me to write up the diagrams first."
        )
    problems: list[str] = []
    seen: set[uuid.UUID] = set()
    for row in rows:
        if row.diagram_id is None or row.diagram_id in seen:
            continue
        seen.add(row.diagram_id)
        reason = out_of_date_reason(
            session, ctx, diagram_id=row.diagram_id, written_version_no=row.diagram_version_no
        )
        if reason:
            problems.append(f"“{row.section}” — {reason}")
    if problems:
        raise ValidationError(
            "These sections are out of date, so I won't finalize yet: "
            + "; ".join(problems)
            + ". Ask me to update the write-up first."
        )

    sections = _sections_of(rows)
    for item in sections:
        if item["diagram_id"]:
            diagram = dg.get_diagram(session, ctx, uuid.UUID(item["diagram_id"]))
            item["diagram_title"] = diagram.title
            item["diagram_type"] = diagram.diagram_type
    row = WriteupFinalization(
        org_id=ctx.org_id,
        task_id=task_id,
        artifact_version_id=version.id,
        finalized_by=ctx.actor_user_id,
        finalized_at=datetime.now(timezone.utc),
        content_hash=_content_hash(rows),
        sections=sections,
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        ctx,
        action="artifact.writeup_finalized",
        entity_type="artifact_version",
        entity_id=version.id,
        bid_id=artifact.bid_id,
        after={"sections": len(sections), "statements": len(rows)},
    )
    return {
        "finalized_at": row.finalized_at.isoformat(),
        "sections": len(sections),
        # Not a reason to refuse -- some requirements are legitimately on hold -- but the
        # person finalizing should know how many the response still does not answer.
        "unanswered": [
            c["code"]
            for c in coverage_for(session, ctx, task_id=task_id, rows=rows)
            if c["state"] in ("unanswered", "restated")
        ],
    }


def final_package(session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID) -> dict | None:
    """The latest finalized write-up, with each diagram drawn from the version it used.

    This is what the final report reads: the words as they were finalized and the
    pictures they were written about, not whatever the draft has become since.
    """
    dg._task(session, ctx, task_id)
    row = session.execute(
        select(WriteupFinalization)
        .where(WriteupFinalization.org_id == ctx.org_id, WriteupFinalization.task_id == task_id)
        .order_by(WriteupFinalization.finalized_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    sections = []
    for item in row.sections:
        out = {**item, "diagram": None}
        if item.get("diagram_id") and item.get("diagram_version_no"):
            diagram = dg.get_diagram(session, ctx, uuid.UUID(item["diagram_id"]))
            version = dg.version_by_number(
                session, ctx, diagram_id=diagram.id, version_no=item["diagram_version_no"]
            )
            out["diagram"] = {
                "diagram_id": item["diagram_id"],
                "version_no": version.version_no,
                "title": diagram.title,
                "type": diagram.diagram_type,
                "svg": dg.svg_for(diagram, version),
            }
        sections.append(out)
    return {
        "finalized_at": row.finalized_at.isoformat(),
        "finalized_by": str(row.finalized_by) if row.finalized_by else None,
        "sections": sections,
    }
