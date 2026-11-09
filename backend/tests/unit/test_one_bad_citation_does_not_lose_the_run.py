"""A finding that cites a block it was not shown is dropped, not fatal.

Every ambiguity finding is anchored to a block the call actually read: that is
what makes it checkable, and nothing in the service will invent an anchor for
one. The enforcement, though, used to be `raise`. On the live bid the model
returned one issue citing ordinal 3 from a batch that did not contain it, and
the whole analysis died with

    ambiguity_analysis cited block ordinals outside its batch: 3

on the screen -- every finding in every batch thrown away, the document read
and paid for, and nothing to show. The rule was right and the penalty was
wrong: one bad citation out of a hundred is the model mis-numbering, not a
reason to lose the other ninety-nine.

Three outcomes, by which side is out of view:

    source out of view      the finding has no anchor at all -- dropped
    conflict out of view    one side was read: keep the finding, drop the
                            locator it cannot support, and do not let it block
    both in view            untouched
"""

from __future__ import annotations

import inspect

from app.models.enums import ClarificationSeverity
from app.services import tender_analysis


def _loop() -> str:
    return inspect.getsource(tender_analysis.analyze_ambiguity)


def test_the_run_no_longer_raises_on_a_citation_outside_the_batch():
    source = _loop()
    assert "cited block ordinals outside its batch" not in source, (
        "one mis-numbered citation must not end the run"
    )


def test_a_finding_with_no_anchor_in_view_is_dropped():
    source = _loop()
    assert "if issue.source_block_ordinal not in allowed:" in source
    dropped = source.index("if issue.source_block_ordinal not in allowed:")
    assert "continue" in source[dropped : dropped + 500]
    # Dropped BEFORE the fingerprint, so an identical finding raised by an
    # earlier run is retired rather than left open with an anchor nobody read.
    assert dropped < source.index("seen_fingerprints.add(")


def test_a_half_read_contradiction_keeps_what_it_saw_and_loses_what_it_did_not():
    source = _loop()
    assert "issue.conflicting_block_ordinal = None" in source
    assert "not in view when the finding was made" in source


def test_a_half_read_contradiction_cannot_block_generation():
    """The contradiction types keep their severity because the schema
    guarantees both blocks were read. With one side unread that guarantee is
    gone, and BLOCKING stops generation -- so it is capped for the same reason
    an absence claim from a window is."""
    source = _loop()
    # The LAST `if out_of_view:` is the severity decision; the first is where
    # the unsupported locator is dropped.
    capped = source.rindex("if out_of_view:")
    window = source[capped : capped + 900]
    assert "ClarificationSeverity.MAJOR" in window
    assert "blocks_generation=severity is ClarificationSeverity.BLOCKING" in source
    assert ClarificationSeverity.MAJOR is not ClarificationSeverity.BLOCKING


def test_how_often_it_happened_is_reported():
    """Silent tolerance is how a model that has started mis-numbering every
    batch goes unnoticed. The count is logged with every run."""
    source = _loop()
    assert "citations_outside_the_batch=miscited" in source
    assert "ambiguity_issue_miscited" in source
