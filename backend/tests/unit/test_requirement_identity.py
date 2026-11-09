"""Two obligations must not become one, and one must not become five.

`_find_existing` decides whether a block of tender text is an obligation the bid
already has or a new one. Getting that wrong is the most expensive mistake in
the extraction pipeline, because both directions of error are invisible: nothing
throws, the run reports success, and the only symptom is a requirement count
lower than the document or a requirement whose meaning changed.

Both directions were live.

    Collapse. The fallback compared `clause_ref == block.clause_ref`
    unconditionally. For a block with no reference SQLAlchemy renders that as
    `IS NULL`, which matches any requirement that also lacks one -- and most
    obligations in a tender are prose with no leading number. So every
    unnumbered obligation after the first was recorded as a new VERSION of the
    first, and disappeared. Thirty-odd obligations became fourteen requirements.

    Drift. The reference was matched bare, and this tender restarts numbering in
    every section: Business 3 (REIT subscription forms), Integration 3 (Bank
    B2B) and Technical 3 (SSL) all present as "3". R-006 passed through five
    unrelated obligations, and every task and drafted statement citing it kept
    pointing at whichever meaning arrived last.

These tests assert the decision itself rather than the count, because a count is
a symptom that many unrelated changes can move.
"""

from __future__ import annotations

from app.services.requirements import qualified_clause_ref


class _Block:
    """Enough DocumentBlock for the identity decision."""

    def __init__(self, clause_ref=None, heading_path=None):
        self.clause_ref = clause_ref
        self.heading_path = heading_path or []


def test_a_bare_number_is_qualified_by_its_section():
    """The whole drift bug in one assertion."""
    business = _Block("3", ["Business Requirements"])
    technical = _Block("3", ["Technical Requirements"])

    assert qualified_clause_ref(business) == "Business Requirements / 3"
    assert qualified_clause_ref(technical) == "Technical Requirements / 3"
    assert qualified_clause_ref(business) != qualified_clause_ref(technical)


def test_the_same_clause_in_the_same_section_keeps_one_identity():
    """Qualification must not go so far that an amendment looks like a new
    obligation. A customer who rewrites Business 3 has not moved it."""
    first = _Block("3", ["Business Requirements"])
    reworded = _Block("3", ["Business Requirements"])
    assert qualified_clause_ref(first) == qualified_clause_ref(reworded)


def test_a_nested_heading_qualifies_by_the_nearest_section():
    ref = qualified_clause_ref(_Block("3", ["Jadwa RFP", "Technical Requirements"]))
    assert ref == "Technical Requirements / 3"


def test_a_reference_with_no_section_survives_unqualified():
    """A document with no headings still has to produce something usable."""
    assert qualified_clause_ref(_Block("6.1", [])) == "6.1"


def test_an_unnumbered_block_has_no_reference_at_all():
    """The collapse bug rests on this: an absent reference must be absent, not
    a value that can be compared to another absent reference."""
    assert qualified_clause_ref(_Block(None, ["Submission"])) is None
    assert qualified_clause_ref(_Block("", ["Submission"])) is None


def test_matching_an_absent_reference_never_uses_the_clause_column():
    """The exact defect: `clause_ref == None` compiles to `IS NULL`, so a block
    with no reference matched every requirement that had none.

    Asserted against the compiled SQL rather than the source text, because the
    bug was invisible in the source -- the line read like an equality check and
    behaved like a wildcard.
    """
    from sqlalchemy import select

    from app.models import Requirement, RequirementVersion

    compiled = str(
        select(Requirement.id)
        .join(RequirementVersion, RequirementVersion.requirement_id == Requirement.id)
        .where(RequirementVersion.clause_ref == None)  # noqa: E711
        .compile()
    )
    assert "IS NULL" in compiled, (
        "If this ever stops rendering IS NULL the reasoning behind the fix "
        "changes; read _find_existing before deleting this test."
    )

    import inspect

    from app.services import requirements

    source = inspect.getsource(requirements._find_existing)
    unnumbered = source.split("if reference is None:")[1].split("by_clause =")[0]
    assert "clause_ref" not in unnumbered, (
        "The no-reference branch must match on wording alone. Comparing the "
        "clause column there is what collapsed every unnumbered obligation."
    )


def test_the_stored_reference_is_the_one_that_was_matched():
    """If the version records a bare "3" while the matcher looks for
    "Technical Requirements / 3", nothing ever matches and every re-extraction
    creates duplicates instead."""
    import inspect

    from app.services import requirements

    source = inspect.getsource(requirements._add_version)
    assert "clause_ref=qualified_clause_ref(block)" in source
