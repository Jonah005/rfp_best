"""The response draft explains the task's diagrams, a section at a time."""

from __future__ import annotations

import uuid

import pytest

from app.api.v1.artifacts import _version_out
from app.core.context import ExecutionContext
from app.core.errors import StateConflict, ValidationError
from app.db.session import apply_tenant_context
from app.gateway.embeddings import HashingEmbeddingProvider
from app.models import User
from app.models.enums import StatementClass
from app.schemas.diagrams import (
    DiagramDraftResult,
    DiagramIntentResult,
    DiagramRevisionResult,
    DiagramStep,
    WriteupResult,
)
from app.services import artifacts, copilot, diagram_chat, diagrams, writeup
from app.services import tasks as task_service
from tests.integration.test_a_customer_clause_is_not_left_in_a_task import (  # noqa: F401
    _build,
    env,
    fresh_org,
)
from tests.integration.test_a_diagram_in_the_task_workspace import FLOW, _task

pytestmark = pytest.mark.integration

ARCH = {
    "direction": "LR",
    "nodes": [
        {"id": "p", "label": "Portal", "sources": ["T1"]},
        {"id": "s", "label": "SMS gateway", "basis": "ASSUMPTION"},
    ],
    "edges": [{"source": "p", "target": "s", "label": "codes"}],
}


def _walk(spec):
    """A clean walkthrough: it names every part of the drawing, as the writer must."""
    names = ", ".join(f"[[{n['label']}]]" for n in spec["nodes"])
    return {
        "statement_class": "DIAGRAM_DESCRIPTION",
        "text": f"The drawing runs through {names}.",
        "paragraph": 1,
    }


def _statements(*texts, role="PROJECT_MANAGER", spec=None):
    """A clean section: a walkthrough of the whole drawing, then one promise per text."""
    return WriteupResult(
        statements=[
            _walk(spec or FLOW),
            *[
                {
                    "statement_class": "PROPOSED_COMMITMENT",
                    "text": text,
                    "required_approver_role": role,
                    "paragraph": 2,
                }
                for text in texts
            ],
        ]
    )


class Writer:
    """A stand-in model. Answers to the writer are queued; the rest are by name."""

    def __init__(self, writes=(), intents=(), req_writes=(), **answers):
        self.writes = list(writes)
        self.req_writes = list(req_writes)
        self.req_payloads = []
        self.intents = list(intents)
        self.answers = answers
        self.payloads = []
        self.calls = []

    def generate_structured(self, call, prompt, *, ctx, session):
        self.calls.append(call)
        if call == "diagram_writeup":
            self.payloads.append(prompt)
            item = self.writes.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if call == "requirements_writeup":
            self.req_payloads.append(prompt)
            item = self.req_writes.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if call == "diagram_intent":
            return self.intents.pop(0)
        return self.answers[call]


@pytest.fixture
def scene(app_sessionmaker, env):  # noqa: F811
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org, actor_kind="SYSTEM")
        apply_tenant_context(s, ctx)
        _bid, _legal, _mixed, bidder = _build(s, ctx, env)
        task = _task(s, ctx, bidder)
        person = User(
            org_id=org, email=f"w-{uuid.uuid4().hex[:8]}@example.com", display_name="Writer"
        )
        s.add(person)
        s.flush()
        task.owner_user_id = person.id
        conversation = task_service.ensure_conversation(s, ctx, task_id=task.id, user_id=person.id)
        yield s, ctx, task, person, conversation
        s.commit()


def _draw(s, ctx, task, spec, kind="FLOW"):
    gateway = Writer(diagram_generation=DiagramDraftResult(enough_information=True, spec=spec))
    diagrams.create(s, ctx, task_id=task.id, diagram_type=kind, gateway=gateway)
    return diagrams.diagrams_for_task(s, ctx, task_id=task.id)[-1]


def _draft(s, ctx, task):
    artifact = artifacts.ensure_for_task(s, ctx, task_id=task.id)
    version = artifacts._current_version(s, ctx, artifact)
    return artifact, artifacts.statements_for_version(s, ctx, version_id=version.id)


def test_a_section_is_written_from_the_diagram_and_keeps_its_types(scene):
    s, ctx, task, person, conversation = scene
    diagram = _draw(s, ctx, task, FLOW)
    artifact, _ = _draft(s, ctx, task)
    gateway = Writer(writes=[_statements("We will run the retest within ten days.")])
    out = writeup.write_section(
        s,
        ctx,
        artifact_id=artifact.id,
        diagram_id=diagram.id,
        instruction=None,
        notes=["use SMS codes for login"],
        gateway=gateway,
    )
    assert out["statements"] >= 1
    _, rows = _draft(s, ctx, task)
    written = [r for r in rows if r.diagram_id == diagram.id]
    assert written and all(r.section == out["section"] for r in written)
    # The platform writes what the picture contains, and it is not called "from the tender".
    assert written[0].statement_class is StatementClass.DIAGRAM_DESCRIPTION
    assert "Retest after fixes" in written[0].text
    assert all(r.diagram_version_no == 1 for r in written)
    promise = next(r for r in written if r.statement_class is StatementClass.PROPOSED_COMMITMENT)
    assert promise.commitment_id is not None
    # What the writer saw: the diagram's own boxes and the team's chat.
    seen = gateway.payloads[0]
    assert "Retest after fixes" in seen and "use SMS codes" in seen


def test_rewriting_one_section_leaves_the_others_where_they_were(scene):
    s, ctx, task, person, conversation = scene
    first = _draw(s, ctx, task, FLOW)
    second = _draw(s, ctx, task, ARCH, "ARCHITECTURE")
    artifact, _ = _draft(s, ctx, task)
    gateway = Writer(
        writes=[
            _statements("Flow one.", "Flow two."),
            _statements("Architecture one.", spec=ARCH),
            _statements("Flow, rewritten."),
        ]
    )
    for d in (first, second):
        writeup.write_section(
            s,
            ctx,
            artifact_id=artifact.id,
            diagram_id=d.id,
            instruction=None,
            notes=[],
            gateway=gateway,
        )
    writeup.write_section(
        s,
        ctx,
        artifact_id=artifact.id,
        diagram_id=first.id,
        instruction="shorter",
        notes=[],
        gateway=gateway,
    )
    _, rows = _draft(s, ctx, task)
    texts = [r.text for r in rows]
    assert "Flow, rewritten." in texts
    assert "Flow one." not in texts and "Flow two." not in texts
    # The rewritten flow section is still first, and the architecture section is intact.
    assert rows[0].diagram_id == first.id and rows[-1].diagram_id == second.id
    assert "Architecture one." in [r.text for r in rows if r.diagram_id == second.id]
    assert [r.order_index for r in rows] == list(range(len(rows)))
    # The old section's promises were withdrawn, not left dangling.
    assert sum(1 for r in rows if r.diagram_id == first.id and r.commitment_id) == 1


def test_a_diagram_with_unconfirmed_parts_always_carries_an_assumption(scene):
    s, ctx, task, person, conversation = scene
    diagram = _draw(s, ctx, task, ARCH, "ARCHITECTURE")
    artifact, _ = _draft(s, ctx, task)
    gateway = Writer(writes=[_statements("We will host the portal.", spec=ARCH)])
    writeup.write_section(
        s,
        ctx,
        artifact_id=artifact.id,
        diagram_id=diagram.id,
        instruction=None,
        notes=[],
        gateway=gateway,
    )
    _, rows = _draft(s, ctx, task)
    assumptions = [r for r in rows if r.statement_class is StatementClass.ASSUMPTION]
    assert assumptions and "SMS gateway" in assumptions[0].text
    assert assumptions[0].assumption_id is not None


def test_asking_in_the_chat_plans_the_write_up_and_the_run_fills_the_draft(scene):
    s, ctx, task, person, conversation = scene
    _draw(s, ctx, task, FLOW)
    _draw(s, ctx, task, ARCH, "ARCHITECTURE")
    gateway = Writer(
        intents=[
            DiagramIntentResult(
                wants_diagram=True, steps=[DiagramStep(action="WRITE_UP", all_diagrams=True)]
            ),
        ],
        writes=[_statements("Flow text."), _statements("Architecture text.", spec=ARCH)],
        req_writes=[
            WriteupResult(
                statements=[
                    {
                        "statement_class": "PROPOSED_COMMITMENT",
                        "required_approver_role": "PROJECT_MANAGER",
                        "text": "We will confirm the standard with the customer.",
                        "paragraph": 1,
                        "answers": ["R-038"],
                    }
                ]
            )
        ],
    )
    answer = copilot.ask(
        s,
        ctx,
        conversation_id=conversation.id,
        question="write these up in the response and how we'll deliver them",
        gateway=gateway,
        embedder=HashingEmbeddingProvider(),
    )
    raw = answer.message.retrieval_trace["writeup"]
    assert len(raw["diagram_ids"]) == 2 and raw["conversation_id"] == str(conversation.id)
    assert "Response draft" in answer.message.content
    # Nothing is written until the plan is carried out.
    assert _draft(s, ctx, task)[1] == []

    progress = []

    class Same:
        """Hands the test's own transaction to `run`, which commits per section."""

        def __init__(self, session):
            self.session = session

        def begin(self):
            pass

        def commit(self):
            self.session.flush()

        def rollback(self):
            pass

        def close(self):
            pass

        def __getattr__(self, name):
            return getattr(self.session, name)

    result = writeup.run(
        writeup.Plan.from_dict(raw),
        session_factory=lambda: Same(s),
        org_id=ctx.org_id,
        user_id=person.id,
        request_id=uuid.uuid4(),
        gateway=gateway,
        progress=progress.append,
    )
    assert len(result["written"]) == 3 and result["failed"] == []
    assert [p["done"] for p in progress if "current" in p][:2] == [0, 1]
    _, rows = _draft(s, ctx, task)
    assert "Flow text." in [r.text for r in rows]
    # The chat's own words reached the writer.
    assert "write these up" in gateway.payloads[0]


def test_one_section_failing_does_not_lose_the_others(scene):
    s, ctx, task, person, conversation = scene
    first = _draw(s, ctx, task, FLOW)
    second = _draw(s, ctx, task, ARCH, "ARCHITECTURE")
    gateway = Writer(
        writes=[
            RuntimeError("model down"),
            RuntimeError("model down"),
            _statements("Architecture text.", spec=ARCH),
        ]
    )

    class Same:
        def __init__(self, session):
            self.session = session

        def begin(self):
            pass

        def commit(self):
            self.session.flush()

        def rollback(self):
            pass

        def close(self):
            pass

        def __getattr__(self, name):
            return getattr(self.session, name)

    result = writeup.run(
        writeup.Plan(
            task_id=task.id,
            diagram_ids=[first.id, second.id],
            titles=["one", "two"],
        ),
        session_factory=lambda: Same(s),
        org_id=ctx.org_id,
        user_id=person.id,
        request_id=uuid.uuid4(),
        gateway=gateway,
    )
    assert len(result["written"]) == 1 and len(result["failed"]) == 1
    assert "model down" in result["failed"][0]["error"]


def test_a_section_is_flagged_when_its_diagram_has_moved_on(scene):
    s, ctx, task, person, conversation = scene
    diagram = _draw(s, ctx, task, FLOW)
    artifact, _ = _draft(s, ctx, task)
    gateway = Writer(
        writes=[_statements("Flow text.")],
        diagram_revision=DiagramRevisionResult(
            spec={
                **FLOW,
                "nodes": [*FLOW["nodes"], {"id": "d", "label": "Sign-off", "basis": "PERSON"}],
                "edges": [*FLOW["edges"], {"source": "c", "target": "d"}],
            }
        ),
    )
    writeup.write_section(
        s,
        ctx,
        artifact_id=artifact.id,
        diagram_id=diagram.id,
        instruction=None,
        notes=[],
        gateway=gateway,
    )
    version = artifacts._current_version(s, ctx, artifact)
    fresh = _version_out(s, ctx, version)
    assert not any(st.out_of_date for st in fresh.statements)

    diagrams.revise(
        s,
        ctx,
        diagram_id=diagram.id,
        instruction="add a sign-off",
        base_version_no=1,
        gateway=gateway,
    )
    moved = _version_out(s, ctx, version)
    flagged = [st for st in moved.statements if st.diagram_id == diagram.id]
    assert flagged and all(st.out_of_date for st in flagged)
    assert "version 2" in flagged[0].out_of_date_reason


def test_nothing_to_write_up_is_said_plainly_and_a_reviewer_is_not_edited(scene):
    s, ctx, task, person, conversation = scene
    gateway = Writer(
        intents=[
            DiagramIntentResult(
                wants_diagram=True, steps=[DiagramStep(action="WRITE_UP", all_diagrams=True)]
            )
        ]
    )
    reply = diagram_chat.handle(
        s,
        ctx,
        task_id=task.id,
        question="write it up",
        last_assistant=None,
        gateway=gateway,
    )
    # With no diagram to write about, the requirements are still answered.
    plan = reply["trace"]["writeup"]
    assert plan["diagram_ids"] == [] and plan["requirements"] is True


def _write_one(s, ctx, task, diagram, text="Flow text."):
    artifact, _ = _draft(s, ctx, task)
    writeup.write_section(
        s,
        ctx,
        artifact_id=artifact.id,
        diagram_id=diagram.id,
        instruction=None,
        notes=[],
        gateway=Writer(writes=[_statements(text)]),
    )
    return artifact


def test_a_reload_finds_the_write_up_still_running_and_what_it_left_unwritten(scene):
    s, ctx, task, person, conversation = scene
    first = _draw(s, ctx, task, FLOW)
    second = _draw(s, ctx, task, ARCH, "ARCHITECTURE")
    _write_one(s, ctx, task, first)

    # Nothing running: the next page load still learns that one diagram has no section.
    state = writeup.status(s, ctx, task_id=task.id)
    assert state["running"] is False and state["has_writeup"] is True
    assert [m["diagram_id"] for m in state["missing"]] == [str(second.id)]

    # Running: a page that was never told about the job finds it by asking the task.
    assert writeup.begin(task.id, org_id=ctx.org_id, titles=["One", "Two"])
    assert writeup.begin(task.id, org_id=ctx.org_id, titles=["Again"]) is False
    writeup.note(task.id, {"total": 2, "done": 1, "failed": 0, "position": 2, "current": "Two"})
    state = writeup.status(s, ctx, task_id=task.id)
    assert state["running"] is True
    assert state["progress"]["current"] == "Two" and state["progress"]["done"] == 1
    # Another organization sees nothing of it.
    other = writeup.running_status(task.id, uuid.uuid4())
    assert other["running"] is False

    writeup.end(
        task.id, {"written": [], "failed": [{"section": "Two", "error": "x"}], "org_id": ctx.org_id}
    )
    after = writeup.status(s, ctx, task_id=task.id)
    assert after["running"] is False
    assert after["last"]["failed"][0]["section"] == "Two" and "org_id" not in after["last"]


def test_finalizing_keeps_the_write_up_as_it_was_and_notices_later_edits(scene):
    s, ctx, task, person, conversation = scene
    diagram = _draw(s, ctx, task, FLOW)
    artifact = _write_one(s, ctx, task, diagram, "We will retest.")
    ctx_user = ExecutionContext(org_id=ctx.org_id, actor_user_id=person.id)
    apply_tenant_context(s, ctx_user)

    done = writeup.finalize(s, ctx_user, task_id=task.id)
    assert done["sections"] == 1
    assert writeup.status(s, ctx, task_id=task.id)["finalized"]["unchanged"] is True

    # The draft moves on: the section is rewritten. What was finalized does not change.
    writeup.write_section(
        s,
        ctx,
        artifact_id=artifact.id,
        diagram_id=diagram.id,
        instruction="different",
        notes=[],
        gateway=Writer(writes=[_statements("Something else entirely.")]),
    )
    assert writeup.status(s, ctx, task_id=task.id)["finalized"]["unchanged"] is False
    package = writeup.final_package(s, ctx, task_id=task.id)
    texts = [st["text"] for sec in package["sections"] for st in sec["statements"]]
    assert "We will retest." in texts and "Something else entirely." not in texts
    # Each diagram comes back drawn, from the version the words were written about.
    assert package["sections"][0]["diagram"]["svg"].lstrip().startswith("<svg")
    assert package["sections"][0]["diagram"]["version_no"] == 1


def test_a_section_that_is_out_of_date_cannot_be_finalized(scene):
    s, ctx, task, person, conversation = scene
    diagram = _draw(s, ctx, task, FLOW)
    _write_one(s, ctx, task, diagram)
    diagrams.revise(
        s,
        ctx,
        diagram_id=diagram.id,
        instruction="add a sign-off",
        base_version_no=1,
        gateway=Writer(
            diagram_revision=DiagramRevisionResult(
                spec={
                    **FLOW,
                    "nodes": [*FLOW["nodes"], {"id": "d", "label": "Sign-off", "basis": "PERSON"}],
                    "edges": [*FLOW["edges"], {"source": "c", "target": "d"}],
                }
            )
        ),
    )
    with pytest.raises(ValidationError, match="out of date"):
        writeup.finalize(s, ctx, task_id=task.id)


def test_nothing_is_finalized_while_it_is_still_being_written_or_when_empty(scene):
    s, ctx, task, person, conversation = scene
    with pytest.raises(ValidationError, match="no write-up"):
        writeup.finalize(s, ctx, task_id=task.id)
    diagram = _draw(s, ctx, task, FLOW)
    _write_one(s, ctx, task, diagram)
    assert writeup.begin(task.id, org_id=ctx.org_id, titles=["x"])
    try:
        with pytest.raises(StateConflict):
            writeup.finalize(s, ctx, task_id=task.id)
    finally:
        writeup.end(task.id)


def _as(text, klass="DIAGRAM_DESCRIPTION", paragraph=1):
    return {"statement_class": klass, "text": text, "paragraph": paragraph}


def test_a_rejected_answer_is_asked_for_again_with_the_reason(scene):
    s, ctx, task, person, conversation = scene
    diagram = _draw(s, ctx, task, FLOW)
    artifact, _ = _draft(s, ctx, task)
    gateway = Writer(
        writes=[
            # Chat shorthand leaked into the customer's document: not accepted.
            WriteupResult(statements=[_walk(FLOW), _as("As D1 shows, we start with the retest.")]),
            _statements("We will retest after fixes."),
        ]
    )
    writeup.write_section(
        s,
        ctx,
        artifact_id=artifact.id,
        diagram_id=diagram.id,
        instruction=None,
        notes=["D1 is the flow"],
        gateway=gateway,
    )
    assert gateway.calls.count("diagram_writeup") == 2
    assert "Do not call diagrams D1" in gateway.payloads[1]
    _, rows = _draft(s, ctx, task)
    assert not any("D1" in r.text for r in rows)


def test_a_sentence_that_keeps_breaking_the_rules_is_dropped_not_the_section(scene):
    s, ctx, task, person, conversation = scene
    diagram = _draw(s, ctx, task, FLOW)
    artifact, _ = _draft(s, ctx, task)
    bad = WriteupResult(statements=[_walk(FLOW), _as("See D2 for the retest.")])
    writeup.write_section(
        s,
        ctx,
        artifact_id=artifact.id,
        diagram_id=diagram.id,
        instruction=None,
        notes=[],
        gateway=Writer(writes=[bad, bad]),
    )
    _, rows = _draft(s, ctx, task)
    assert rows and not any("D2" in r.text for r in rows)


def test_a_section_with_nothing_usable_fails_with_a_reason(scene):
    s, ctx, task, person, conversation = scene
    diagram = _draw(s, ctx, task, FLOW)
    artifact, _ = _draft(s, ctx, task)
    bad = WriteupResult(statements=[_as("See D2 for the retest.")])
    with pytest.raises(ValidationError, match="broke the rules twice"):
        writeup.write_section(
            s,
            ctx,
            artifact_id=artifact.id,
            diagram_id=diagram.id,
            instruction=None,
            notes=[],
            gateway=Writer(writes=[bad, bad]),
        )
    assert _draft(s, ctx, task)[1] == []


def test_the_writer_may_only_name_parts_that_are_on_the_drawing(scene):
    s, ctx, task, person, conversation = scene
    diagram = _draw(s, ctx, task, FLOW)
    artifact, _ = _draft(s, ctx, task)
    invented = WriteupResult(
        statements=[_walk(FLOW), _as("Then the [[Quantum gateway]] signs everything off.")]
    )
    gateway = Writer(writes=[invented, _statements("We will retest.")])
    writeup.write_section(
        s,
        ctx,
        artifact_id=artifact.id,
        diagram_id=diagram.id,
        instruction=None,
        notes=[],
        gateway=gateway,
    )
    assert "Quantum gateway" in gateway.payloads[1]  # said back to the writer as the reason
    _, rows = _draft(s, ctx, task)
    assert not any("Quantum" in r.text for r in rows)
    # The brackets that let the code check the names are gone from what is stored.
    assert not any("[[" in r.text for r in rows)


def test_parts_the_writer_never_mentions_are_added_by_the_platform(scene):
    s, ctx, task, person, conversation = scene
    diagram = _draw(s, ctx, task, FLOW)
    artifact, _ = _draft(s, ctx, task)
    thin = WriteupResult(
        statements=[
            _as("It starts with [[Test requested]]."),
            _as("We will retest.", "DIAGRAM_DESCRIPTION", 2),
        ]
    )
    writeup.write_section(
        s,
        ctx,
        artifact_id=artifact.id,
        diagram_id=diagram.id,
        instruction=None,
        notes=[],
        gateway=Writer(writes=[thin, thin]),
    )
    _, rows = _draft(s, ctx, task)
    assert any(
        r.text.startswith("The drawing also includes") and "Retest after fixes" in r.text
        for r in rows
    )


def test_a_section_is_stored_as_numbered_paragraphs(scene):
    s, ctx, task, person, conversation = scene
    diagram = _draw(s, ctx, task, FLOW)
    artifact, _ = _draft(s, ctx, task)
    writeup.write_section(
        s,
        ctx,
        artifact_id=artifact.id,
        diagram_id=diagram.id,
        instruction=None,
        notes=[],
        gateway=Writer(writes=[_statements("We will retest.", "We will report.")]),
    )
    _, rows = _draft(s, ctx, task)
    assert [r.paragraph_no for r in rows][:3] == [1, 2, 2]
    package_rows = writeup._sections_of(rows)
    assert package_rows[0]["statements"][1]["paragraph_no"] == 2


def test_the_added_assumption_only_lists_what_the_writer_left_out(scene):
    s, ctx, task, person, conversation = scene
    diagram = _draw(s, ctx, task, ARCH, "ARCHITECTURE")
    artifact, _ = _draft(s, ctx, task)
    covering = WriteupResult(
        statements=[
            _as("Read [[Portal]] and [[SMS gateway]] left to right."),
            {
                "statement_class": "ASSUMPTION",
                "paragraph": 2,
                "text": "We assume the SMS gateway exists and the portal reaches the SMS gateway.",
            },
        ]
    )
    writeup.write_section(
        s,
        ctx,
        artifact_id=artifact.id,
        diagram_id=diagram.id,
        instruction=None,
        notes=[],
        gateway=Writer(writes=[covering]),
    )
    _, rows = _draft(s, ctx, task)
    assumptions = [r for r in rows if r.statement_class is StatementClass.ASSUMPTION]
    # The box and the link are both named already, so nothing is repeated.
    assert len(assumptions) == 1


def test_a_first_draft_may_not_use_the_diagram_description_class():
    from app.core.errors import ValidationError as AppError
    from app.schemas.drafting import DraftStatementProposal
    from app.services import drafting

    proposal = DraftStatementProposal(statement_class="DIAGRAM_DESCRIPTION", text="It shows a box.")
    with pytest.raises(AppError):
        drafting.map_proposals(ExecutionContext(org_id=uuid.uuid4()), [proposal], {}, {})
    ok = drafting.map_proposals(
        ExecutionContext(org_id=uuid.uuid4()), [proposal], {}, {}, allow_descriptions=True
    )
    assert ok[0]["statement_class"] == "DIAGRAM_DESCRIPTION"


def test_a_description_has_to_say_which_diagram(scene):
    s, ctx, task, person, conversation = scene
    artifact, _ = _draft(s, ctx, task)
    with pytest.raises(ValidationError, match="which diagram"):
        artifacts.save_draft(
            s,
            ctx,
            artifact_id=artifact.id,
            statements=[{"statement_class": "DIAGRAM_DESCRIPTION", "text": "A picture."}],
        )


def test_removing_a_duplicate_takes_its_section_with_it_and_names_the_two_apart(scene):
    s, ctx, task, person, conversation = scene
    first = _draw(s, ctx, task, FLOW)
    second = _draw(s, ctx, task, FLOW)
    _write_one(s, ctx, task, first)
    _write_one(s, ctx, task, second, "Second flow text.")

    # "Delete the duplicate" without saying which is not guessed at: both are described.
    vague = Writer(
        intents=[DiagramIntentResult(wants_diagram=True, steps=[DiagramStep(action="REMOVE")])]
    )
    reply = diagram_chat.handle(
        s, ctx, task_id=task.id, question="delete the duplicate", last_assistant=None, gateway=vague
    )
    assert "Which diagram" in reply["content"] and "3 parts" in reply["content"]
    assert len(diagrams.diagrams_for_task(s, ctx, task_id=task.id)) == 2

    clear = Writer(
        intents=[
            DiagramIntentResult(
                wants_diagram=True, steps=[DiagramStep(action="REMOVE", target="D2")]
            )
        ]
    )
    reply = diagram_chat.handle(
        s, ctx, task_id=task.id, question="remove D2", last_assistant=None, gateway=clear
    )
    assert "Removed" in reply["content"] and "Response draft" in reply["content"]
    remaining = diagrams.diagrams_for_task(s, ctx, task_id=task.id)
    assert [d.id for d in remaining] == [first.id]
    _, rows = _draft(s, ctx, task)
    assert rows and all(r.diagram_id == first.id for r in rows)
