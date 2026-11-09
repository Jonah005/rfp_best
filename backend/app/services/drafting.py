"""Bounded structured drafting from governed task sources."""

from __future__ import annotations

import json
import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import PermissionDenied, ValidationError
from app.core.logging import get_logger
from app.gateway.gateway import ModelGateway, build_gateway
from app.gateway.registry import get as get_call_type
from app.gateway.registry import wrap_untrusted
from app.models import (
    AssessmentEvidence,
    Evidence,
    Requirement,
    RequirementAssessment,
    RequirementVersion,
    TaskRequirement,
)
from app.models.enums import (
    ApprovalStatus,
    KnowledgeStatus,
    StatementClass,
    is_self_evidencing,
)
from app.schemas.drafting import DraftStatementResult
from app.services import artifacts, tasks
from app.services.requirement_scope import LIVE_REQUIREMENT

log = get_logger("drafting")

# Sentences that speak about the review process, not to the customer: "we hold no approved
# evidence", "the draft", "evidence supplied with this task". They are the system's own
# working notes and must never reach a customer, whichever class the model gave them.
INTERNAL_PROCESS = re.compile(
    r"(?:no\s+)?approved\s+(?:company\s+)?evidence|evidence\s+(?:available|supplied|sources?|on\s+file)"
    r"|supplied\s+with\s+this\s+(?:task|response)|\bthis\s+draft\b|\bthe\s+draft\b"
    r"|\bknowledge\s+base\b|\bevidence\s+records?\b|\binternal\s+(?:drafting|review)\s+note\b",
    re.IGNORECASE,
)


def _expires(evidence) -> dict:
    """{"expires": ISO date} when the evidence has an expiry date, else nothing."""
    expiry = getattr(evidence, "expiry_date", None)
    return {"expires": expiry.isoformat()} if expiry else {}


def gather_sources(
    session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID
) -> tuple[dict[str, dict], dict[str, Evidence], list[dict]]:
    """What a draft for this task may rest on: tender wording and governed evidence.

    Chosen by code, never by a model. Returns the tender sources by ref (T1 ...),
    the evidence rows by ref (E1 ...) and the evidence as it is shown to the model.
    """
    requirement_rows = session.execute(
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

    tender_sources: dict[str, dict] = {}
    for index, (requirement, version) in enumerate(requirement_rows, start=1):
        ref = f"T{index}"
        tender_sources[ref] = {
            "ref": ref,
            "requirement_id": str(requirement.id),
            "code": requirement.code,
            "text": version.requirement_text,
            "source_locator": version.source_locator,
        }

    requirement_ids = [row[0].id for row in requirement_rows]
    evidence_rows: list[Evidence] = []
    # Evidence a contributor uploaded from inside this task, once a knowledge
    # administrator has approved it. Until then it is not a source: an
    # unapproved upload may be true and still must not back a claim.
    from app.models import TaskEvidence

    task_evidence = list(
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
    if requirement_ids:
        evidence_rows = list(
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
                    RequirementAssessment.requirement_id.in_(requirement_ids),
                    RequirementAssessment.is_current.is_(True),
                    Evidence.approval_status == ApprovalStatus.APPROVED,
                    Evidence.approved_for_reuse.is_(True),
                    Evidence.status == KnowledgeStatus.CURRENT_VERIFIED,
                )
                .distinct()
                .order_by(Evidence.created_at)
            ).scalars()
        )

    seen_ids = {e.id for e in evidence_rows}
    evidence_rows += [e for e in task_evidence if e.id not in seen_ids]
    # Evidence from a document that calls itself a test fixture is never a source,
    # even where its row was approved; the answer then says the company holds none.
    from app.services import report_attachments

    invented = report_attachments.fixture_version_ids(
        session, ctx
    ) | report_attachments.superseded_version_ids(session, ctx)
    evidence_rows = [e for e in evidence_rows if e.source_document_version_id not in invented]
    evidence_sources: dict[str, Evidence] = {
        f"E{index}": evidence for index, evidence in enumerate(evidence_rows, start=1)
    }
    evidence_payload = [
        {
            "ref": ref,
            "evidence_id": str(evidence.id),
            "excerpt": evidence.excerpt,
            "source_locator": evidence.source_locator,
            "status": evidence.status.value,
            # What the item IS, and whether it can prove anything on its
            # own. The copilot has been told this for a while; the drafting
            # call never was, so a blank form or a customer's old tender
            # could be cited in a customer-facing sentence as proof.
            "document_type": getattr(evidence.evidence_type, "value", evidence.evidence_type),
            "can_prove_capability": bool(
                is_self_evidencing(evidence.evidence_type)
                and evidence.is_substantiated is not False
            ),
            **_expires(evidence),
        }
        for ref, evidence in evidence_sources.items()
    ]
    return tender_sources, evidence_sources, evidence_payload


# Who must approve a fact that had to be rewritten as a commitment.
RECLASSIFIED_APPROVER_ROLE = "BID_MANAGER"

#: Appended to a statement the drafter wrote as a fact but could not prove. It is
#: for the approver, who must see why this is a promise. Once the named role has
#: approved the promise the note has done its job and must not reach the customer.
COMMITMENT_NOTE = (
    "[Written as a commitment, not as a fact: the evidence cited for it cannot prove a "
    "capability on its own. A named approver must stand behind this before it is released.]"
)


def strip_commitment_note(text: str) -> str:
    """The statement as the customer should read it, without the approver's note."""
    return text.replace(COMMITMENT_NOTE, "").rstrip()


def map_proposals(
    ctx: ExecutionContext,
    proposals,
    tender_sources: dict[str, dict],
    evidence_sources: dict[str, Evidence],
    *,
    allow_descriptions: bool = False,
) -> list[dict]:
    """Turn a model's typed proposals into statements the draft path accepts.

    Every rule that keeps a claim honest lives here, so a write-up and a first
    draft are held to the same ones: refs must be inside the authorized set, a
    fact needs proof that can prove it (or becomes a commitment), a tender
    statement needs its locator.
    """
    mapped: list[dict] = []
    # A first draft reads "the tender says X, we will do Y, we assume Z" and often
    # leaves the sentences untagged. Untagged, they answer nothing as far as
    # coverage is concerned, so the write-up wrote the whole answer a second time.
    # Code can see the grouping: a tender statement names its clause, and the
    # sentences that follow it, until the next tender statement, are about it.
    group_codes: list[str] = []
    for proposal in proposals:
        if INTERNAL_PROCESS.search(proposal.text or ""):
            log.info("draft_internal_note_dropped", text=proposal.text[:80])
            continue
        row: dict = {
            "statement_class": proposal.statement_class.value,
            "text": proposal.text,
        }
        if proposal.paragraph:
            row["paragraph_no"] = proposal.paragraph
        # Only requirements of this task can be answered; anything else is dropped, not guessed.
        known = {v["code"] for v in tender_sources.values()}
        codes = [c for c in dict.fromkeys(proposal.answers) if c in known]
        if proposal.statement_class is StatementClass.TENDER_DERIVED:
            ref = tender_sources.get(proposal.tender_source_ref or "")
            group_codes = codes or ([ref["code"]] if ref and ref.get("code") else [])
            codes = codes or list(group_codes)
        elif not codes and group_codes:
            codes = list(group_codes)
        if codes:
            row["requirement_codes"] = codes
        if proposal.statement_class is StatementClass.EVIDENCE_BACKED_FACT:
            unknown = set(proposal.evidence_refs) - set(evidence_sources)
            if unknown:
                raise ValidationError(
                    "The draft model cited evidence refs outside its authorized source set."
                )
            cited = [evidence_sources[ref] for ref in proposal.evidence_refs]
            # A fact needs something that can prove it, and this is checked
            # here rather than only asked for in the prompt.
            #
            # The payload already tells the model `can_prove_capability` per
            # source and the prompt already says never to cite a false one for
            # a fact. Both were true while a requirement was going READY on
            # seven excerpts from a document that calls itself fictional --
            # which is the lesson: an instruction the model can ignore is not a
            # control. The bound belongs in code, like every other bound here.
            #
            # Reclassified rather than dropped, because the sentence is not
            # worthless: what the company would have to DO to make it true is
            # exactly a PROPOSED_COMMITMENT, which needs a named approver
            # before release and is never presented as a fact about the
            # company. Dropping it would lose the drafter's work silently;
            # leaving it would put an unbacked claim in front of a customer.
            unprovable = [
                e
                for e in cited
                if not (is_self_evidencing(e.evidence_type) and e.is_substantiated is not False)
            ]
            if unprovable or not cited:
                log.info(
                    "draft_fact_reclassified",
                    reason="no_admissible_proof",
                    cited=len(cited),
                    unprovable=len(unprovable),
                )
                row["statement_class"] = StatementClass.PROPOSED_COMMITMENT.value
                # The model wrote a fact, so it named no approver. The code routes
                # it to the bid manager, who stands behind anything promised.
                row["required_approver_role"] = (
                    proposal.required_approver_role or RECLASSIFIED_APPROVER_ROLE
                )
                row["commitment_owner_user_id"] = ctx.actor_user_id
                row["text"] = f"{proposal.text} {COMMITMENT_NOTE}"
                mapped.append(row)
                continue
            row["evidence_ids"] = [e.id for e in cited]
        elif proposal.statement_class is StatementClass.DIAGRAM_DESCRIPTION:
            # Only a write-up about a diagram may describe one; a first draft that
            # produced this has misread its job.
            if not allow_descriptions:
                raise ValidationError("A diagram description is not allowed in this draft.")
        elif proposal.statement_class is StatementClass.TENDER_DERIVED:
            if proposal.tender_source_ref not in tender_sources:
                raise ValidationError(
                    "The draft model cited a tender ref outside this task's requirements."
                )
            row["source_locator"] = tender_sources[proposal.tender_source_ref]["source_locator"]
        elif proposal.statement_class is StatementClass.PROPOSED_COMMITMENT:
            row["required_approver_role"] = proposal.required_approver_role
            row["commitment_owner_user_id"] = ctx.actor_user_id
        mapped.append(row)

    return mapped


def generate(
    session: Session,
    ctx: ExecutionContext,
    *,
    artifact_id: uuid.UUID,
    instruction: str | None = None,
    gateway: ModelGateway | None = None,
):
    """Generate typed proposals and persist them through the normal draft path.

    Source selection is deterministic.  The model receives only tender wording
    linked to this task and governed evidence attached to current assessments;
    it cannot retrieve, choose permissions or write rows directly.
    """
    artifact = artifacts.get_artifact(session, ctx, artifact_id=artifact_id)
    # The system may prepare a first draft for a task nobody has opened yet --
    # that is what lets a task open as "AI believes this is ready, please
    # approve". A person must be on the task; nobody else may.
    if ctx.actor_kind != "SYSTEM" and (
        ctx.actor_user_id is None
        or not tasks.is_on_task(session, ctx, task_id=artifact.task_id, user_id=ctx.actor_user_id)
    ):
        raise PermissionDenied("Only an active task contributor may generate its draft.")

    tender_sources, evidence_sources, evidence_payload = gather_sources(
        session, ctx, task_id=artifact.task_id
    )
    if not tender_sources:
        # Nothing in the tender is left for this task to answer (every clause it
        # covered was withdrawn, or none was ever linked). Asking the model anyway
        # produced a "this cannot be drafted" sentence that was then saved and
        # submitted as the company's response.
        raise ValidationError(
            "This task has no live tender requirements to answer, so there is nothing "
            "to draft. Link a requirement to it, or mark the task as not needed."
        )
    # A letter the tender prescribes is a form: code fills it from recorded facts.
    # The model would describe the clause instead of writing the letter.
    from app.models import Task
    from app.services import form_documents

    task = session.get(Task, artifact.task_id)
    form_kind = form_documents.detect(task.title if task else "", tender_sources)
    if form_kind:
        return artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=form_documents.build(
                session,
                ctx,
                kind=form_kind,
                bid_id=artifact.bid_id,
                tender_sources=tender_sources,
                task_title=task.title if task else "",
                gateway=gateway,
            ),
            change_reason=instruction,
            generated_by=f"form:{form_kind}@{form_documents.FORM_VERSION}",
        )
    payload = {
        "task_id": str(artifact.task_id),
        "instruction": instruction or "Draft a complete response for the covered requirements.",
        "tender_sources": list(tender_sources.values()),
        "evidence_sources": evidence_payload,
    }
    model_gateway = gateway or build_gateway()
    result = model_gateway.generate_structured(
        "draft_statement_generation",
        wrap_untrusted(json.dumps(payload, ensure_ascii=False, default=str)),
        ctx=ctx,
        session=session,
    )
    if not isinstance(result, DraftStatementResult):
        raise ValidationError("draft_statement_generation returned an unexpected schema.")

    mapped = map_proposals(ctx, result.statements, tender_sources, evidence_sources)

    call = get_call_type("draft_statement_generation")
    return artifacts.save_draft(
        session,
        ctx,
        artifact_id=artifact.id,
        statements=mapped,
        change_reason=instruction,
        generated_by=f"llm:{call.name}@{call.prompt_version}",
    )


def prepare_ready_drafts(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    gateway: ModelGateway | None = None,
) -> dict[str, int]:
    """Write a first draft for every task the evidence says is ready.

    `AI_READY_FOR_REVIEW` promised "a draft can be proposed" and then nobody
    proposed one: the task opened empty and its owner had to press Generate.
    This is the step the walkthrough describes -- Noor opens SEC-01 and the
    answer is already there, cited, waiting for her approval.

    Only tasks in that state, and only the untouched empty first version, so a
    draft a person has started is never overwritten. Each draft runs in its own
    savepoint and a failure is counted, never raised: the model being down must
    not be the reason an assessment fails.
    """
    from app.models import ArtifactStatement, ArtifactVersion, ResponseArtifact, Task
    from app.models.enums import ArtifactVersionState, TaskState

    # "Untouched": the empty version 1 every task is created with, still a
    # draft, still without a single statement.
    has_statements = (
        select(ArtifactStatement.id)
        .where(ArtifactStatement.artifact_version_id == ResponseArtifact.current_version_id)
        .exists()
    )
    rows = (
        session.execute(
            select(ResponseArtifact)
            .join(Task, Task.id == ResponseArtifact.task_id)
            .join(ArtifactVersion, ArtifactVersion.id == ResponseArtifact.current_version_id)
            .where(
                ResponseArtifact.org_id == ctx.org_id,
                ResponseArtifact.bid_id == bid_id,
                Task.state == TaskState.AI_READY_FOR_REVIEW,
                ArtifactVersion.version_no == 1,
                ArtifactVersion.state == ArtifactVersionState.DRAFT,
                ~has_statements,
            )
        )
        .scalars()
        .all()
    )
    drafted = failed = 0
    model_gateway = gateway
    for artifact in rows:
        try:
            with session.begin_nested():
                if model_gateway is None:
                    model_gateway = build_gateway()
                generate(
                    session,
                    ctx,
                    artifact_id=artifact.id,
                    instruction=(
                        "Prepare a first draft for review. Answer every covered "
                        "requirement from the supplied evidence only."
                    ),
                    gateway=model_gateway,
                )
            drafted += 1
        except Exception:  # noqa: BLE001 - a draft that cannot be written is left for a person
            failed += 1
    return {"drafted": drafted, "not_drafted": failed}
