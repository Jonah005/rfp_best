"""Company evidence added from inside a task.

Ahmed is on SEC-03 and finds the approved methodology says nothing about mobile
testing. He has the document that does. Before this, adding it meant leaving
the task for the knowledge screen, with no record of which task needed it, so
the task could not say what it was waiting on and its draft had no reason to
prefer it.

Governance is unchanged, and deliberately so: the upload is ordinary company
knowledge. It is scanned, parsed and extracted like any other document, and its
excerpts wait for a knowledge administrator. Until one approves them they are
not a source for any draft -- an unapproved upload may be true and still must
not back a claim to a customer. What this adds is the link back to the task,
the task's view of where its uploads stand, and a notice to the task owner the
moment they are approved, so the draft can be regenerated from them.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import PermissionDenied
from app.models import Document, DocumentVersion, Evidence, Task, TaskEvidence
from app.models.enums import ApprovalStatus, ConfidentialityClass, DocumentSide, DocumentType
from app.services import audit, ingestion, notifications
from app.services import tasks as task_service


def _may_add(session: Session, ctx: ExecutionContext, task: Task) -> None:
    if ctx.actor_kind == "SYSTEM":
        return
    if ctx.actor_user_id is None or not task_service.is_on_task(
        session, ctx, task_id=task.id, user_id=ctx.actor_user_id
    ):
        raise PermissionDenied("Only someone working on this task can add evidence to it.")


def upload_to_task(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    data: bytes,
    filename: str,
    confidentiality_class: ConfidentialityClass = ConfidentialityClass.INTERNAL,
    document_type: DocumentType | None = None,
    title: str | None = None,
    declared_mime: str | None = None,
    scanner=None,
    storage=None,
) -> DocumentVersion:
    """Register a company document uploaded from a task, and remember where from.

    Parsing and extraction are the caller's to schedule, exactly as for any
    company upload; `parse_now` below is the synchronous path scripts and tests
    use.
    """
    task = task_service.get_task(session, ctx, task_id=task_id)
    _may_add(session, ctx, task)
    version = ingestion.register_upload(
        session,
        ctx,
        data=data,
        filename=filename,
        side=DocumentSide.COMPANY,
        bid_id=None,
        confidentiality_class=confidentiality_class,
        title=title,
        declared_mime=declared_mime,
        document_type=document_type,
        scanner=scanner,
        storage=storage,
    )
    session.add(
        TaskEvidence(
            org_id=ctx.org_id,
            task_id=task.id,
            document_version_id=version.id,
            added_by=ctx.actor_user_id,
        )
    )
    session.flush()
    audit.record(
        session,
        ctx,
        action="task.evidence_uploaded",
        entity_type="task",
        entity_id=task.id,
        bid_id=task.bid_id,
        after={"document_version_id": str(version.id), "filename": filename},
    )
    return version


def parse_now(
    session: Session,
    ctx: ExecutionContext,
    *,
    version_id: uuid.UUID,
    storage=None,
    embedder=None,
    gateway=None,
) -> DocumentVersion:
    """Parse, index and extract immediately (scripts and tests; the API enqueues)."""
    return ingestion.parse_and_index(
        session,
        ctx,
        version_id=version_id,
        storage=storage,
        embedder=embedder,
        knowledge_gateway=gateway,
    )


def status(session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID) -> list[dict]:
    """Where each document uploaded from this task stands.

    What the task shows instead of going quiet: "3 excerpts extracted, 1
    approved, 2 waiting for a knowledge administrator".
    """
    task_service.get_task(session, ctx, task_id=task_id)
    rows = session.execute(
        select(TaskEvidence, DocumentVersion, Document)
        .join(DocumentVersion, DocumentVersion.id == TaskEvidence.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(TaskEvidence.org_id == ctx.org_id, TaskEvidence.task_id == task_id)
        .order_by(TaskEvidence.created_at)
    ).all()
    out = []
    for link, version, document in rows:
        counts: dict[ApprovalStatus, int] = {
            status: int(n)
            for status, n in session.execute(
                select(Evidence.approval_status, func.count())
                .where(
                    Evidence.org_id == ctx.org_id,
                    Evidence.source_document_version_id == version.id,
                )
                .group_by(Evidence.approval_status)
            ).all()
        }
        approved = counts.get(ApprovalStatus.APPROVED, 0)
        total = sum(counts.values())
        out.append(
            {
                "document_version_id": str(version.id),
                "filename": document.original_filename,
                "parse_state": getattr(version.state, "value", version.state),
                "excerpts": total,
                "approved": approved,
                "awaiting_approval": total - approved,
                "usable_in_draft": approved > 0,
                "added_by": str(link.added_by) if link.added_by else None,
            }
        )
    return out


def on_evidence_approved(session: Session, ctx: ExecutionContext, *, evidence: Evidence) -> int:
    """Tell the owners of tasks this evidence was uploaded for that it is now usable.

    Called from `knowledge.approve_evidence`. Returns the number of tasks told.
    """
    tasks = (
        session.execute(
            select(Task)
            .join(TaskEvidence, TaskEvidence.task_id == Task.id)
            .where(
                TaskEvidence.org_id == ctx.org_id,
                TaskEvidence.document_version_id == evidence.source_document_version_id,
            )
        )
        .scalars()
        .all()
    )
    for task in tasks:
        recipients = [task.owner_user_id] if task.owner_user_id else []
        if recipients:
            notifications.notify(
                session,
                ctx,
                recipients=recipients,
                kind="task.evidence_approved",
                title=f"{task.code}: evidence you uploaded was approved",
                body="It can now be used in this task's draft. Regenerate the draft to use it.",
                screen="copilot",
                bid_id=task.bid_id,
                focus_id=task.id,
            )
    return len(tasks)
