# ruff: noqa: F811
"""Pictures in the response draft: added, laid out, removed, and explained on request."""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.models import DraftImage
from app.schemas.rewrite import ExplainResult
from app.services import artifacts, draft_images
from tests.integration.test_a_customer_clause_is_not_left_in_a_task import (  # noqa: F401
    env,
    fresh_org,
)
from tests.integration.test_a_diagram_is_written_up_in_the_response import (  # noqa: F401
    ARCH,
    _draft,
    _draw,
    scene,
)

pytestmark = pytest.mark.integration


def _png(width=200, height=100):
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode()


class Explainer:
    def __init__(self):
        self.calls = []

    def read_image(self, call, instruction, image, mime, *, ctx=None, session=None):
        self.calls.append(("vision", call, instruction, image, mime))
        return ExplainResult(text="A red panel [[x]] shown as D1.", unclear=["tiny text"])

    def generate_structured(self, call, prompt, *, ctx, session):
        self.calls.append(("text", call, prompt))
        return ExplainResult(text="The flow starts at the portal.")


def _section(s, ctx, task):
    diagram = _draw(s, ctx, task, ARCH)
    artifact, _ = _draft(s, ctx, task)
    artifacts.write_section(
        s,
        ctx,
        artifact_id=artifact.id,
        section="The flow",
        statements=[
            {"statement_class": "DIAGRAM_DESCRIPTION", "text": "One.", "paragraph_no": 1},
            {"statement_class": "DIAGRAM_DESCRIPTION", "text": "Two.", "paragraph_no": 2},
        ],
        diagram_id=diagram.id,
        diagram_version_no=1,
        generated_by="test",
    )
    return artifact, diagram


def _me(ctx, person):
    return ExecutionContext(org_id=ctx.org_id, actor_user_id=person.id)


def test_a_picture_is_added_after_a_paragraph_and_laid_out(scene):
    s, ctx, task, person, _ = scene
    artifact, diagram = _section(s, ctx, task)
    me = _me(ctx, person)
    added = draft_images.add_upload(
        s,
        me,
        artifact_id=artifact.id,
        content_base64="data:image/png;base64," + _png(),
        filename="shot.png",
        section="The flow",
        diagram_id=diagram.id,
        after_paragraph=1,
    )
    assert added["after_paragraph"] == 1 and added["natural_width"] == 200
    assert draft_images.image_data(s, me, artifact_id=artifact.id, image_id=added["id"])[
        "data_url"
    ].startswith("data:image/png;base64,")
    changed = draft_images.update_image(
        s,
        me,
        artifact_id=artifact.id,
        image_id=added["id"],
        fields={
            "width_pct": 40,
            "crop": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5},
            "caption": " Hi ",
        },
    )
    assert changed["width_pct"] == 40 and changed["crop"]["w"] == 0.5 and changed["caption"] == "Hi"
    with pytest.raises(ValidationError):
        draft_images.update_image(
            s, me, artifact_id=artifact.id, image_id=added["id"], fields={"width_pct": 5}
        )
    with pytest.raises(ValidationError):
        draft_images.update_image(
            s,
            me,
            artifact_id=artifact.id,
            image_id=added["id"],
            fields={"crop": {"x": 0.8, "y": 0, "w": 0.5, "h": 0.5}},
        )
    cleared = draft_images.update_image(
        s, me, artifact_id=artifact.id, image_id=added["id"], fields={"crop": None}
    )
    assert cleared["crop"] is None


def test_something_that_is_not_a_picture_is_refused(scene):
    s, ctx, task, person, _ = scene
    artifact, diagram = _section(s, ctx, task)
    me = _me(ctx, person)
    bad = base64.b64encode(b"MZ not a picture at all").decode()
    with pytest.raises(ValidationError):
        draft_images.add_upload(
            s,
            me,
            artifact_id=artifact.id,
            content_base64=bad,
            filename=None,
            section="The flow",
            diagram_id=diagram.id,
            after_paragraph=1,
        )
    with pytest.raises(ValidationError):  # no such part of the draft
        draft_images.add_upload(
            s,
            me,
            artifact_id=artifact.id,
            content_base64=_png(),
            filename=None,
            section="Nowhere",
            diagram_id=None,
            after_paragraph=1,
        )


def test_a_figure_keeps_its_layout_and_a_diagram_cannot_be_deleted_as_a_picture(scene):
    s, ctx, task, person, _ = scene
    artifact, diagram = _section(s, ctx, task)
    me = _me(ctx, person)
    layout = draft_images.set_figure_layout(
        s, me, artifact_id=artifact.id, diagram_id=diagram.id, fields={"width_pct": 55}
    )
    again = draft_images.set_figure_layout(
        s, me, artifact_id=artifact.id, diagram_id=diagram.id, fields={"caption": "Overview"}
    )
    assert again["id"] == layout["id"] and again["width_pct"] == 55
    with pytest.raises(ValidationError):
        draft_images.delete_image(s, me, artifact_id=artifact.id, image_id=layout["id"])


def test_deleting_leaves_other_pictures_and_leaving_a_diagram_out_takes_its_pictures(scene):
    s, ctx, task, person, _ = scene
    artifact, diagram = _section(s, ctx, task)
    me = _me(ctx, person)
    kw = dict(
        artifact_id=artifact.id,
        content_base64=_png(),
        filename=None,
        section="The flow",
        diagram_id=diagram.id,
        after_paragraph=2,
    )
    first = draft_images.add_upload(s, me, **kw)
    second = draft_images.add_upload(s, me, **kw)
    assert second["position"] == first["position"] + 1
    draft_images.delete_image(s, me, artifact_id=artifact.id, image_id=first["id"])
    assert [i["id"] for i in draft_images.list_images(s, me, artifact_id=artifact.id)] == [
        second["id"]
    ]
    with pytest.raises(NotFound):
        draft_images.delete_image(s, me, artifact_id=artifact.id, image_id=first["id"])
    artifacts.drop_diagram_section(s, me, artifact_id=artifact.id, diagram_id=diagram.id)
    assert draft_images.list_images(s, me, artifact_id=artifact.id) == []
    assert s.query(DraftImage).count() >= 0


def test_a_new_paragraph_after_a_picture_pushes_the_later_ones_down(scene):
    s, ctx, task, person, _ = scene
    artifact, diagram = _section(s, ctx, task)
    me = _me(ctx, person)
    after_two = draft_images.add_upload(
        s,
        me,
        artifact_id=artifact.id,
        content_base64=_png(),
        filename=None,
        section="The flow",
        diagram_id=diagram.id,
        after_paragraph=2,
    )
    _, rows = _draft(s, ctx, task)
    one = next(r for r in rows if r.text == "One.")
    new = artifacts.add_statement_after(
        s,
        me,
        artifact_id=artifact.id,
        after_statement_id=one.id,
        text="Between.",
        new_paragraph=True,
    )
    _, now = _draft(s, ctx, task)
    by_text = {r.text: r.paragraph_no for r in now}
    assert by_text == {"One.": 1, "Between.": 2, "Two.": 3}
    assert [r.text for r in now] == ["One.", "Between.", "Two."]
    assert new.paragraph_no == 2
    moved = draft_images.list_images(s, me, artifact_id=artifact.id)[0]
    assert moved["id"] == after_two["id"] and moved["after_paragraph"] == 3


def test_explaining_a_picture_needs_consent_and_shows_the_model_only_the_part_pointed_at(scene):
    s, ctx, task, person, _ = scene
    artifact, diagram = _section(s, ctx, task)
    me = _me(ctx, person)
    pic = draft_images.add_upload(
        s,
        me,
        artifact_id=artifact.id,
        content_base64=_png(400, 200),
        filename=None,
        section="The flow",
        diagram_id=diagram.id,
        after_paragraph=1,
    )
    gateway = Explainer()
    kw = dict(
        artifact_id=artifact.id,
        image_id=pic["id"],
        diagram_id=None,
        instruction="explain the left half",
        previous=[],
    )
    with pytest.raises(ValidationError):
        draft_images.explain(s, me, gateway, region=None, use_vision=False, **kw)
    assert gateway.calls == []
    out = draft_images.explain(
        s, me, gateway, region={"x": 0, "y": 0, "w": 0.5, "h": 1}, use_vision=True, **kw
    )
    assert "[[" not in out["text"] and "D1" not in out["text"] and out["unclear"] == ["tiny text"]
    _, call, instruction, image, mime = gateway.calls[0]
    assert call == "image_explain" and mime == "image/png"
    assert "explain the left half" in instruction and "One." in instruction
    with Image.open(io.BytesIO(image)) as sent:
        assert sent.size == (200, 200)


def test_explaining_a_drawn_figure_sends_its_structure_as_text_never_a_picture(scene):
    s, ctx, task, person, _ = scene
    artifact, diagram = _section(s, ctx, task)
    me = _me(ctx, person)
    gateway = Explainer()
    out = draft_images.explain(
        s,
        me,
        gateway,
        artifact_id=artifact.id,
        image_id=None,
        diagram_id=diagram.id,
        instruction="what does the middle do?",
        region=None,
        previous=["Earlier answer."],
        use_vision=False,
    )
    assert out["text"] == "The flow starts at the portal."
    kind, call, prompt = gateway.calls[0]
    assert kind == "text" and call == "figure_explain"
    assert "what does the middle do?" in prompt and "Earlier answer." in prompt
    assert "boxes" in prompt


def test_a_paragraph_can_go_directly_under_the_diagram_and_everything_moves_down(scene):
    s, ctx, task, person, _ = scene
    artifact, diagram = _section(s, ctx, task)
    me = _me(ctx, person)
    pic = draft_images.add_upload(
        s,
        me,
        artifact_id=artifact.id,
        content_base64=_png(),
        filename=None,
        section="The flow",
        diagram_id=diagram.id,
        after_paragraph=1,
    )
    _, rows = _draft(s, ctx, task)
    first = min(rows, key=lambda r: r.order_index)
    artifacts.add_statement_after(
        s,
        me,
        artifact_id=artifact.id,
        after_statement_id=first.id,
        text="Explained.",
        new_paragraph=True,
        before=True,
    )
    _, now = _draft(s, ctx, task)
    assert [(r.text, r.paragraph_no) for r in now] == [
        ("Explained.", 1),
        ("One.", 2),
        ("Two.", 3),
    ]
    moved = draft_images.list_images(s, me, artifact_id=artifact.id)[0]
    assert moved["id"] == pic["id"] and moved["after_paragraph"] == 2
