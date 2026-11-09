"""A finding must reach the row it concerns without claiming the bid is stuck.

918 clarifications on the live bid, 328 of them open, and **zero linked to any
requirement**. The matrix therefore showed no open clarification against a
single row, `NEEDS_CLIENT_CLARIFICATION` was never used, and the questions the
system had correctly identified -- the vague one-week deadline, the workflows
supplied only after an NDA, the separately-quoted integrations -- were visible
only in a separate workbook nobody drafts from.

The cause was not an oversight. Linking a clarification to a requirement MADE
that requirement report "an open clarification blocks a reliable answer", so a
detector could be visible or be honest and not both. The identity-drift
detector chose invisible and documented why: linking it once put 51 of 59 rows
into that state over notes about our own re-extraction, and the matrix said
nothing else.

Two different questions had one answer:

    does this finding concern this requirement?   -> visibility
    are we waiting on the customer?               -> gating

They are now separate. Linking is what puts a finding on a row. Gating turns on
`recommended_question`: a finding that carries one is a question a customer can
answer, and a finding without one is a note about our own processing. That
distinction was already in the data and simply was not read.
"""

from __future__ import annotations

import inspect


def test_readiness_gates_on_a_question_not_on_a_link():
    """The change, checked where it decides.

    `awaiting_customer` counts only linked open findings that carry a question;
    `open_clarification` still counts them all, because the matrix column should
    show a reviewer everything that touches the row.
    """
    from app.services import assessment

    source = inspect.getsource(assessment.assess_requirement)
    assert "awaiting_customer = sum(" in source
    assert "recommended_question" in source
    # Nobody asks the issuer, so a gap in the tender no longer demotes a row: what
    # the company can prove is the same with or without an unclear clause. The gap
    # is named on the row instead.
    assert "has_open_clarification=False" in source
    assert "unresolved gap(s) in the tender" in source
    assert "open_clarification = len(linked)" in source, (
        "the visible count must stay the full linked count"
    )


def test_an_internal_note_is_linked_and_does_not_gate():
    """Identity drift: reaches its row, raises no question, cannot block."""
    from app.services import requirements

    body = inspect.getsource(requirements._flag_identity_drift).split('"""')[2]
    assert "requirement_ids=[requirement.id]" in body
    assert "recommended_question=None" in body


def test_an_ambiguity_finding_is_linked_to_the_blocks_it_cites():
    """The 224 open findings that were unreachable -- and, since v3, no wider.

    This read the source for `requirement_ids=_requirements_of(`, which pinned
    the block-wide link that later turned out to hold "develop in C#" hostage
    to a question about third-party licences. The behaviour is now tested for
    real in `tests/integration/test_a_question_holds_up_only_what_it_is_about.py`;
    what stays here is the rule that keeps a finding reachable: when the model
    names nothing it was shown, EVERY requirement of the cited blocks -- both
    blocks, for a contradiction -- is linked, never none.
    """
    import uuid

    from app.services.tender_analysis import concerned_requirements

    first_block, second_block = [(uuid.uuid4(), "R-001")], [(uuid.uuid4(), "R-002")]
    linked, narrowed = concerned_requirements(first_block + second_block, [])
    assert linked == [rid for rid, _ in first_block + second_block]
    assert narrowed is False


def test_the_link_reuses_extractions_own_lookup():
    """Not a second implementation of block-to-requirement.

    Re-deriving it here is how two parts of this codebase come to disagree about
    the same fact; `check_extraction_funnel.py` reported the pre-fix number after
    the fix landed for exactly that reason.
    """
    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis._requirements_of)
    assert "from app.services.requirements import _requirements_from_block" in source
    assert "char_start" not in source, "the locator comparison belongs in one place"


def test_a_missing_second_block_is_not_an_error():
    """Most findings cite one block. `secondary` is None then, and the link must
    simply skip it rather than failing the whole analysis run."""
    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis._requirements_of)
    assert "if block is None:" in source


def test_a_requirement_cited_twice_is_linked_once():
    """Both cited blocks can share a requirement. The join table is unique on
    (clarification, requirement), so a duplicate would be a constraint error in
    the middle of an analysis rather than a tidy no-op."""
    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis._requirements_of)
    assert "seen.setdefault" in source


def test_every_open_finding_is_linked_not_only_this_run_s():
    """22 of 304.

    Links are written when a finding is raised, and a rerun re-raises only what
    the model produced that time. The other 282 open findings had been raised by
    earlier runs, so nothing ever revisited them -- and the 22 that did link
    happened to carry no customer question, while 122 open findings did. The
    rows that should have read NEEDS_CLIENT_CLARIFICATION were exactly the ones
    still detached, so the matrix reported none.

    The analysis now backfills the whole open set from the locator each issue
    already stores. No model call: the join is over data that exists.
    """
    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.analyze_ambiguity)
    assert "link_open_issues_to_their_blocks(session, ctx, bid_id=bid_id)" in source
    assert source.index("for batch in batches:") < source.index(
        "link_open_issues_to_their_blocks"
    ), "the backfill must run after the raising loop, over everything it produced"


def test_a_closed_finding_is_never_linked_back_onto_a_row():
    """History stays history. Linking a resolved question would put it back on a
    live row and gate work that is not actually waiting on anybody."""
    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.link_open_issues_to_their_blocks)
    assert "ClarificationStatus.OPEN" in source
    assert "ClarificationStatus.SENT_TO_CLIENT" in source
    assert "CLOSED" not in source and "ANSWERED" not in source


def test_the_backfill_reports_links_it_actually_created():
    """`attach_requirements` returns the number written, not the number offered.

    A count of what a function was asked to do, reported as what it did, is the
    exact shape of "cleared 26 rows" printed above a run that deleted none.
    """
    from app.services import clarifications

    source = inspect.getsource(clarifications.attach_requirements)
    assert "created += 1" in source
    assert "return created" in source
    # The increment must sit inside the branch that actually inserts, or an
    # existing link would be counted as a new one.
    assert source.index("if exists is None:") < source.index("created += 1")
