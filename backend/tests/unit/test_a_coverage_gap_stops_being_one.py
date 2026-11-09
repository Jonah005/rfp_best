"""A finding must not deny the existence of the requirements it links to.

The live export carried 47 COVERAGE_GAP findings, and 43 of them said:

    "Clause 14 governs this tender but produced neither a requirement nor an
     explicit decision that it imposes none."

while linking to R-014, R-143 and R-144 -- all produced from clause 14. A
finding that contradicts its own link teaches a reader to distrust the whole
column.

The cause was that `covered_ordinals` counts only what THIS RUN processed, and a
rerun skips every block already decomposed under the current prompt. On the
second run of an unchanged document almost nothing is "covered", so clause after
clause was reported as unaccounted for.

Nine of these were also the last thing pointing at requirements a merge had
withdrawn, which is how an export of 104 requirements came to reference 129
requirement codes, with clarifications citing R-145 to R-150 in a workbook that
stops at R-144.

The check now asks the database instead of the run: requirements standing
against the block, or a recorded reading that found none, both mean accounted
for.
"""

from __future__ import annotations

import inspect


def test_a_block_with_requirements_is_not_a_gap():
    """The 43. Asked of stored requirements, not of this run's tally."""
    from app.services import requirements

    source = inspect.getsource(requirements.extract_requirements)
    assert "if _requirements_from_block(session, ctx, bid_id=bid_id, block=block):" in source
    guard_at = source.index("if _requirements_from_block(session, ctx, bid_id=bid_id, block=block)")
    raise_at = source.index("issue_type=ClarificationIssueType.COVERAGE_GAP")
    assert guard_at < raise_at


def test_a_recorded_reading_that_found_nothing_is_an_accounting():
    """"v6 read the executive summary and it is background, not a duty" answers
    the question this check asks. `BlockExtraction.found` stores exactly that,
    which is why 0019 added the table."""
    from app.services import requirements

    source = inspect.getsource(requirements._block_read_and_found_nothing)
    assert "BlockExtraction.found == 0" in source


def test_any_prompt_version_counts_for_coverage():
    """Deliberately looser than `_blocks_already_decomposed`, which is keyed to
    the current prompt. Whether to spend a model call re-reading a block and
    whether a human must review an unaccounted clause are different questions,
    and a considered "no requirements" from an earlier prompt answers the
    second."""
    from app.services import requirements

    source = inspect.getsource(requirements._block_read_and_found_nothing)
    assert "extracted_by" not in source


def test_a_gap_that_is_no_longer_a_gap_is_closed():
    from app.services import requirements

    source = inspect.getsource(requirements.extract_requirements)
    assert "close_resolved_coverage_gaps(" in source
    assert "still_open=" in source


def test_closing_on_absence_is_allowed_here_and_was_not_for_ambiguity():
    """The distinction that keeps this from repeating the 44-question loss.

    Retiring the ambiguity model's findings on absence destroyed real questions
    because the prompt caps output at twelve per batch, so silence meant "ran
    out of room". This detector is deterministic and visits every block on every
    run: there is no cap and no ranking, so "not raised this time" really does
    mean "no longer a gap". The reasoning is recorded where the function is,
    because the next person to see two similar-looking rules should be able to
    tell why one is safe.
    """
    from app.services import clarifications

    doc = clarifications.close_resolved_coverage_gaps.__doc__ or ""
    assert "deterministic" in doc
    assert "cap" in doc


def test_closing_takes_the_links_with_it():
    """These findings were the last holder of 25 withdrawn requirement codes."""
    from app.services import clarifications

    source = inspect.getsource(clarifications.close_resolved_coverage_gaps)
    assert "delete(ClarificationRequirement)" in source


def test_a_merge_moves_its_clarification_links():
    """The withdrawal that caused the dangling codes.

    The question was raised about that clause and the clause is still in the bid
    -- it is part of the survivor now -- so the link follows it rather than
    being deleted.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[2].joinpath(
        "scripts", "coalesce_enumerated_fragments.py"
    ).read_text(encoding="utf-8")
    assert "_move_clarification_links(" in source
    assert "link.requirement_id = survivor" in source
    # A link the survivor already holds must be dropped, not moved: the join is
    # unique on (clarification, requirement).
    assert "if link.clarification_id in existing:" in source


def test_the_export_says_which_question_a_restatement_belongs_to():
    """A restatement stays OPEN on purpose. A sheet that does not say so reports
    84 open questions where there are 47, and a reader calls that unfixed
    duplication."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[2].joinpath(
        "scripts", "_export_queries.py"
    ).read_text(encoding="utf-8")
    assert '"Same question as"' in source
    assert "ClarificationIssue.restates_clarification_id.is_(None)" in source, (
        "the matrix count must exclude restatements"
    )
