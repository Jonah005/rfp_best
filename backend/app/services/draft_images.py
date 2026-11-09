"""Pictures in the response draft: adding, laying out, removing, and asking about them.

Only the current draft changes. A pasted picture is the person's own material -- it is
never evidence and carries no claim; what the AI says about one is a suggestion the
person may keep as the caption or as a paragraph, and leaves nothing behind otherwise.
A picture leaves the platform for a vision model only when the person asks for exactly
that picture to be explained.
"""

from __future__ import annotations

import base64
import io
import json
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.context import ExecutionContext
from app.core.errors import NotFound, StateConflict, ValidationError
from app.gateway.gateway import ModelGateway
from app.gateway.registry import wrap_untrusted
from app.models import DraftImage
from app.models.enums import ArtifactVersionState
from app.schemas.rewrite import ExplainResult
from app.services import artifacts, audit
from app.services import diagrams as dg

MAX_BYTES = 8 * 1024 * 1024
MOST_PICTURES = 60
_KINDS = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"RIFF": "image/webp",
}
_DREF = re.compile(r"\bD\d+\b")


def _sniff(data: bytes) -> str:
    for magic, mime in _KINDS.items():
        if data.startswith(magic):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    raise ValidationError("Only PNG, JPEG, GIF or WebP pictures can be added.")


def _size(data: bytes) -> tuple[int, int]:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as picture:
            return int(picture.width), int(picture.height)
    except Exception as exc:  # noqa: BLE001 - any unreadable file is the same answer
        raise ValidationError("That file could not be read as a picture.") from exc


def _draft(session: Session, ctx: ExecutionContext, artifact_id: uuid.UUID):
    artifact = artifacts._artifact(session, ctx, artifact_id)
    current = artifacts._current_version(session, ctx, artifact)
    if current.state is not ArtifactVersionState.DRAFT:
        raise StateConflict(
            "The response is already with reviewers, so it can't be edited here. "
            "Ask for changes once it is back in draft."
        )
    return artifact, current


def _row(session: Session, ctx: ExecutionContext, artifact_id: uuid.UUID, image_id: uuid.UUID):
    row = session.get(DraftImage, image_id)
    if row is None or row.org_id != ctx.org_id or row.artifact_id != artifact_id:
        raise NotFound("That picture is not in this response.")
    return row


def _crop(value: object) -> dict | None:
    if value is None:
        return None
    try:
        x, y, w, h = (float(value[k]) for k in ("x", "y", "w", "h"))  # type: ignore[index]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("A crop needs x, y, w and h.") from exc
    if not (0 <= x <= 1 and 0 <= y <= 1 and 0.05 <= w <= 1 and 0.05 <= h <= 1):
        raise ValidationError("That crop is outside the picture.")
    if x + w > 1.0001 or y + h > 1.0001:
        raise ValidationError("That crop is outside the picture.")
    if w > 0.995 and h > 0.995:
        return None
    return {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}


def _out(row: DraftImage) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "section": row.section,
        "diagram_id": row.diagram_id,
        "after_paragraph": row.after_paragraph,
        "position": row.position,
        "content_type": row.content_type,
        "filename": row.filename,
        "natural_width": row.natural_width,
        "natural_height": row.natural_height,
        "width_pct": row.width_pct,
        "crop": row.crop,
        "caption": row.caption,
        "alt": row.alt,
        "updated_at": row.updated_at,
    }


def list_images(session: Session, ctx: ExecutionContext, *, artifact_id: uuid.UUID) -> list[dict]:
    artifacts._artifact(session, ctx, artifact_id)
    rows = session.execute(
        select(DraftImage)
        .where(DraftImage.org_id == ctx.org_id, DraftImage.artifact_id == artifact_id)
        .order_by(DraftImage.after_paragraph, DraftImage.position, DraftImage.created_at)
    ).scalars()
    return [_out(row) for row in rows]


def image_data(
    session: Session, ctx: ExecutionContext, *, artifact_id: uuid.UUID, image_id: uuid.UUID
) -> dict:
    artifacts._artifact(session, ctx, artifact_id)
    row = _row(session, ctx, artifact_id, image_id)
    if row.data is None:
        raise NotFound("That picture has no stored file.")
    return {
        "content_type": row.content_type,
        "data_url": f"data:{row.content_type};base64,{base64.b64encode(row.data).decode()}",
    }


def add_upload(
    session: Session,
    ctx: ExecutionContext,
    *,
    artifact_id: uuid.UUID,
    content_base64: str,
    filename: str | None,
    section: str | None,
    diagram_id: uuid.UUID | None,
    after_paragraph: int | None,
) -> dict:
    artifact, current = _draft(session, ctx, artifact_id)
    try:
        data = base64.b64decode(content_base64.split(",", 1)[-1], validate=False)
    except Exception as exc:  # noqa: BLE001
        raise ValidationError("That picture could not be read.") from exc
    if not data:
        raise ValidationError("That picture is empty.")
    if len(data) > MAX_BYTES:
        raise ValidationError("That picture is larger than 8 MB.")
    mime = _sniff(data)
    width, height = _size(data)
    taken = session.execute(
        select(DraftImage.id).where(
            DraftImage.org_id == ctx.org_id,
            DraftImage.artifact_id == artifact_id,
            DraftImage.kind == "UPLOAD",
        )
    ).all()
    if len(taken) >= MOST_PICTURES:
        raise ValidationError(f"A response can hold at most {MOST_PICTURES} added pictures.")

    rows = artifacts.statements_for_version(session, ctx, version_id=current.id)
    here = [r for r in rows if r.diagram_id == diagram_id and (r.section or None) == section]
    if not here:
        raise ValidationError("That part of the response is not in the draft.")
    paragraphs = sorted({r.paragraph_no for r in here if r.paragraph_no})
    after = int(after_paragraph or 0)
    if paragraphs and after not in paragraphs:
        after = paragraphs[-1]
    after = after or 1
    last = session.execute(
        select(DraftImage.position)
        .where(
            DraftImage.org_id == ctx.org_id,
            DraftImage.artifact_id == artifact_id,
            DraftImage.after_paragraph == after,
            DraftImage.diagram_id.is_(None)
            if diagram_id is None
            else DraftImage.diagram_id == diagram_id,
        )
        .order_by(DraftImage.position.desc())
    ).first()
    row = DraftImage(
        org_id=ctx.org_id,
        artifact_id=artifact_id,
        kind="UPLOAD",
        section=section,
        diagram_id=diagram_id,
        after_paragraph=after,
        position=(last[0] + 1) if last else 0,
        content_type=mime,
        data=data,
        filename=(filename or "")[:200] or None,
        natural_width=width,
        natural_height=height,
        width_pct=60 if width < 700 else 80,
        created_by=ctx.actor_user_id,
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        ctx,
        action="artifact.picture_added",
        entity_type="artifact_version",
        entity_id=current.id,
        bid_id=artifact.bid_id,
        after={"image_id": str(row.id), "bytes": len(data)},
    )
    return _out(row)


def update_image(
    session: Session,
    ctx: ExecutionContext,
    *,
    artifact_id: uuid.UUID,
    image_id: uuid.UUID,
    fields: dict,
) -> dict:
    """Change how a picture or figure is shown. `fields` holds only what changed."""
    _draft(session, ctx, artifact_id)
    row = _row(session, ctx, artifact_id, image_id)
    _apply(row, fields)
    session.flush()
    return _out(row)


def _apply(row: DraftImage, fields: dict) -> None:
    if "width_pct" in fields and fields["width_pct"] is not None:
        width = int(fields["width_pct"])
        if not 10 <= width <= 100:
            raise ValidationError("A picture is between 10% and 100% of the page width.")
        row.width_pct = width
    if "crop" in fields:
        row.crop = _crop(fields["crop"])
    if "caption" in fields:
        text = " ".join(str(fields["caption"] or "").split())
        if len(text) > 1500:
            raise ValidationError("A caption can be at most 1500 characters.")
        row.caption = text or None
    if "alt" in fields:
        row.alt = " ".join(str(fields["alt"] or "").split())[:300] or None


def set_figure_layout(
    session: Session,
    ctx: ExecutionContext,
    *,
    artifact_id: uuid.UUID,
    diagram_id: uuid.UUID,
    fields: dict,
) -> dict:
    """How a drawn diagram is shown in the document: its width, the part shown, a caption."""
    _draft(session, ctx, artifact_id)
    dg.get_diagram(session, ctx, diagram_id)
    row = session.execute(
        select(DraftImage).where(
            DraftImage.org_id == ctx.org_id,
            DraftImage.artifact_id == artifact_id,
            DraftImage.kind == "DIAGRAM",
            DraftImage.diagram_id == diagram_id,
        )
    ).scalar_one_or_none()
    if row is None:
        row = DraftImage(
            org_id=ctx.org_id,
            artifact_id=artifact_id,
            kind="DIAGRAM",
            diagram_id=diagram_id,
            after_paragraph=0,
            width_pct=100,
            created_by=ctx.actor_user_id,
        )
        session.add(row)
    if "natural_width" in fields and fields["natural_width"]:
        row.natural_width = int(fields["natural_width"])
        row.natural_height = int(fields.get("natural_height") or 0) or None
    _apply(row, fields)
    session.flush()
    return _out(row)


def delete_image(
    session: Session, ctx: ExecutionContext, *, artifact_id: uuid.UUID, image_id: uuid.UUID
) -> None:
    artifact, current = _draft(session, ctx, artifact_id)
    row = _row(session, ctx, artifact_id, image_id)
    if row.kind != "UPLOAD":
        raise ValidationError("A drawn diagram is left out of the response, not deleted here.")
    session.delete(row)
    session.flush()
    audit.record(
        session,
        ctx,
        action="artifact.picture_removed",
        entity_type="artifact_version",
        entity_id=current.id,
        bid_id=artifact.bid_id,
        before={"image_id": str(image_id)},
    )


def drop_for(
    session: Session,
    ctx: ExecutionContext,
    *,
    artifact_id: uuid.UUID,
    diagram_id: uuid.UUID | None = None,
    section: str | None = None,
) -> None:
    """A section left out of the response takes its pictures with it."""
    query = delete(DraftImage).where(
        DraftImage.org_id == ctx.org_id, DraftImage.artifact_id == artifact_id
    )
    if diagram_id is not None:
        query = query.where(DraftImage.diagram_id == diagram_id)
    else:
        query = query.where(DraftImage.diagram_id.is_(None), DraftImage.section == section)
    session.execute(query)


# --- asking the AI about a picture ----------------------------------------------


def _region(crop: dict | None, region: dict | None) -> dict | None:
    """The part of the stored picture to show the model, from what the person sees.

    The person points at a part of what is on screen -- which may already be a crop --
    so the two compose.
    """
    if region is None:
        return crop
    picked = _crop(region)
    if picked is None:
        return crop
    if crop is None:
        return picked
    return {
        "x": crop["x"] + picked["x"] * crop["w"],
        "y": crop["y"] + picked["y"] * crop["h"],
        "w": picked["w"] * crop["w"],
        "h": picked["h"] * crop["h"],
    }


def _shown_to_model(data: bytes, part: dict | None) -> tuple[bytes, str]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as opened:
        opened.load()
        picture: Image.Image = opened
        if part is not None:
            w, h = picture.size
            box = (
                int(part["x"] * w),
                int(part["y"] * h),
                max(int(part["x"] * w) + 1, int((part["x"] + part["w"]) * w)),
                max(int(part["y"] * h) + 1, int((part["y"] + part["h"]) * h)),
            )
            picture = picture.crop(box)
        picture.thumbnail((2000, 2000))
        if picture.mode not in ("RGB", "RGBA"):
            picture = picture.convert("RGB")
        out = io.BytesIO()
        picture.save(out, format="PNG")
        return out.getvalue(), "image/png"


def _nearby(
    session: Session,
    ctx: ExecutionContext,
    artifact_id: uuid.UUID,
    *,
    section: str | None,
    diagram_id: uuid.UUID | None,
    paragraph: int,
) -> str:
    artifact = artifacts._artifact(session, ctx, artifact_id)
    current = artifacts._current_version(session, ctx, artifact)
    rows = artifacts.statements_for_version(session, ctx, version_id=current.id)
    text = " ".join(
        r.text
        for r in rows
        if r.diagram_id == diagram_id
        and (r.section or None) == section
        and (r.paragraph_no or 0) == paragraph
    )
    return text[:900]


def _tidy(result: ExplainResult) -> dict:
    text = " ".join(result.text.replace("[[", "").replace("]]", "").split())
    if _DREF.search(text):
        text = _DREF.sub("the diagram", text)
    if not text:
        raise ValidationError("The AI gave no explanation. Try again.")
    return {"text": text, "unclear": [u for u in result.unclear if u.strip()][:8]}


def explain(
    session: Session,
    ctx: ExecutionContext,
    gateway: ModelGateway,
    *,
    artifact_id: uuid.UUID,
    image_id: uuid.UUID | None,
    diagram_id: uuid.UUID | None,
    instruction: str | None,
    region: dict | None,
    previous: list[str],
    use_vision: bool,
) -> dict:
    """Explain a pasted picture (a vision model) or a drawn figure (its structure, as text)."""
    artifact = artifacts._artifact(session, ctx, artifact_id)
    note = (instruction or "").strip() or "Explain this picture for the customer."
    previous = [p[:1500] for p in previous[-3:] if p.strip()]
    from app.services import writeup

    customer = None
    try:
        customer = writeup.bid_context(session, ctx, artifact.task_id).get("customer")
    except Exception:  # noqa: BLE001 - only a hint for the wording
        customer = None

    if image_id is not None:
        row = _row(session, ctx, artifact_id, image_id)
        if row.data is None:
            raise NotFound("That picture has no stored file.")
        if not use_vision:
            raise ValidationError(
                "Explaining a picture sends it to a vision model outside the platform. "
                "Confirm that for this picture."
            )
        part = _region(row.crop, region)
        data, mime = _shown_to_model(row.data, part)
        if len(data) > get_settings().vision_max_image_bytes:
            raise ValidationError("That picture is too large to explain. Crop it first.")
        payload = {
            "instruction": note,
            "customer": customer,
            "section": row.section,
            "paragraph_above": _nearby(
                session,
                ctx,
                artifact_id,
                section=row.section,
                diagram_id=row.diagram_id,
                paragraph=row.after_paragraph,
            ),
            "caption": row.caption,
            "part": "the part of the picture shown" if region else None,
            "previous": previous,
            "asked_at": datetime.now(timezone.utc).isoformat(),
        }
        result = gateway.read_image(
            "image_explain",
            wrap_untrusted(json.dumps(payload, ensure_ascii=False, default=str)),
            data,
            mime,
            ctx=ctx,
            session=session,
        )
        if not isinstance(result, ExplainResult):
            raise ValidationError("The vision model returned an unexpected answer.")
        return _tidy(result)

    if diagram_id is None:
        raise ValidationError("Say which picture or figure to explain.")
    diagram = dg.get_diagram(session, ctx, diagram_id)
    if diagram.current_version_id is None:
        raise ValidationError("That diagram has no drawing yet.")
    version = dg._version(session, ctx, diagram.current_version_id)
    layout = session.execute(
        select(DraftImage).where(
            DraftImage.org_id == ctx.org_id,
            DraftImage.artifact_id == artifact_id,
            DraftImage.kind == "DIAGRAM",
            DraftImage.diagram_id == diagram_id,
        )
    ).scalar_one_or_none()
    current = artifacts._current_version(session, ctx, artifact)
    first = next(
        (
            r
            for r in artifacts.statements_for_version(session, ctx, version_id=current.id)
            if r.diagram_id == diagram_id
        ),
        None,
    )
    payload = {
        "instruction": note,
        "customer": customer,
        "section": first.section if first else diagram.title,
        "diagram": {"title": diagram.title, **writeup.describe(version)},
        "paragraph_above": _nearby(
            session,
            ctx,
            artifact_id,
            section=first.section if first else None,
            diagram_id=diagram_id,
            paragraph=1,
        ),
        "caption": layout.caption if layout else None,
        "previous": previous,
        "asked_at": datetime.now(timezone.utc).isoformat(),
    }
    answer = dg._ask(
        gateway,
        "figure_explain",
        payload,
        ctx=ctx,
        session=session,
        expect=ExplainResult,
    )
    return _tidy(answer)


def shift_after(
    session: Session,
    ctx: ExecutionContext,
    *,
    artifact_id: uuid.UUID,
    section: str | None,
    diagram_id: uuid.UUID | None,
    after: int,
) -> None:
    """A new paragraph went in after paragraph `after`: pictures after later ones move down."""
    rows = session.execute(
        select(DraftImage).where(
            DraftImage.org_id == ctx.org_id,
            DraftImage.artifact_id == artifact_id,
            DraftImage.kind == "UPLOAD",
            DraftImage.after_paragraph > after,
            DraftImage.diagram_id.is_(None)
            if diagram_id is None
            else DraftImage.diagram_id == diagram_id,
        )
    ).scalars()
    for row in rows:
        if diagram_id is None and row.section != section:
            continue
        row.after_paragraph += 1
