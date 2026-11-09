"""Conservative amendment impact propagation.

Over-flagging is recoverable; silently leaving an approved response current
after its tender wording changed is not.  This service therefore propagates a
confirmed affected requirement to every current assessment, task and artifact
linked to that stable requirement identity.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.models import (
    AmendmentImpact,
    ArtifactRequirement,
    Document,
    DocumentClassification,
    DocumentVersion,
    Requirement,
    RequirementAssessment,
    ResponseArtifact,
    Task,
    TaskRequirement,
)
from app.models.enums import (
    ArtifactState,
    AssessmentState,
    DocumentSide,
    DocumentType,
    TaskState,
)
from app.services import audit
from app.services import tasks as task_service


def propagate(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    amendment_document_version_id: uuid.UUID,
    affected_requirement_ids: list[uuid.UUID],
    reason: str,
) -> AmendmentImpact:
    if not affected_requirement_ids:
        raise ValidationError("Amendment impact needs at least one affected requirement.")
    if not reason.strip():
        raise ValidationError("Amendment impact needs an explanation.")
    existing = session.execute(
        select(AmendmentImpact).where(
            AmendmentImpact.org_id == ctx.org_id,
            AmendmentImpact.amendment_document_version_id == amendment_document_version_id,
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    version = session.get(DocumentVersion, amendment_document_version_id)
    document = session.get(Document, version.document_id) if version else None
    if (
        version is None
        or document is None
        or version.org_id != ctx.org_id
        or document.org_id != ctx.org_id
        or document.bid_id != bid_id
        or document.side is not DocumentSide.TENDER
    ):
        raise NotFound("That amendment document version does not exist for this bid.")
    classification = session.execute(
        select(DocumentClassification.document_type).where(
            DocumentClassification.org_id == ctx.org_id,
            DocumentClassification.document_version_id == version.id,
        )
    ).scalar_one_or_none()
    if classification is not None and classification is not DocumentType.AMENDMENT:
        raise ValidationError("Impact propagation accepts tender amendments only.")

    requirements = list(
        session.execute(
            select(Requirement).where(
                Requirement.org_id == ctx.org_id,
                Requirement.bid_id == bid_id,
                Requirement.id.in_(affected_requirement_ids),
            )
        ).scalars()
    )
    if len(requirements) != len(set(affected_requirement_ids)):
        raise ValidationError("One or more affected requirements do not belong to this bid.")

    assessments = list(
        session.execute(
            select(RequirementAssessment).where(
                RequirementAssessment.org_id == ctx.org_id,
                RequirementAssessment.requirement_id.in_(affected_requirement_ids),
                RequirementAssessment.is_current.is_(True),
            )
        ).scalars()
    )
    for assessment in assessments:
        assessment.assessment_state = AssessmentState.STALE
        assessment.stale_reason = reason.strip()

    tasks = list(
        session.execute(
            select(Task)
            .join(TaskRequirement, TaskRequirement.task_id == Task.id)
            .where(
                Task.org_id == ctx.org_id,
                Task.bid_id == bid_id,
                TaskRequirement.requirement_id.in_(affected_requirement_ids),
            )
            .distinct()
        ).scalars()
    )
    affected_tasks: list[Task] = []
    tasks = list(tasks)
    # Everyone working on the changed requirements is told, whatever state the
    # work is in: approved work goes back for revalidation below, and work in
    # progress is about to be answered against wording that no longer applies.
    from app.services import notifications

    for task in tasks:
        if task.owner_user_id:
            notifications.notify(
                session,
                ctx,
                recipients=[task.owner_user_id],
                kind="task.amended",
                title=f"{task.code}: the tender changed a requirement this task answers",
                body=reason.strip(),
                screen="copilot",
                bid_id=bid_id,
                focus_id=task.id,
            )
    for task in tasks:
        if task.state in {TaskState.APPROVED, TaskState.COMPLETED}:
            task_service.transition(
                session,
                ctx,
                task_id=task.id,
                to_state=TaskState.REVALIDATION_REQUIRED,
                reason=reason,
            )
            affected_tasks.append(task)

    artifacts = list(
        session.execute(
            select(ResponseArtifact)
            .join(
                ArtifactRequirement,
                ArtifactRequirement.artifact_id == ResponseArtifact.id,
            )
            .where(
                ResponseArtifact.org_id == ctx.org_id,
                ResponseArtifact.bid_id == bid_id,
                ArtifactRequirement.requirement_id.in_(affected_requirement_ids),
            )
            .distinct()
        ).scalars()
    )
    affected_artifacts: list[ResponseArtifact] = []
    for artifact in artifacts:
        if artifact.state is ArtifactState.APPROVED:
            artifact.state = ArtifactState.REVALIDATION_REQUIRED
            artifact.revalidation_reason = reason.strip()
            affected_artifacts.append(artifact)

    impact = AmendmentImpact(
        org_id=ctx.org_id,
        bid_id=bid_id,
        amendment_document_version_id=amendment_document_version_id,
        affected_requirement_ids=[row.id for row in requirements],
        affected_assessment_ids=[row.id for row in assessments],
        affected_task_ids=[row.id for row in affected_tasks],
        affected_artifact_ids=[row.id for row in affected_artifacts],
        analyzed_at=datetime.now(timezone.utc),
        analyzed_by=ctx.actor_user_id,
        trace={"reason": reason.strip(), "strategy": "stable-requirement-link-cascade"},
    )
    session.add(impact)
    session.flush()
    audit.record(
        session,
        ctx,
        action="amendment.impact_propagated",
        entity_type="amendment_impact",
        entity_id=impact.id,
        bid_id=bid_id,
        after={
            "requirements": len(requirements),
            "assessments": len(assessments),
            "tasks": len(affected_tasks),
            "artifacts": len(affected_artifacts),
        },
        reason=reason,
    )
    return impact


def for_bid(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID) -> list[AmendmentImpact]:
    return list(
        session.execute(
            select(AmendmentImpact)
            .where(
                AmendmentImpact.org_id == ctx.org_id,
                AmendmentImpact.bid_id == bid_id,
            )
            .order_by(AmendmentImpact.analyzed_at.desc())
        ).scalars()
    )
