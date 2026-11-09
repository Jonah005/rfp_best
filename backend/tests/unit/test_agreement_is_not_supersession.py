"""A newer prompt confirming a requirement must not look like it deleting one.

The retire sweep decides what an older prompt version left behind by asking
which rows carry the current version's stamp. Its first run against v6 proposed
withdrawing:

    R-008  "By availing B2B service the reconciliation of amount transferred
            should be in sync with bank."
    R-030  "The solution should support deployment on windows 2012 and new
            servers."
    R-037  "Each link click should have a log entry in sql server database..."
    R-002  "Bidders intent on submitting a proposal should so notify the Jadwa
            contact person with signed Jadwa NDA."

Four obligations, each quoting its block exactly, each reported as something v6
had read and declined to raise. v6 had done nothing of the kind. It re-proposed
every one of them; reconciliation matched each to the row already there; the
wording was byte-identical, so `_persist_requirement` took its "same wording, no
new version" branch and wrote nothing -- leaving `extracted_by` reading v5.

Agreement between two prompt versions produced the same evidence as absence.
That is the failure this codebase keeps meeting from new angles: a stamp that
looks authoritative sitting next to the thing that actually decides.

Two defences, because either alone can be defeated:

  1. confirming a requirement updates its stamp, so agreement is recorded;
  2. the sweep cross-checks against `BlockExtraction.found`, so a block where
     proposals were made but no row carries the stamp is skipped rather than
     swept -- which holds even for rows written before defence 1 existed.
"""

from __future__ import annotations

import inspect


def test_confirming_a_requirement_updates_its_stamp():
    """The same-wording branch must record who vouched for the text.

    Checked in the source rather than through a fake, because the branch is
    reached only after a chain of matching this test would otherwise have to
    reconstruct -- and reconstructing the subject is how a test comes to pass
    against code that does not work.
    """
    from app.services import requirements

    source = inspect.getsource(requirements._persist_requirement)
    branch = source.index("same_wording(current.requirement_text, proposal.requirement_text)")
    tail = source[branch:]
    refresh = tail.index("_refresh_classification")
    assert "current.extracted_by = extracted_by" in tail[refresh:], (
        "a requirement re-confirmed by a newer prompt keeps the old stamp, so "
        "agreement is indistinguishable from the newer prompt finding nothing"
    )


def test_the_stamp_update_happens_before_the_early_return():
    """Order matters: the branch returns False immediately afterwards, and an
    update written below that return would never execute."""
    from app.services import requirements

    source = inspect.getsource(requirements._persist_requirement)
    branch = source.index("same_wording(current.requirement_text, proposal.requirement_text)")
    tail = source[branch:]
    assert tail.index("current.extracted_by = extracted_by") < tail.index("return False")


def _verdict(**kw):
    import importlib.util
    import sys
    from pathlib import Path

    scripts = str(Path("scripts").resolve())
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location(
        "_retire_sweep", "scripts/retire_superseded_extractions.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.sweep_verdict(**kw)


def test_agreement_is_reported_and_skipped_not_retired():
    """Defence two, for rows that predate defence one.

    The current extractor read this block and produced two proposals, and
    NEITHER became a row carrying its stamp -- so both were absorbed by rows
    that already said the same thing. Those rows are confirmed by the new pass,
    not superseded by it. Retiring them would delete requirements for being
    correct, which is the failure this interlock exists to stop.
    """
    assert (
        _verdict(
            read_by_current=True,
            stamps={"v8", "v9"},
            current="v9",
            produced=2,
            current_rows_here=0,
        )
        == "SKIP"
    )


def test_a_block_the_current_extractor_never_read_is_left_alone():
    """No record and no current row means it was not read, so nothing here is
    superseded by a pass that never saw it."""
    assert (
        _verdict(
            read_by_current=False, stamps={"v8"}, current="v9", produced=0, current_rows_here=0
        )
        == "UNREAD"
    )


def test_a_block_with_only_current_stamps_has_nothing_older_to_retire():
    assert (
        _verdict(read_by_current=True, stamps={"v9"}, current="v9", produced=1, current_rows_here=1)
        == "UNREAD"
    )


def test_a_genuinely_superseded_block_is_still_retired():
    """The half that stops the interlock swallowing the script's whole purpose.

    Older stamps remain AND the current pass produced rows of its own here, so
    the old rows really were replaced rather than confirmed. A verdict function
    that answered SKIP to everything would pass every test above and make the
    sweep a no-op.
    """
    assert (
        _verdict(
            read_by_current=True,
            stamps={"v8", "v9"},
            current="v9",
            produced=2,
            current_rows_here=2,
        )
        == "RETIRE"
    )


def test_the_decision_is_a_function_rather_than_a_source_layout():
    """Why this file stopped reading the script's text.

    The previous version asserted that the string "produced and not
    current_rows_here" appeared at a LOWER CHARACTER OFFSET in
    `retire_superseded_extractions.py` than an assignment to
    `lifecycle_state`. It protected a rule by pinning a file's layout: moving
    the withdrawal into `requirements.withdraw` -- which changed nothing about
    the decision -- broke it immediately, while a rearrangement that broke the
    RULE and left both strings in place would have passed.

    Three tests in this project have now been converted from grepping source to
    asserting behaviour, and one was written that way and deleted on the same
    day. The rule is: if a test reads code as text, it is testing the
    implementation's spelling.
    """
    import importlib.util
    import inspect

    spec = importlib.util.spec_from_file_location(
        "_retire_sweep2", "scripts/retire_superseded_extractions.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.sweep_verdict)
    # Every branch is reachable and named, so a fourth outcome cannot be added
    # silently.
    assert set(inspect.signature(module.sweep_verdict).parameters) == {
        "read_by_current",
        "stamps",
        "current",
        "produced",
        "current_rows_here",
    }
