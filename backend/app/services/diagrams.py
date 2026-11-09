"""Diagrams for a task's response: deciding what is needed, drawing it, changing it.

The split is the same as everywhere else in the platform. A model proposes; code
decides what is kept.

* A model proposes WHICH diagrams a task needs and whether the sources are
  enough to draw them. A person accepts, declines or adds a type, and a decline
  is remembered.
* A model proposes a STRUCTURE. Code validates it, corrects the claims it
  cannot back (a box said to rest on evidence that is not admissible becomes an
  assumption and is drawn as one), and draws it.
* A model proposes CHANGES and gives advice. A change is saved only when a
  person asked for it; advice is stored as advice and changes nothing until a
  person chooses to apply it.
* Who may edit, what a version is, and whether a save conflicts with someone
  else's are decided here, never by the model.

Every save is a new immutable version. Two people working on the same diagram
cannot overwrite each other: a save names the version it was based on, and if
the diagram has moved on the save is refused with the newer version to compare.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.context import ExecutionContext
from app.core.errors import NotFound, PermissionDenied, StateConflict, ValidationError
from app.core.logging import get_logger
from app.gateway.gateway import ModelGateway, build_gateway
from app.gateway.registry import wrap_untrusted
from app.models import (
    AssessmentEvidence,
    Diagram,
    DiagramNeed,
    DiagramRequirement,
    DiagramVersion,
    Evidence,
    Requirement,
    RequirementAssessment,
    RequirementVersion,
    Task,
    TaskEvidence,
    TaskRequirement,
)
from app.models.enums import (
    ApprovalStatus,
    ClarificationIssueType,
    ClarificationSeverity,
    DiagramNeedStatus,
    DiagramOrigin,
    DiagramType,
    KnowledgeStatus,
    is_self_evidencing,
)
from app.schemas.diagrams import (
    DiagramDraftResult,
    DiagramPlanResult,
    DiagramReviewResult,
    DiagramRevisionResult,
    MissingInformation,
    StyleIntentResult,
)
from app.services import audit, clarifications, tasks
from app.services import diagram_spec as ds
from app.services.requirement_scope import LIVE_REQUIREMENT

log = get_logger("diagrams")

ALLOWED_TYPES = frozenset(t.value for t in DiagramType)
MAX_EVIDENCE = 30


# --- who may do what ----------------------------------------------------------


def _task(session: Session, ctx: ExecutionContext, task_id: uuid.UUID) -> Task:
    task = session.get(Task, task_id)
    if task is None or task.org_id != ctx.org_id:
        raise NotFound("That task does not exist.")
    return task


def _may_edit(session: Session, ctx: ExecutionContext, task: Task) -> None:
    """Only someone on the task changes its diagrams. Reading is a wider question,
    answered by the route's permission."""
    if ctx.actor_kind == "SYSTEM":
        return
    if ctx.actor_user_id is None or not tasks.is_on_task(
        session, ctx, task_id=task.id, user_id=ctx.actor_user_id
    ):
        raise PermissionDenied("Only someone working on this task can change its diagrams.")


def get_diagram(session: Session, ctx: ExecutionContext, diagram_id: uuid.UUID) -> Diagram:
    row = session.get(Diagram, diagram_id)
    if row is None or row.org_id != ctx.org_id:
        raise NotFound("That diagram does not exist.")
    return row


def _version(session: Session, ctx: ExecutionContext, version_id: uuid.UUID) -> DiagramVersion:
    row = session.get(DiagramVersion, version_id)
    if row is None or row.org_id != ctx.org_id:
        raise NotFound("That diagram version does not exist.")
    return row


def current_version(session: Session, ctx: ExecutionContext, diagram: Diagram) -> DiagramVersion:
    if diagram.current_version_id is None:
        raise NotFound("That diagram has no versions yet.")
    return _version(session, ctx, diagram.current_version_id)


# --- what the model is allowed to see -----------------------------------------


def _hash(text: str) -> str:
    return hashlib.sha256(" ".join((text or "").split()).encode()).hexdigest()[:16]


@dataclass
class Sources:
    requirements: dict[str, dict]  # ref T1.. -> {code, text, id}
    evidence: dict[str, Evidence]  # ref E1.. -> row

    @property
    def codes(self) -> dict[str, uuid.UUID]:
        return {v["code"]: uuid.UUID(v["id"]) for v in self.requirements.values()}

    def basis(self) -> dict[str, str]:
        return {v["code"]: _hash(v["text"]) for v in self.requirements.values()}

    def payload(self) -> dict:
        return {
            "tender_sources": [
                {"ref": ref, "code": v["code"], "text": v["text"]}
                for ref, v in self.requirements.items()
            ],
            "evidence_sources": [
                {
                    "ref": ref,
                    "excerpt": (e.excerpt or "")[:900],
                    "document_type": getattr(e.evidence_type, "value", e.evidence_type),
                    "can_prove_capability": _provable(e),
                }
                for ref, e in self.evidence.items()
            ],
        }


def _provable(evidence: Evidence) -> bool:
    """The same test the drafter applies before a sentence may be called a fact."""
    return bool(
        is_self_evidencing(evidence.evidence_type) and evidence.is_substantiated is not False
    )


def gather(session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID) -> Sources:
    """The task's requirements and the company evidence a person may rely on.

    Selection is deterministic, exactly as for drafting: the model is handed a
    closed set and cannot go looking for more.
    """
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
    requirements = {
        f"T{i}": {"code": r.code, "text": v.requirement_text, "id": str(r.id)}
        for i, (r, v) in enumerate(rows, start=1)
    }
    ids = [r.id for r, _ in rows]
    evidence: list[Evidence] = []
    if ids:
        evidence = list(
            session.execute(
                select(Evidence)
                .join(AssessmentEvidence, AssessmentEvidence.evidence_id == Evidence.id)
                .join(
                    RequirementAssessment,
                    RequirementAssessment.id == AssessmentEvidence.assessment_id,
                )
                .where(
                    Evidence.org_id == ctx.org_id,
                    RequirementAssessment.org_id == ctx.org_id,
                    RequirementAssessment.requirement_id.in_(ids),
                    RequirementAssessment.is_current.is_(True),
                    Evidence.approval_status == ApprovalStatus.APPROVED,
                    Evidence.approved_for_reuse.is_(True),
                    Evidence.status == KnowledgeStatus.CURRENT_VERIFIED,
                )
                .distinct()
                .order_by(Evidence.created_at)
                .limit(MAX_EVIDENCE)
            ).scalars()
        )
    seen = {e.id for e in evidence}
    evidence += list(
        session.execute(
            select(Evidence)
            .join(
                TaskEvidence,
                TaskEvidence.document_version_id == Evidence.source_document_version_id,
            )
            .where(
                TaskEvidence.org_id == ctx.org_id,
                TaskEvidence.task_id == task_id,
                Evidence.approval_status == ApprovalStatus.APPROVED,
                Evidence.approved_for_reuse.is_(True),
                Evidence.status == KnowledgeStatus.CURRENT_VERIFIED,
            )
        ).scalars()
    )
    evidence = [e for i, e in enumerate(evidence) if e.id not in seen or i < len(seen)]
    unique: list[Evidence] = []
    ids_seen: set[uuid.UUID] = set()
    for e in evidence:
        if e.id not in ids_seen:
            ids_seen.add(e.id)
            unique.append(e)
    return Sources(
        requirements=requirements,
        evidence={f"E{i}": e for i, e in enumerate(unique[:MAX_EVIDENCE], start=1)},
    )


def _ask(
    gateway: ModelGateway,
    call: str,
    payload: dict,
    *,
    ctx: ExecutionContext,
    session: Session,
    expect: type,
):
    result = gateway.generate_structured(
        call,
        wrap_untrusted(json.dumps(payload, ensure_ascii=False, default=str)),
        ctx=ctx,
        session=session,
    )
    if not isinstance(result, expect):
        raise ValidationError(f"{call} returned an unexpected schema.")
    return result


# --- correcting what a model claims about its own boxes ------------------------


def _correct_claims(
    spec: ds.Spec, sources: Sources, *, person_asked: bool
) -> tuple[ds.Spec, list[str]]:
    """Downgrade every claim of backing that the sources do not bear out.

    The prompt asks the model to say where each box comes from. Asking is not a
    control, so the claims are checked: a box said to rest on evidence that is
    not in the admissible set, or cannot prove anything, is drawn as an
    assumption; one said to come from the tender with no tender reference is too.
    The direction is always toward "not confirmed", never away from it.
    """
    notes: list[str] = []
    t_refs = set(sources.requirements)
    e_ok = {ref for ref, e in sources.evidence.items() if _provable(e)}

    def fix(item, label: str, *, is_node: bool):
        refs = [r for r in item.sources if r in t_refs or r in sources.evidence]
        item.sources = refs
        basis = item.basis
        if basis == "EVIDENCE" and not (set(refs) & e_ok):
            item.basis = "ASSUMPTION"
            notes.append(f"'{label}' cited no evidence that can prove it; shown as an assumption.")
        elif basis == "TENDER" and is_node and not (set(refs) & t_refs):
            if getattr(item, "kind", None) != "start_end":
                item.basis = "ASSUMPTION"
                notes.append(f"'{label}' cites no tender wording; shown as an assumption.")
        elif basis == "PERSON" and not person_asked:
            item.basis = "ASSUMPTION"
            notes.append(f"'{label}' was not asked for by anyone; shown as an assumption.")

    labels = {n.id: n.label for n in spec.nodes}
    for node in spec.nodes:
        fix(node, node.label, is_node=True)
    for edge in spec.edges:
        fix(edge, f"{labels[edge.source]} -> {labels[edge.target]}", is_node=False)
    return spec, notes


def _validated(raw: object, sources: Sources, *, person_asked: bool) -> tuple[ds.Spec, list[str]]:
    try:
        spec = ds.parse_spec(raw)
    except ds.SpecError as exc:
        raise ValidationError(f"The proposed diagram cannot be drawn: {exc}") from exc
    spec, tidy_notes = ds.tidy(spec)
    spec, claim_notes = _correct_claims(spec, sources, person_asked=person_asked)
    return spec, [*tidy_notes, *claim_notes]


def _crowded(spec: ds.Spec, previous_nodes: int | None) -> list[str]:
    """Reasons a proposal would draw as a tangle. A change that does not make an
    already large diagram larger is not held to the limit: the size was not its doing."""
    report = ds.readability(spec)
    if previous_nodes is not None and len(spec.nodes) <= previous_nodes:
        return []
    return report["issues"]


def _with_one_retry(
    produce, sources: Sources, *, person_asked: bool, previous_nodes: int | None = None
):
    """Ask once more, with the reason, when a proposal cannot be drawn or would be a tangle.

    Two different failures share the one retry: a structure that is invalid, and one
    that is valid but too crowded to read. In the second case, if the second attempt
    is no better the first is kept and the person is told it is dense.
    """
    try:
        first = produce(None)
        if first is None:
            return None
        checked = _validated(first[0], sources, person_asked=person_asked)
    except ValidationError as exc:
        second = produce(str(exc))
        if second is None:
            return None
        return second, _validated(second[0], sources, person_asked=person_asked)

    issues = _crowded(checked[0], previous_nodes)
    if not issues:
        return first, checked
    second = produce(
        "That would draw as a tangle: "
        + "; ".join(issues)
        + ". Redraw it simpler: merge related boxes, drop minor detail, and name the detail "
        "diagrams that could follow instead of drawing them here."
    )
    if second is not None:
        try:
            better = _validated(second[0], sources, person_asked=person_asked)
        except ValidationError:
            better = None
        if better is not None and len(_crowded(better[0], previous_nodes)) < len(issues):
            return second, better
    spec, notes = checked
    notes = [
        *notes,
        f"This is still dense ({len(spec.nodes)} boxes). Ask me to simplify it, or to draw "
        "part of it as a separate diagram.",
    ]
    return first, (spec, notes)


# --- versions -------------------------------------------------------------------


def _save_version(
    session: Session,
    ctx: ExecutionContext,
    diagram: Diagram,
    *,
    spec: ds.Spec,
    style: ds.Style,
    origin: DiagramOrigin,
    sources: Sources | None,
    instruction: str | None = None,
    extra_notes: list[str] | None = None,
    could_not: list[dict] | None = None,
    previous: DiagramVersion | None = None,
) -> DiagramVersion:
    number = (
        session.execute(
            select(func.max(DiagramVersion.version_no)).where(
                DiagramVersion.org_id == ctx.org_id, DiagramVersion.diagram_id == diagram.id
            )
        ).scalar_one()
        or 0
    ) + 1
    old_spec = ds.parse_spec(previous.spec) if previous is not None else None
    summary = ds.describe_changes(old_spec, spec)
    if extra_notes:
        summary = [*summary, *extra_notes]
    if sources is not None:
        basis = sources.basis()
    else:
        basis = dict(previous.basis) if previous is not None else {}
    version = DiagramVersion(
        org_id=ctx.org_id,
        diagram_id=diagram.id,
        version_no=number,
        spec=spec.model_dump(),
        style=style.model_dump(),
        origin=origin.value,
        instruction=instruction,
        change_summary="\n".join(summary),
        could_not=could_not or None,
        unconfirmed=ds.unconfirmed_elements(spec) or None,
        basis=basis,
        created_by=ctx.actor_user_id,
    )
    session.add(version)
    session.flush()
    diagram.current_version_id = version.id
    session.flush()
    audit.record(
        session,
        ctx,
        action=f"diagram.{origin.value.lower()}",
        entity_type="diagram",
        entity_id=diagram.id,
        bid_id=diagram.bid_id,
        after={"version_no": number, "origin": origin.value, "changes": summary[:20]},
    )
    return version


def _require_base(diagram: Diagram, current: DiagramVersion, base_version_no: int) -> None:
    if base_version_no != current.version_no:
        raise StateConflict(
            f"Someone saved version {current.version_no} of this diagram after you opened "
            f"version {base_version_no}. Look at their version and make your change on top of it; "
            "nothing was overwritten."
        )


# --- needs ----------------------------------------------------------------------


def needs_for_task(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID
) -> list[DiagramNeed]:
    return list(
        session.execute(
            select(DiagramNeed)
            .where(DiagramNeed.org_id == ctx.org_id, DiagramNeed.task_id == task_id)
            .order_by(DiagramNeed.diagram_type)
        ).scalars()
    )


def _clean_missing(items: list[MissingInformation], sources: Sources) -> list[dict]:
    known = set(sources.codes)
    out = []
    for item in items:
        codes = [c for c in item.requirement_codes if c in known]
        out.append(
            {
                "what": item.what,
                "why": item.why,
                # Only codes that exist on this task are kept. A model that
                # names a requirement nobody wrote is inventing a reference.
                "requirement_codes": codes,
                "suggested_question": None if codes else item.suggested_question,
            }
        )
    return out


def plan(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    gateway: ModelGateway | None = None,
) -> list[DiagramNeed]:
    """Ask which diagrams the task needs. Suggestions only; a person decides."""
    task = _task(session, ctx, task_id)
    _may_edit(session, ctx, task)
    sources = gather(session, ctx, task_id=task_id)
    if not sources.requirements:
        raise ValidationError("This task covers no requirements, so there is nothing to draw for.")
    basis = sources.basis()

    existing = {n.diagram_type: n for n in needs_for_task(session, ctx, task_id=task_id)}
    declined = []
    for need in existing.values():
        if need.status != DiagramNeedStatus.DECLINED.value:
            continue
        # A decline stands until the wording behind it changes.
        if need.decided_basis == basis:
            declined.append({"diagram_type": need.diagram_type, "reason": need.decline_reason})
    diagrams = list(
        session.execute(
            select(Diagram).where(
                Diagram.org_id == ctx.org_id,
                Diagram.task_id == task_id,
                Diagram.archived.is_(False),
            )
        ).scalars()
    )
    payload = {
        "task": {"code": task.code, "title": task.title},
        **sources.payload(),
        "existing_diagrams": [{"type": d.diagram_type, "title": d.title} for d in diagrams],
        "declined_types": declined,
    }
    result: DiagramPlanResult = _ask(
        gateway or build_gateway(),
        "diagram_planning",
        payload,
        ctx=ctx,
        session=session,
        expect=DiagramPlanResult,
    )
    declined_types = {d["diagram_type"] for d in declined}
    for proposal in result.needs:
        kind = proposal.diagram_type.strip().upper()
        # A model may propose from the list; it may not invent a type.
        if kind not in ALLOWED_TYPES or kind in declined_types:
            continue
        row = existing.get(kind)
        missing = _clean_missing(proposal.missing, sources)
        enough = bool(proposal.enough_information) and not missing
        if row is None:
            row = DiagramNeed(
                org_id=ctx.org_id,
                task_id=task_id,
                diagram_type=kind,
                status=DiagramNeedStatus.SUGGESTED.value,
                source="AI",
            )
            session.add(row)
        elif row.status == DiagramNeedStatus.DECLINED.value:
            # The wording changed since the decline: put the question back.
            row.status = DiagramNeedStatus.SUGGESTED.value
        row.why = proposal.why
        row.enough_information = enough
        row.missing = missing or None
    session.flush()
    audit.record(
        session,
        ctx,
        action="diagram.planned",
        entity_type="task",
        entity_id=task_id,
        bid_id=task.bid_id,
        after={"proposed": [n.diagram_type for n in result.needs]},
    )
    return needs_for_task(session, ctx, task_id=task_id)


def decide_need(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    diagram_type: str,
    accept: bool,
    reason: str | None = None,
) -> DiagramNeed:
    """Accept a suggested type, or decline it with a reason that is remembered."""
    task = _task(session, ctx, task_id)
    _may_edit(session, ctx, task)
    kind = diagram_type.strip().upper()
    if kind not in ALLOWED_TYPES:
        raise ValidationError(f"Unknown diagram type '{diagram_type}'.")
    if not accept and not (reason or "").strip():
        raise ValidationError("Say why it is not needed, so it is not suggested again.")
    row = session.execute(
        select(DiagramNeed).where(
            DiagramNeed.org_id == ctx.org_id,
            DiagramNeed.task_id == task_id,
            DiagramNeed.diagram_type == kind,
        )
    ).scalar_one_or_none()
    if row is None:
        row = DiagramNeed(
            org_id=ctx.org_id,
            task_id=task_id,
            diagram_type=kind,
            status=DiagramNeedStatus.SUGGESTED.value,
            source="PERSON",
        )
        session.add(row)
    sources = gather(session, ctx, task_id=task_id)
    row.status = (DiagramNeedStatus.ACCEPTED if accept else DiagramNeedStatus.DECLINED).value
    row.decline_reason = None if accept else (reason or "").strip()
    row.decided_basis = None if accept else sources.basis()
    row.decided_by = ctx.actor_user_id
    session.flush()
    audit.record(
        session,
        ctx,
        action="diagram.need_accepted" if accept else "diagram.need_declined",
        entity_type="task",
        entity_id=task_id,
        bid_id=task.bid_id,
        after={"diagram_type": kind, "reason": reason},
    )
    return row


# --- creating, changing, restyling ------------------------------------------------


def _requirement_ids(sources: Sources, codes: list[str] | None) -> list[uuid.UUID]:
    known = sources.codes
    chosen = [known[c] for c in (codes or []) if c in known]
    return chosen or list(known.values())


def create(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    diagram_type: str,
    note: str | None = None,
    title: str | None = None,
    gateway: ModelGateway | None = None,
) -> dict:
    """Ask for a diagram of one type. Either a diagram, or a list of what is missing."""
    task = _task(session, ctx, task_id)
    _may_edit(session, ctx, task)
    kind = diagram_type.strip().upper()
    if kind not in ALLOWED_TYPES:
        raise ValidationError(f"Unknown diagram type '{diagram_type}'.")
    sources = gather(session, ctx, task_id=task_id)
    if not sources.requirements:
        raise ValidationError("This task covers no requirements, so there is nothing to draw for.")
    model = gateway or build_gateway()
    person_asked = bool((note or "").strip())

    def produce(previous_error: str | None):
        payload = {
            "diagram_type": kind,
            "task": {"code": task.code, "title": task.title},
            **sources.payload(),
            "person_note": note or "",
        }
        if previous_error:
            payload["your_previous_attempt_failed_because"] = previous_error
        result: DiagramDraftResult = _ask(
            model,
            "diagram_generation",
            payload,
            ctx=ctx,
            session=session,
            expect=DiagramDraftResult,
        )
        if not result.enough_information or result.spec is None:
            produce.declined = result  # type: ignore[attr-defined]
            return None
        produce.declined = None  # type: ignore[attr-defined]
        return result.spec, result

    produce.declined = None  # type: ignore[attr-defined]
    outcome = _with_one_retry(produce, sources, person_asked=person_asked)

    need = session.execute(
        select(DiagramNeed).where(
            DiagramNeed.org_id == ctx.org_id,
            DiagramNeed.task_id == task_id,
            DiagramNeed.diagram_type == kind,
        )
    ).scalar_one_or_none()

    if outcome is None:
        result = produce.declined  # type: ignore[attr-defined]
        missing = _clean_missing(result.missing, sources) if result is not None else []
        if need is None:
            need = DiagramNeed(
                org_id=ctx.org_id,
                task_id=task_id,
                diagram_type=kind,
                status=DiagramNeedStatus.ACCEPTED.value,
                source="PERSON",
            )
            session.add(need)
        need.enough_information = False
        need.missing = missing or [
            {
                "what": "The model could not say what is missing.",
                "why": "It did not return a diagram or a reason.",
                "requirement_codes": [],
                "suggested_question": None,
            }
        ]
        session.flush()
        return {"created": False, "missing": need.missing, "diagram_id": None}

    (raw, result), (spec, notes) = outcome
    diagram = Diagram(
        org_id=ctx.org_id,
        bid_id=task.bid_id,
        task_id=task_id,
        diagram_type=kind,
        title=(title or result.title or f"{kind.replace('_', ' ').title()} diagram")[:300],
        owner_user_id=ctx.actor_user_id,
    )
    session.add(diagram)
    session.flush()
    for requirement_id in _requirement_ids(sources, result.requirement_codes):
        session.add(
            DiagramRequirement(
                org_id=ctx.org_id, diagram_id=diagram.id, requirement_id=requirement_id
            )
        )
    session.flush()
    version = _save_version(
        session,
        ctx,
        diagram,
        spec=spec,
        style=ds.Style(),
        origin=DiagramOrigin.AI_GENERATED,
        sources=sources,
        instruction=note,
        extra_notes=notes,
    )
    if need is None:
        need = DiagramNeed(
            org_id=ctx.org_id,
            task_id=task_id,
            diagram_type=kind,
            status=DiagramNeedStatus.ACCEPTED.value,
            source="PERSON",
        )
        session.add(need)
    need.status = DiagramNeedStatus.ACCEPTED.value
    need.enough_information = True
    need.missing = None
    session.flush()
    return {
        "created": True,
        "diagram_id": diagram.id,
        "version_no": version.version_no,
        "missing": [],
        "detail": list(result.detail_diagrams or []),
    }


def revise(
    session: Session,
    ctx: ExecutionContext,
    *,
    diagram_id: uuid.UUID,
    instruction: str,
    base_version_no: int,
    gateway: ModelGateway | None = None,
    requirement_changes: list[dict] | None = None,
) -> dict:
    """Change a diagram as a person asked, and say what could not be done."""
    if not (instruction or "").strip():
        raise ValidationError("Say what should change.")
    diagram = get_diagram(session, ctx, diagram_id)
    task = _task(session, ctx, diagram.task_id)
    _may_edit(session, ctx, task)
    current = current_version(session, ctx, diagram)
    _require_base(diagram, current, base_version_no)
    sources = gather(session, ctx, task_id=task.id)
    model = gateway or build_gateway()

    holder: dict = {}

    def produce(previous_error: str | None):
        payload = {
            "diagram_type": diagram.diagram_type,
            "current_diagram": current.spec,
            "instruction": instruction,
            **sources.payload(),
        }
        if requirement_changes:
            payload["requirement_changes"] = requirement_changes
        if previous_error:
            payload["your_previous_attempt_failed_because"] = previous_error
        result: DiagramRevisionResult = _ask(
            model,
            "diagram_revision",
            payload,
            ctx=ctx,
            session=session,
            expect=DiagramRevisionResult,
        )
        holder["result"] = result
        if result.spec is None:
            return None
        return result.spec, result

    outcome = _with_one_retry(
        produce,
        sources,
        person_asked=True,
        previous_nodes=len(ds.parse_spec(current.spec).nodes),
    )
    result = holder.get("result")
    could_not = [c.model_dump() for c in (result.could_not if result else [])]
    if outcome is None:
        return {
            "changed": False,
            "could_not": could_not,
            "missing": _clean_missing(result.missing, sources) if result else [],
            "version_no": current.version_no,
        }
    (_raw, _result), (spec, notes) = outcome
    version = _save_version(
        session,
        ctx,
        diagram,
        spec=spec,
        style=ds.parse_style(current.style),
        origin=DiagramOrigin.AI_REVISED,
        sources=sources,
        instruction=instruction,
        extra_notes=notes,
        could_not=could_not,
        previous=current,
    )
    return {
        "changed": True,
        "version_no": version.version_no,
        "could_not": could_not,
        "missing": _clean_missing(result.missing, sources) if result else [],
    }


def restyle(
    session: Session,
    ctx: ExecutionContext,
    *,
    diagram_id: uuid.UUID,
    wish: str,
    base_version_no: int,
    gateway: ModelGateway | None = None,
) -> dict:
    """Change how a diagram looks, from words. What it says is untouched."""
    if not (wish or "").strip():
        raise ValidationError("Describe how it should look.")
    diagram = get_diagram(session, ctx, diagram_id)
    task = _task(session, ctx, diagram.task_id)
    _may_edit(session, ctx, task)
    current = current_version(session, ctx, diagram)
    _require_base(diagram, current, base_version_no)
    result: StyleIntentResult = _ask(
        gateway or build_gateway(),
        "diagram_style_intent",
        {"current_style": current.style, "wish": wish},
        ctx=ctx,
        session=session,
        expect=StyleIntentResult,
    )
    merged = {**current.style, **(result.style or {})}
    try:
        style = ds.parse_style(merged)
    except ds.SpecError:
        # A value outside the closed set: keep what is valid, drop the rest.
        keep = {}
        for key, value in (result.style or {}).items():
            try:
                ds.parse_style({**current.style, key: value})
                keep[key] = value
            except ds.SpecError:
                continue
        style = ds.parse_style({**current.style, **keep})
    if style.model_dump() == current.style:
        return {
            "changed": False,
            "understood": result.understood,
            "could_not": result.could_not,
            "version_no": current.version_no,
        }
    spec = ds.parse_spec(current.spec)
    version = _save_version(
        session,
        ctx,
        diagram,
        spec=spec,
        style=style,
        origin=DiagramOrigin.STYLE_CHANGED,
        sources=None,
        instruction=wish,
        extra_notes=[f"Look changed: {result.understood}"] if result.understood else None,
        could_not=[
            {"request": t, "reason": "Not something the style options can express."}
            for t in result.could_not
        ]
        or None,
        previous=current,
    )
    # Style changes do not alter what is said, so the change list is the style line only.
    version.change_summary = f"Look changed: {result.understood or 'style updated'}"
    session.flush()
    return {
        "changed": True,
        "understood": result.understood,
        "could_not": result.could_not,
        "version_no": version.version_no,
    }


def edit(
    session: Session,
    ctx: ExecutionContext,
    *,
    diagram_id: uuid.UUID,
    spec: object,
    base_version_no: int,
    change_reason: str,
) -> DiagramVersion:
    """Save a person's own edit to the structure. No model is involved."""
    if not (change_reason or "").strip():
        raise ValidationError("Say why you are changing it; the next reader sees this.")
    diagram = get_diagram(session, ctx, diagram_id)
    task = _task(session, ctx, diagram.task_id)
    _may_edit(session, ctx, task)
    current = current_version(session, ctx, diagram)
    _require_base(diagram, current, base_version_no)
    try:
        new_spec = ds.parse_spec(spec)
    except ds.SpecError as exc:
        raise ValidationError(f"That diagram cannot be drawn: {exc}") from exc
    old = ds.parse_spec(current.spec)
    # What a person adds by hand is theirs; what they leave alone keeps its basis.
    old_by_id = {n.id: n for n in old.nodes}
    for node in new_spec.nodes:
        before = old_by_id.get(node.id)
        if before is None or before.label != node.label:
            node.basis = "PERSON"
            node.sources = []
    return _save_version(
        session,
        ctx,
        diagram,
        spec=new_spec,
        style=ds.parse_style(current.style),
        origin=DiagramOrigin.HUMAN_EDITED,
        sources=None,
        instruction=change_reason,
        previous=current,
    )


def import_mermaid(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    text: str,
    title: str | None = None,
) -> Diagram:
    """Bring in a diagram someone already has as Mermaid text. Exact, no model."""
    task = _task(session, ctx, task_id)
    _may_edit(session, ctx, task)
    try:
        kind, spec = ds.parse_mermaid(text)
    except ds.SpecError as exc:
        raise ValidationError(f"That Mermaid text could not be read: {exc}") from exc
    sources = gather(session, ctx, task_id=task_id)
    diagram = Diagram(
        org_id=ctx.org_id,
        bid_id=task.bid_id,
        task_id=task_id,
        diagram_type=kind,
        title=(title or f"Imported {kind.lower()} diagram")[:300],
        owner_user_id=ctx.actor_user_id,
    )
    session.add(diagram)
    session.flush()
    for requirement_id in sources.codes.values():
        session.add(
            DiagramRequirement(
                org_id=ctx.org_id, diagram_id=diagram.id, requirement_id=requirement_id
            )
        )
    session.flush()
    _save_version(
        session,
        ctx,
        diagram,
        spec=spec,
        style=ds.Style(),
        origin=DiagramOrigin.HUMAN_EDITED,
        sources=sources,
        instruction="Imported from Mermaid text.",
    )
    return diagram


_IMAGE_KINDS = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"RIFF": "image/webp",
}


def _sniff_image(data: bytes) -> str:
    for magic, mime in _IMAGE_KINDS.items():
        if data.startswith(magic):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    raise ValidationError("Only PNG, JPEG or WebP pictures can be read.")


def import_image(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    image: bytes,
    use_vision: bool,
    title: str | None = None,
    gateway: ModelGateway | None = None,
) -> dict:
    """Read a picture of a diagram into a structure a person can then edit.

    The picture leaves the platform only if the person says so for this picture:
    `use_vision` must be true. What comes back is checked exactly like any other
    proposal, every box is marked as the person's own, and anything the model
    could not read is listed rather than guessed.
    """
    task = _task(session, ctx, task_id)
    _may_edit(session, ctx, task)
    if not use_vision:
        raise ValidationError(
            "Reading a picture sends it to a vision model outside the platform. Confirm that "
            "for this picture, or paste the diagram as Mermaid instead."
        )
    if len(image) > get_settings().vision_max_image_bytes:
        raise ValidationError("That picture is too large to read.")
    mime = _sniff_image(image)
    model = gateway or build_gateway()
    reading = model.read_image(
        "diagram_image_reading",
        "Read this diagram.",
        image,
        mime,
        ctx=ctx,
        session=session,
    )
    if not reading.is_a_diagram or reading.spec is None:
        return {"created": False, "reason": "The picture does not look like a diagram."}
    try:
        spec = ds.parse_spec(reading.spec)
    except ds.SpecError as exc:
        raise ValidationError(f"The picture could not be turned into a diagram: {exc}") from exc
    # Everything on it is what the person supplied; none of it is a claim the
    # platform has checked against the tender or the evidence.
    spec = ds.Spec.model_validate(
        {
            **spec.model_dump(),
            "nodes": [{**n.model_dump(), "basis": "PERSON", "sources": []} for n in spec.nodes],
        }
    )
    kind = (
        reading.diagram_type if reading.diagram_type in {t.value for t in DiagramType} else "FLOW"
    )
    sources = gather(session, ctx, task_id=task_id)
    diagram = Diagram(
        org_id=ctx.org_id,
        bid_id=task.bid_id,
        task_id=task_id,
        diagram_type=kind,
        title=(title or reading.title or "Diagram from a picture")[:300],
        owner_user_id=ctx.actor_user_id,
    )
    session.add(diagram)
    session.flush()
    for requirement_id in sources.codes.values():
        session.add(
            DiagramRequirement(
                org_id=ctx.org_id, diagram_id=diagram.id, requirement_id=requirement_id
            )
        )
    session.flush()
    version = _save_version(
        session,
        ctx,
        diagram,
        spec=spec,
        style=ds.Style(),
        origin=DiagramOrigin.IMAGE_IMPORTED,
        sources=sources,
        instruction="Read from a picture with a vision model.",
        extra_notes=[f"Could not read with confidence: {u}" for u in reading.unclear],
    )
    audit.record(
        session,
        ctx,
        action="diagram.image_sent_to_vision_model",
        entity_type="diagram",
        entity_id=diagram.id,
        bid_id=task.bid_id,
        after={
            "sha256": hashlib.sha256(image).hexdigest(),
            "bytes": len(image),
            "mime": mime,
            "model": getattr(getattr(model, "vision", None), "model_id", "unknown"),
        },
    )
    return {
        "created": True,
        "diagram_id": diagram.id,
        "version_no": version.version_no,
        "unclear": reading.unclear,
    }


# --- advice ------------------------------------------------------------------------


def review(
    session: Session,
    ctx: ExecutionContext,
    *,
    diagram_id: uuid.UUID,
    focus: str | None = None,
    gateway: ModelGateway | None = None,
) -> list[dict]:
    """Suggestions for improving a diagram. Advice only: the diagram is not touched."""
    diagram = get_diagram(session, ctx, diagram_id)
    task = _task(session, ctx, diagram.task_id)
    _may_edit(session, ctx, task)
    current = current_version(session, ctx, diagram)
    sources = gather(session, ctx, task_id=task.id)
    result: DiagramReviewResult = _ask(
        gateway or build_gateway(),
        "diagram_review",
        {
            "diagram_type": diagram.diagram_type,
            "diagram": current.spec,
            "focus": focus or "",
            **sources.payload(),
        },
        ctx=ctx,
        session=session,
        expect=DiagramReviewResult,
    )
    known = set(sources.codes)
    notes = [
        {
            "severity": s.severity,
            "text": s.text,
            "boxes": s.boxes,
            "requirement_codes": [c for c in s.requirement_codes if c in known],
            "needs_information": s.needs_information,
        }
        for s in result.suggestions
    ]
    # Stored on the version it reviewed. Writing advice is not a change to the
    # diagram, so this is the one field on a version that may be updated.
    current.review_notes = [*notes, {"verdict": result.verdict}] if result.verdict else notes
    session.flush()
    audit.record(
        session,
        ctx,
        action="diagram.reviewed",
        entity_type="diagram",
        entity_id=diagram.id,
        bid_id=diagram.bid_id,
        after={"version_no": current.version_no, "suggestions": len(notes)},
    )
    return current.review_notes or []


def apply_suggestions(
    session: Session,
    ctx: ExecutionContext,
    *,
    diagram_id: uuid.UUID,
    suggestion_indexes: list[int],
    base_version_no: int,
    gateway: ModelGateway | None = None,
) -> dict:
    """Carry out the suggestions a person picked. Nothing is applied until they ask."""
    diagram = get_diagram(session, ctx, diagram_id)
    current = current_version(session, ctx, diagram)
    notes = [n for n in (current.review_notes or []) if "text" in n]
    chosen = []
    for i in suggestion_indexes:
        if i < 0 or i >= len(notes):
            raise ValidationError("That suggestion does not exist any more; review again.")
        if notes[i].get("needs_information"):
            raise ValidationError(
                "That suggestion needs information the sources do not contain. Add it, or "
                "raise it as a question, then ask again."
            )
        chosen.append(notes[i]["text"])
    if not chosen:
        raise ValidationError("Pick at least one suggestion to apply.")
    instruction = "Apply these reviewed suggestions, and nothing else:\n- " + "\n- ".join(chosen)
    return revise(
        session,
        ctx,
        diagram_id=diagram_id,
        instruction=instruction,
        base_version_no=base_version_no,
        gateway=gateway,
    )


# --- keeping up with changes -----------------------------------------------------------


def staleness(session: Session, ctx: ExecutionContext, diagram: Diagram) -> dict:
    """Has anything this diagram answers changed since its latest version was made?

    Worked out on every read from the requirement wording, so it cannot be
    forgotten by a hook that did not fire: the diagram is stale when the words it
    was built from are no longer the words on the requirement.
    """
    version = current_version(session, ctx, diagram)
    linked = session.execute(
        select(Requirement, RequirementVersion)
        .join(DiagramRequirement, DiagramRequirement.requirement_id == Requirement.id)
        .outerjoin(RequirementVersion, RequirementVersion.id == Requirement.current_version_id)
        .where(DiagramRequirement.org_id == ctx.org_id, DiagramRequirement.diagram_id == diagram.id)
    ).all()
    changed: list[dict] = []
    for requirement, rv in linked:
        then = (version.basis or {}).get(requirement.code)
        withdrawn = requirement.lifecycle_state.value == "WITHDRAWN"
        now = None if withdrawn or rv is None else _hash(rv.requirement_text)
        if withdrawn and then is not None:
            changed.append({"code": requirement.code, "change": "WITHDRAWN"})
        elif then is not None and now is not None and then != now:
            changed.append(
                {"code": requirement.code, "change": "REWORDED", "text": rv.requirement_text}
            )
    return {"stale": bool(changed), "changed": changed}


def refresh(
    session: Session,
    ctx: ExecutionContext,
    *,
    diagram_id: uuid.UUID,
    base_version_no: int,
    gateway: ModelGateway | None = None,
) -> dict:
    """Bring a diagram up to date with requirement changes -- only what they affect."""
    diagram = get_diagram(session, ctx, diagram_id)
    state = staleness(session, ctx, diagram)
    if not state["stale"]:
        return {"changed": False, "could_not": [], "missing": [], "note": "Nothing has changed."}
    return revise(
        session,
        ctx,
        diagram_id=diagram_id,
        instruction=(
            "The requirements behind this diagram changed. Update only the parts they affect "
            "and leave everything else exactly as it is."
        ),
        base_version_no=base_version_no,
        gateway=gateway,
        requirement_changes=state["changed"],
    )


# --- missing information becomes a question, when a person says so ---------------------


def raise_question(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    what: str,
    why: str | None,
    question: str,
    requirement_codes: list[str] | None = None,
):
    """Record missing information as an open question for the customer.

    Only on a person's say-so: the model reports a gap, it does not open
    questions with the customer. Repeating the same request finds the same
    question rather than creating another.
    """
    task = _task(session, ctx, task_id)
    _may_edit(session, ctx, task)
    if not (question or "").strip():
        raise ValidationError("A question needs its wording.")
    sources = gather(session, ctx, task_id=task_id)
    ids = [sources.codes[c] for c in (requirement_codes or []) if c in sources.codes]
    fingerprint = "diagram:" + _hash(f"{task.bid_id}|{what}")
    return clarifications.raise_issue(
        session,
        ctx,
        bid_id=task.bid_id,
        issue_type=ClarificationIssueType.INCOMPLETE_SCOPE,
        description=(
            f"A diagram for {task.code} cannot be drawn without: {what}. {why or ''}"
        ).strip(),
        severity=ClarificationSeverity.MINOR,
        fingerprint=fingerprint,
        detected_by="diagram_assistant@v1",
        recommended_question=question.strip(),
        requirement_ids=ids,
    )


# --- reading ------------------------------------------------------------------------------


def archive(session: Session, ctx: ExecutionContext, *, diagram_id: uuid.UUID) -> Diagram:
    """Take a diagram off the task. Its versions are kept; it just stops being listed."""
    diagram = get_diagram(session, ctx, diagram_id)
    _may_edit(session, ctx, _task(session, ctx, diagram.task_id))
    diagram.archived = True
    session.flush()
    audit.record(
        session,
        ctx,
        action="diagram.archived",
        entity_type="diagram",
        entity_id=diagram.id,
        bid_id=diagram.bid_id,
        after={"title": diagram.title},
    )
    return diagram


def set_left_out(
    session: Session, ctx: ExecutionContext, *, diagram_id: uuid.UUID, left_out: bool
) -> Diagram:
    """Keep a diagram on the task but out of (or back in) the written response."""
    diagram = get_diagram(session, ctx, diagram_id)
    _may_edit(session, ctx, _task(session, ctx, diagram.task_id))
    if diagram.left_out != left_out:
        diagram.left_out = left_out
        session.flush()
        audit.record(
            session,
            ctx,
            action="diagram.left_out" if left_out else "diagram.included",
            entity_type="diagram",
            entity_id=diagram.id,
            bid_id=diagram.bid_id,
            after={"title": diagram.title},
        )
    return diagram


def diagrams_for_task(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID
) -> list[Diagram]:
    return list(
        session.execute(
            select(Diagram)
            .where(
                Diagram.org_id == ctx.org_id,
                Diagram.task_id == task_id,
                Diagram.archived.is_(False),
            )
            .order_by(Diagram.created_at)
        ).scalars()
    )


def versions(session: Session, ctx: ExecutionContext, *, diagram_id: uuid.UUID):
    get_diagram(session, ctx, diagram_id)
    return list(
        session.execute(
            select(DiagramVersion)
            .where(DiagramVersion.org_id == ctx.org_id, DiagramVersion.diagram_id == diagram_id)
            .order_by(DiagramVersion.version_no)
        ).scalars()
    )


def version_by_number(
    session: Session, ctx: ExecutionContext, *, diagram_id: uuid.UUID, version_no: int
) -> DiagramVersion:
    get_diagram(session, ctx, diagram_id)
    row = session.execute(
        select(DiagramVersion).where(
            DiagramVersion.org_id == ctx.org_id,
            DiagramVersion.diagram_id == diagram_id,
            DiagramVersion.version_no == version_no,
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("That version does not exist.")
    return row


def svg_for(diagram: Diagram, version: DiagramVersion) -> str:
    return ds.render(
        diagram.diagram_type,
        ds.parse_spec(version.spec),
        ds.parse_style(version.style),
        title=diagram.title,
    )
