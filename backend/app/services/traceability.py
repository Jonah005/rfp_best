"""Answering "why did we say that?" -- and the other views a person asks for.

Everything below reads rows other parts of the system already wrote. Nothing
here decides anything:

  * `trace_statement` -- one claim, end to end: the requirement that asked for
    it, the evidence behind it and who approved that evidence, the answers other
    departments gave, the approval chain, and the documents and packages it went
    out in. Six months after submission, "why did we tell the customer we had
    two OSCP testers?" is one call.
  * `timeline` -- a task's history in order, including its artifact, its
    approvals and the requests it raised. "What happened with the references?"
  * `bid_dashboard` -- the separate measures (coverage, evidence, tasks,
    approvals, package) side by side, so 74% is never a number without a why.
  * `customer_letter` -- the open questions for the customer as a document a bid
    manager can send, optionally marking them sent.
"""

from __future__ import annotations

import io
import uuid
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound
from app.models import (
    Approval,
    ArtifactRequirement,
    ArtifactStatement,
    ArtifactVersion,
    AuditEvent,
    ClarificationIssue,
    Commitment,
    CrossDepartmentRequest,
    Document,
    DocumentVersion,
    Evidence,
    GeneratedDocument,
    GeneratedDocumentVersion,
    PackageItem,
    Requirement,
    RequirementVersion,
    ResponseArtifact,
    Task,
    User,
)
from app.models import Package as ReleasePackage
from app.models.enums import (
    ArtifactState,
    ClarificationSeverity,
    ClarificationStatus,
    TaskState,
)


def _name(session: Session, user_id) -> str | None:
    if user_id is None:
        return None
    user = session.get(User, user_id)
    return user.display_name if user else str(user_id)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _source(session: Session, document_version_id) -> str | None:
    if document_version_id is None:
        return None
    version = session.get(DocumentVersion, document_version_id)
    document = session.get(Document, version.document_id) if version else None
    return document.original_filename if document else None


def trace_statement(session: Session, ctx: ExecutionContext, *, statement_id: uuid.UUID) -> dict:
    statement = session.get(ArtifactStatement, statement_id)
    if statement is None or statement.org_id != ctx.org_id:
        raise NotFound("That statement does not exist.")
    version = session.get(ArtifactVersion, statement.artifact_version_id)
    artifact = session.get(ResponseArtifact, version.artifact_id) if version else None
    task = session.get(Task, artifact.task_id) if artifact else None
    if version is None or artifact is None or task is None:
        raise NotFound("That statement's answer, version or task no longer exists.")

    requirements = []
    for requirement, current in session.execute(
        select(Requirement, RequirementVersion)
        .join(ArtifactRequirement, ArtifactRequirement.requirement_id == Requirement.id)
        .join(RequirementVersion, RequirementVersion.id == Requirement.current_version_id)
        .where(
            ArtifactRequirement.org_id == ctx.org_id,
            ArtifactRequirement.artifact_id == artifact.id,
        )
        .order_by(Requirement.code)
    ).all():
        locator = current.source_locator or {}
        requirements.append(
            {
                "code": requirement.code,
                "text": current.requirement_text,
                "source_document": _source(session, current.source_document_version_id),
                "clause": current.clause_ref or locator.get("clause_ref"),
                "page": locator.get("page_from"),
            }
        )

    evidence = []
    for row in session.execute(
        select(Evidence).where(
            Evidence.org_id == ctx.org_id,
            Evidence.id.in_(list(statement.evidence_ids or []) or [None]),
        )
    ).scalars():
        approved = (
            session.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == ctx.org_id,
                    AuditEvent.entity_type == "evidence",
                    AuditEvent.entity_id == row.id,
                    AuditEvent.action == "evidence.approved",
                )
                .order_by(AuditEvent.occurred_at.desc())
            )
            .scalars()
            .first()
        )
        evidence.append(
            {
                "evidence_id": str(row.id),
                "excerpt": row.excerpt,
                "source_document": _source(session, row.source_document_version_id),
                "status": row.status.value,
                "expiry_date": _iso(row.expiry_date),
                "approved_by": _name(session, approved.actor_user_id) if approved else None,
                "approved_at": _iso(approved.occurred_at) if approved else None,
            }
        )

    commitment = None
    if statement.commitment_id:
        held = session.get(Commitment, statement.commitment_id)
        if held is not None:
            commitment = {
                "required_role": held.required_approver_role,
                "status": held.approval_status.value,
                "approved_by": _name(session, held.approved_by),
                "approved_at": _iso(held.approved_at),
            }

    approvals = [
        {
            "step": a.step_no,
            "role": a.required_role,
            "state": a.state.value,
            "by": _name(session, a.approver_user_id),
            "at": _iso(a.decided_at),
            "comment": a.comment,
        }
        for a in session.execute(
            select(Approval)
            .where(Approval.org_id == ctx.org_id, Approval.entity_id == version.id)
            .order_by(Approval.step_no)
        ).scalars()
    ]

    answers = [
        {
            "request_id": str(r.id),
            "asked": r.request_text,
            "answer": r.resolution_text,
            "state": getattr(r.state, "value", r.state),
            "resolved_at": _iso(r.resolved_at),
        }
        for r in session.execute(
            select(CrossDepartmentRequest).where(
                CrossDepartmentRequest.org_id == ctx.org_id,
                CrossDepartmentRequest.origin_task_id == task.id,
            )
        ).scalars()
    ]

    released = []
    for generated_version, document in session.execute(
        select(GeneratedDocumentVersion, GeneratedDocument)
        .join(
            GeneratedDocument,
            GeneratedDocument.id == GeneratedDocumentVersion.generated_document_id,
        )
        .where(
            GeneratedDocumentVersion.org_id == ctx.org_id,
            GeneratedDocument.bid_id == artifact.bid_id,
        )
    ).all():
        used = any(
            entry.get("artifact_version_id") == str(version.id)
            for entry in (generated_version.generation_trace or {}).get("artifacts", [])
        )
        if not used:
            continue
        packages = [
            {
                "package_version": p.version_no,
                "state": p.state.value,
                "released_at": _iso(p.released_at),
            }
            for p in session.execute(
                select(ReleasePackage)
                .join(PackageItem, PackageItem.package_id == ReleasePackage.id)
                .where(PackageItem.generated_document_version_id == generated_version.id)
            ).scalars()
        ]
        released.append(
            {
                "document": document.name,
                "version": generated_version.version_no,
                "mode": generated_version.output_mode.value,
                "packages": packages,
            }
        )

    return {
        "statement": {
            "id": str(statement.id),
            "class": statement.statement_class.value,
            "text": statement.text,
        },
        "answer": {
            "task": task.code,
            "task_title": task.title,
            "owner": _name(session, task.owner_user_id),
            "artifact": artifact.title,
            "version": version.version_no,
            "version_state": version.state.value,
        },
        "requirements": requirements,
        "evidence": evidence,
        "commitment": commitment,
        "department_answers": answers,
        "approvals": approvals,
        "released_in": released,
    }


def timeline(session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID) -> list[dict]:
    """A task's whole history, oldest first: the task, its answer, its approvals, its requests."""
    task = session.get(Task, task_id)
    if task is None or task.org_id != ctx.org_id:
        raise NotFound("That task does not exist.")
    artifact = session.execute(
        select(ResponseArtifact).where(ResponseArtifact.task_id == task.id)
    ).scalar_one_or_none()
    related: list[uuid.UUID] = [task.id]
    if artifact is not None:
        related.append(artifact.id)
        related += list(
            session.execute(
                select(ArtifactVersion.id).where(ArtifactVersion.artifact_id == artifact.id)
            ).scalars()
        )
    related += list(
        session.execute(
            select(CrossDepartmentRequest.id).where(
                CrossDepartmentRequest.origin_task_id == task.id
            )
        ).scalars()
    )
    approvals = list(
        session.execute(select(Approval.id).where(Approval.entity_id.in_(related))).scalars()
    )
    events = session.execute(
        select(AuditEvent)
        .where(
            AuditEvent.org_id == ctx.org_id,
            or_(AuditEvent.entity_id.in_(related + approvals)),
        )
        .order_by(AuditEvent.occurred_at)
    ).scalars()
    return [
        {
            "at": _iso(e.occurred_at),
            "who": _name(session, e.actor_user_id) or getattr(e.actor_kind, "value", "SYSTEM"),
            "action": e.action,
            "entity_type": e.entity_type,
            "reason": e.reason,
            "after": e.after_state,
        }
        for e in events
    ]


def bid_dashboard(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID) -> dict:
    """Coverage, evidence, tasks, approvals and package readiness, each with its own number."""
    from app.services import readiness

    summary = readiness.summarize(session, ctx, bid_id=bid_id).as_dict()
    tasks = list(
        session.execute(
            select(Task).where(Task.org_id == ctx.org_id, Task.bid_id == bid_id)
        ).scalars()
    )
    done = sum(1 for t in tasks if t.state in (TaskState.APPROVED, TaskState.COMPLETED))
    waiting = sum(
        1
        for t in tasks
        if t.state in (TaskState.WAITING_FOR_DEPENDENCY, TaskState.WAITING_FOR_ACCESS)
    )
    # An answer is a piece of writing. A conflict task that was settled has an artifact
    # shell with no sentences and never will, and a replaced artifact is not an answer
    # any more, so neither counts against the approvals figure.
    with_text = (
        select(ArtifactVersion.artifact_id)
        .join(ArtifactStatement, ArtifactStatement.artifact_version_id == ArtifactVersion.id)
        .where(ArtifactVersion.org_id == ctx.org_id)
    )
    artifacts = list(
        session.execute(
            select(ResponseArtifact).where(
                ResponseArtifact.org_id == ctx.org_id,
                ResponseArtifact.bid_id == bid_id,
                ResponseArtifact.state != ArtifactState.SUPERSEDED,
                ResponseArtifact.id.in_(with_text),
            )
        ).scalars()
    )
    approved = sum(1 for a in artifacts if a.state is ArtifactState.APPROVED)
    latest = (
        session.execute(
            select(ReleasePackage)
            .where(ReleasePackage.org_id == ctx.org_id, ReleasePackage.bid_id == bid_id)
            .order_by(ReleasePackage.version_no.desc())
        )
        .scalars()
        .first()
    )
    open_findings = 0
    if latest is not None:
        from app.services import release

        open_findings = sum(
            1
            for f in release.validation_findings(session, ctx, package_id=latest.id)
            if f.resolved_at is None
        )

    def pct(a: int, b: int) -> float:
        return round(100.0 * a / b, 1) if b else 0.0

    return {
        **summary,
        "tasks_total": len(tasks),
        "tasks_approved": done,
        "tasks_waiting_on_departments": waiting,
        "task_completion_pct": pct(done, len(tasks)),
        "answers_total": len(artifacts),
        "answers_approved": approved,
        "approvals_pct": pct(approved, len(artifacts)),
        "package_state": latest.state.value if latest else None,
        "package_open_findings": open_findings,
        "package_readiness_pct": (
            100.0
            if latest is not None
            and latest.state.value in ("RELEASE_PENDING_APPROVALS", "RELEASED")
            else pct(approved, len(artifacts))
            if latest is None
            else max(0.0, 100.0 - 10.0 * open_findings)
        ),
    }


def customer_letter(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    mark_sent: bool = False,
) -> tuple[bytes, int]:
    """The open questions for the customer, as a document to send. Returns (docx, count).

    Canonical questions only (restatements are the same question in other
    words), most serious first. `mark_sent` moves each one to SENT_TO_CLIENT, so
    the rationale on every row it holds stops saying "not yet sent".
    """
    from docx import Document as WordDocument

    from app.models import Bid, Opportunity
    from app.services import clarifications

    bid = session.get(Bid, bid_id)
    if bid is None or bid.org_id != ctx.org_id:
        raise NotFound("That bid does not exist.")
    opportunity = session.get(Opportunity, bid.opportunity_id)
    order = {
        ClarificationSeverity.BLOCKING: 0,
        ClarificationSeverity.MAJOR: 1,
        ClarificationSeverity.MINOR: 2,
    }
    issues = sorted(
        session.execute(
            select(ClarificationIssue).where(
                ClarificationIssue.org_id == ctx.org_id,
                ClarificationIssue.bid_id == bid_id,
                ClarificationIssue.status == ClarificationStatus.OPEN,
                ClarificationIssue.recommended_question.is_not(None),
                ClarificationIssue.restates_clarification_id.is_(None),
            )
        ).scalars(),
        key=lambda i: (order.get(i.severity, 3), i.created_at),
    )
    document = WordDocument()
    document.add_heading("Request for Clarification", 0)
    if opportunity is not None:
        document.add_paragraph(f"Tender: {opportunity.tender_name}")
        if opportunity.tender_reference:
            document.add_paragraph(f"Reference: {opportunity.tender_reference}")
        document.add_paragraph(f"To: {opportunity.customer_name}")
    document.add_paragraph(f"Date: {date.today().isoformat()}")
    document.add_paragraph(
        "We would be grateful for clarification of the following points before "
        "submission of our proposal."
    )
    for number, issue in enumerate(issues, start=1):
        document.add_paragraph(f"{number}. {issue.recommended_question}")
    buffer = io.BytesIO()
    document.save(buffer)
    if mark_sent:
        for issue in issues:
            clarifications.update_status(
                session,
                ctx,
                issue_id=issue.id,
                status=ClarificationStatus.SENT_TO_CLIENT,
                reason="Included in the clarification letter sent to the customer.",
            )
    return buffer.getvalue(), len(issues)
