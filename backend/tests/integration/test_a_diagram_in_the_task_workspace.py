"""A task's diagrams: drawn, changed, reviewed, declined, and kept up to date.

The model here is a stand-in that returns what a well-behaved and a badly
behaved model might. What is under test is what the platform does with it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.core.errors import StateConflict
from app.db.session import apply_tenant_context
from app.models import RequirementVersion, Task, TaskRequirement
from app.schemas.diagrams import (
    DiagramDraftResult,
    DiagramPlanResult,
    DiagramReviewResult,
    DiagramRevisionResult,
    MissingInformation,
    NeedProposal,
    StyleIntentResult,
    Suggestion,
)
from app.services import diagrams
from tests.integration.test_a_customer_clause_is_not_left_in_a_task import (  # noqa: F401
    _build,
    env,
    fresh_org,
)

pytestmark = pytest.mark.integration

FLOW = {
    "direction": "LR",
    "nodes": [
        {"id": "a", "label": "Test requested", "kind": "start_end", "sources": ["T1"]},
        {"id": "b", "label": "Scope agreed", "sources": ["T1"]},
        {"id": "c", "label": "Retest after fixes", "basis": "EVIDENCE", "sources": ["E9"]},
    ],
    "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "c"}],
}


class Stub:
    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def generate_structured(self, call, prompt, *, ctx, session):
        self.calls.append(call)
        answer = self.answers[call]
        return answer() if callable(answer) else answer


def _task(s, ctx, bidder):
    return s.execute(
        select(Task)
        .join(TaskRequirement, TaskRequirement.task_id == Task.id)
        .where(TaskRequirement.requirement_id == bidder.id)
    ).scalar_one()


@pytest.fixture
def setup(app_sessionmaker, env):  # noqa: F811
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org, actor_kind="SYSTEM")
        apply_tenant_context(s, ctx)
        _bid, _legal, _mixed, bidder = _build(s, ctx, env)
        task = _task(s, ctx, bidder)
        yield s, ctx, task, bidder
        s.commit()


def test_a_diagram_is_drawn_and_a_false_evidence_claim_becomes_an_assumption(setup):
    s, ctx, task, _ = setup
    gateway = Stub(diagram_generation=DiagramDraftResult(enough_information=True, spec=FLOW))
    out = diagrams.create(s, ctx, task_id=task.id, diagram_type="FLOW", gateway=gateway)
    assert out["created"] is True
    diagram = diagrams.diagrams_for_task(s, ctx, task_id=task.id)[0]
    version = diagrams.current_version(s, ctx, diagram)
    by_id = {n["id"]: n for n in version.spec["nodes"]}
    # E9 does not exist, so the claim of evidence cannot stand.
    assert by_id["c"]["basis"] == "ASSUMPTION"
    assert by_id["b"]["basis"] == "TENDER"
    assert version.unconfirmed
    assert "<svg" in diagrams.svg_for(diagram, version)


def test_not_enough_information_creates_nothing_and_says_what_is_needed(setup):
    s, ctx, task, bidder = setup
    gateway = Stub(
        diagram_generation=DiagramDraftResult(
            enough_information=False,
            missing=[
                MissingInformation(
                    what="Which systems are in scope",
                    why="Boxes would be invented",
                    requirement_codes=[bidder.code, "R-999"],
                    suggested_question="Which systems are in scope for testing?",
                )
            ],
        )
    )
    out = diagrams.create(s, ctx, task_id=task.id, diagram_type="ARCHITECTURE", gateway=gateway)
    assert out["created"] is False
    assert diagrams.diagrams_for_task(s, ctx, task_id=task.id) == []
    # A code the model made up is not pointed at.
    assert out["missing"][0]["requirement_codes"] == [bidder.code]
    issue = diagrams.raise_question(
        s,
        ctx,
        task_id=task.id,
        what="Which systems are in scope",
        why=None,
        question="Which systems are in scope for testing?",
        requirement_codes=[bidder.code],
    )
    assert issue is not None


def _make(s, ctx, task):
    gateway = Stub(diagram_generation=DiagramDraftResult(enough_information=True, spec=FLOW))
    diagrams.create(s, ctx, task_id=task.id, diagram_type="FLOW", gateway=gateway)
    return diagrams.diagrams_for_task(s, ctx, task_id=task.id)[0]


def test_a_stale_save_is_refused_and_a_change_is_a_new_version(setup):
    s, ctx, task, _ = setup
    diagram = _make(s, ctx, task)
    new_spec = {
        **FLOW,
        "nodes": [*FLOW["nodes"], {"id": "d", "label": "Report", "basis": "PERSON"}],
        "edges": [*FLOW["edges"], {"source": "c", "target": "d"}],
    }
    revision = Stub(diagram_revision=DiagramRevisionResult(spec=new_spec))
    out = diagrams.revise(
        s,
        ctx,
        diagram_id=diagram.id,
        instruction="Add a report step",
        base_version_no=1,
        gateway=revision,
    )
    assert out["changed"] and out["version_no"] == 2
    with pytest.raises(StateConflict):
        diagrams.revise(
            s,
            ctx,
            diagram_id=diagram.id,
            instruction="Another change",
            base_version_no=1,
            gateway=revision,
        )
    assert len(diagrams.versions(s, ctx, diagram_id=diagram.id)) == 2
    # The first version is still there, unchanged.
    first = diagrams.version_by_number(s, ctx, diagram_id=diagram.id, version_no=1)
    assert len(first.spec["nodes"]) == 3


def test_restyle_changes_only_the_look(setup):
    s, ctx, task, _ = setup
    diagram = _make(s, ctx, task)
    gateway = Stub(
        diagram_style_intent=StyleIntentResult(
            style={"theme": "BLUEPRINT", "font": "NOT_A_FONT"}, understood="Blueprint look"
        )
    )
    out = diagrams.restyle(
        s, ctx, diagram_id=diagram.id, wish="like a blueprint", base_version_no=1, gateway=gateway
    )
    assert out["changed"]
    v1 = diagrams.version_by_number(s, ctx, diagram_id=diagram.id, version_no=1)
    v2 = diagrams.version_by_number(s, ctx, diagram_id=diagram.id, version_no=2)
    assert v2.style["theme"] == "BLUEPRINT"
    assert v2.spec == v1.spec


def test_a_review_changes_nothing_until_a_suggestion_is_applied(setup):
    s, ctx, task, _ = setup
    diagram = _make(s, ctx, task)
    review = Stub(
        diagram_review=DiagramReviewResult(
            suggestions=[Suggestion(severity="IMPORTANT", text="Show who approves the scope.")]
        )
    )
    found = diagrams.review(s, ctx, diagram_id=diagram.id, gateway=review)
    assert found and found[0]["text"].startswith("Show who")
    assert len(diagrams.versions(s, ctx, diagram_id=diagram.id)) == 1
    new_spec = {
        **FLOW,
        "nodes": [*FLOW["nodes"], {"id": "d", "label": "Approver signs off", "basis": "PERSON"}],
    }
    out = diagrams.apply_suggestions(
        s,
        ctx,
        diagram_id=diagram.id,
        suggestion_indexes=[0],
        base_version_no=1,
        gateway=Stub(diagram_revision=DiagramRevisionResult(spec=new_spec)),
    )
    assert out["changed"] and out["version_no"] == 2


def test_a_declined_type_stays_declined_until_the_wording_changes(setup):
    s, ctx, task, bidder = setup
    proposal = DiagramPlanResult(
        needs=[
            NeedProposal(
                diagram_type="FLOW",
                why="Process",
                requirement_codes=[bidder.code],
                enough_information=True,
            ),
            NeedProposal(
                diagram_type="ARCHITECTURE",
                why="Solution",
                requirement_codes=[bidder.code],
                enough_information=True,
            ),
        ]
    )
    gateway = Stub(diagram_planning=proposal)
    diagrams.plan(s, ctx, task_id=task.id, gateway=gateway)
    diagrams.decide_need(
        s, ctx, task_id=task.id, diagram_type="ARCHITECTURE", accept=False, reason="Not asked"
    )
    diagrams.plan(s, ctx, task_id=task.id, gateway=gateway)
    status = {n.diagram_type: n.status for n in diagrams.needs_for_task(s, ctx, task_id=task.id)}
    assert status["ARCHITECTURE"] == "DECLINED"
    assert status["FLOW"] != "DECLINED"


def test_a_reworded_requirement_makes_the_diagram_stale_and_refresh_clears_it(setup):
    s, ctx, task, bidder = setup
    diagram = _make(s, ctx, task)
    assert diagrams.staleness(s, ctx, diagram)["stale"] is False
    rv = s.get(RequirementVersion, bidder.current_version_id)
    rv.requirement_text = rv.requirement_text + " Retesting is required within 30 days."
    s.flush()
    state = diagrams.staleness(s, ctx, diagram)
    assert state["stale"] and state["changed"][0]["change"] == "REWORDED"
    out = diagrams.refresh(
        s,
        ctx,
        diagram_id=diagram.id,
        base_version_no=1,
        gateway=Stub(diagram_revision=DiagramRevisionResult(spec=FLOW)),
    )
    assert out["version_no"] >= 1
    assert diagrams.staleness(s, ctx, diagram)["stale"] is False


def test_pasted_mermaid_is_imported_without_a_model(setup):
    s, ctx, task, _ = setup
    d = diagrams.import_mermaid(s, ctx, task_id=task.id, text="flowchart LR\n A[Start] --> B[End]")
    v = diagrams.current_version(s, ctx, d)
    assert len(v.spec["nodes"]) == 2
    assert v.origin == "HUMAN_EDITED"


PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


class SeeingStub:
    """Stands in for a vision model; records what it was sent."""

    model_id = "stand-in-vision"

    def __init__(self, reply):
        self.reply = reply
        self.sent = []

    def read(self, system, instruction, image, mime, *, max_tokens=None):
        from app.gateway.base import LLMResponse

        self.sent.append((mime, len(image)))
        return LLMResponse(text=self.reply, model_id=self.model_id)


def _gateway(vision):
    from app.gateway.deepseek import NullLLMProvider
    from app.gateway.gateway import ModelGateway

    return ModelGateway(NullLLMProvider(), None, vision=vision)


def test_a_picture_is_read_only_when_the_person_agrees_for_that_picture(setup):
    from app.core.errors import ValidationError

    s, ctx, task, _ = setup
    vision = SeeingStub("{}")
    with pytest.raises(ValidationError):
        diagrams.import_image(
            s, ctx, task_id=task.id, image=PNG, use_vision=False, gateway=_gateway(vision)
        )
    assert vision.sent == []


def test_a_picture_becomes_a_checked_diagram_owned_by_the_person(setup):
    import json

    s, ctx, task, _ = setup
    reply = json.dumps(
        {
            "is_a_diagram": True,
            "diagram_type": "ARCHITECTURE",
            "spec": {
                "nodes": [
                    {"id": "a", "label": "Portal", "kind": "system", "basis": "TENDER"},
                    {"id": "b", "label": "Database", "kind": "store"},
                ],
                "edges": [{"source": "a", "target": "b"}],
            },
            "unclear": ["a label on the right"],
        }
    )
    vision = SeeingStub(reply)
    out = diagrams.import_image(
        s, ctx, task_id=task.id, image=PNG, use_vision=True, gateway=_gateway(vision)
    )
    assert out["created"] and vision.sent == [("image/png", len(PNG))]
    d = diagrams.diagrams_for_task(s, ctx, task_id=task.id)[0]
    v = diagrams.current_version(s, ctx, d)
    assert v.origin == "IMAGE_IMPORTED"
    # A model's "this comes from the tender" is not accepted for a picture.
    assert {n["basis"] for n in v.spec["nodes"]} == {"PERSON"}
    assert "a label on the right" in v.change_summary


def test_something_that_is_not_a_picture_is_refused(setup):
    from app.core.errors import ValidationError

    s, ctx, task, _ = setup
    with pytest.raises(ValidationError):
        diagrams.import_image(
            s,
            ctx,
            task_id=task.id,
            image=b"MZ not an image",
            use_vision=True,
            gateway=_gateway(SeeingStub("{}")),
        )


def test_without_a_configured_vision_model_the_answer_says_how_to_set_one_up(setup):
    from app.core.errors import ProviderError

    s, ctx, task, _ = setup
    with pytest.raises(ProviderError, match="VISION_API_KEY"):
        diagrams.import_image(
            s, ctx, task_id=task.id, image=PNG, use_vision=True, gateway=_gateway(None)
        )


def test_the_routes_return_the_shape_the_screen_reads(setup):
    from app.api.v1 import diagrams as routes

    s, ctx, task, _ = setup
    _make(s, ctx, task)
    out = routes.task_diagrams(task.id, session=s, ctx=ctx)
    d = out["diagrams"][0]
    assert d["svg"].startswith("<svg") and d["staleness"]["stale"] is False
    assert d["version_no"] == 1 and "spec" in d


def _chain(n):
    return {
        "nodes": [{"id": f"n{i}", "label": f"Step {i}", "sources": ["T1"]} for i in range(n)],
        "edges": [{"source": f"n{i}", "target": f"n{i + 1}"} for i in range(n - 1)],
    }


class Sequenced:
    """Answers the generation call with a different draft each time it is asked."""

    def __init__(self, *drafts):
        self.drafts = list(drafts)
        self.prompts = []

    def generate_structured(self, call, prompt, *, ctx, session):
        self.prompts.append(prompt)
        return self.drafts.pop(0)


def test_a_crowded_first_draft_is_sent_back_and_the_simpler_one_is_kept(setup):
    s, ctx, task, _ = setup
    talker = Sequenced(
        DiagramDraftResult(enough_information=True, spec=_chain(24)),
        DiagramDraftResult(
            enough_information=True, spec=_chain(9), detail_diagrams=["Admin screens in detail"]
        ),
    )
    out = diagrams.create(s, ctx, task_id=task.id, diagram_type="FLOW", gateway=talker)
    assert out["created"] and out["detail"] == ["Admin screens in detail"]
    assert "tangle" in talker.prompts[1]
    d = diagrams.diagrams_for_task(s, ctx, task_id=task.id)[0]
    assert len(diagrams.current_version(s, ctx, d).spec["nodes"]) == 9


def test_a_draft_that_stays_crowded_is_kept_but_the_person_is_told(setup):
    s, ctx, task, _ = setup
    talker = Sequenced(
        DiagramDraftResult(enough_information=True, spec=_chain(24)),
        DiagramDraftResult(enough_information=True, spec=_chain(24)),
    )
    diagrams.create(s, ctx, task_id=task.id, diagram_type="FLOW", gateway=talker)
    d = diagrams.diagrams_for_task(s, ctx, task_id=task.id)[0]
    assert "still dense" in diagrams.current_version(s, ctx, d).change_summary


def test_a_change_that_does_not_grow_a_big_diagram_is_not_sent_back(setup):
    s, ctx, task, _ = setup
    big = Sequenced(
        DiagramDraftResult(enough_information=True, spec=_chain(22)),
        DiagramDraftResult(enough_information=True, spec=_chain(22)),
    )
    diagrams.create(s, ctx, task_id=task.id, diagram_type="FLOW", gateway=big)
    d = diagrams.diagrams_for_task(s, ctx, task_id=task.id)[0]
    edit = Sequenced(DiagramRevisionResult(spec=_chain(21)))
    out = diagrams.revise(
        s, ctx, diagram_id=d.id, instruction="drop one step", base_version_no=1, gateway=edit
    )
    assert out["changed"] and len(edit.prompts) == 1
