"""One definition of "the same clause", used everywhere that decides it.

This defect has now been found three times in three places, and each time the
symptom was different and the cause identical: something compared bare
`DocumentBlock.clause_ref` values and treated unrelated obligations as one.

    requirements._find_existing   two prose obligations with no reference
                                  matched each other, because `clause_ref ==
                                  None` compiles to IS NULL.

    requirements._add_version     a bare "3" stored as the identity, so R-006
                                  passed through five unrelated obligations.

    precedence.collect_candidates the one that actually capped the tender at 14
                                  requirements. Resolutions are created per
                                  distinct key; keying on the bare reference
                                  collapsed 39 numbered blocks into 14
                                  contests, precedence kept one winner each,
                                  and extraction -- which only ever read
                                  winners -- never saw the other 25.

Fixing the first two and leaving the third is what cost a day. So the
definition now lives in `clause_identity`, and this file guards it: the
behavioural tests say what a reference means, and the last test fails if either
consumer starts keying on a bare reference again.
"""

from __future__ import annotations

import inspect

from app.services.clause_identity import (
    MAX_CLAUSE_REF,
    normalized_heading,
    qualified_clause_ref,
)


class _Block:
    def __init__(self, clause_ref=None, heading_path=None, id=None, document_version_id=None):
        self.clause_ref = clause_ref
        self.heading_path = heading_path or []
        self.id = id
        self.document_version_id = document_version_id


def test_the_collision_that_cost_the_tender():
    """Three real clauses from the Jadwa RFP that shared the reference "3"."""
    reit = _Block("3", ["Business Requirements"])
    ssl = _Block("3", ["Technical Requirements"])
    assert qualified_clause_ref(reit) != qualified_clause_ref(ssl)
    assert qualified_clause_ref(reit) == "Business Requirements / 3"


def test_the_same_clause_amended_keeps_one_identity():
    """Qualification must not go so far that an amendment looks like a new
    obligation: a customer who rewrites Business 3 has not moved it."""
    assert qualified_clause_ref(_Block("3", ["Business Requirements"])) == qualified_clause_ref(
        _Block("3", ["Business Requirements"])
    )


def test_an_absent_reference_is_absent():
    """Not an empty string, not the heading alone. Callers must branch on None,
    because comparing it in SQL matches every other row that also lacks one."""
    assert qualified_clause_ref(_Block(None, ["Submission"])) is None
    assert qualified_clause_ref(_Block("", ["Submission"])) is None
    assert qualified_clause_ref(_Block("   ", ["Submission"])) is None


def test_a_reference_with_no_section_survives():
    assert qualified_clause_ref(_Block("6.1", [])) == "6.1"
    assert qualified_clause_ref(_Block("6.1", ["", "  "])) == "6.1"


def test_page_furniture_does_not_become_part_of_the_identity():
    """A real heading from the ADX RFP arrives with the page's running header
    folded into it. Left in, the identity depends on pagination -- re-export the
    PDF, the clause lands on page 20, and it becomes a different clause."""
    polluted = "8.11 Appendix I Technical Proposal Format Page 19 of 32|<RFP Template.docx> <date>"
    clean = "8.11 Appendix I Technical Proposal Format"
    assert normalized_heading([polluted]) == clean
    assert qualified_clause_ref(_Block("4", [polluted])) == f"{clean} / 4"
    # And the same clause on a different page is still the same clause.
    other_page = polluted.replace("Page 19 of 32", "Page 20 of 32")
    assert qualified_clause_ref(_Block("4", [other_page])) == f"{clean} / 4"


def test_a_long_reference_is_bounded_without_merging_two_clauses():
    """The column is varchar(200). Plain truncation would re-create the exact
    collision this module prevents: two long headings sharing a prefix would
    truncate to the same key."""
    shared = "A" * 300
    one = qualified_clause_ref(_Block("1", [shared + "-alpha"]))
    two = qualified_clause_ref(_Block("1", [shared + "-beta"]))
    assert len(one) <= MAX_CLAUSE_REF
    assert len(two) <= MAX_CLAUSE_REF
    assert one != two, "truncation must not merge two distinct clauses"


def test_the_nearest_section_qualifies_not_the_document_title():
    ref = qualified_clause_ref(_Block("3", ["Jadwa RFP", "Technical Requirements"]))
    assert ref == "Technical Requirements / 3"


def test_neither_consumer_keys_on_a_bare_reference():
    """The guard.

    `precedence.collect_candidates` builds the dict whose keys become clause
    resolutions, and `requirements._add_version` writes the identity that
    matching later reads. If either goes back to `block.clause_ref`, clauses
    start disappearing again with no error anywhere -- so this asserts on the
    source of those two functions specifically.
    """
    from app.services import precedence, requirements

    collect = inspect.getsource(precedence.collect_candidates)
    body = collect.split('candidates: dict[str, list[ClauseCandidate]] = {}')[1]
    assert "qualified_clause_ref(block)" in body
    assert "setdefault(block.clause_ref" not in body, (
        "collect_candidates is keying on the bare reference again. That is what "
        "collapsed 39 clauses into 14 resolutions."
    )

    add_version = inspect.getsource(requirements._add_version)
    assert "clause_ref=qualified_clause_ref(block)" in add_version

    # And both must be the SAME function, not two copies that can drift.
    assert requirements.qualified_clause_ref is qualified_clause_ref
    assert precedence.qualified_clause_ref is qualified_clause_ref
