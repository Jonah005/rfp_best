"""Typed response artifacts, immutable versions, reviews and approvals.

The working draft is the current DRAFT ``ArtifactVersion``.  Once submitted,
that row is never edited again: rework creates a new version and preserves the
reviewed snapshot.  Model or human prose is persisted only as typed statements
whose backing is checked before review and checked again before approval.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import IllegalTransition, NotFound, PermissionDenied, StateConflict
from app.core.errors import ValidationError as AppValidationError
from app.core.permissions import COMMITMENT_APPROVER_ROLES, RoleCode
from app.models import (
    Approval,
    ArtifactRequirement,
    ArtifactStatement,
    ArtifactVersion,
    Assumption,
    Commitment,
    Evidence,
    EvidenceLink,
    Requirement,
    RequirementVersion,
    ResponseArtifact,
    Review,
    Task,
    TaskRequirement,
)
from app.models.enums import (
    ApprovalStatus,
    ApprovalStepState,
    ArtifactState,
    ArtifactVersionState,
    AssumptionStatus,
    ConfidentialityClass,
    EvidenceLinkRole,
    EvidenceLinkTarget,
    KnowledgeStatus,
    RequirementLifecycle,
    ReviewDecision,
    StatementApproval,
    StatementClass,
    TaskState,
)
from app.services import audit, notifications
from app.services.requirement_scope import LIVE_REQUIREMENT

ENTITY_TYPE = "ARTIFACT_VERSION"
ADMISSIBLE_EVIDENCE = frozenset({KnowledgeStatus.CURRENT_VERIFIED})


def _artifact(session: Session, ctx: ExecutionContext, artifact_id: uuid.UUID) -> ResponseArtifact:
    artifact = session.get(ResponseArtifact, artifact_id)
    if artifact is None or artifact.org_id != ctx.org_id:
        raise NotFound("That response artifact does not exist.")
    return artifact


def _version(session: Session, ctx: ExecutionContext, version_id: uuid.UUID) -> ArtifactVersion:
    version = session.get(ArtifactVersion, version_id)
    if version is None or version.org_id != ctx.org_id:
        raise NotFound("That artifact version does not exist.")
    return version


def _current_version(
    session: Session, ctx: ExecutionContext, artifact: ResponseArtifact
) -> ArtifactVersion:
    if artifact.current_version_id is None:
        raise StateConflict("The artifact has no current version.")
    version = _version(session, ctx, artifact.current_version_id)
    if version.artifact_id != artifact.id:
        raise StateConflict("The artifact's current-version pointer is inconsistent.")
    return version


def ensure_for_task(
    session: Session,
    ctx: ExecutionContext,
    *,
    task_id: uuid.UUID,
    title: str | None = None,
) -> ResponseArtifact:
    task = session.get(Task, task_id)
    if task is None or task.org_id != ctx.org_id:
        raise NotFound("That task does not exist.")
    existing = session.execute(
        select(ResponseArtifact).where(
            ResponseArtifact.org_id == ctx.org_id,
            ResponseArtifact.task_id == task_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if title and title.strip() and existing.state is ArtifactState.DRAFT:
            existing.title = title.strip()
            session.flush()
        return existing

    artifact = ResponseArtifact(
        org_id=ctx.org_id,
        bid_id=task.bid_id,
        task_id=task.id,
        title=(title or task.title).strip(),
        state=ArtifactState.DRAFT,
    )
    session.add(artifact)
    session.flush()
    version = ArtifactVersion(
        org_id=ctx.org_id,
        artifact_id=artifact.id,
        version_no=1,
        state=ArtifactVersionState.DRAFT,
        created_by=ctx.actor_user_id,
    )
    session.add(version)
    session.flush()
    artifact.current_version_id = version.id

    requirement_ids = session.execute(
        select(TaskRequirement.requirement_id)
        .join(Requirement, Requirement.id == TaskRequirement.requirement_id)
        .where(
            TaskRequirement.org_id == ctx.org_id,
            TaskRequirement.task_id == task.id,
            LIVE_REQUIREMENT,
        )
    ).scalars()
    for requirement_id in requirement_ids:
        session.add(
            ArtifactRequirement(
                org_id=ctx.org_id,
                artifact_id=artifact.id,
                requirement_id=requirement_id,
            )
        )
    session.flush()
    audit.record(
        session,
        ctx,
        action="artifact.created",
        entity_type="response_artifact",
        entity_id=artifact.id,
        bid_id=task.bid_id,
        after={"task_id": str(task.id), "version": 1},
    )
    return artifact


def for_task(session: Session, ctx: ExecutionContext, *, task_id: uuid.UUID) -> ResponseArtifact:
    artifact = session.execute(
        select(ResponseArtifact).where(
            ResponseArtifact.org_id == ctx.org_id,
            ResponseArtifact.task_id == task_id,
        )
    ).scalar_one_or_none()
    if artifact is None:
        raise NotFound("That task has no response artifact.")
    return artifact


def list_artifacts(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    state: ArtifactState | None = None,
    requirement_id: uuid.UUID | None = None,
) -> list[ResponseArtifact]:
    stmt = select(ResponseArtifact).where(ResponseArtifact.org_id == ctx.org_id)
    if requirement_id is not None:
        stmt = stmt.join(
            ArtifactRequirement,
            ArtifactRequirement.artifact_id == ResponseArtifact.id,
        ).where(ArtifactRequirement.requirement_id == requirement_id)
    if bid_id is not None:
        stmt = stmt.where(ResponseArtifact.bid_id == bid_id)
    if task_id is not None:
        stmt = stmt.where(ResponseArtifact.task_id == task_id)
    if state is not None:
        stmt = stmt.where(ResponseArtifact.state == state)
    return list(session.execute(stmt.order_by(ResponseArtifact.created_at.desc())).scalars())


def get_artifact(
    session: Session, ctx: ExecutionContext, *, artifact_id: uuid.UUID
) -> ResponseArtifact:
    return _artifact(session, ctx, artifact_id)


def versions(
    session: Session, ctx: ExecutionContext, *, artifact_id: uuid.UUID
) -> list[ArtifactVersion]:
    _artifact(session, ctx, artifact_id)
    return list(
        session.execute(
            select(ArtifactVersion)
            .where(
                ArtifactVersion.org_id == ctx.org_id,
                ArtifactVersion.artifact_id == artifact_id,
            )
            .order_by(ArtifactVersion.version_no)
        ).scalars()
    )


def version_by_number(
    session: Session,
    ctx: ExecutionContext,
    *,
    artifact_id: uuid.UUID,
    version_no: int,
) -> ArtifactVersion:
    row = session.execute(
        select(ArtifactVersion).where(
            ArtifactVersion.org_id == ctx.org_id,
            ArtifactVersion.artifact_id == artifact_id,
            ArtifactVersion.version_no == version_no,
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("That artifact version does not exist.")
    return row


def statements_for_version(
    session: Session, ctx: ExecutionContext, *, version_id: uuid.UUID
) -> list[ArtifactStatement]:
    _version(session, ctx, version_id)
    return list(
        session.execute(
            select(ArtifactStatement)
            .where(
                ArtifactStatement.org_id == ctx.org_id,
                ArtifactStatement.artifact_version_id == version_id,
            )
            .order_by(ArtifactStatement.order_index)
        ).scalars()
    )


def _evidence(
    session: Session,
    ctx: ExecutionContext,
    evidence_ids: Sequence[uuid.UUID],
) -> list[Evidence]:
    ids = list(dict.fromkeys(evidence_ids))
    rows = list(
        session.execute(
            select(Evidence).where(
                Evidence.org_id == ctx.org_id,
                Evidence.id.in_(ids),
            )
        ).scalars()
    )
    if len(rows) != len(ids):
        raise AppValidationError("One or more evidence records are unavailable.")
    today = date.today()
    for row in rows:
        if (
            row.approval_status is not ApprovalStatus.APPROVED
            or not row.approved_for_reuse
            or row.status not in ADMISSIBLE_EVIDENCE
            or (row.expiry_date is not None and row.expiry_date < today)
        ):
            raise AppValidationError(
                "Evidence-backed statements require current, approved, unexpired evidence."
            )
    return rows


def _withdraw_draft_children(
    session: Session, ctx: ExecutionContext, statements: Sequence[ArtifactStatement]
) -> None:
    statement_ids = [row.id for row in statements]
    if statement_ids:
        session.execute(
            delete(EvidenceLink).where(
                EvidenceLink.org_id == ctx.org_id,
                EvidenceLink.target_type == EvidenceLinkTarget.ARTIFACT_STATEMENT,
                EvidenceLink.target_id.in_(statement_ids),
            )
        )
    for row in statements:
        if row.commitment_id:
            commitment = session.get(Commitment, row.commitment_id)
            if commitment is not None and commitment.org_id == ctx.org_id:
                commitment.approval_status = ApprovalStatus.WITHDRAWN
        if row.assumption_id:
            assumption = session.get(Assumption, row.assumption_id)
            if assumption is not None and assumption.org_id == ctx.org_id:
                assumption.status = AssumptionStatus.WITHDRAWN
        session.delete(row)
    session.flush()


def _approver_role(value: object) -> str:
    """The role that must approve a commitment, or a refusal that says why.

    Forgiving about SHAPE, strict about MEANING. "bid manager", "Bid-Manager"
    and "BID_MANAGER" are the same role written three ways, and rejecting the
    first two fails a whole draft over punctuation. Anything that is not one of
    the real roles is still refused -- an invented job title is a commitment
    routed to nobody, which is worse than a rejected draft.

    The old message was "A proposed commitment needs a valid approver role."
    It named neither the offending value nor the valid ones, so the only way to
    learn either was to read this function.
    """
    known = set(COMMITMENT_APPROVER_ROLES)
    raw = str(value or "").strip()
    candidate = raw.upper().replace("-", "_").replace(" ", "_")
    while "__" in candidate:
        candidate = candidate.replace("__", "_")
    if candidate in known:
        return candidate
    raise AppValidationError(
        f"A proposed commitment needs a valid approver role. Got {raw or '(nothing)'!r}. "
        f"Valid roles: {', '.join(sorted(known))}."
    )


def _add_statement(
    session: Session,
    ctx: ExecutionContext,
    *,
    artifact: ResponseArtifact,
    current: ArtifactVersion,
    index: int,
    payload: Mapping[str, Any],
    generated_by: str | None,
    section: str | None = None,
    diagram_id: uuid.UUID | None = None,
    diagram_version_no: int | None = None,
) -> ArtifactStatement:
    """Validate one typed statement and write it, with whatever it must link to."""
    if section is None:
        section = payload.get("section") or None
    if diagram_id is None and payload.get("diagram_id"):
        diagram_id = uuid.UUID(str(payload["diagram_id"]))
    if diagram_version_no is None:
        diagram_version_no = payload.get("diagram_version_no")
    paragraph_no = payload.get("paragraph_no")
    try:
        statement_class = StatementClass(str(payload["statement_class"]))
    except (KeyError, ValueError) as exc:
        raise AppValidationError("Every statement needs a recognized statement_class.") from exc
    text = str(payload.get("text", "")).strip()
    if not text:
        raise AppValidationError("A statement cannot be blank.")
    locator = payload.get("source_locator")
    evidence_ids = [uuid.UUID(str(v)) for v in payload.get("evidence_ids", [])]
    commitment_id = None
    assumption_id = None
    author_user_id = None

    if statement_class is StatementClass.EVIDENCE_BACKED_FACT:
        if not evidence_ids:
            raise AppValidationError("EVIDENCE_BACKED_FACT requires at least one evidence record.")
        _evidence(session, ctx, evidence_ids)
    elif statement_class is StatementClass.TENDER_DERIVED:
        if not isinstance(locator, dict) or not locator:
            raise AppValidationError("TENDER_DERIVED requires a source locator.")
    elif statement_class is StatementClass.DIAGRAM_DESCRIPTION:
        if diagram_id is None:
            raise AppValidationError("A diagram description must say which diagram it describes.")
    elif statement_class is StatementClass.PROPOSED_COMMITMENT:
        required_role = _approver_role(payload.get("required_approver_role"))
        owner = payload.get("commitment_owner_user_id")
        commitment = Commitment(
            org_id=ctx.org_id,
            bid_id=artifact.bid_id,
            text=text,
            owner_user_id=uuid.UUID(str(owner)) if owner else ctx.actor_user_id,
            required_approver_role=required_role,
            approval_status=ApprovalStatus.PENDING_APPROVAL,
        )
        session.add(commitment)
        session.flush()
        commitment_id = commitment.id
    elif statement_class is StatementClass.ASSUMPTION:
        assumption = Assumption(
            org_id=ctx.org_id,
            bid_id=artifact.bid_id,
            text=text,
            raised_by=ctx.actor_user_id,
            status=AssumptionStatus.OPEN,
        )
        session.add(assumption)
        session.flush()
        assumption_id = assumption.id
    elif statement_class is StatementClass.HUMAN_PROVIDED_INPUT:
        if ctx.actor_user_id is None:
            raise AppValidationError("Human-provided input needs an attributed user.")
        author_user_id = ctx.actor_user_id

    row = ArtifactStatement(
        org_id=ctx.org_id,
        artifact_version_id=current.id,
        order_index=index,
        statement_class=statement_class,
        text=text,
        source_locator=locator,
        author_user_id=author_user_id,
        generated_by=generated_by,
        approval_state=StatementApproval.DRAFT,
        commitment_id=commitment_id,
        assumption_id=assumption_id,
        evidence_ids=evidence_ids or None,
        section=section,
        diagram_id=diagram_id,
        diagram_version_no=diagram_version_no,
        paragraph_no=int(paragraph_no) if paragraph_no else None,
        requirement_codes=list(payload.get("requirement_codes") or []) or None,
    )
    session.add(row)
    session.flush()
    for evidence_id in evidence_ids:
        session.add(
            EvidenceLink(
                org_id=ctx.org_id,
                evidence_id=evidence_id,
                target_type=EvidenceLinkTarget.ARTIFACT_STATEMENT,
                target_id=row.id,
                link_role=EvidenceLinkRole.SUPPORTS,
            )
        )
    return row


def save_draft(
    session: Session,
    ctx: ExecutionContext,
    *,
    artifact_id: uuid.UUID,
    statements: Sequence[Mapping[str, Any]],
    change_reason: str | None = None,
    generated_by: str | None = None,
) -> ArtifactVersion:
    artifact = _artifact(session, ctx, artifact_id)
    current = _current_version(session, ctx, artifact)
    if current.state is not ArtifactVersionState.DRAFT:
        if artifact.state not in {
            ArtifactState.APPROVED,
            ArtifactState.REWORK_REQUIRED,
            ArtifactState.REVALIDATION_REQUIRED,
        }:
            raise StateConflict("The current version is under review and cannot be edited.")
        next_no = (
            session.execute(
                select(func.max(ArtifactVersion.version_no)).where(
                    ArtifactVersion.org_id == ctx.org_id,
                    ArtifactVersion.artifact_id == artifact.id,
                )
            ).scalar_one()
            or 0
        )
        current = ArtifactVersion(
            org_id=ctx.org_id,
            artifact_id=artifact.id,
            version_no=next_no + 1,
            state=ArtifactVersionState.DRAFT,
            supersedes_version_id=artifact.approved_version_id,
            revalidation_reason=change_reason,
            created_by=ctx.actor_user_id,
        )
        session.add(current)
        session.flush()
        artifact.current_version_id = current.id
        artifact.state = ArtifactState.DRAFT
    else:
        prior = statements_for_version(session, ctx, version_id=current.id)
        _withdraw_draft_children(session, ctx, prior)

    if not statements:
        raise AppValidationError("A draft needs at least one typed statement.")

    rendered: list[str] = []
    for index, payload in enumerate(statements):
        row = _add_statement(
            session,
            ctx,
            artifact=artifact,
            current=current,
            index=index,
            payload=payload,
            generated_by=generated_by,
        )
        rendered.append(row.text)
    current.rendered_text = "\n\n".join(rendered)
    session.flush()
    audit.record(
        session,
        ctx,
        action="artifact.draft_saved",
        entity_type="artifact_version",
        entity_id=current.id,
        bid_id=artifact.bid_id,
        after={"version": current.version_no, "statements": len(statements)},
        reason=change_reason,
    )
    return current


def write_section(
    session: Session,
    ctx: ExecutionContext,
    *,
    artifact_id: uuid.UUID,
    section: str,
    statements: Sequence[Mapping[str, Any]],
    diagram_id: uuid.UUID | None = None,
    diagram_version_no: int | None = None,
    generated_by: str | None = None,
    change_reason: str | None = None,
    append: bool = False,
) -> ArtifactVersion:
    """Add one section to the current draft, or rewrite it if it is already there.

    With `append` the statements are added after whatever the section already holds,
    which is how a long section arrives in parts.

    A whole-draft save replaces everything, which is wrong for a write-up that
    arrives a section at a time: the sections already written -- and any the
    person edited -- must stay. A section is identified by the diagram it explains
    (or by its heading when it explains none). Rewriting one withdraws only that
    section's commitments and assumptions and puts the new run where the old one
    was. Only a draft can grow; a version under review or approved is left alone.
    """
    artifact = _artifact(session, ctx, artifact_id)
    current = _current_version(session, ctx, artifact)
    if current.state is not ArtifactVersionState.DRAFT:
        raise StateConflict(
            "The response is already with reviewers, so I can't add to it. "
            "Ask for changes once it is back in draft."
        )
    if not statements:
        raise AppValidationError("A section needs at least one typed statement.")

    existing = statements_for_version(session, ctx, version_id=current.id)

    def in_section(row: ArtifactStatement) -> bool:
        if diagram_id is not None:
            return row.diagram_id == diagram_id
        return row.diagram_id is None and row.section == section

    old = [row for row in existing if in_section(row)]
    if append:
        keep = list(existing)
        last = max((row.order_index for row in old), default=None)
        position = None if last is None else last + 1
    else:
        keep = [row for row in existing if not in_section(row)]
        position = min((row.order_index for row in old), default=None)
        if old:
            _withdraw_draft_children(session, ctx, old)

    # Everything after the old section moves down to make room; a new section goes last.
    if position is None:
        position = (max((row.order_index for row in keep), default=-1)) + 1
    for row in keep:
        if row.order_index >= position:
            row.order_index += len(statements)
    session.flush()

    for offset, payload in enumerate(statements):
        _add_statement(
            session,
            ctx,
            artifact=artifact,
            current=current,
            index=position + offset,
            payload=payload,
            generated_by=generated_by,
            section=section,
            diagram_id=diagram_id,
            diagram_version_no=diagram_version_no,
        )
    # Close any gap left by a rewrite that produced fewer statements.
    ordered = statements_for_version(session, ctx, version_id=current.id)
    for index, row in enumerate(ordered):
        row.order_index = index
    current.rendered_text = "\n\n".join(row.text for row in ordered)
    session.flush()
    audit.record(
        session,
        ctx,
        action="artifact.section_written",
        entity_type="artifact_version",
        entity_id=current.id,
        bid_id=artifact.bid_id,
        after={"section": section, "statements": len(statements), "rewrote": bool(old)},
        reason=change_reason,
    )
    return current


def drop_diagram_section(
    session: Session, ctx: ExecutionContext, *, artifact_id: uuid.UUID, diagram_id: uuid.UUID
) -> int:
    """Take a removed diagram's section out of the draft. Returns how many statements went.

    Only a draft changes; a version already with reviewers is left exactly as it is.
    """
    artifact = _artifact(session, ctx, artifact_id)
    current = _current_version(session, ctx, artifact)
    if current.state is not ArtifactVersionState.DRAFT:
        return 0
    rows = statements_for_version(session, ctx, version_id=current.id)
    gone = [row for row in rows if row.diagram_id == diagram_id]
    if not gone:
        return 0
    _withdraw_draft_children(session, ctx, gone)
    from app.services import draft_images

    draft_images.drop_for(session, ctx, artifact_id=artifact_id, diagram_id=diagram_id)
    left = statements_for_version(session, ctx, version_id=current.id)
    for index, row in enumerate(left):
        row.order_index = index
    current.rendered_text = "\n\n".join(row.text for row in left)
    session.flush()
    return len(gone)


def drop_section(
    session: Session, ctx: ExecutionContext, *, artifact_id: uuid.UUID, section: str
) -> int:
    """Take a heading-only section (one that explains no diagram) out of the draft."""
    artifact = _artifact(session, ctx, artifact_id)
    current = _current_version(session, ctx, artifact)
    if current.state is not ArtifactVersionState.DRAFT:
        return 0
    rows = statements_for_version(session, ctx, version_id=current.id)
    gone = [row for row in rows if row.diagram_id is None and row.section == section]
    if not gone:
        return 0
    _withdraw_draft_children(session, ctx, gone)
    from app.services import draft_images

    draft_images.drop_for(session, ctx, artifact_id=artifact_id, section=section)
    left = statements_for_version(session, ctx, version_id=current.id)
    for index, row in enumerate(left):
        row.order_index = index
    current.rendered_text = "\n\n".join(row.text for row in left)
    session.flush()
    return len(gone)


def _validate_for_submission(
    session: Session, ctx: ExecutionContext, version: ArtifactVersion
) -> list[ArtifactStatement]:
    rows = statements_for_version(session, ctx, version_id=version.id)
    if not rows:
        raise AppValidationError("An empty artifact cannot be submitted for review.")
    for row in rows:
        if row.statement_class is StatementClass.EVIDENCE_BACKED_FACT:
            _evidence(session, ctx, row.evidence_ids or [])
        if row.statement_class is StatementClass.TENDER_DERIVED and not row.source_locator:
            raise AppValidationError("A tender-derived statement lost its source locator.")
    return rows


def submit(
    session: Session,
    ctx: ExecutionContext,
    *,
    artifact_id: uuid.UUID,
    approval_roles: Sequence[str],
    note: str | None = None,
) -> ResponseArtifact:
    from app.services import tasks as task_service

    artifact = _artifact(session, ctx, artifact_id)
    version = _current_version(session, ctx, artifact)
    if version.state is not ArtifactVersionState.DRAFT:
        raise IllegalTransition("Only a DRAFT artifact version can be submitted.")
    rows = _validate_for_submission(session, ctx, version)

    roles = list(dict.fromkeys(str(role) for role in approval_roles))
    allowed_roles = {r.value for r in RoleCode}
    if not roles or any(role not in allowed_roles for role in roles):
        raise AppValidationError("Every approval step needs a recognized role.")

    task = session.get(Task, artifact.task_id)
    if task is None or task.org_id != ctx.org_id:
        raise StateConflict("The artifact's task no longer exists.")
    if task.state is TaskState.BLOCKED:
        # Blocked means no evidence proves a requirement, not that nobody may
        # answer it. A draft that answers every requirement, or states what it
        # is waiting on as a condition, can go to review; the review and the
        # commitment approvals are the control, and the readiness views go on
        # showing the requirement as blocked. A requirement with no answer at
        # all still holds the task back.
        from app.services import writeup

        open_codes = [
            c["code"]
            for c in writeup.coverage_for(session, ctx, task_id=task.id, rows=rows)
            if c["state"] in ("unanswered", "restated")
        ]
        if open_codes:
            raise IllegalTransition(
                "This task is blocked and these requirements have no answer yet: "
                + ", ".join(open_codes)
                + ". Answer them (a condition is a valid answer) before submitting."
            )
        task_service.transition(
            session,
            ctx,
            task_id=task.id,
            to_state=TaskState.IN_PROGRESS,
            reason="Every requirement is answered or stated as a condition; the missing "
            "proof stays visible in readiness.",
        )
    if task.state in {
        TaskState.NOT_STARTED,
        TaskState.AI_READY_FOR_REVIEW,
        TaskState.NEEDS_INPUT,
        TaskState.REWORK_REQUIRED,
        TaskState.REVALIDATION_REQUIRED,
    }:
        task_service.transition(
            session,
            ctx,
            task_id=task.id,
            to_state=TaskState.IN_PROGRESS,
            reason="A typed artifact draft is ready to submit.",
        )
    if task.state is not TaskState.IN_PROGRESS:
        raise IllegalTransition("The task must be IN_PROGRESS before its artifact is submitted.")

    version.state = ArtifactVersionState.SUBMITTED_FOR_REVIEW
    artifact.state = ArtifactState.SUBMITTED_FOR_REVIEW
    for row in rows:
        row.approval_state = StatementApproval.PENDING
    session.flush()

    for step_no, role in enumerate(roles, start=1):
        session.add(
            Approval(
                org_id=ctx.org_id,
                entity_type=ENTITY_TYPE,
                entity_id=version.id,
                step_no=step_no,
                required_role=role,
                state=ApprovalStepState.PENDING,
                is_blocking=True,
            )
        )
    version.state = ArtifactVersionState.IN_REVIEW
    artifact.state = ArtifactState.IN_REVIEW
    session.flush()
    task_service.transition(
        session,
        ctx,
        task_id=task.id,
        to_state=TaskState.READY_FOR_APPROVAL,
        reason="The artifact entered its ordered approval chain.",
    )
    audit.record(
        session,
        ctx,
        action="artifact.submitted",
        entity_type="artifact_version",
        entity_id=version.id,
        bid_id=artifact.bid_id,
        before={"state": ArtifactVersionState.DRAFT.value},
        after={"state": version.state.value, "approval_roles": roles},
        reason=note,
    )
    return artifact


def approval_chain(
    session: Session,
    ctx: ExecutionContext,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
) -> list[Approval]:
    return list(
        session.execute(
            select(Approval)
            .where(
                Approval.org_id == ctx.org_id,
                Approval.entity_type == entity_type,
                Approval.entity_id == entity_id,
            )
            .order_by(Approval.step_no)
        ).scalars()
    )


def list_approvals(
    session: Session,
    ctx: ExecutionContext,
    *,
    entity_type: str | None = None,
    state: ApprovalStepState | None = None,
    mine: bool = False,
) -> list[Approval]:
    stmt = select(Approval).where(Approval.org_id == ctx.org_id)
    if entity_type:
        stmt = stmt.where(Approval.entity_type == entity_type)
    if state:
        stmt = stmt.where(Approval.state == state)
    if mine:
        if ctx.actor_user_id is None:
            return []
        stmt = stmt.where(
            or_(
                Approval.approver_user_id == ctx.actor_user_id,
                Approval.required_role.in_(list(ctx.role_codes)),
            )
        )
    return list(session.execute(stmt.order_by(Approval.created_at, Approval.step_no)).scalars())


def _unapproved_commitments(
    session: Session, ctx: ExecutionContext, version_id: uuid.UUID
) -> list[uuid.UUID]:
    return list(
        session.execute(
            select(Commitment.id)
            .join(ArtifactStatement, ArtifactStatement.commitment_id == Commitment.id)
            .where(
                ArtifactStatement.org_id == ctx.org_id,
                ArtifactStatement.artifact_version_id == version_id,
                Commitment.approval_status != ApprovalStatus.APPROVED,
            )
        ).scalars()
    )


def staffed_approver_roles(session: Session, ctx: ExecutionContext) -> set[str]:
    """Role codes at least one person in this organization actually holds.

    Which roles exist in the product is a closed enum; which of them this
    customer has STAFFED is a fact about their user table, and the difference
    is the whole point. The draft model is shown the enum, because that is what
    the server validates against -- so an organization with no
    SOLUTION_ARCHITECT can still be handed a commitment routed to one, and that
    commitment is then pending approval from nobody.

    Counting holders is code's job, for the same reason counting evidence is:
    it is arithmetic over the database, and a model asked to do it will guess.
    """
    from app.models import Role, UserRole

    return set(
        session.execute(
            select(Role.code)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(Role.org_id == ctx.org_id, UserRole.org_id == ctx.org_id)
            .distinct()
        ).scalars()
    )


def blocking_commitments(
    session: Session, ctx: ExecutionContext, *, version_id: uuid.UUID
) -> list[Commitment]:
    """The unapproved commitments on this version, in full.

    `_unapproved_commitments` answers "may this pass". This answers "why not",
    and the second question is the one a person has to act on.
    """
    return list(
        session.execute(
            select(Commitment)
            .join(ArtifactStatement, ArtifactStatement.commitment_id == Commitment.id)
            .where(
                ArtifactStatement.org_id == ctx.org_id,
                ArtifactStatement.artifact_version_id == version_id,
                Commitment.approval_status != ApprovalStatus.APPROVED,
            )
            .order_by(Commitment.created_at)
        )
        .scalars()
        .unique()
    )


def commitment_blockage(session: Session, ctx: ExecutionContext, *, version_id: uuid.UUID) -> str:
    """One sentence naming what is holding this version, and who can move it.

    The message this replaces was "Every proposed commitment must be approved
    before the artifact can pass." True, and useless: it named neither the
    commitments nor the roles, so the only way to learn either was to query the
    table by hand -- which is precisely what the fixture walk had to do.

    The distinction it draws is the one that decides what a person does next.
    A commitment waiting on a role somebody holds is a QUEUE: go and ask them.
    A commitment waiting on a role NOBODY holds is not a queue at all -- it
    will never be decided, no approver was ever notified, and the artifact is
    stopped for good until a person either staffs that role or re-routes the
    promise. From outside the two are indistinguishable, and the second is
    another instance of the failure this project keeps paying for: an
    obstruction that produces no signal.

    Nothing here re-routes anything. Code may report that a commitment has no
    possible approver; deciding to move it to another role changes who may bind
    the company, and that is a person's decision and the audit log's record.
    """
    rows = blocking_commitments(session, ctx, version_id=version_id)
    if not rows:
        return ""
    staffed = staffed_approver_roles(session, ctx)
    orphaned = sorted(
        {r.required_approver_role for r in rows if r.required_approver_role not in staffed}
    )
    parts = [
        f"{len(rows)} proposed commitment(s) on this version are unapproved; every one "
        "must be decided before the artifact can pass."
    ]
    if orphaned:
        parts.append(
            "Nobody in this organization holds "
            + ", ".join(orphaned)
            + ", so those commitments cannot be approved by anyone as routed. Assign the "
            "role to somebody, or have an authorized person re-route the commitment to a "
            "role you staff."
        )
    return " ".join(parts)


def unconfirmed_requirements(
    session: Session, ctx: ExecutionContext, *, artifact_id: uuid.UUID
) -> list[str]:
    """Codes of the requirements this artifact answers that nobody has accepted.

    The last gate in the chain, and the one that was missing.

    Everything else about a response is approved by a person: the evidence by a
    knowledge administrator, the version by a reviewer, each proposed commitment
    by a named role. The model's reading of the CLAUSE was approved by nobody --
    `RequirementLifecycle.CONFIRMED` was written by one function and read by
    nothing, so a requirement stayed DRAFT_EXTRACTED forever and it changed
    nothing. Every other approval therefore approved an answer to a question no
    person had checked, and a misread clause carried the whole chain with it.

    Checked at APPROVAL rather than earlier on purpose. Work should start before
    the requirement list is signed off -- assessment, assignment, drafting, even
    submitting for review are all reversible and all useful while somebody is
    still reading the tender. What must not happen is an IMMUTABLE version
    becoming the company's answer to a clause nobody has agreed was read
    correctly.
    """
    rows = session.execute(
        select(Requirement.code)
        .join(ArtifactRequirement, ArtifactRequirement.requirement_id == Requirement.id)
        .where(
            ArtifactRequirement.org_id == ctx.org_id,
            ArtifactRequirement.artifact_id == artifact_id,
            Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN,
            # The LIFECYCLE is the fact; `confirmed_by` / `confirmed_at` are
            # attribution beside it.
            #
            # The first version of this checked `confirmed_at IS NULL` instead,
            # which created two sources of truth for one thing and immediately
            # disagreed with itself: rows confirmed before the columns existed
            # read CONFIRMED with a NULL timestamp and were refused as
            # unconfirmed. Collapsing two facts into one column, or splitting
            # one fact across two, is the same mistake in opposite directions,
            # and this project has a rule about it.
            #
            # So a legacy CONFIRMED row with no attribution passes: somebody did
            # accept it, and all that is missing is the record of who.
            Requirement.lifecycle_state != RequirementLifecycle.CONFIRMED,
        )
    ).scalars()
    return sorted(rows)


def decide_approval(
    session: Session,
    ctx: ExecutionContext,
    *,
    approval_id: uuid.UUID,
    decision: ApprovalStepState,
    comment: str | None = None,
) -> Approval:
    from app.services import tasks as task_service

    approval = session.get(Approval, approval_id)
    if approval is None or approval.org_id != ctx.org_id:
        raise NotFound("That approval step does not exist.")
    if approval.state is not ApprovalStepState.PENDING:
        raise StateConflict("That approval step has already been decided.")
    if decision not in {ApprovalStepState.APPROVED, ApprovalStepState.REJECTED}:
        raise AppValidationError("An approval decision must be APPROVED or REJECTED.")
    if approval.approver_user_id not in (None, ctx.actor_user_id):
        raise PermissionDenied("That approval step is assigned to another user.")
    if approval.required_role not in ctx.role_codes:
        raise PermissionDenied(f"This step requires the {approval.required_role} role.")
    earlier_open = session.execute(
        select(func.count())
        .select_from(Approval)
        .where(
            Approval.org_id == ctx.org_id,
            Approval.entity_type == approval.entity_type,
            Approval.entity_id == approval.entity_id,
            Approval.step_no < approval.step_no,
            Approval.is_blocking.is_(True),
            Approval.state != ApprovalStepState.APPROVED,
        )
    ).scalar_one()
    if earlier_open:
        raise StateConflict("Earlier blocking approval steps must be approved first.")

    if approval.entity_type != ENTITY_TYPE:
        raise AppValidationError("This endpoint currently decides artifact-version approvals.")
    version = _version(session, ctx, approval.entity_id)
    artifact = _artifact(session, ctx, version.artifact_id)
    if version.state is not ArtifactVersionState.IN_REVIEW:
        raise StateConflict("The artifact version is not in review.")

    remaining_after_this = session.execute(
        select(func.count())
        .select_from(Approval)
        .where(
            Approval.org_id == ctx.org_id,
            Approval.entity_type == approval.entity_type,
            Approval.entity_id == approval.entity_id,
            Approval.id != approval.id,
            Approval.is_blocking.is_(True),
            Approval.state != ApprovalStepState.APPROVED,
        )
    ).scalar_one()
    if (
        decision is ApprovalStepState.APPROVED
        and remaining_after_this == 0
        and _unapproved_commitments(session, ctx, version.id)
    ):
        raise StateConflict(commitment_blockage(session, ctx, version_id=version.id))

    # And the question itself has to have been accepted by somebody.
    if decision is ApprovalStepState.APPROVED and remaining_after_this == 0:
        unconfirmed = unconfirmed_requirements(session, ctx, artifact_id=artifact.id)
        if unconfirmed:
            raise StateConflict(
                "This answers "
                + ", ".join(unconfirmed)
                + ", which nobody has confirmed. Approving would make it the company's "
                "immutable answer to a clause no person has agreed was read correctly. "
                "Confirm the requirement first."
            )

    now = datetime.now(timezone.utc)
    approval.state = decision
    approval.approver_user_id = ctx.actor_user_id
    approval.decided_at = now
    approval.comment = comment
    session.add(
        Review(
            org_id=ctx.org_id,
            entity_type=ENTITY_TYPE,
            entity_id=version.id,
            reviewer_user_id=ctx.actor_user_id,
            decision=(
                ReviewDecision.APPROVED
                if decision is ApprovalStepState.APPROVED
                else ReviewDecision.CHANGES_REQUESTED
            ),
            comment=comment,
            reviewed_at=now,
        )
    )

    task = session.get(Task, artifact.task_id)
    if task is None or task.org_id != ctx.org_id:
        raise StateConflict("The artifact's task no longer exists.")
    if decision is ApprovalStepState.REJECTED:
        if not (comment or "").strip():
            raise AppValidationError("A rejection requires a comment.")
        version.state = ArtifactVersionState.REWORK_REQUIRED
        artifact.state = ArtifactState.REWORK_REQUIRED
        for row in statements_for_version(session, ctx, version_id=version.id):
            row.approval_state = StatementApproval.REJECTED
        task_service.transition(
            session,
            ctx,
            task_id=task.id,
            to_state=TaskState.REWORK_REQUIRED,
            reason=comment,
        )
    else:
        if remaining_after_this == 0:
            previous = artifact.approved_version_id
            if previous and previous != version.id:
                prior = _version(session, ctx, previous)
                prior.state = ArtifactVersionState.SUPERSEDED
            version.state = ArtifactVersionState.APPROVED
            version.approved_by = ctx.actor_user_id
            version.approved_at = now
            artifact.state = ArtifactState.APPROVED
            artifact.approved_version_id = version.id
            for row in statements_for_version(session, ctx, version_id=version.id):
                row.approval_state = StatementApproval.APPROVED
                row.approved_by = ctx.actor_user_id
                row.approved_at = now
            task_service.transition(
                session,
                ctx,
                task_id=task.id,
                to_state=TaskState.APPROVED,
                reason="Every blocking artifact approval step passed.",
            )
            task_service.transition(
                session,
                ctx,
                task_id=task.id,
                to_state=TaskState.COMPLETED,
                reason="The task has an approved immutable artifact version.",
            )
    session.flush()
    audit.record(
        session,
        ctx,
        action="approval.decided",
        entity_type="approval",
        entity_id=approval.id,
        bid_id=artifact.bid_id,
        before={"state": ApprovalStepState.PENDING.value},
        after={"state": decision.value, "step": approval.step_no},
        reason=comment,
    )
    if task.owner_user_id is not None:
        notifications.notify(
            session,
            ctx,
            recipients=[task.owner_user_id],
            kind="approval.decided",
            title=f"Your draft for {task.code} was {decision.value.lower()}",
            body=comment,
            screen="approvals",
            bid_id=artifact.bid_id,
            focus_id=artifact.id,
        )
    return approval


def record_review(
    session: Session,
    ctx: ExecutionContext,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    decision: ReviewDecision,
    comment: str | None = None,
) -> Review:
    if ctx.actor_user_id is None:
        raise AppValidationError("A review needs an attributed reviewer.")
    row = Review(
        org_id=ctx.org_id,
        entity_type=entity_type,
        entity_id=entity_id,
        reviewer_user_id=ctx.actor_user_id,
        decision=decision,
        comment=comment,
        reviewed_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        ctx,
        action="review.recorded",
        entity_type=entity_type,
        entity_id=entity_id,
        after={"decision": decision.value},
        reason=comment,
    )
    return row


def list_commitments(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    approval_status: ApprovalStatus | None = None,
) -> list[Commitment]:
    stmt = select(Commitment).where(Commitment.org_id == ctx.org_id, Commitment.bid_id == bid_id)
    if approval_status:
        stmt = stmt.where(Commitment.approval_status == approval_status)
    return list(session.execute(stmt.order_by(Commitment.created_at)).scalars())


def decide_commitment(
    session: Session,
    ctx: ExecutionContext,
    *,
    commitment_id: uuid.UUID,
    decision: ApprovalStatus,
    comment: str | None = None,
) -> Commitment:
    row = session.get(Commitment, commitment_id)
    if row is None or row.org_id != ctx.org_id:
        raise NotFound("That commitment does not exist.")
    if row.approval_status is not ApprovalStatus.PENDING_APPROVAL:
        raise StateConflict("That commitment is not pending approval.")
    if row.required_approver_role not in ctx.role_codes:
        raise PermissionDenied(f"This commitment requires {row.required_approver_role} approval.")
    if decision not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
        raise AppValidationError("A commitment decision must be APPROVED or REJECTED.")
    if decision is ApprovalStatus.REJECTED and not (comment or "").strip():
        raise AppValidationError("A rejected commitment needs a reason.")
    row.approval_status = decision
    row.approved_by = ctx.actor_user_id if decision is ApprovalStatus.APPROVED else None
    row.approved_at = datetime.now(timezone.utc) if decision is ApprovalStatus.APPROVED else None
    row.rejection_reason = comment if decision is ApprovalStatus.REJECTED else None
    session.flush()
    audit.record(
        session,
        ctx,
        action="commitment.decided",
        entity_type="commitment",
        entity_id=row.id,
        bid_id=row.bid_id,
        after={"approval_status": decision.value},
        reason=comment,
    )
    if row.owner_user_id is not None:
        notifications.notify(
            session,
            ctx,
            recipients=[row.owner_user_id],
            kind="commitment.decided",
            title=f"A commitment you own was {decision.value.lower()}",
            body=comment,
            screen="approvals",
            bid_id=row.bid_id,
            focus_id=row.id,
        )
    return row


def list_assumptions(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    status: AssumptionStatus | None = None,
) -> list[Assumption]:
    stmt = select(Assumption).where(Assumption.org_id == ctx.org_id, Assumption.bid_id == bid_id)
    if status:
        stmt = stmt.where(Assumption.status == status)
    return list(session.execute(stmt.order_by(Assumption.created_at)).scalars())


def update_assumption(
    session: Session,
    ctx: ExecutionContext,
    *,
    assumption_id: uuid.UUID,
    status: AssumptionStatus,
    resolution: str | None = None,
) -> Assumption:
    row = session.get(Assumption, assumption_id)
    if row is None or row.org_id != ctx.org_id:
        raise NotFound("That assumption does not exist.")
    if status is AssumptionStatus.RESOLVED and not (resolution or "").strip():
        raise AppValidationError("Resolving an assumption requires a resolution.")
    before = row.status
    row.status = status
    row.resolution = resolution
    if status in {AssumptionStatus.RESOLVED, AssumptionStatus.WITHDRAWN}:
        row.resolved_by = ctx.actor_user_id
        row.resolved_at = datetime.now(timezone.utc)
    session.flush()
    audit.record(
        session,
        ctx,
        action="assumption.updated",
        entity_type="assumption",
        entity_id=row.id,
        bid_id=row.bid_id,
        before={"status": before.value},
        after={"status": status.value},
        reason=resolution,
    )
    return row


#: Sources whose excerpt an approver does not read merely because they can
#: read the answer built on them.
_EXCERPT_WITHHELD = frozenset({ConfidentialityClass.RESTRICTED, ConfidentialityClass.PRIVILEGED})


def approval_review(session: Session, ctx: ExecutionContext, *, approval_id: uuid.UUID) -> dict:
    """Everything an approver needs to read before deciding.

    The approval row names an artifact version by identifier. Deciding on an
    identifier is signing something unread, so this returns the answer itself:
    the task, the requirements it answers, every statement with the evidence or
    commitment behind it, and what changed since the previous version.

    A source classified RESTRICTED or PRIVILEGED is named but its excerpt is
    withheld: being asked to approve an answer is not a grant to read the
    document behind it.
    """
    from app.models import Document, DocumentVersion

    approval = session.get(Approval, approval_id)
    if approval is None or approval.org_id != ctx.org_id:
        raise NotFound("That approval does not exist.")
    if approval.entity_type != ENTITY_TYPE:
        return {
            "approval_id": approval.id,
            "entity_type": approval.entity_type,
            "reviewable": False,
            "note": "This approval is not for a written response, so there is no text to show.",
        }

    version = _version(session, ctx, approval.entity_id)
    artifact = _artifact(session, ctx, version.artifact_id)
    task = session.get(Task, artifact.task_id)

    # The clause wording lives on the requirement's current version, not on the
    # requirement row itself.
    requirements = list(
        session.execute(
            select(Requirement, RequirementVersion.requirement_text)
            .join(ArtifactRequirement, ArtifactRequirement.requirement_id == Requirement.id)
            .outerjoin(RequirementVersion, RequirementVersion.id == Requirement.current_version_id)
            .where(
                ArtifactRequirement.org_id == ctx.org_id,
                ArtifactRequirement.artifact_id == artifact.id,
                LIVE_REQUIREMENT,
            )
            .order_by(Requirement.code)
        ).all()
    )

    current = statements_for_version(session, ctx, version_id=version.id)
    previous_row = session.execute(
        select(ArtifactVersion)
        .where(
            ArtifactVersion.org_id == ctx.org_id,
            ArtifactVersion.artifact_id == artifact.id,
            ArtifactVersion.version_no < version.version_no,
        )
        .order_by(ArtifactVersion.version_no.desc())
        .limit(1)
    ).scalar_one_or_none()
    previous = (
        statements_for_version(session, ctx, version_id=previous_row.id) if previous_row else []
    )
    previous_texts = {(row.text or "").strip() for row in previous}
    current_texts = {(row.text or "").strip() for row in current}

    evidence_ids = {eid for row in current for eid in (row.evidence_ids or [])}
    evidence: dict = {}
    if evidence_ids:
        for row in session.execute(
            select(Evidence).where(Evidence.org_id == ctx.org_id, Evidence.id.in_(evidence_ids))
        ).scalars():
            title = None
            version_row = session.get(DocumentVersion, row.source_document_version_id)
            if version_row is not None:
                document = session.get(Document, version_row.document_id)
                title = document.title if document is not None else None
            withheld = row.confidentiality_class in _EXCERPT_WITHHELD
            evidence[row.id] = {
                "id": row.id,
                "document": title,
                "locator": row.source_locator,
                "excerpt": None if withheld else (row.excerpt or "")[:600],
                "excerpt_withheld": withheld,
                "status": row.status.value,
                "expiry_date": row.expiry_date.isoformat() if row.expiry_date else None,
                "is_substantiated": row.is_substantiated,
            }

    commitments = {
        c.id: c
        for c in session.execute(
            select(Commitment).where(
                Commitment.org_id == ctx.org_id,
                Commitment.id.in_([r.commitment_id for r in current if r.commitment_id] or [None]),
            )
        ).scalars()
    }

    rows = []
    for statement in current:
        commitment = commitments.get(statement.commitment_id) if statement.commitment_id else None
        rows.append(
            {
                "id": statement.id,
                "order_index": statement.order_index,
                "statement_class": statement.statement_class.value,
                "text": statement.text,
                "change": (
                    "UNCHANGED" if (statement.text or "").strip() in previous_texts else "NEW"
                ),
                "evidence": [
                    evidence[e] for e in (statement.evidence_ids or []) if e in evidence
                ],
                "commitment": (
                    {
                        "required_approver_role": commitment.required_approver_role,
                        "approval_status": commitment.approval_status.value,
                    }
                    if commitment is not None
                    else None
                ),
                "source_locator": statement.source_locator,
            }
        )

    return {
        "approval_id": approval.id,
        "entity_type": approval.entity_type,
        "reviewable": True,
        "task": {"id": task.id, "code": task.code, "title": task.title} if task else None,
        "artifact_title": artifact.title,
        "version_no": version.version_no,
        "previous_version_no": previous_row.version_no if previous_row else None,
        "revalidation_reason": version.revalidation_reason,
        "requirements": [
            {"code": r.code, "text": text or "", "mandatory": bool(r.is_mandatory)}
            for r, text in requirements
        ],
        "statements": rows,
        "removed_statements": [
            {"statement_class": r.statement_class.value, "text": r.text}
            for r in previous
            if (r.text or "").strip() not in current_texts
        ],
    }


def commitment_roles(
    session: Session, ctx: ExecutionContext, commitment_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Which role must approve each commitment, so an editor can save it back."""
    if not commitment_ids:
        return {}
    return {
        row.id: row.required_approver_role
        for row in session.execute(
            select(Commitment).where(
                Commitment.org_id == ctx.org_id, Commitment.id.in_(list(commitment_ids))
            )
        ).scalars()
    }


# --- editing the draft one sentence at a time ---------------------------------


def _draft_statement(
    session: Session, ctx: ExecutionContext, *, artifact_id: uuid.UUID, statement_id: uuid.UUID
) -> tuple[ResponseArtifact, ArtifactVersion, ArtifactStatement]:
    artifact = _artifact(session, ctx, artifact_id)
    current = _current_version(session, ctx, artifact)
    if current.state is not ArtifactVersionState.DRAFT:
        raise StateConflict(
            "The response is already with reviewers, so it can't be edited here. "
            "Ask for changes once it is back in draft."
        )
    row = session.get(ArtifactStatement, statement_id)
    if row is None or row.org_id != ctx.org_id or row.artifact_version_id != current.id:
        raise NotFound("That sentence is not in the current draft.")
    return artifact, current, row


def _renumber(session: Session, ctx: ExecutionContext, current: ArtifactVersion) -> None:
    ordered = statements_for_version(session, ctx, version_id=current.id)
    for index, item in enumerate(ordered):
        item.order_index = index
    current.rendered_text = "\n\n".join(item.text for item in ordered)
    session.flush()


def edit_statement_text(
    session: Session,
    ctx: ExecutionContext,
    *,
    artifact_id: uuid.UUID,
    statement_id: uuid.UUID,
    text: str,
) -> ArtifactStatement:
    """Change the words of one sentence in the draft.

    A sentence that was backed by evidence or by the tender is no longer backed once
    a person has put different words on it, so it becomes that person's own input --
    the platform never keeps a citation on wording the source did not say. A promise
    or a condition keeps its kind and its approval trail, and the promise or
    condition itself is updated to the new words.
    """
    new = " ".join(str(text).split())
    if not new:
        raise AppValidationError("A sentence cannot be blank.")
    if ctx.actor_user_id is None:
        raise AppValidationError("An edit needs an attributed user.")
    artifact, current, row = _draft_statement(
        session, ctx, artifact_id=artifact_id, statement_id=statement_id
    )
    if new == row.text:
        return row
    before = {"text": row.text, "class": row.statement_class.value}
    if row.statement_class in (StatementClass.EVIDENCE_BACKED_FACT, StatementClass.TENDER_DERIVED):
        session.execute(
            delete(EvidenceLink).where(
                EvidenceLink.org_id == ctx.org_id,
                EvidenceLink.target_type == EvidenceLinkTarget.ARTIFACT_STATEMENT,
                EvidenceLink.target_id == row.id,
            )
        )
        row.statement_class = StatementClass.HUMAN_PROVIDED_INPUT
        row.author_user_id = ctx.actor_user_id
        row.source_locator = None
        row.evidence_ids = None
        row.generated_by = None
    elif row.statement_class is StatementClass.PROPOSED_COMMITMENT and row.commitment_id:
        commitment = session.get(Commitment, row.commitment_id)
        if commitment is not None and commitment.org_id == ctx.org_id:
            commitment.text = new
            commitment.approval_status = ApprovalStatus.PENDING_APPROVAL
    elif row.statement_class is StatementClass.ASSUMPTION and row.assumption_id:
        assumption = session.get(Assumption, row.assumption_id)
        if assumption is not None and assumption.org_id == ctx.org_id:
            assumption.text = new
    row.text = new
    session.flush()
    _renumber(session, ctx, current)
    audit.record(
        session,
        ctx,
        action="artifact.statement_edited",
        entity_type="artifact_version",
        entity_id=current.id,
        bid_id=artifact.bid_id,
        before=before,
        after={"text": new, "class": row.statement_class.value},
    )
    return row


def delete_statement(
    session: Session, ctx: ExecutionContext, *, artifact_id: uuid.UUID, statement_id: uuid.UUID
) -> None:
    """Take one sentence out of the draft, with the promise or condition behind it."""
    artifact, current, row = _draft_statement(
        session, ctx, artifact_id=artifact_id, statement_id=statement_id
    )
    before = {"text": row.text, "class": row.statement_class.value}
    _withdraw_draft_children(session, ctx, [row])
    _renumber(session, ctx, current)
    audit.record(
        session,
        ctx,
        action="artifact.statement_removed",
        entity_type="artifact_version",
        entity_id=current.id,
        bid_id=artifact.bid_id,
        before=before,
    )


def add_statement_after(
    session: Session,
    ctx: ExecutionContext,
    *,
    artifact_id: uuid.UUID,
    after_statement_id: uuid.UUID,
    text: str,
    new_paragraph: bool = False,
    before: bool = False,
) -> ArtifactStatement:
    """Add a sentence of the person's own wording after another.

    By default it joins the same paragraph. With `new_paragraph` it opens a paragraph of
    its own after the anchor's paragraph, and later paragraphs of the section (and the
    pictures after them) move down by one. With `before` the paragraph opens the anchor's
    paragraph's place instead, so the anchor and everything after it moves down -- how a
    paragraph goes directly under a section's diagram.
    """
    new = " ".join(str(text).split())
    if not new:
        raise AppValidationError("A sentence cannot be blank.")
    if ctx.actor_user_id is None:
        raise AppValidationError("An edit needs an attributed user.")
    artifact, current, anchor = _draft_statement(
        session, ctx, artifact_id=artifact_id, statement_id=after_statement_id
    )
    everything = statements_for_version(session, ctx, version_id=current.id)
    paragraph_no = anchor.paragraph_no
    position = anchor.order_index + 1
    if before:
        number = anchor.paragraph_no or 1
        same = [
            item
            for item in everything
            if item.diagram_id == anchor.diagram_id
            and item.section == anchor.section
            and (item.paragraph_no or 1) == number
        ]
        position = min(item.order_index for item in same)
        for item in everything:
            if (
                item.diagram_id == anchor.diagram_id
                and item.section == anchor.section
                and (item.paragraph_no or 1) >= number
            ):
                item.paragraph_no = (item.paragraph_no or 1) + 1
        paragraph_no = number
        from app.services import draft_images

        draft_images.shift_after(
            session,
            ctx,
            artifact_id=artifact_id,
            section=anchor.section,
            diagram_id=anchor.diagram_id,
            after=number - 1,
        )
    elif new_paragraph:
        number = anchor.paragraph_no or 1
        same = [
            item
            for item in everything
            if item.diagram_id == anchor.diagram_id
            and item.section == anchor.section
            and (item.paragraph_no or 1) == number
        ]
        position = max(item.order_index for item in same) + 1
        for item in everything:
            if (
                item.diagram_id == anchor.diagram_id
                and item.section == anchor.section
                and (item.paragraph_no or 1) > number
            ):
                item.paragraph_no = (item.paragraph_no or 1) + 1
        paragraph_no = number + 1
        from app.services import draft_images

        draft_images.shift_after(
            session,
            ctx,
            artifact_id=artifact_id,
            section=anchor.section,
            diagram_id=anchor.diagram_id,
            after=number,
        )
    for item in everything:
        if item.order_index >= position:
            item.order_index += 1
    session.flush()
    row = ArtifactStatement(
        org_id=ctx.org_id,
        artifact_version_id=current.id,
        order_index=position,
        statement_class=StatementClass.HUMAN_PROVIDED_INPUT,
        text=new,
        author_user_id=ctx.actor_user_id,
        approval_state=StatementApproval.DRAFT,
        section=anchor.section,
        diagram_id=anchor.diagram_id,
        diagram_version_no=anchor.diagram_version_no,
        paragraph_no=paragraph_no,
    )
    session.add(row)
    session.flush()
    _renumber(session, ctx, current)
    audit.record(
        session,
        ctx,
        action="artifact.statement_added",
        entity_type="artifact_version",
        entity_id=current.id,
        bid_id=artifact.bid_id,
        after={"text": new},
    )
    return row
