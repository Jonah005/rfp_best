"""Diagram routes: what a task needs, drawing, changing, restyling, reviewing."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_context, get_db, require
from app.core.context import ExecutionContext
from app.core.permissions import Perm
from app.services import diagrams as svc

router = APIRouter(tags=["diagrams"])

READ = [Depends(require(Perm.ARTIFACT_READ))]
WRITE = [Depends(require(Perm.ARTIFACT_WRITE))]


class DecideNeedIn(BaseModel):
    diagram_type: str
    accept: bool
    reason: str | None = Field(default=None, max_length=1000)


class CreateIn(BaseModel):
    diagram_type: str
    note: str | None = Field(default=None, max_length=4000)
    title: str | None = Field(default=None, max_length=300)


class ReviseIn(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)
    base_version_no: int


class EditIn(BaseModel):
    spec: dict
    base_version_no: int
    change_reason: str = Field(min_length=1, max_length=1000)


class ReviewIn(BaseModel):
    focus: str | None = Field(default=None, max_length=2000)


class ApplyIn(BaseModel):
    suggestion_indexes: list[int]
    base_version_no: int


class BaseIn(BaseModel):
    base_version_no: int


class MermaidIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    title: str | None = Field(default=None, max_length=300)


class QuestionIn(BaseModel):
    what: str = Field(min_length=1, max_length=1000)
    why: str | None = Field(default=None, max_length=2000)
    question: str = Field(min_length=1, max_length=2000)
    requirement_codes: list[str] | None = None


def _need(n) -> dict:
    return {
        "id": n.id,
        "diagram_type": n.diagram_type,
        "status": n.status,
        "source": n.source,
        "why": n.why,
        "enough_information": n.enough_information,
        "decline_reason": n.decline_reason,
        "missing": n.missing,
    }


def _summary(session: Session, ctx: ExecutionContext, d) -> dict:
    v = svc.current_version(session, ctx, d)
    return {
        "id": d.id,
        "task_id": d.task_id,
        "diagram_type": d.diagram_type,
        "title": d.title,
        "owner_user_id": d.owner_user_id,
        "left_out": d.left_out,
        "version_no": v.version_no,
        "origin": v.origin,
        "change_summary": v.change_summary,
        "could_not": v.could_not,
        "unconfirmed": v.unconfirmed,
        "review_notes": v.review_notes,
        "style": v.style,
        "spec": v.spec,
        "svg": svc.svg_for(d, v),
        "staleness": svc.staleness(session, ctx, d),
    }


@router.get("/tasks/{task_id}/diagrams", dependencies=READ)
def task_diagrams(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return jsonable_encoder(
        {
            "needs": [_need(n) for n in svc.needs_for_task(session, ctx, task_id=task_id)],
            "diagrams": [
                _summary(session, ctx, d)
                for d in svc.diagrams_for_task(session, ctx, task_id=task_id)
            ],
        }
    )


@router.post("/tasks/{task_id}/diagrams/plan", dependencies=WRITE)
def plan(
    task_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    needs = svc.plan(session, ctx, task_id=task_id)
    return jsonable_encoder({"needs": [_need(n) for n in needs]})


@router.post("/tasks/{task_id}/diagrams/needs", dependencies=WRITE)
def decide_need(
    task_id: uuid.UUID,
    payload: DecideNeedIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    n = svc.decide_need(
        session,
        ctx,
        task_id=task_id,
        diagram_type=payload.diagram_type,
        accept=payload.accept,
        reason=payload.reason,
    )
    return jsonable_encoder(_need(n))


@router.post("/tasks/{task_id}/diagrams", dependencies=WRITE)
def create(
    task_id: uuid.UUID,
    payload: CreateIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return jsonable_encoder(
        svc.create(
            session,
            ctx,
            task_id=task_id,
            diagram_type=payload.diagram_type,
            note=payload.note,
            title=payload.title,
        )
    )


@router.post("/tasks/{task_id}/diagrams/mermaid", dependencies=WRITE)
def import_mermaid(
    task_id: uuid.UUID,
    payload: MermaidIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    d = svc.import_mermaid(session, ctx, task_id=task_id, text=payload.text, title=payload.title)
    return jsonable_encoder(_summary(session, ctx, d))


@router.post("/tasks/{task_id}/diagrams/image", dependencies=WRITE)
def import_image(
    task_id: uuid.UUID,
    file: UploadFile = File(...),
    use_vision: bool = Form(default=False),
    title: str | None = Form(default=None),
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    data = file.file.read(svc.get_settings().vision_max_image_bytes + 1)
    return jsonable_encoder(
        svc.import_image(
            session, ctx, task_id=task_id, image=data, use_vision=use_vision, title=title
        )
    )


@router.post("/tasks/{task_id}/diagrams/question", dependencies=WRITE)
def raise_question(
    task_id: uuid.UUID,
    payload: QuestionIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    issue = svc.raise_question(
        session,
        ctx,
        task_id=task_id,
        what=payload.what,
        why=payload.why,
        question=payload.question,
        requirement_codes=payload.requirement_codes,
    )
    return jsonable_encoder({"clarification_id": issue.id})


@router.get("/diagrams/{diagram_id}", dependencies=READ)
def get_diagram(
    diagram_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return jsonable_encoder(_summary(session, ctx, svc.get_diagram(session, ctx, diagram_id)))


@router.get("/diagrams/{diagram_id}/versions", dependencies=READ)
def list_versions(
    diagram_id: uuid.UUID,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> list[dict]:
    return jsonable_encoder(
        [
            {
                "version_no": v.version_no,
                "origin": v.origin,
                "instruction": v.instruction,
                "change_summary": v.change_summary,
                "created_at": v.created_at,
            }
            for v in svc.versions(session, ctx, diagram_id=diagram_id)
        ]
    )


@router.get("/diagrams/{diagram_id}/versions/{version_no}", dependencies=READ)
def get_version(
    diagram_id: uuid.UUID,
    version_no: int,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    d = svc.get_diagram(session, ctx, diagram_id)
    v = svc.version_by_number(session, ctx, diagram_id=diagram_id, version_no=version_no)
    return jsonable_encoder(
        {
            "version_no": v.version_no,
            "spec": v.spec,
            "style": v.style,
            "svg": svc.svg_for(d, v),
            "change_summary": v.change_summary,
        }
    )


@router.post("/diagrams/{diagram_id}/revise", dependencies=WRITE)
def revise(
    diagram_id: uuid.UUID,
    payload: ReviseIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return jsonable_encoder(
        svc.revise(
            session,
            ctx,
            diagram_id=diagram_id,
            instruction=payload.instruction,
            base_version_no=payload.base_version_no,
        )
    )


@router.post("/diagrams/{diagram_id}/restyle", dependencies=WRITE)
def restyle(
    diagram_id: uuid.UUID,
    payload: ReviseIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return jsonable_encoder(
        svc.restyle(
            session,
            ctx,
            diagram_id=diagram_id,
            wish=payload.instruction,
            base_version_no=payload.base_version_no,
        )
    )


@router.post("/diagrams/{diagram_id}/edit", dependencies=WRITE)
def edit(
    diagram_id: uuid.UUID,
    payload: EditIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    v = svc.edit(
        session,
        ctx,
        diagram_id=diagram_id,
        spec=payload.spec,
        base_version_no=payload.base_version_no,
        change_reason=payload.change_reason,
    )
    return jsonable_encoder({"version_no": v.version_no, "change_summary": v.change_summary})


@router.post("/diagrams/{diagram_id}/review", dependencies=WRITE)
def review(
    diagram_id: uuid.UUID,
    payload: ReviewIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return jsonable_encoder(
        {"suggestions": svc.review(session, ctx, diagram_id=diagram_id, focus=payload.focus)}
    )


@router.post("/diagrams/{diagram_id}/apply", dependencies=WRITE)
def apply(
    diagram_id: uuid.UUID,
    payload: ApplyIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return jsonable_encoder(
        svc.apply_suggestions(
            session,
            ctx,
            diagram_id=diagram_id,
            suggestion_indexes=payload.suggestion_indexes,
            base_version_no=payload.base_version_no,
        )
    )


@router.post("/diagrams/{diagram_id}/refresh", dependencies=WRITE)
def refresh(
    diagram_id: uuid.UUID,
    payload: BaseIn,
    session: Session = Depends(get_db),
    ctx: ExecutionContext = Depends(get_current_context),
) -> dict:
    return jsonable_encoder(
        svc.refresh(session, ctx, diagram_id=diagram_id, base_version_no=payload.base_version_no)
    )
