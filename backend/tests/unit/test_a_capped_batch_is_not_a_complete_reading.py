"""A run that hit its own output limit has not read the document.

`close_findings_this_run_did_not_repeat` retires open findings the latest run did
not raise again, and the guard on it was `covered` -- only blocks the run
actually read. That guard was written and was not enough, because "read the
block" and "was able to say everything it found about the block" are different
claims and only the first was checked.

The ambiguity prompt ends "Report at most 12 issues, the most serious first."
The live tender's 43 blocks fitted into two batches at 25 blocks / 20,000
characters, so the largest number of findings any single run could produce was
24. It produced exactly 24. Both batches came back at the cap, both were counted
as read, and the retirement closed 44 distinct questions that nothing live
covered:

    Please specify the exact submission deadline date.
    Will the detailed workflows be provided to bidders after signing the NDA?
    Please confirm the intended working hours: 9am-4pm or 9pm-4pm?
    Clarify 'avoid redirection and input controls' in Technical Requirement 7.

None of those were rewordings. The model had run out of room, and the system
read its silence as a retraction.

Two changes, and the second is the one that matters. Batches are smaller, so a
document gets more allowance overall -- but that is a mitigation and it can be
outgrown by a longer tender. The rule is that a batch returning exactly the cap
is truncated, and a truncated batch's blocks are not eligible for any conclusion
drawn from absence. That is the same principle the severity downgrade in this
module already applies to BLANK and INCOMPLETE_SCOPE: a partial view cannot
establish that something is not there.
"""

from __future__ import annotations

import inspect


def test_the_cap_is_one_number_in_two_places_and_they_agree():
    """The prompt tells the model twelve; the guard tests for twelve. If they
    drift, a capped batch stops being recognised as capped and the whole
    protection silently lapses -- which is why both sites carry a comment
    pointing at the other."""
    from app.gateway.registry import get as get_call_type
    from app.services.tender_analysis import MAX_ISSUES_PER_BATCH

    prompt = get_call_type("ambiguity_analysis").system_prompt
    assert f"at most {MAX_ISSUES_PER_BATCH} issues" in prompt


def test_a_capped_batch_contributes_no_covered_blocks():
    """The fix, asserted where it decides."""
    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.analyze_ambiguity)
    assert "if len(result.issues) < MAX_ISSUES_PER_BATCH:" in source
    guard_at = source.index("if len(result.issues) < MAX_ISSUES_PER_BATCH:")
    update_at = source.index("covered_blocks.update(")
    assert guard_at < update_at, "coverage must be recorded inside the guard"


def test_the_document_gets_more_room_than_one_cap_allows():
    """43 blocks in two batches made 24 findings the ceiling for the whole
    tender. Batches are now small enough that the ceiling is a property of the
    document's length rather than of the batching constant."""
    from app.services.tender_analysis import MAX_BATCH_BLOCKS, MAX_BATCH_CHARACTERS

    assert MAX_BATCH_BLOCKS <= 10
    assert MAX_BATCH_CHARACTERS <= 8_000


def test_smaller_batches_do_not_silence_a_cross_batch_disagreement():
    """The cost of narrowing the window, and the thing that must not regress.

    Contradiction types need both blocks in view, and smaller batches make that
    rarer. The prompt's answer is that a disagreement with something out of view
    is still reportable -- as INCOMPLETE_SCOPE or UNDEFINED_FIELD -- so nothing
    goes unsaid, it is merely said with a weaker type.
    """
    from app.gateway.registry import get as get_call_type

    prompt = get_call_type("ambiguity_analysis").system_prompt
    assert "appears" in prompt and "to disagree with something you cannot see" in prompt
    assert "INCOMPLETE_SCOPE or UNDEFINED_FIELD" in prompt


def test_the_outline_still_ships_with_every_batch():
    """Smaller windows make the whole-document outline more load-bearing, not
    less: it is what stops the model reporting an unseen section as missing."""
    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.analyze_ambiguity)
    # The outline reaching every batch is asserted on the prompt the model is
    # actually sent, in test_a_question_holds_up_only_what_it_is_about.py --
    # not on the spelling of the call, which changed without the rule changing.
    assert "_payload(" in source and "outline" in source
