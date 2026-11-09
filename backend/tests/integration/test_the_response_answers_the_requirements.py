# ruff: noqa: F811
"""The response answers the tender's requirements, and the person picks the diagrams."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.models import Bid, Diagram, RequirementAssessment, TaskRequirement
from app.models.enums import (
    AssessmentState,
    BidReadiness,
    EvidenceStatus,
    StatementClass,
)
from app.schemas.diagrams import DiagramIntentResult, DiagramStep, WriteupResult
from app.services import diagram_chat, diagrams, writeup
from tests.integration.test_a_customer_clause_is_not_left_in_a_task import (  # noqa: F401
    env,
    fresh_org,
)
from tests.integration.test_a_diagram_in_the_task_workspace import FLOW
from tests.integration.test_a_diagram_is_written_up_in_the_response import (  # noqa: F401
    ARCH,
    Writer,
    _draft,
    _draw,
    _statements,
    _walk,
    scene,
)
from tests.integration.test_assessment_readiness import _company_version, _requirement

pytestmark = pytest.mark.integration


def _add(s, ctx, env, task, code, text, readiness=None, evidence=EvidenceStatus.NONE):
    """A requirement on the task, with (or without) a current assessment."""
    bid = s.get(Bid, task.bid_id)
    req = _requirement(
        s, ctx, bid=bid, doc_version=_company_version(s, ctx, env), code=code, text=text
    )
    s.add(TaskRequirement(org_id=ctx.org_id, task_id=task.id, requirement_id=req.id))
    s.flush()
    if readiness is not None:
        s.add(
            RequirementAssessment(
                org_id=ctx.org_id,
                requirement_id=req.id,
                bid_id=bid.id,
                evidence_status=evidence,
                bid_readiness=readiness,
                rationale="Because of the state of the evidence.",
                assessment_state=AssessmentState.COMPUTED,
                version=1,
                is_current=True,
                computed_at=datetime.now(timezone.utc),
                computed_by="test",
            )
        )
        s.flush()
    return req


def _answer(cls, text, code, paragraph=1, role="PROJECT_MANAGER"):
    row = {"statement_class": cls, "text": text, "paragraph": paragraph, "answers": [code]}
    if cls == "PROPOSED_COMMITMENT":
        row["required_approver_role"] = role
    return row


def _ref(s, ctx, task, diagram):
    """The chat's name for a diagram (D1, D2 ...): its place in the task's own listing.

    Diagrams made in one test transaction share a timestamp, so their order is not the
    order they were drawn in; ask for it rather than assume it.
    """
    rows = diagrams.diagrams_for_task(s, ctx, task_id=task.id)
    return f"D{[d.id for d in rows].index(diagram.id) + 1}"


def _unit_of_work(s):
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

    return lambda: Same(s)


def _run(s, ctx, person, plan, gateway):
    return writeup.run(
        plan,
        session_factory=_unit_of_work(s),
        org_id=ctx.org_id,
        user_id=person.id,
        request_id=uuid.uuid4(),
        gateway=gateway,
    )


def _plan(task, **kw):
    return writeup.Plan(task_id=task.id, diagram_ids=[], titles=[], requirements=True, **kw)


def test_each_requirement_gets_an_answer_and_one_the_customer_must_clarify_gets_only_a_condition(
    scene, env
):  # noqa: F811
    s, ctx, task, person, conversation = scene
    arabic = _add(
        s,
        ctx,
        env,
        task,
        "R-019",
        "The portal shall support Arabic.",
        BidReadiness.NEEDS_CLIENT_CLARIFICATION,
    )
    _add(s, ctx, env, task, "R-024", "The footer year shall update itself.", BidReadiness.BLOCKED)
    gateway = Writer(
        req_writes=[
            # The first answer promises Arabic, which the customer must clarify: rejected.
            WriteupResult(
                statements=[
                    _answer("PROPOSED_COMMITMENT", "We will deliver full Arabic support.", "R-019"),
                    _answer("PROPOSED_COMMITMENT", "The footer year will be computed.", "R-024", 2),
                    _answer("PROPOSED_COMMITMENT", "We will confirm the standard.", "R-038", 3),
                ]
            ),
            WriteupResult(
                statements=[
                    _answer(
                        "ASSUMPTION",
                        "Arabic support depends on the customer supplying approved translations.",
                        "R-019",
                    ),
                    _answer("PROPOSED_COMMITMENT", "The footer year will be computed.", "R-024", 2),
                    _answer("PROPOSED_COMMITMENT", "We will confirm the standard.", "R-038", 3),
                ]
            ),
        ]
    )
    result = _run(s, ctx, person, _plan(task), gateway)
    assert result["failed"] == [] and result["written"][0]["section"] == writeup.REQUIREMENTS_TITLE
    # The second attempt was told why the first was refused.
    assert "on hold" in gateway.req_payloads[1]
    _, rows = _draft(s, ctx, task)
    blocked = [r for r in rows if "R-019" in (r.requirement_codes or [])]
    assert blocked and {r.statement_class for r in blocked} == {StatementClass.ASSUMPTION}
    assert arabic.code == "R-019"
    by_code = {c["code"]: c for c in writeup.coverage_for(s, ctx, task_id=task.id, rows=rows)}
    assert (
        by_code["R-019"]["state"] == "conditional"
        and by_code["R-019"]["needs"] == "Needs the customer to clarify it"
    )
    assert by_code["R-024"]["state"] == "answered"
    # R-038 has no assessment: no evidence, but a promise of how we will meet it is fine.
    assert by_code["R-038"]["state"] == "answered"
    assert by_code["R-038"]["needs"] == "Not assessed yet"
    # What the writer was shown: each requirement with its stance and its state.
    assert '"stance": "hold"' in gateway.req_payloads[0]
    assert '"stance": "commit_only"' in gateway.req_payloads[0]


def test_restating_the_requirement_is_not_an_answer_and_the_gap_is_admitted(scene, env):  # noqa: F811
    s, ctx, task, person, conversation = scene
    _add(s, ctx, env, task, "R-024", "The footer year shall update itself.", BidReadiness.DRAFTABLE)
    restated = WriteupResult(
        statements=[
            {
                "statement_class": "TENDER_DERIVED",
                "text": "The tender requires the footer year to update itself.",
                "tender_source_ref": "T1",
                "paragraph": 1,
                "answers": ["R-024"],
            },
            _answer("PROPOSED_COMMITMENT", "We will confirm the standard.", "R-038", 2),
        ]
    )
    gateway = Writer(req_writes=[restated, restated])
    _run(s, ctx, person, _plan(task), gateway)
    _, rows = _draft(s, ctx, task)
    by_code = {c["code"]: c for c in writeup.coverage_for(s, ctx, task_id=task.id, rows=rows)}
    # Still no answer after the retry: the draft says so in a sentence, and the check agrees.
    assert by_code["R-024"]["state"] == "unanswered"
    admitted = [r for r in rows if r.statement_class is StatementClass.ASSUMPTION]
    assert any("footer year" in r.text and "has not been settled" in r.text for r in admitted)


def test_many_requirements_are_answered_in_parts_of_one_section(scene, env):  # noqa: F811
    s, ctx, task, person, conversation = scene
    codes = []
    for i in range(8):
        code = f"R-1{i:02d}"
        codes.append(code)
        _add(s, ctx, env, task, code, f"Requirement number {i}.", BidReadiness.DRAFTABLE)
    every = ["R-038", *codes]
    ordered = sorted(every)
    parts = [ordered[:6], ordered[6:]]
    gateway = Writer(
        req_writes=[
            WriteupResult(
                statements=[
                    _answer(
                        "PROPOSED_COMMITMENT",
                        f"We will do {code}.",
                        code,
                        n,
                    )
                    for n, code in enumerate(part, start=1)
                ]
            )
            for part in parts
        ]
    )
    result = _run(s, ctx, person, _plan(task), gateway)
    assert result["failed"] == [] and len(gateway.req_payloads) == 2
    _, rows = _draft(s, ctx, task)
    section = [r for r in rows if r.section == writeup.REQUIREMENTS_TITLE]
    assert len(section) == 9
    numbers = [r.paragraph_no for r in section]
    assert numbers == sorted(numbers) and len(set(numbers)) == 9
    assert [r.order_index for r in rows] == list(range(len(rows)))


def test_two_diagrams_get_a_question_and_not_two_sections(scene):
    s, ctx, task, person, conversation = scene
    _draw(s, ctx, task, FLOW)
    _draw(s, ctx, task, ARCH, "ARCHITECTURE")
    gateway = Writer(
        intents=[DiagramIntentResult(wants_diagram=True, steps=[DiagramStep(action="WRITE_UP")])]
    )
    reply = diagram_chat.handle(
        s, ctx, task_id=task.id, question="write it up", last_assistant=None, gateway=gateway
    )
    assert "Which should I write up" in reply["content"]
    assert "writeup" not in reply["trace"]
    assert _draft(s, ctx, task)[1] == []


def test_naming_a_diagram_writes_only_that_one(scene):
    s, ctx, task, person, conversation = scene
    _draw(s, ctx, task, FLOW)
    arch = _draw(s, ctx, task, ARCH, "ARCHITECTURE")
    gateway = Writer(
        intents=[
            DiagramIntentResult(
                wants_diagram=True,
                steps=[DiagramStep(action="WRITE_UP", targets=[_ref(s, ctx, task, arch)])],
            )
        ]
    )
    reply = diagram_chat.handle(
        s,
        ctx,
        task_id=task.id,
        question="just the architecture",
        last_assistant=None,
        gateway=gateway,
    )
    plan = reply["trace"]["writeup"]
    assert plan["diagram_ids"] == [str(arch.id)] and plan["requirements"] is False


def test_leaving_a_diagram_out_removes_its_section_and_it_is_never_asked_about_again(scene):
    s, ctx, task, person, conversation = scene
    first = _draw(s, ctx, task, FLOW)
    second = _draw(s, ctx, task, ARCH, "ARCHITECTURE")
    artifact, _ = _draft(s, ctx, task)
    gateway = Writer(writes=[_statements("Flow text."), _statements("Arch text.", spec=ARCH)])
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
    gateway = Writer(
        intents=[
            DiagramIntentResult(
                wants_diagram=True,
                steps=[DiagramStep(action="LEAVE_OUT", targets=[_ref(s, ctx, task, first)])],
            ),
            DiagramIntentResult(wants_diagram=True, steps=[DiagramStep(action="WRITE_UP")]),
        ]
    )
    reply = diagram_chat.handle(
        s, ctx, task_id=task.id, question="leave the flow out", last_assistant=None, gateway=gateway
    )
    assert "Left out" in reply["content"] and "Its section is gone" in reply["content"]
    _, rows = _draft(s, ctx, task)
    assert {r.diagram_id for r in rows} == {second.id}
    state = writeup.status(s, ctx, task_id=task.id)
    assert {d["title"]: d["state"] for d in state["diagrams"]}[
        writeup.section_title(first)
    ] == "left_out"
    assert state["missing"] == []
    # With one diagram left in there is nothing to choose between, so no question.
    again = diagram_chat.handle(
        s, ctx, task_id=task.id, question="write it up", last_assistant=None, gateway=gateway
    )
    plan = again["trace"]["writeup"]
    assert plan["diagram_ids"] == [str(second.id)] and plan["requirements"] is True


def test_naming_a_left_out_diagram_puts_it_back(scene):
    s, ctx, task, person, conversation = scene
    first = _draw(s, ctx, task, FLOW)
    _draw(s, ctx, task, ARCH, "ARCHITECTURE")
    diagrams.set_left_out(s, ctx, diagram_id=first.id, left_out=True)
    gateway = Writer(
        intents=[
            DiagramIntentResult(
                wants_diagram=True,
                steps=[DiagramStep(action="INCLUDE", targets=[_ref(s, ctx, task, first)])],
            )
        ]
    )
    reply = diagram_chat.handle(
        s, ctx, task_id=task.id, question="put the flow back", last_assistant=None, gateway=gateway
    )
    assert s.get(Diagram, first.id).left_out is False
    assert reply["trace"]["writeup"]["diagram_ids"] == [str(first.id)]


def test_the_chat_can_ask_for_the_requirements_alone(scene, env):  # noqa: F811
    s, ctx, task, person, conversation = scene
    gateway = Writer(
        intents=[
            DiagramIntentResult(
                wants_diagram=True, steps=[DiagramStep(action="WRITE_REQUIREMENTS")]
            )
        ]
    )
    reply = diagram_chat.handle(
        s,
        ctx,
        task_id=task.id,
        question="answer the tender questions",
        last_assistant=None,
        gateway=gateway,
    )
    plan = reply["trace"]["writeup"]
    assert plan["diagram_ids"] == [] and plan["requirements"] is True


def test_finalizing_says_which_requirements_are_still_unanswered(scene, env):  # noqa: F811
    s, ctx, task, person, conversation = scene
    _add(s, ctx, env, task, "R-024", "The footer year shall update itself.", BidReadiness.DRAFTABLE)
    good = WriteupResult(
        statements=[
            _answer("PROPOSED_COMMITMENT", "We will confirm the standard.", "R-038", 1),
            {
                "statement_class": "TENDER_DERIVED",
                "text": "The tender wants the year updated.",
                "tender_source_ref": "T1",
                "paragraph": 2,
                "answers": ["R-024"],
            },
        ]
    )
    _run(s, ctx, person, _plan(task), Writer(req_writes=[good, good]))
    _, rows = _draft(s, ctx, task)
    assert rows
    out = writeup.finalize(s, ctx, task_id=task.id)
    # Restated is not answered: finalizing still goes ahead, but says which are missing.
    assert out["unanswered"] == ["R-024"]
    package = writeup.final_package(s, ctx, task_id=task.id)
    tagged = [
        st for sec in package["sections"] for st in sec["statements"] if st["requirement_codes"]
    ]
    assert tagged


def test_the_status_lists_coverage_for_the_screen(scene, env):  # noqa: F811
    s, ctx, task, person, conversation = scene
    state = writeup.status(s, ctx, task_id=task.id)
    by_code = {c["code"]: c for c in state["coverage"]}
    # The customer's own clauses are not ours to answer; the bidder's requirement is.
    assert by_code["R-037"]["state"] == "not_ours" and by_code["R-038"]["state"] == "unanswered"
    assert state["coverage_counts"]["unanswered"] >= 1
    assert state["requirements_written"] is False


def test_a_requirement_the_diagram_already_answers_is_not_answered_twice(scene):
    s, ctx, task, person, conversation = scene
    flow = _draw(s, ctx, task, FLOW)
    artifact, _ = _draft(s, ctx, task)
    section = WriteupResult(
        statements=[
            _walk(FLOW),
            {**_answer("PROPOSED_COMMITMENT", "We will run the retest.", "R-038", 2)},
        ]
    )
    writeup.write_section(
        s,
        ctx,
        artifact_id=artifact.id,
        diagram_id=flow.id,
        instruction=None,
        notes=[],
        gateway=Writer(writes=[section]),
    )
    gateway = Writer(req_writes=[])  # asking the writer again would fail: nothing is queued
    result = _run(s, ctx, person, _plan(task), gateway)
    assert result["failed"] == [] and gateway.req_payloads == []
    _, rows = _draft(s, ctx, task)
    assert not [r for r in rows if r.section == writeup.REQUIREMENTS_TITLE]
    by_code = {c["code"]: c for c in writeup.coverage_for(s, ctx, task_id=task.id, rows=rows)}
    assert by_code["R-038"]["state"] == "answered"
    assert by_code["R-038"]["diagram_ids"] == [str(flow.id)]


def test_a_section_that_talks_about_the_drawing_from_outside_is_sent_back(scene):
    s, ctx, task, person, conversation = scene
    diagram = _draw(s, ctx, task, FLOW)
    artifact, _ = _draft(s, ctx, task)
    outside = WriteupResult(
        statements=[
            {
                "statement_class": "DIAGRAM_DESCRIPTION",
                "text": "This section explains the diagram for the tender.",
                "paragraph": 1,
            },
            *_statements("We will run the retest.").statements,
        ]
    )
    gateway = Writer(writes=[outside, _statements("We will run the retest.")])
    writeup.write_section(
        s,
        ctx,
        artifact_id=artifact.id,
        diagram_id=diagram.id,
        instruction=None,
        notes=[],
        gateway=gateway,
    )
    assert "from outside" in gateway.payloads[1]


def test_the_writer_is_told_who_the_customer_is_and_what_they_have_answered(scene):
    from app.models import ClarificationIssue
    from app.models.enums import (
        ClarificationIssueType,
        ClarificationSeverity,
        ClarificationStatus,
    )

    s, ctx, task, person, conversation = scene
    for text, status, reply in (
        ("Which screens are mandatory at launch?", ClarificationStatus.ANSWERED, "All of them."),
        ("What is the menu depth?", ClarificationStatus.OPEN, None),
    ):
        s.add(
            ClarificationIssue(
                org_id=ctx.org_id,
                bid_id=task.bid_id,
                issue_type=ClarificationIssueType.INCOMPLETE_SCOPE,
                description=text,
                severity=ClarificationSeverity.MAJOR,
                blocks_generation=False,
                status=status,
                client_response=reply,
                detected_by="test",
                fingerprint=uuid.uuid4().hex,
            )
        )
    s.flush()
    info = writeup.bid_context(s, ctx, task.id)
    assert info["customer"] == "ADNOC"
    assert info["open_questions"] == 1
    assert info["assumptions"] == [
        {"question": "Which screens are mandatory at launch?", "answer": "All of them."}
    ]
    diagram = _draw(s, ctx, task, FLOW)
    artifact, _ = _draft(s, ctx, task)
    gateway = Writer(writes=[_statements("We will run the retest.")])
    writeup.write_section(
        s,
        ctx,
        artifact_id=artifact.id,
        diagram_id=diagram.id,
        instruction=None,
        notes=[],
        gateway=gateway,
    )
    assert "All of them." in gateway.payloads[0] and "ADNOC" in gateway.payloads[0]
