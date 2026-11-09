"""Two key spaces that silently failed to meet.

`collect_candidates` groups clauses by the QUALIFIED reference -- "4.2 Testing
scope / 4.2" -- because tenders restart numbering in every section, and a bare
"3" once gathered REIT subscription forms, a bank integration and an SSL
obligation into a single contest. A contest keeps one winner, so two real
obligations were discarded.

`DocumentRelation.clause_refs` holds BARE references, because that is what an
amendment says about itself: "this replaces clause 4.2". Nothing writing a
relation knows, or should know, which heading the block ends up under.

The resolver looked the qualified key up in the bare-keyed map. It never matched,
so EXPLICIT_SUPERSESSION -- the highest-confidence method there is, the customer
stating in writing which document governs -- never fired once, and every such
clause fell through to issue-date ordering without a word. An amendment issued
BEFORE the document it amends therefore lost to the document it replaced, which
is the ordinary case: the amendment is dated, the consolidated RFP is re-issued
later.

Six integration tests reported this the whole time. They were read as a stubbed
provider running out of canned responses and left red, which is the real lesson
here: a red test nobody reads is worse than no test, because it converts a
finding into background noise.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.precedence import _is_superseded


class _Block:
    def __init__(self, clause_ref=None, heading=None):
        self.clause_ref = clause_ref
        self.heading_path = [heading] if heading else []


class _Version:
    def __init__(self):
        self.id = uuid.uuid4()


class _Candidate:
    def __init__(self, block):
        self.block = block
        self.version = _Version()


def _scene(clause_ref="4.2", heading="4.2 Testing scope"):
    return _Candidate(_Block(clause_ref=clause_ref, heading=heading))


def test_a_relation_naming_the_bare_clause_supersedes_the_qualified_one():
    """The bug, in one assertion.

    The relation says "4.2". The candidate is keyed "4.2 Testing scope / 4.2".
    They are the same clause, and before this they were not the same string.
    """
    candidate = _scene()
    assert _is_superseded(candidate, {"4.2": {candidate.version.id}})


def test_a_relation_naming_the_qualified_clause_also_works():
    """A screen shows the qualified key, so a person correcting precedence by
    hand will quite reasonably name it. Both spellings mean the clause."""
    candidate = _scene()
    assert _is_superseded(candidate, {"4.2 Testing scope / 4.2": {candidate.version.id}})


def test_a_whole_document_supersession_still_applies():
    """`"*"` is a relation with no clause list: the document as a whole is
    replaced. It must not be lost while fixing the per-clause path."""
    candidate = _scene()
    assert _is_superseded(candidate, {"*": {candidate.version.id}})


def test_a_different_clause_does_not_supersede_this_one():
    """The half that stops the fix from over-matching. Widening "4.2" to mean
    any clause would silently retire obligations the customer never touched --
    the same damage as the original bug, pointed the other way."""
    candidate = _scene()
    assert not _is_superseded(candidate, {"6.1": {candidate.version.id}})


def test_a_different_version_is_untouched():
    """A relation supersedes a specific version FOR a clause. Another version
    addressing the same clause is a competitor, not a casualty."""
    candidate = _scene()
    assert not _is_superseded(candidate, {"4.2": {uuid.uuid4()}})


def test_a_block_with_no_reference_is_not_superseded_by_a_named_clause():
    """An unnumbered block has no bare ref and no qualified one. It must not
    match a relation that names a clause, and it must not throw."""
    candidate = _Candidate(_Block())
    assert not _is_superseded(candidate, {"4.2": {candidate.version.id}})
    assert _is_superseded(candidate, {"*": {candidate.version.id}})


@pytest.mark.parametrize("written", ["4.2", " 4.2 ", "4.2\n"])
def test_surrounding_whitespace_in_a_relation_does_not_lose_a_clause(written: str):
    """`clause_refs` is written by a detector reading amendment prose and by
    people. A trailing newline is not a different clause, and losing a
    supersession to one would be invisible."""
    from app.services.precedence import _explicit_supersession_map

    candidate = _scene()

    class _Relation:
        clause_refs = [written]

        def __init__(self, to_version_id):
            self.to_version_id = to_version_id

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return self._rows

    class _Session:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, _statement):
            return _Result(self._rows)

    class _Ctx:
        org_id = uuid.uuid4()

    mapping = _explicit_supersession_map(
        _Session([_Relation(candidate.version.id)]), _Ctx(), {uuid.uuid4()}
    )
    assert _is_superseded(candidate, mapping)
