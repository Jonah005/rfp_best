"""The contract between the prompt, the builder, the extractor and the columns.

Four separate bugs in this area, and not one of them was a mistake inside any
of those four pieces. Each piece was correct; they disagreed with each other,
and nothing anywhere failed when they did.

    1. Evidence rendered above the "AUTHORIZED CONTEXT" heading, so the model
       quoted it and refused to use it.
    2. [E#] emitted in a format the extractor did not match, so a grounded
       answer reported "no sources cited".
    3. [R#] extracted with no column to land in -- same symptom, new cause.
    4. Requirements STILL rendered above the heading, because fix (1) was
       applied to evidence only. The copilot denied that any Jadwa contact
       person existed while R-002, in the same context, required notifying one.

Every one produced a confidently wrong sentence in the interface and nothing
else: no exception, no log line, no failing test. That is the property worth
attacking, and it cannot be attacked by testing the pieces harder. These tests
assert the AGREEMENTS instead -- the things that must stay true between the
pieces, whatever anyone adds later.

The point is the fifth bug, which has not happened yet. When somebody adds a
fifth kind of material and wires up three of the four places, one of these
fails and names the one they missed.
"""

from __future__ import annotations

import pytest

from app.gateway.registry import get as get_call_type
from app.services.citations import ANSWERING_KEYS, CONTEXT_KINDS, ContextKind, kind_for, refs_in


@pytest.mark.parametrize("kind", CONTEXT_KINDS, ids=lambda k: k.key)
def test_the_prompt_names_every_kind_the_builder_can_render(kind: ContextKind):
    """Bug 4 exactly: the builder rendered requirements, the prompt described
    three kinds, and the model treated the undescribed fourth as scenery."""
    prompt = get_call_type("task_copilot").system_prompt
    example = kind.pattern.replace(r"\d+", "#").replace("#", "###" if "-" in kind.pattern else "#")
    assert example in prompt, (
        f"The builder renders {kind.key} but the copilot prompt never mentions "
        f"{example}. The model has no reason to treat it as usable context."
    )


@pytest.mark.parametrize("kind", CONTEXT_KINDS, ids=lambda k: k.key)
def test_every_kind_is_extractable_from_a_citation(kind: ContextKind):
    """Bug 2: a prefix the prompt invites and the extractor cannot read."""
    sample = kind.pattern.replace(r"\d+", "7").replace("-", "-00")
    assert refs_in(f"backed by [{sample}].") == {sample}
    assert kind_for(sample) is kind


@pytest.mark.parametrize("kind", CONTEXT_KINDS, ids=lambda k: k.key)
def test_every_kind_either_records_its_citations_or_says_why_not(kind: ContextKind):
    """Bug 3: extracted and then dropped, with nothing to show it was dropped."""
    from app.models import ChatMessage

    if kind.records_to is None:
        assert kind.why_unrecorded
    else:
        assert kind.records_to in ChatMessage.__table__.c


def test_no_context_is_rendered_above_the_authorized_heading():
    """Bugs 1 and 4, which are the same bug found twice, four months apart.

    The prompt says "answer ONLY from the AUTHORIZED CONTEXT". Anything printed
    before that heading is, by the model's correct reading, not authorized. It
    will quote such material and then decline to use it -- the most confusing
    failure this system can produce, because the answer is visibly present in
    the transcript.
    """
    from app.services.context_builder import TaskContext

    class _Task:
        code = "COM-01"
        title = "Commercial requirements"

        class state:
            value = "NOT_STARTED"

    rendered = TaskContext(
        task=_Task(),
        sources=[],
        requirement_block="[R-002] (ELIGIBILITY, mandatory, none/blocked) Notify the contact.",
        retrieved_block="[S1] a chunk",
        evidence_block="[E1] an approved item",
        resolution_block="[R1] an answer from HR",
        trace={},
    ).prompt_block()

    heading = rendered.index("AUTHORIZED CONTEXT")
    for kind in CONTEXT_KINDS:
        position = rendered.find(kind.heading)
        assert position != -1, f"{kind.key} is declared but never rendered"
        assert position > heading, (
            f"{kind.key} is rendered above the AUTHORIZED CONTEXT heading. The model is "
            "told to answer only from what follows that heading, so it will read this "
            "material and correctly refuse to use it."
        )


def test_a_task_holding_only_requirements_still_has_nothing_to_answer_from():
    """Requirements are authorized and citable, and they state the QUESTION.

    Counting them as material would let the model answer "what has the company
    done" out of the tender's description of what it wants -- which is the
    single most expensive mistake available here, because the result reads like
    a substantiated claim.
    """
    from app.services.context_builder import TaskContext

    class _Task:
        code = "COM-01"
        title = "Commercial"

        class state:
            value = "NOT_STARTED"

    rendered = TaskContext(
        task=_Task(),
        sources=[],
        requirement_block="[R-002] (ELIGIBILITY, mandatory, none/blocked) Notify the contact.",
        retrieved_block="",
        evidence_block="",
        resolution_block="",
        trace={},
    ).prompt_block()

    assert "(nothing admissible was found)" in rendered
    assert "requirement" not in ANSWERING_KEYS


def test_a_requirement_and_a_department_answer_cannot_be_confused():
    """[R-002] is a requirement; [R2] is HR's reply. One letter apart, and a
    reviewer following a citation to the wrong table would find the wrong
    thing -- or nothing, and conclude the citation was invented."""
    assert kind_for("R-002").key == "requirement"
    assert kind_for("R2").key == "resolution"
    assert refs_in("per [R-002], and [R2] confirms") == {"R-002", "R2"}


def test_the_prompt_forbids_range_citations():
    """A range is recorded as its two endpoints, so "[E1] through [E12]" files
    an answer as resting on two sources instead of twelve. Expanding ranges at
    the counting end would be worse: it would invent ten attributions the model
    never made."""
    prompt = get_call_type("task_copilot").system_prompt.lower()
    assert "range" in prompt
    assert "through" in prompt


def test_the_prompt_names_the_governed_route_to_missing_material():
    """Left to itself the model offers to accept pasted context, which is the
    exact ungoverned path the cross-department request exists to replace."""
    prompt = get_call_type("task_copilot").system_prompt.lower()
    assert "cross-department request" in prompt
    assert "paste" in prompt


def test_an_answer_records_which_prompt_produced_it():
    """`chat_message.prompt_version` existed from the first migration and
    nothing ever wrote to it.

    The cost only shows up the day a prompt changes: an answer from before the
    rewrite and one from after are indistinguishable in the database, so
    "is the new prompt actually live?" becomes unanswerable from the record and
    has to be settled by staring at output and guessing. That is not a
    theoretical concern -- it cost an afternoon.
    """
    import inspect

    from app.services import copilot

    source = inspect.getsource(copilot.ask)
    assert "prompt_version=" in source, "an answer must record the prompt that shaped it"
