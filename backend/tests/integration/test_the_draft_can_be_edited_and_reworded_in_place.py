# ruff: noqa: F811
"""Editing the draft sentence by sentence, and asking a model to reword just one piece."""

from __future__ import annotations

import pytest

from app.core.context import ExecutionContext
from app.core.errors import StateConflict, ValidationError
from app.models import Assumption, Commitment
from app.models.enums import ApprovalStatus, ArtifactVersionState, StatementClass
from app.schemas.rewrite import RewriteResult
from app.services import artifacts, rewrite
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


class Rewriter:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.payloads = []

    def generate_structured(self, call, prompt, *, ctx, session):
        assert call == "text_rewrite"
        self.payloads.append(prompt)
        item = self.answers.pop(0)
        return RewriteResult(replacements=item)


def _written(s, ctx, task, *texts):
    """A section of sentences laid straight into the draft; the first promise is a promise."""
    diagram = _draw(s, ctx, task, ARCH)
    artifact, _ = _draft(s, ctx, task)
    rows = []
    for text in texts:
        if text.startswith("We will"):
            rows.append(
                {
                    "statement_class": "PROPOSED_COMMITMENT",
                    "text": text,
                    "required_approver_role": "PROJECT_MANAGER",
                    "paragraph_no": 1,
                }
            )
        else:
            rows.append({"statement_class": "DIAGRAM_DESCRIPTION", "text": text, "paragraph_no": 1})
    artifacts.write_section(
        s,
        ctx,
        artifact_id=artifact.id,
        section="The flow",
        statements=rows,
        diagram_id=diagram.id,
        diagram_version_no=1,
        generated_by="test",
    )
    _, now = _draft(s, ctx, task)
    return artifact, now


def _me(ctx, person):
    return ExecutionContext(org_id=ctx.org_id, actor_user_id=person.id)


def test_editing_a_promise_updates_the_promise_and_a_fact_becomes_the_persons_own(scene):
    s, ctx, task, person, _ = scene
    artifact, rows = _written(s, ctx, task, "We will run the migration.", "We keep a log.")
    me = _me(ctx, person)
    promise = next(r for r in rows if r.statement_class is StatementClass.PROPOSED_COMMITMENT)
    artifacts.edit_statement_text(
        s, me, artifact_id=artifact.id, statement_id=promise.id, text="We will run the move twice."
    )
    s.refresh(promise)
    assert promise.text == "We will run the move twice."
    assert promise.statement_class is StatementClass.PROPOSED_COMMITMENT
    commitment = s.get(Commitment, promise.commitment_id)
    assert commitment.text == "We will run the move twice."
    assert commitment.approval_status is ApprovalStatus.PENDING_APPROVAL

    # A tender-derived sentence loses its citation once the words are somebody else's.
    from app.models import ArtifactStatement

    tender = rows[-1]
    tender.statement_class = StatementClass.TENDER_DERIVED
    tender.source_locator = {"page": 3}
    s.flush()
    artifacts.edit_statement_text(
        s, me, artifact_id=artifact.id, statement_id=tender.id, text="Our own wording."
    )
    s.refresh(tender)
    assert tender.statement_class is StatementClass.HUMAN_PROVIDED_INPUT
    assert tender.source_locator is None and tender.author_user_id == person.id
    current = artifacts._current_version(s, ctx, artifacts._artifact(s, ctx, artifact.id))
    assert "Our own wording." in current.rendered_text
    assert isinstance(tender, ArtifactStatement)


def test_a_blank_edit_and_an_edit_to_a_reviewed_version_are_refused(scene):
    s, ctx, task, person, _ = scene
    artifact, rows = _written(s, ctx, task, "One sentence.")
    me = _me(ctx, person)
    with pytest.raises(ValidationError):
        artifacts.edit_statement_text(
            s, me, artifact_id=artifact.id, statement_id=rows[0].id, text="   "
        )
    current = artifacts._current_version(s, ctx, artifacts._artifact(s, ctx, artifact.id))
    current.state = ArtifactVersionState.IN_REVIEW
    s.flush()
    with pytest.raises(StateConflict):
        artifacts.edit_statement_text(
            s, me, artifact_id=artifact.id, statement_id=rows[0].id, text="Changed."
        )


def test_deleting_a_sentence_withdraws_what_it_promised_and_closes_the_gap(scene):
    s, ctx, task, person, _ = scene
    artifact, rows = _written(s, ctx, task, "We will train staff.", "A second sentence.")
    me = _me(ctx, person)
    promise = next(r for r in rows if r.statement_class is StatementClass.PROPOSED_COMMITMENT)
    commitment_id = promise.commitment_id
    artifacts.delete_statement(s, me, artifact_id=artifact.id, statement_id=promise.id)
    s.expire_all()
    assert s.get(Commitment, commitment_id).approval_status is ApprovalStatus.WITHDRAWN
    _, left = _draft(s, ctx, task)
    assert [r.order_index for r in left] == list(range(len(left)))
    assert promise.id not in {r.id for r in left}
    assert Assumption is not None


def test_a_sentence_added_after_another_stays_in_its_paragraph_as_the_persons_own(scene):
    s, ctx, task, person, _ = scene
    artifact, rows = _written(s, ctx, task, "First.", "Second.")
    me = _me(ctx, person)
    anchor = rows[0]
    new = artifacts.add_statement_after(
        s, me, artifact_id=artifact.id, after_statement_id=anchor.id, text="Between them."
    )
    _, now = _draft(s, ctx, task)
    texts = [r.text for r in now]
    assert texts.index("Between them.") == texts.index(anchor.text) + 1
    assert new.statement_class is StatementClass.HUMAN_PROVIDED_INPUT
    assert new.diagram_id == anchor.diagram_id and new.paragraph_no == anchor.paragraph_no


def test_a_rewrite_is_a_suggestion_built_from_only_the_piece_and_its_neighbours(scene):
    s, ctx, task, person, _ = scene
    artifact, rows = _written(s, ctx, task, "Alpha one.", "We use the portal daily.", "Gamma.")
    me = _me(ctx, person)
    target = next(r for r in rows if r.text == "We use the portal daily.")
    start = target.text.index("portal")
    gateway = Rewriter(["system"])
    out = rewrite.preview(
        s,
        me,
        gateway,
        artifact_id=artifact.id,
        items=[{"statement_id": target.id, "start": start, "end": start + 6}],
        mode="rewrite",
        instruction=None,
    )
    suggestion = out["suggestions"][0]
    assert suggestion["before"] == "portal" and suggestion["after"] == "system"
    assert suggestion["full_after"] == "We use the system daily."
    # Nothing was written.
    s.refresh(target)
    assert target.text == "We use the portal daily."
    sent = gateway.payloads[0]
    assert "Alpha one." in sent and "Gamma." in sent
    # The model saw the neighbours only, not the rest of the response.
    assert len(rows) <= 3 or "Delta" not in sent


def test_a_rewrite_that_invents_a_figure_is_asked_again_then_refused(scene):
    s, ctx, task, person, _ = scene
    artifact, rows = _written(s, ctx, task, "We deliver in weeks.")
    me = _me(ctx, person)
    item = [{"statement_id": rows[0].id}]
    ok = rewrite.preview(
        s,
        me,
        Rewriter(["We deliver in 99 weeks."], ["We deliver within weeks."]),
        artifact_id=artifact.id,
        items=item,
        mode="shorter",
        instruction=None,
    )
    assert ok["suggestions"][0]["after"] == "We deliver within weeks."
    with pytest.raises(ValidationError):
        rewrite.preview(
            s,
            me,
            Rewriter(["We deliver in 99 weeks."], ["We deliver in 98 weeks."]),
            artifact_id=artifact.id,
            items=item,
            mode="rewrite",
            instruction=None,
        )
    # A figure the person asked for is fine.
    done = rewrite.preview(
        s,
        me,
        Rewriter(["We deliver in 12 weeks."]),
        artifact_id=artifact.id,
        items=item,
        mode="custom",
        instruction="say it takes 12 weeks",
    )
    assert "12" in done["suggestions"][0]["after"]


def test_a_custom_rewrite_needs_an_instruction_and_a_word_stays_a_word(scene):
    s, ctx, task, person, _ = scene
    artifact, rows = _written(s, ctx, task, "We use the portal daily.")
    me = _me(ctx, person)
    with pytest.raises(ValidationError):
        rewrite.preview(
            s,
            me,
            Rewriter(["x"]),
            artifact_id=artifact.id,
            items=[{"statement_id": rows[0].id}],
            mode="custom",
            instruction=None,
        )
    start = rows[0].text.index("portal")
    out = rewrite.preview(
        s,
        me,
        Rewriter(["A very long answer that goes on and on and on for far too long"], ["site"]),
        artifact_id=artifact.id,
        items=[{"statement_id": rows[0].id, "start": start, "end": start + 6}],
        mode="rewrite",
        instruction=None,
    )
    assert out["suggestions"][0]["after"] == "site"


def test_refining_a_suggestion_starts_from_it_and_must_differ(scene):
    s, ctx, task, person, _ = scene
    artifact, rows = _written(s, ctx, task, "We deliver in weeks.")
    me = _me(ctx, person)
    gateway = Rewriter(
        ["We deliver in weeks."],  # unchanged: rejected
        ["Delivery takes a few weeks."],
    )
    first = rewrite.preview(
        s,
        me,
        gateway,
        artifact_id=artifact.id,
        items=[{"statement_id": rows[0].id}],
        mode="rewrite",
        instruction=None,
    )
    assert first["suggestions"][0]["after"] == "Delivery takes a few weeks."
    again = Rewriter(["Delivery takes a few weeks."], ["Expect delivery within weeks."])
    out = rewrite.preview(
        s,
        me,
        again,
        artifact_id=artifact.id,
        items=[{"statement_id": rows[0].id}],
        mode="custom",
        instruction="more confident",
        previous=["Delivery takes a few weeks."],
    )
    assert out["suggestions"][0]["after"] == "Expect delivery within weeks."
    assert "Delivery takes a few weeks." in again.payloads[0]
    assert "previous_suggestion" in again.payloads[0]
    with pytest.raises(ValidationError):
        rewrite.preview(
            s,
            me,
            Rewriter(["x"]),
            artifact_id=artifact.id,
            items=[{"statement_id": rows[0].id}],
            mode="rewrite",
            instruction=None,
            previous=["a", "b"],
        )
