"""A requirement must recognise an exact copy of itself.

R-055 was a byte-for-byte copy of R-050 -- same quoted clause, same
restatement, same block, same category, same owner -- and reconciliation scored
it 0.748 and created a new row. Two thousandths under the assignment floor.

The cause was not the threshold and not the metric. `_requirements_from_block`
returns the stored `requirement_text` (the clause as the document states it),
while every proposal was reduced to `normalized_requirement or
requirement_text` (the model's restatement) before scoring. So the matcher was
comparing a clause against a paraphrase, and a row could not match a perfect
copy of itself. Which duplicates appeared on any given rerun was decided by
where each pair happened to fall either side of 0.75.

This is the fourth time in this file that two values which both look like "the
requirement text" have sat next to each other with only one of them deciding.
The tests below fix the invariant rather than the incident: both sides of a
similarity comparison draw the SAME field, and that field is the clause.

Why the clause and not the restatement -- the measured reason, not a preference.
The restatement compresses. On the live bid, R-024 and R-025 are two distinct
Tradenet obligations ("create beneficiary accounts", "create electronic trading
accounts") and score 0.879 as restatements against 0.804 as clauses. The
restatement pulls two distinct obligations 7.5 points closer together, and it
does that by compressing away the words that tell them apart. Matching on it --
or on the better of the two pairings -- moves every sibling pair toward the
merge bar, and a merged obligation is unrecoverable while a duplicate row is
merely visible.
"""

from __future__ import annotations

import inspect

import pytest

from app.services.requirements import (
    _POSITIONAL_FLOOR,
    _match_text,
    _similarity,
)

# The two texts R-050 and R-055 carry, verbatim from the live bid. Identical to
# each other; the clause and the restatement differ from one another, which is
# the entire mechanism.
# Taken from the database with scripts/dump_identity_fixture.py, not written by
# hand. The first draft of this test used a paraphrase I composed myself; it
# scored 0.877 where the real pair scores 0.7479, so the test passed while the
# bug it claimed to cover was untouched. That is exactly how the difflib
# `autojunk` defect survived its own unit test earlier in this codebase: a
# fixture that resembles the data is not the data.
CLAUSE = (
    "Jadwa will provide access to Core OMS system for the following purpose: "
    "Create Beneficiary accounts, Create Electronic trading account with defined "
    "settings, Auto approval of compliance based on new system approval."
)
RESTATEMENT = (
    "Jadwa will provide access to the Core OMS system for creating beneficiary "
    "accounts, electronic trading accounts, and auto-approval of compliance."
)

# The pair that must never merge, also verbatim.
BENEFICIARY_CLAUSE = (
    "The system must integrate with the Tradenet Core OMS system to create "
    "beneficiary accounts."
)
TRADING_CLAUSE = (
    "The system must integrate with the Tradenet Core OMS system to create "
    "electronic trading accounts within the same platform."
)
BENEFICIARY_RESTATED = (
    "System must integrate with Tradenet Core OMS to create beneficiary accounts."
)
TRADING_RESTATED = (
    "System must integrate with Tradenet Core OMS to create electronic trading accounts."
)


class _Proposal:
    def __init__(self, requirement_text: str, normalized_requirement: str | None):
        self.requirement_text = requirement_text
        self.normalized_requirement = normalized_requirement


def test_a_row_recognises_an_exact_copy_of_itself():
    """The regression, stated as the thing that was impossible before.

    The stored side of the comparison is `requirement_text`, because that is
    what `_requirements_from_block` returns. So the proposal side must be the
    same field, and an identical row must score 1.0 -- not 0.748.
    """
    stored_clause = CLAUSE
    proposal = _Proposal(requirement_text=CLAUSE, normalized_requirement=RESTATEMENT)
    assert _similarity(stored_clause, _match_text(proposal)) == pytest.approx(1.0)


def test_the_old_cross_field_comparison_really_did_fall_below_the_floor():
    """Guards the diagnosis itself.

    If this ever stops holding, the story told in `_match_text` is no longer
    true of these texts and the comment is misleading someone. The failing
    comparison is reconstructed explicitly rather than described.
    """
    cross_field = _similarity(CLAUSE, RESTATEMENT)
    assert cross_field < _POSITIONAL_FLOOR, (
        "the clause-vs-restatement score is what created R-055; if it now clears "
        "the floor, re-derive the explanation before trusting it"
    )
    # The exact figure, so a change to `_similarity` cannot quietly move the
    # story out from under the explanation in `_match_text`. Measured twice by
    # different routes: scripts/replay_r055.py scored R-050 at 0.748 against the
    # live proposal, and scripts/dump_identity_fixture.py at 0.7479.
    assert cross_field == pytest.approx(0.7479, abs=0.0005)


def test_two_distinct_obligations_stay_distinct_on_the_clause():
    """The reason the fix is not "compare restatements".

    Restatement-to-restatement is consistent -- and it merges these two.
    """
    as_clauses = _similarity(BENEFICIARY_CLAUSE, TRADING_CLAUSE)
    as_restatements = _similarity(BENEFICIARY_RESTATED, TRADING_RESTATED)

    # The comparison is between the two fields, not against the bar. The bar has
    # since moved to 0.90 for an unrelated reason (the SMS obligations reaching
    # 0.848), and pinning this test to it made it assert something about the
    # threshold rather than about the fields.
    assert as_clauses < as_restatements, (
        "the restatement compresses away the words that distinguish these two "
        "obligations; the clause keeps them, which is why identity is judged on it"
    )
    assert as_restatements - as_clauses > 0.05, (
        "if the two fields no longer disagree materially, this test has stopped "
        "documenting why the clause was chosen"
    )


def test_match_text_ignores_the_restatement_entirely():
    """No fallback, no preference order. A proposal with a restatement and one
    without must be judged by the same words, or the comparison silently
    changes kind depending on whether the model filled a nullable field."""
    with_restatement = _Proposal(CLAUSE, RESTATEMENT)
    without = _Proposal(CLAUSE, None)
    assert _match_text(with_restatement) == _match_text(without) == CLAUSE.strip()


def test_no_similarity_call_in_the_matcher_scores_a_restatement():
    """The invariant, checked where it can actually drift.

    Both defects here were introduced by someone writing
    `normalized_requirement or requirement_text` next to a `_similarity` call
    without noticing that the other operand came from a different column. This
    fails the moment that expression reappears in either matcher.
    """
    from app.services import requirements

    for function in (
        requirements._reconcile_against_existing,
        requirements._find_existing,
    ):
        source = inspect.getsource(function)
        assert "normalized_requirement or" not in source, (
            f"{function.__name__} builds a comparison text from the restatement; "
            "similarity must be scored on the clause via _match_text"
        )


def test_the_clause_cannot_be_omitted_by_a_caller():
    """A forgotten argument must break loudly.

    `match_text` was first written as `str | None = None`, falling back to
    `normalized`. Every caller that forgot it silently got the cross-field
    comparison back -- and one did, within the hour: the replay script called
    `_find_existing` without it and reported "would create a new row" against a
    pipeline that was already fixed. A default that restores the defect is worse
    than no fix, because it also removes the evidence.
    """
    signature = inspect.signature(
        __import__("app.services.requirements", fromlist=["_find_existing"])._find_existing
    )
    parameter = signature.parameters["match_text"]
    assert parameter.default is inspect.Parameter.empty, (
        "_find_existing must require match_text; a default silently reintroduces "
        "the clause-vs-restatement comparison"
    )


def test_the_stored_side_is_still_the_clause():
    """The other half of the pair. `_match_text` is only correct while
    `_requirements_from_block` returns `requirement_text`; if that query is ever
    changed to select the restatement, this fix inverts into the same bug."""
    from app.services import requirements

    source = inspect.getsource(requirements._requirements_from_block)
    assert "RequirementVersion.requirement_text" in source
    assert "RequirementVersion.normalized_requirement" not in source
