"""Permission-aware context construction.

This is the module that decides what the copilot is allowed to see, and it is
the single most security-sensitive piece of the product. Four rules govern it.

**Nothing is trusted from cache.** The task's stored `retrieval_scope` says what
the task may *ever* see. What it sees on this message is that ceiling narrowed
by the caller's live roles and live grants, evaluated now. A grant revoked a
second ago is gone from this call.

**The model does not retrieve.** It has no tools and no query path. The context
block is assembled here, before the call, and is the entirety of what the model
can draw on. That is why prompt injection in an uploaded document cannot widen
access: the document text arrives as data inside an already-bounded block, and
nothing it says can add a source to that block.

**Refusal is informative.** When nothing admissible exists, the answer is not a
flat "no". It names the departments that likely hold the material and offers the
three request types in least-privilege order, because a contributor who cannot
proceed still needs a route forward.

**Everything is traced.** The returned `retrieval_trace` records the query, the
filters applied, how many chunks were admissible before ranking, and which
candidates survived. A leak that cannot be investigated afterwards is a leak
that will be denied rather than fixed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, PermissionDenied
from app.core.logging import get_logger
from app.models import (
    AccessGrant,
    Department,
    Evidence,
    Requirement,
    RequirementAssessment,
    RequirementVersion,
    Task,
)
from app.models.enums import ConfidentialityClass, DocumentSide, is_self_evidencing
from app.services import tasks as task_service
from app.services.citations import ANSWERING_KEYS, CONTEXT_KINDS
from app.services.retrieval import RetrievalScope, RetrievalService

log = get_logger("context")

MAX_RETRIEVED = 8
MAX_REQUIREMENTS = 25

REQUEST_LADDER = [
    {
        "request_type": "REQUEST_ANSWER",
        "grants_access": False,
        "describe": "Ask the owning department to answer the question directly.",
    },
    {
        "request_type": "REQUEST_EVIDENCE",
        "grants_access": True,
        "describe": "Ask them to share specific evidence items with this task only.",
    },
    {
        "request_type": "REQUEST_ACCESS",
        "grants_access": True,
        "describe": "Ask for task-scoped, expiring read access to the resource itself.",
    },
]


@dataclass
class Source:
    """One citable item in the authorized context."""

    ref: str
    kind: str
    id: uuid.UUID
    text: str
    locator: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ref": self.ref,
            "kind": self.kind,
            "id": str(self.id),
            "locator": self.locator,
        }


@dataclass
class TaskContext:
    task: Task
    sources: list[Source]
    requirement_block: str
    retrieved_block: str
    evidence_block: str
    resolution_block: str
    trace: dict
    # Which evidence row each [E#] in the prompt refers to. Without it an answer
    # can cite [E1] and the system has no idea what was cited -- which is how a
    # well-grounded answer came to display as "no sources cited".
    evidence_refs: dict[str, uuid.UUID] = field(default_factory=dict)
    # [R#] -> the cross-department request whose answer it is. Same purpose as
    # evidence_refs: without it a citation is a label with nothing behind it.
    resolution_refs: dict[str, uuid.UUID] = field(default_factory=dict)
    out_of_scope: bool = False
    # WHY there is nothing to say, which is not the same question as whether
    # there is. Conflating the two produced a refusal that told a contributor
    # she was not authorized to see her own department's evidence when the truth
    # was that her wording matched nothing -- and sent her to raise an access
    # request that would have been answered "you already have it".
    refusal_kind: str = ""  # "NOT_PERMITTED" | "NO_MATCH" | ""
    # {code, id, name, why} each -- see _suggested for why the id is needed.
    suggested_departments: list[dict] = field(default_factory=list)

    def prompt_block(self) -> str:
        """Everything the model may use, under one heading that says so.

        The heading matters more than it looks. `COPILOT_RULE` says "answer ONLY
        from the AUTHORIZED CONTEXT", and this block used to put that heading on
        the query-time search results alone, with approved evidence sitting
        above it under a heading of its own. So the model read the evidence,
        correctly concluded it was not authorized context, and refused --
        quoting the very sentence that answered the question while declining to
        use it. It was obeying the instructions exactly; the instructions were
        wrong.

        That fix was made for evidence and not for requirements, which kept
        being rendered above the heading for months afterwards. The cost showed
        up as the copilot denying that any contact person existed while R-002,
        two inches up the same screen, required notifying one. Same bug, same
        function, second kind of material.

        So the sections are no longer written out by hand here. They are driven
        by `CONTEXT_KINDS`, every one of them inside the authorized heading,
        and a contract test asserts that nothing is ever emitted above it
        again. The kinds differ in provenance, not in permission: a requirement
        is the tender's own words, evidence passed approval, a resolution is a
        colleague's written answer, a retrieved chunk is raw corpus text. Each
        keeps a distinct citation prefix so a reviewer can weigh them
        differently without the prompt having to rank them.
        """
        parts = [
            f"TASK {self.task.code}: {self.task.title}",
            f"STATE: {self.task.state.value}",
            "",
            "AUTHORIZED CONTEXT (the only material you may draw on)",
        ]
        # Section order follows CONTEXT_KINDS so the headings, the prefixes the
        # prompt names, and the columns that record them can never fall out of
        # step. Requirements come first because they are what the task is FOR.
        blocks = {
            "requirement": self.requirement_block,
            "evidence": self.evidence_block,
            "resolution": self.resolution_block,
            "retrieved": self.retrieved_block,
        }
        for kind in CONTEXT_KINDS:
            body = blocks.get(kind.key)
            if body:
                parts += ["", kind.heading, body]
        # Requirements do not count here: a task that holds its requirements and
        # nothing else has nothing to answer FROM, and saying otherwise invites
        # the model to answer a company question out of the tender's wording.
        if not any(blocks[key] for key in ANSWERING_KEYS):
            parts += ["(nothing admissible was found)"]
        return "\n".join(parts)

    def refusal(self) -> dict:
        """What to say when there is nothing to answer from, and why.

        Two different failures, and the remedy differs. NOT_PERMITTED means
        material exists and this task may not see it: the request ladder is the
        way forward. NO_MATCH means the task can see its material and none of it
        is about this question: raising an access request would waste a
        colleague's afternoon and come back "you already have it".
        """
        if self.refusal_kind == "NOT_PERMITTED":
            return {
                "reason": "Nothing you are entitled to see answers this question.",
                "kind": "NOT_PERMITTED",
                "likely_owning_departments": self.suggested_departments,
                "options": REQUEST_LADDER,
            }
        return {
            "reason": (
                "This task's own evidence does not cover that. Nothing was withheld "
                "from you -- there is nothing on file about it."
            ),
            "kind": "NO_MATCH",
            "likely_owning_departments": self.suggested_departments,
            "options": [],
        }


def _live_grants(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID, user_id: uuid.UUID
) -> list[AccessGrant]:
    """Grants that are active *now*, evaluated from expires_at/revoked_at.

    Deliberately not read from `materialized_state`: a revocation must take
    effect on the next query, not after a sweep job runs.
    """
    now = datetime.now(timezone.utc)
    return list(
        session.execute(
            select(AccessGrant).where(
                AccessGrant.org_id == ctx.org_id,
                AccessGrant.task_id == task_id,
                AccessGrant.grantee_user_id == user_id,
                AccessGrant.revoked_at.is_(None),
                AccessGrant.expires_at > now,
            )
        )
        .scalars()
        .all()
    )


def build(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    query: str,
    user_id: uuid.UUID | None = None,
    embedder=None,
    limit: int = MAX_RETRIEVED,
) -> TaskContext:
    """Assemble everything the copilot may draw on for one message.

    Membership is checked first: a person who is not on the task gets no context
    at all, however broad their organizational role. Task isolation would mean
    nothing if any bid manager could read every task's working thread.
    """
    actor = user_id or ctx.actor_user_id
    if actor is None:
        raise PermissionDenied("A task conversation requires an identified user.")

    task = session.get(Task, task_id)
    if task is None or task.org_id != ctx.org_id:
        raise NotFound("That task does not exist.")
    if not task_service.is_on_task(session, ctx, task_id=task_id, user_id=actor):
        raise PermissionDenied(
            "You are not on this task. Task context is not visible to people who are "
            "not working it."
        )

    stored = task.retrieval_scope or {}
    ceiling = ConfidentialityClass(
        stored.get("max_confidentiality", ConfidentialityClass.INTERNAL.value)
    )
    grants = _live_grants(session, ctx, task_id=task_id, user_id=actor)

    scope = RetrievalScope(
        task_id=task_id,
        sides=(DocumentSide.COMPANY,),
        max_confidentiality=ceiling,
    )
    service = RetrievalService(session, ctx)
    result = service.search(query, scope=scope, limit=limit, embedder=embedder)

    sources: list[Source] = []
    for index, candidate in enumerate(result.candidates, start=1):
        sources.append(
            Source(
                ref=f"S{index}",
                kind="chunk",
                id=candidate.chunk_id,
                text=candidate.text,
                locator=candidate.source_locator(),
            )
        )

    requirements = task_service.task_requirements(session, ctx, task_id=task_id)
    requirement_block = _render_requirements(session, ctx, requirements)
    evidence_block, evidence_refs = _render_evidence(session, ctx, requirements)
    resolution_block, resolution_refs = _render_resolutions(session, ctx, task_id=task_id)

    trace = {
        "query": query,
        "task_id": str(task_id),
        "user_id": str(actor),
        "stored_scope": stored,
        "filters_applied": result.filters_applied,
        "admissible_before_ranking": result.admissible_before_ranking,
        "live_grants": [
            {
                "grant_id": str(g.id),
                "resource_type": g.resource_type.value,
                "resource_id": str(g.resource_id),
                "expires_at": g.expires_at.isoformat(),
            }
            for g in grants
        ],
        "candidates": [
            {
                "ref": s.ref,
                "chunk_id": str(s.id),
                "score": round(c.score, 4),
                "why_matched": c.why_matched,
            }
            for s, c in zip(sources, result.candidates, strict=False)
        ],
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }

    return TaskContext(
        task=task,
        sources=sources,
        requirement_block=requirement_block,
        retrieved_block=_render_sources(sources),
        evidence_block=evidence_block,
        evidence_refs=evidence_refs,
        resolution_block=resolution_block,
        resolution_refs=resolution_refs,
        trace=trace,
        # Refuse only when there is genuinely nothing to work with. Query-time
        # retrieval finding nothing is NOT that: evidence already approved
        # against this task's requirements is part of its context and is in the
        # prompt regardless of what the search returned. Refusing while holding
        # it is the copilot declining to read its own brief.
        out_of_scope=not (sources or evidence_block or resolution_block),
        refusal_kind=("NOT_PERMITTED" if result.out_of_scope else "NO_MATCH"),
        suggested_departments=_suggested(
            session,
            ctx,
            task=task,
            from_retrieval=result.owning_departments,
            requirements=requirements,
        ),
    )


def _render_sources(sources: list[Source]) -> str:
    from app.gateway.registry import wrap_untrusted

    if not sources:
        return ""
    body = "\n\n".join(f"[{s.ref}] {s.text}" for s in sources)
    return wrap_untrusted(body)


def _render_requirements(
    session: Session, ctx: ExecutionContext, requirements: list[Requirement]
) -> str:
    """Render the covered requirements with their current assessment.

    Two batched queries rather than two per requirement. The previous shape
    issued 2N round trips, which is invisible on a demo task and a real cost on
    a bid with hundreds of requirements -- and this runs on every copilot
    message.
    """
    subset = requirements[:MAX_REQUIREMENTS]
    if not subset:
        return ""

    version_ids = [r.current_version_id for r in subset if r.current_version_id]
    texts: dict[uuid.UUID, str] = {}
    if version_ids:
        texts = {
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

    assessments = {
        row.requirement_id: row
        for row in session.execute(
            select(RequirementAssessment).where(
                RequirementAssessment.org_id == ctx.org_id,
                RequirementAssessment.requirement_id.in_([r.id for r in subset]),
                RequirementAssessment.is_current.is_(True),
            )
        )
        .scalars()
        .all()
    }

    lines = []
    for requirement in subset:
        assessment = assessments.get(requirement.id)
        status = (
            f"{assessment.evidence_status.value} / {assessment.bid_readiness.value}"
            if assessment
            else "unassessed"
        )
        text = texts.get(requirement.current_version_id) if requirement.current_version_id else None
        # Bracketed, so the model can cite a requirement the same way it cites
        # anything else. Unbracketed codes read as prose and got treated as
        # prose: the model would describe a requirement and then say the
        # context contained nothing on the subject.
        lines.append(
            f"[{requirement.code}] ({requirement.category.value}, "
            f"{'mandatory' if requirement.is_mandatory else 'optional'}, {status}) "
            f"{text or '(no text)'}"
        )
    return "\n".join(lines)


def evidence_line(index: int, evidence, role: str) -> str:
    """One [E#] line, as the model sees it.

    Separate from the query so it can be asserted directly. The previous test
    for this behaviour grepped the function's source for a field name, and went
    on passing when the field was removed from the formatted string -- a test
    that cannot fail is worse than none, because it is counted as cover.

    The type is here because `evidence_type` was stamped on every excerpt at
    extraction and read by nothing. The copilot was handed blank CV forms and
    pricing templates against a mandatory requirement about key personnel with
    no way to tell them from a signed reference letter. Attaching them stays
    deliberate -- the closest material a company holds is worth showing a
    drafter -- but presenting them unlabelled is what turns "the nearest thing
    we have" into "approved evidence".
    """
    expiry = f", expires {evidence.expiry_date.isoformat()}" if evidence.expiry_date else ""
    kind = getattr(evidence.evidence_type, "value", evidence.evidence_type) or "UNCLASSIFIED"
    caveat = (
        ""
        if is_self_evidencing(evidence.evidence_type)
        else "; A BLANK FORM OR TEMPLATE -- it shows the shape of an answer, not that the "
        "company has done anything. Never cite it as proof of capability"
    )
    # The type says what kind of material this is; it cannot say whether the
    # material exists yet. A register row about ISO 27001 is correctly typed
    # CERTIFICATE and still reads "Certificate no.: [REPLACE] | NOT EVIDENCED",
    # and the drafter has to be told which one they are looking at.
    #
    # The stored note is quoted rather than summarised, because it names what is
    # missing -- "the register marks this credential NOT EVIDENCED" tells a
    # person what to go and get, where "unsubstantiated" tells them nothing.
    if evidence.is_substantiated is False:
        caveat += (
            "; NOT SUBSTANTIATED -- this row RECORDS that the company should hold this, "
            "it is not the thing itself. Never cite it as proof"
        )
        if evidence.substantiation_note:
            caveat += f" ({evidence.substantiation_note.rstrip('.')})"
    # [E#] rather than a bare dash: material the model may not cite is material
    # it will not use, and an uncitable block reads as background.
    return (
        f"[E{index}] ({kind}, {evidence.status.value}{expiry}, "
        f"role {role}{caveat}) {evidence.excerpt[:400]}"
    )


def _render_evidence(
    session: Session, ctx: ExecutionContext, requirements: list[Requirement]
) -> tuple[str, dict[str, uuid.UUID]]:
    """Evidence already attached to this task's requirements.

    Included with its currentness, so the copilot cannot present an expired
    certificate as a live one -- the two-axis distinction from Phase H survives
    into the prompt rather than being flattened on the way in.
    """
    if not requirements:
        return "", {}
    from app.models import AssessmentEvidence

    rows = session.execute(
        select(Evidence, AssessmentEvidence, RequirementAssessment)
        .join(AssessmentEvidence, AssessmentEvidence.evidence_id == Evidence.id)
        .join(
            RequirementAssessment,
            RequirementAssessment.id == AssessmentEvidence.assessment_id,
        )
        .where(
            Evidence.org_id == ctx.org_id,
            RequirementAssessment.is_current.is_(True),
            RequirementAssessment.requirement_id.in_([r.id for r in requirements]),
        )
        .limit(60)
    ).all()
    from app.services import report_attachments

    stale = report_attachments.superseded_version_ids(session, ctx)
    rows = [row for row in rows if row[0].source_document_version_id not in stale][:20]
    lines: list[str] = []
    refs: dict[str, uuid.UUID] = {}
    for index, (evidence, link, _assessment) in enumerate(rows, start=1):
        lines.append(evidence_line(index, evidence, link.role.value))
        refs[f"E{index}"] = evidence.id
    return "\n".join(lines), refs


def _render_resolutions(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID
) -> tuple[str, dict[str, uuid.UUID]]:
    """Answers received from other departments.

    An answer to a cross-department request enters the requester's context as
    text. The record behind it does not, which is exactly the distinction
    `REQUEST_ANSWER` exists to make.
    """
    from app.models import CrossDepartmentRequest
    from app.models.enums import RequestState

    rows = (
        session.execute(
            select(CrossDepartmentRequest).where(
                CrossDepartmentRequest.org_id == ctx.org_id,
                CrossDepartmentRequest.origin_task_id == task_id,
                CrossDepartmentRequest.state.in_(
                    [
                        RequestState.ANSWERED,
                        RequestState.EVIDENCE_SHARED,
                        RequestState.ACCESS_GRANTED,
                    ]
                ),
            )
        )
        .scalars()
        .all()
    )
    # Name the department. The first version printed `target_department_id`,
    # so a model reading its own context saw "Asked 5901b49b-...", and any
    # answer that attributed a claim to its source attributed it to a UUID.
    names = {
        d.id: d.name
        for d in session.execute(select(Department).where(Department.org_id == ctx.org_id))
        .scalars()
        .all()
    }
    lines: list[str] = []
    refs: dict[str, uuid.UUID] = {}
    index = 0
    for r in rows:
        if not r.resolution_text:
            continue
        index += 1
        who = names.get(r.target_department_id, "another department")
        lines.append(f"[R{index}] Asked {who}: {r.request_text}\n  Answer: {r.resolution_text}")
        refs[f"R{index}"] = r.id
    return "\n".join(lines), refs


# Why each department gets suggested, in the reader's terms. The list is
# produced by asking which sensitive domains this person cannot read -- an
# honest signal, but a weak one, and stating it lets a reader discount a
# suggestion the system had no real basis for. "HR, COMMERCIAL, LEGAL" reads
# like an answer; "HR, because you cannot read HR records" reads like the guess
# it actually is.
_WHY_SUGGESTED = {
    "HR": "you cannot read HR records, so people and CV material may sit there",
    "COMMERCIAL": "you cannot read pricing, so commercial material may sit there",
    "LEGAL": "you cannot read privileged legal material",
}


def _suggested(
    session: Session,
    ctx: ExecutionContext,
    *,
    task: Task,
    from_retrieval: list[str],
    requirements: list[Requirement],
) -> list[dict]:
    """Departments worth asking -- never including the one already asking.

    Suggesting a task's own department tells a contributor to raise a
    cross-department request to herself, which reads as the system not knowing
    where she works. It is also the most likely suggestion to be wrong: the
    task's requirements are what put it in that department in the first place,
    so its own code always scores highly.

    Each suggestion carries its department id and the reason it was suggested.
    The id is what makes the suggestion actionable -- a contributor cannot look
    one up, because listing departments needs `org:read`, which is an
    administrator's. Without it the copilot could name three departments and
    offer no way to reach any of them.
    """
    own = session.get(Department, task.department_id) if task.department_id else None
    own_code = own.code.value if own else None
    codes = [c for c in (from_retrieval or _departments_for(session, ctx, requirements))]

    rows = {
        d.code.value: d
        for d in session.execute(select(Department).where(Department.org_id == ctx.org_id))
        .scalars()
        .all()
    }
    out: list[dict] = []
    for code in codes:
        if code == own_code or code not in rows:
            continue
        out.append(
            {
                "code": code,
                "id": str(rows[code].id),
                "name": rows[code].name,
                "why": _WHY_SUGGESTED.get(code, "may hold material this task cannot see"),
            }
        )
    return out


def _departments_for(
    session: Session, ctx: ExecutionContext, requirements: list[Requirement]
) -> list[str]:
    from app.services import category_routing

    table = category_routing.routes(session, ctx)
    codes = {table[r.category].value for r in requirements if r.category in table}
    known = {
        d.code.value
        for d in session.execute(select(Department).where(Department.org_id == ctx.org_id))
        .scalars()
        .all()
    }
    return sorted(codes & known)
