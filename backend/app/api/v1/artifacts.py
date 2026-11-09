"""Typed artifact, review and approval routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_context, get_db, require
from app.core.context import ExecutionContext
from app.core.errors import ValidationError
from app.core.permissions import Perm
from app.gateway.gateway import build_gateway
from app.models.enums import (
    ApprovalStatus,
    ApprovalStepState,
    ArtifactState,
    AssumptionStatus,
    ReviewDecision,
)
from app.schemas.artifacts import (
    ApprovalDecisionIn,
    ApprovalOut,
    ArtifactCreate,
    ArtifactOut,
    AssumptionOut,
    AssumptionUpdate,
    CommitmentDecisionIn,
    CommitmentOut,
    GenerateDraftIn,
    ReviewIn,
    ReviewOut,
    StatementOut,
    SubmitArtifact,
    VersionCreate,
    VersionOut,
)
from app.services import artifacts as artifact_service
from app.services import draft_images, drafting, rewrite, writeup

router = APIRouter(tags=["artifacts"])


def _enum(enum_cls, value: str, label: str):
    try:
        return enum_cls(value)
    except ValueError as exc:
        allowed = ", ".join(sorted(member.value for member in enum_cls))
        raise ValidationError(f"Unknown {label} '{value}'. Allowed: {allowed}.") from exc


def _version_out(session: Session, ctx: ExecutionContext, version) -> VersionOut:
    statements = artifact_service.statements_for_version(session, ctx, version_id=version.id)
    roles = artifact_service.commitment_roles(
        session, ctx, [row.commitment_id for row in statements if row.commitment_id]
    )
    out = []
    moved: dict[tuple[uuid.UUID, int | None], str | None] = {}
    for row in statements:
        item = StatementOut.model_validate(row)
        if row.commitment_id:
            item.required_approver_role = roles.get(row.commitment_id)
        if row.diagram_id is not None:
            key = (row.diagram_id, row.diagram_version_no)
            if key not in moved:
                moved[key] = writeup.out_of_date_reason(
                    session,
                    ctx,
                    diagram_id=row.diagram_id,
                    written_version_no=row.diagram_version_no,
                )
            item.out_of_date = moved[key] is not None
            item.out_of_date_reason = moved[key]
        out.append(item)
    return VersionOut(
        **VersionOut.model_validate(version).model_dump(exclude={"statements"}),
        statements=out,
    )


def _artifact_out(session: Session, ctx: ExecutionContext, artifact) -> ArtifactOut:
    current = None
    if artifact.current_version_id:
        current = _version_out(
            session,
            ctx,
            artifact_service.version_by_number(
                session,
                ctx,
                artifact_id=artifact.id,
                version_no=max(
                    version.version_no
                    for version in artifact_service.versions(session, ctx, artifact_id=artifact.id)
                ),
            ),
        )
    return ArtifactOut(
        **ArtifactOut.model_validate(artifact).model_dump(exclude={"current_version"}),
        current_version=current,
    )


@router.post(
    "/tasks/{task_id}/artifact",
    response_model=ArtifactOut,
    dependencies=[Depends(require(Perm.ARTIFACT_WRITE))],
)
def ensure_task_artifact(
    task_id: uuid.UUID,
    payload: ArtifactCreate,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ArtifactOut:
    artifact = artifact_service.ensure_for_task(session, ctx, task_id=task_id, title=payload.title)
    return _artifact_out(session, ctx, artifact)


@router.get(
    "/tasks/{task_id}/artifact",
    response_model=ArtifactOut,
    dependencies=[Depends(require(Perm.ARTIFACT_READ))],
)
def task_artifact(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ArtifactOut:
    artifact = artifact_service.for_task(session, ctx, task_id=task_id)
    return _artifact_out(session, ctx, artifact)


@router.get(
    "/artifacts",
    response_model=list[ArtifactOut],
    dependencies=[Depends(require(Perm.ARTIFACT_READ))],
)
def list_artifacts(
    bid_id: uuid.UUID | None = Query(default=None),
    task_id: uuid.UUID | None = Query(default=None),
    state: str | None = Query(default=None),
    requirement_id: uuid.UUID | None = Query(default=None),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[ArtifactOut]:
    rows = artifact_service.list_artifacts(
        session,
        ctx,
        bid_id=bid_id,
        task_id=task_id,
        state=_enum(ArtifactState, state, "artifact state") if state else None,
        requirement_id=requirement_id,
    )
    return [_artifact_out(session, ctx, row) for row in rows]


@router.get(
    "/artifacts/{artifact_id}",
    response_model=ArtifactOut,
    dependencies=[Depends(require(Perm.ARTIFACT_READ))],
)
def get_artifact(
    artifact_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ArtifactOut:
    artifact = artifact_service.get_artifact(session, ctx, artifact_id=artifact_id)
    return _artifact_out(session, ctx, artifact)


@router.get(
    "/artifacts/{artifact_id}/versions",
    response_model=list[VersionOut],
    dependencies=[Depends(require(Perm.ARTIFACT_READ))],
)
def list_versions(
    artifact_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[VersionOut]:
    return [
        _version_out(session, ctx, row)
        for row in artifact_service.versions(session, ctx, artifact_id=artifact_id)
    ]


@router.post(
    "/artifacts/{artifact_id}/versions",
    response_model=VersionOut,
    dependencies=[Depends(require(Perm.ARTIFACT_WRITE))],
)
def save_version(
    artifact_id: uuid.UUID,
    payload: VersionCreate,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> VersionOut:
    version = artifact_service.save_draft(
        session,
        ctx,
        artifact_id=artifact_id,
        statements=[item.model_dump() for item in payload.statements],
        change_reason=payload.change_reason,
    )
    return _version_out(session, ctx, version)


@router.post(
    "/artifacts/{artifact_id}/generate-draft",
    response_model=VersionOut,
    dependencies=[Depends(require(Perm.ARTIFACT_WRITE))],
)
def generate_draft(
    artifact_id: uuid.UUID,
    payload: GenerateDraftIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> VersionOut:
    version = drafting.generate(
        session,
        ctx,
        artifact_id=artifact_id,
        instruction=payload.instruction,
    )
    return _version_out(session, ctx, version)


@router.get(
    "/artifacts/{artifact_id}/versions/{version_no}",
    response_model=VersionOut,
    dependencies=[Depends(require(Perm.ARTIFACT_READ))],
)
def get_version(
    artifact_id: uuid.UUID,
    version_no: int,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> VersionOut:
    return _version_out(
        session,
        ctx,
        artifact_service.version_by_number(
            session, ctx, artifact_id=artifact_id, version_no=version_no
        ),
    )


@router.post(
    "/artifacts/{artifact_id}/submit",
    response_model=ArtifactOut,
    dependencies=[Depends(require(Perm.ARTIFACT_WRITE))],
)
def submit_artifact(
    artifact_id: uuid.UUID,
    payload: SubmitArtifact,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ArtifactOut:
    artifact = artifact_service.submit(
        session,
        ctx,
        artifact_id=artifact_id,
        approval_roles=payload.approval_roles,
        note=payload.note,
    )
    return _artifact_out(session, ctx, artifact)


@router.get(
    "/approvals",
    response_model=list[ApprovalOut],
    dependencies=[Depends(require(Perm.ARTIFACT_READ))],
)
def list_approvals(
    entity_type: str | None = Query(default=None),
    state: str | None = Query(default=None),
    mine: bool = Query(default=False),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[ApprovalOut]:
    rows = artifact_service.list_approvals(
        session,
        ctx,
        entity_type=entity_type,
        state=_enum(ApprovalStepState, state, "approval state") if state else None,
        mine=mine,
    )
    return [ApprovalOut.model_validate(row) for row in rows]


@router.get(
    "/approvals/chain",
    response_model=list[ApprovalOut],
    dependencies=[Depends(require(Perm.ARTIFACT_READ))],
)
def approval_chain(
    entity_type: str,
    entity_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[ApprovalOut]:
    rows = artifact_service.approval_chain(
        session, ctx, entity_type=entity_type, entity_id=entity_id
    )
    return [ApprovalOut.model_validate(row) for row in rows]


@router.get(
    "/approvals/{approval_id}/review",
    response_model=dict,
    dependencies=[Depends(require(Perm.ARTIFACT_READ))],
)
def review_approval(
    approval_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """The answer behind an approval step: text, requirements, evidence, changes."""
    return jsonable_encoder(artifact_service.approval_review(session, ctx, approval_id=approval_id))


@router.post(
    "/approvals/{approval_id}/decide",
    response_model=ApprovalOut,
    dependencies=[Depends(require(Perm.APPROVAL_DECIDE))],
)
def decide_approval(
    approval_id: uuid.UUID,
    payload: ApprovalDecisionIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ApprovalOut:
    row = artifact_service.decide_approval(
        session,
        ctx,
        approval_id=approval_id,
        decision=_enum(ApprovalStepState, payload.decision, "approval decision"),
        comment=payload.comment,
    )
    return ApprovalOut.model_validate(row)


@router.post(
    "/reviews",
    response_model=ReviewOut,
    dependencies=[Depends(require(Perm.APPROVAL_DECIDE))],
)
def record_review(
    payload: ReviewIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> ReviewOut:
    row = artifact_service.record_review(
        session,
        ctx,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        decision=_enum(ReviewDecision, payload.decision, "review decision"),
        comment=payload.comment,
    )
    return ReviewOut.model_validate(row)


@router.get(
    "/bids/{bid_id}/commitments",
    response_model=list[CommitmentOut],
    dependencies=[Depends(require(Perm.ARTIFACT_READ))],
)
def list_commitments(
    bid_id: uuid.UUID,
    approval_status: str | None = Query(default=None),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[CommitmentOut]:
    rows = artifact_service.list_commitments(
        session,
        ctx,
        bid_id=bid_id,
        approval_status=(
            _enum(ApprovalStatus, approval_status, "commitment approval status")
            if approval_status
            else None
        ),
    )
    return [CommitmentOut.model_validate(row) for row in rows]


@router.post(
    "/commitments/{commitment_id}/approve",
    response_model=CommitmentOut,
    # A promise is decided by the role it was routed to, and the service enforces
    # exactly that (`required_approver_role in ctx.role_codes`). Requiring the
    # general approval permission as well meant a Security or Solution Architect
    # lead was named as the approver and then refused, so the promise could never
    # be decided by the person it was meant for. Reading the answer is what the
    # route needs; WHO may decide is the named role, checked in the service.
    dependencies=[Depends(require(Perm.ARTIFACT_READ))],
)
def decide_commitment(
    commitment_id: uuid.UUID,
    payload: CommitmentDecisionIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> CommitmentOut:
    row = artifact_service.decide_commitment(
        session,
        ctx,
        commitment_id=commitment_id,
        decision=_enum(ApprovalStatus, payload.decision, "commitment decision"),
        comment=payload.comment,
    )
    return CommitmentOut.model_validate(row)


@router.get(
    "/bids/{bid_id}/assumptions",
    response_model=list[AssumptionOut],
    dependencies=[Depends(require(Perm.ARTIFACT_READ))],
)
def list_assumptions(
    bid_id: uuid.UUID,
    status: str | None = Query(default=None),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[AssumptionOut]:
    rows = artifact_service.list_assumptions(
        session,
        ctx,
        bid_id=bid_id,
        status=_enum(AssumptionStatus, status, "assumption status") if status else None,
    )
    return [AssumptionOut.model_validate(row) for row in rows]


@router.patch(
    "/assumptions/{assumption_id}",
    response_model=AssumptionOut,
    dependencies=[Depends(require(Perm.ARTIFACT_WRITE))],
)
def update_assumption(
    assumption_id: uuid.UUID,
    payload: AssumptionUpdate,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> AssumptionOut:
    row = artifact_service.update_assumption(
        session,
        ctx,
        assumption_id=assumption_id,
        status=_enum(AssumptionStatus, payload.status, "assumption status"),
        resolution=payload.resolution,
    )
    return AssumptionOut.model_validate(row)


# --- editing the draft in place ------------------------------------------------


class _Piece(BaseModel):
    statement_id: uuid.UUID
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)


class RewritePreviewIn(BaseModel):
    items: list[_Piece] = Field(min_length=1, max_length=rewrite.MOST_PIECES)
    mode: str = "rewrite"
    instruction: str | None = Field(default=None, max_length=1000)
    paragraph: bool = False
    # The wording last suggested for each item, when the person is refining it.
    previous: list[str] = Field(default_factory=list, max_length=rewrite.MOST_PIECES)


class _Edit(BaseModel):
    statement_id: uuid.UUID
    text: str = Field(min_length=1, max_length=4000)


class EditsIn(BaseModel):
    edits: list[_Edit] = Field(min_length=1, max_length=rewrite.MOST_PIECES)


class AddAfterIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    new_paragraph: bool = False
    before: bool = False


@router.post(
    "/artifacts/{artifact_id}/statements/rewrite-preview",
    dependencies=[Depends(require(Perm.ARTIFACT_WRITE))],
)
def rewrite_preview(
    artifact_id: uuid.UUID,
    payload: RewritePreviewIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Suggest new wording for the chosen pieces. Changes nothing."""
    result = rewrite.preview(
        session,
        ctx,
        build_gateway(),
        artifact_id=artifact_id,
        items=[item.model_dump() for item in payload.items],
        mode=payload.mode,
        instruction=payload.instruction,
        paragraph=payload.paragraph,
        previous=payload.previous,
    )
    return jsonable_encoder(result)


@router.post(
    "/artifacts/{artifact_id}/statements/edits",
    dependencies=[Depends(require(Perm.ARTIFACT_WRITE))],
)
def edit_statements(
    artifact_id: uuid.UUID,
    payload: EditsIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    """Put new words on one or more sentences of the draft, all or none."""
    became_own: list[uuid.UUID] = []
    for edit in payload.edits:
        row = artifact_service.edit_statement_text(
            session,
            ctx,
            artifact_id=artifact_id,
            statement_id=edit.statement_id,
            text=edit.text,
        )
        if row.statement_class.value == "HUMAN_PROVIDED_INPUT":
            became_own.append(row.id)
    return jsonable_encoder({"changed": len(payload.edits), "human_input": became_own})


@router.delete(
    "/artifacts/{artifact_id}/statements/{statement_id}",
    dependencies=[Depends(require(Perm.ARTIFACT_WRITE))],
)
def delete_statement(
    artifact_id: uuid.UUID,
    statement_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    artifact_service.delete_statement(
        session, ctx, artifact_id=artifact_id, statement_id=statement_id
    )
    return {"deleted": True}


@router.post(
    "/artifacts/{artifact_id}/statements/{statement_id}/add-after",
    dependencies=[Depends(require(Perm.ARTIFACT_WRITE))],
)
def add_statement_after(
    artifact_id: uuid.UUID,
    statement_id: uuid.UUID,
    payload: AddAfterIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    row = artifact_service.add_statement_after(
        session,
        ctx,
        artifact_id=artifact_id,
        after_statement_id=statement_id,
        text=payload.text,
        new_paragraph=payload.new_paragraph,
        before=payload.before,
    )
    return jsonable_encoder({"id": row.id})


# --- pictures in the draft ------------------------------------------------------


class PictureIn(BaseModel):
    content_base64: str = Field(min_length=8, max_length=14_000_000)
    filename: str | None = Field(default=None, max_length=200)
    section: str | None = Field(default=None, max_length=200)
    diagram_id: uuid.UUID | None = None
    after_paragraph: int | None = Field(default=None, ge=0, le=500)


class _Crop(BaseModel):
    x: float
    y: float
    w: float
    h: float


class LayoutIn(BaseModel):
    width_pct: int | None = Field(default=None, ge=10, le=100)
    # Send `crop` as an object to crop, as null to show the whole picture again.
    crop: _Crop | None = None
    caption: str | None = Field(default=None, max_length=1500)
    alt: str | None = Field(default=None, max_length=300)
    natural_width: int | None = Field(default=None, ge=1, le=100_000)
    natural_height: int | None = Field(default=None, ge=1, le=100_000)


class ExplainIn(BaseModel):
    instruction: str | None = Field(default=None, max_length=1000)
    region: _Crop | None = None
    previous: list[str] = Field(default_factory=list, max_length=3)
    use_vision: bool = False


def _fields(payload: LayoutIn) -> dict:
    sent = payload.model_fields_set
    out: dict = {}
    for key in ("width_pct", "caption", "alt", "natural_width", "natural_height"):
        if key in sent:
            out[key] = getattr(payload, key)
    if "crop" in sent:
        out["crop"] = payload.crop.model_dump() if payload.crop else None
    return out


@router.get(
    "/artifacts/{artifact_id}/images",
    dependencies=[Depends(require(Perm.ARTIFACT_READ))],
)
def list_pictures(
    artifact_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    return jsonable_encoder(draft_images.list_images(session, ctx, artifact_id=artifact_id))


@router.get(
    "/artifacts/{artifact_id}/images/{image_id}/data",
    dependencies=[Depends(require(Perm.ARTIFACT_READ))],
)
def picture_data(
    artifact_id: uuid.UUID,
    image_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return draft_images.image_data(session, ctx, artifact_id=artifact_id, image_id=image_id)


@router.post(
    "/artifacts/{artifact_id}/images",
    dependencies=[Depends(require(Perm.ARTIFACT_WRITE))],
)
def add_picture(
    artifact_id: uuid.UUID,
    payload: PictureIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return jsonable_encoder(
        draft_images.add_upload(
            session,
            ctx,
            artifact_id=artifact_id,
            content_base64=payload.content_base64,
            filename=payload.filename,
            section=payload.section,
            diagram_id=payload.diagram_id,
            after_paragraph=payload.after_paragraph,
        )
    )


@router.patch(
    "/artifacts/{artifact_id}/images/{image_id}",
    dependencies=[Depends(require(Perm.ARTIFACT_WRITE))],
)
def change_picture(
    artifact_id: uuid.UUID,
    image_id: uuid.UUID,
    payload: LayoutIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return jsonable_encoder(
        draft_images.update_image(
            session, ctx, artifact_id=artifact_id, image_id=image_id, fields=_fields(payload)
        )
    )


@router.put(
    "/artifacts/{artifact_id}/figures/{diagram_id}",
    dependencies=[Depends(require(Perm.ARTIFACT_WRITE))],
)
def lay_out_figure(
    artifact_id: uuid.UUID,
    diagram_id: uuid.UUID,
    payload: LayoutIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return jsonable_encoder(
        draft_images.set_figure_layout(
            session, ctx, artifact_id=artifact_id, diagram_id=diagram_id, fields=_fields(payload)
        )
    )


@router.delete(
    "/artifacts/{artifact_id}/images/{image_id}",
    dependencies=[Depends(require(Perm.ARTIFACT_WRITE))],
)
def remove_picture(
    artifact_id: uuid.UUID,
    image_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    draft_images.delete_image(session, ctx, artifact_id=artifact_id, image_id=image_id)
    return {"deleted": True}


@router.post(
    "/artifacts/{artifact_id}/images/{image_id}/explain",
    dependencies=[Depends(require(Perm.ARTIFACT_WRITE))],
)
def explain_picture(
    artifact_id: uuid.UUID,
    image_id: uuid.UUID,
    payload: ExplainIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return draft_images.explain(
        session,
        ctx,
        build_gateway(),
        artifact_id=artifact_id,
        image_id=image_id,
        diagram_id=None,
        instruction=payload.instruction,
        region=payload.region.model_dump() if payload.region else None,
        previous=payload.previous,
        use_vision=payload.use_vision,
    )


@router.post(
    "/artifacts/{artifact_id}/figures/{diagram_id}/explain",
    dependencies=[Depends(require(Perm.ARTIFACT_WRITE))],
)
def explain_figure(
    artifact_id: uuid.UUID,
    diagram_id: uuid.UUID,
    payload: ExplainIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return draft_images.explain(
        session,
        ctx,
        build_gateway(),
        artifact_id=artifact_id,
        image_id=None,
        diagram_id=diagram_id,
        instruction=payload.instruction,
        region=None,
        previous=payload.previous,
        use_vision=False,
    )
