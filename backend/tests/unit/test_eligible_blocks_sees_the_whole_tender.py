"""Extraction must read the tender, not just precedence's winners.

`eligible_blocks` decides what requirement extraction is allowed to see, and it
used to be defined as "blocks that won a current clause resolution". That single
inversion capped a 39-clause tender at 14 requirements, and it did so silently:
no error, no warning, a clean run, and a coverage ratio computed over the
already-truncated set so even the self-check reported success.

Two distinct losses, both asserted below:

    no reference  -> no resolution -> never eligible. Unnumbered prose
                     obligations could not become requirements at all.

    shared number -> one resolution for the whole group, one winner, and the
                     rest dropped as losers of a contest that never happened.
                     This is where 25 of 39 clauses went.

The rule now: everything on the tender side is eligible, and a block is excluded
only when a confident current resolution says a DIFFERENT document version
supersedes it. These tests drive the real function against a fake session so
they assert the rule rather than the SQL.
"""

from __future__ import annotations

import uuid

from app.services.requirements import eligible_blocks


class _Block:
    def __init__(self, ordinal, clause_ref, heading, version_id):
        self.id = uuid.uuid4()
        self.ordinal = ordinal
        self.clause_ref = clause_ref
        self.heading_path = [heading] if heading else []
        self.document_version_id = version_id
        self.text = f"clause {clause_ref} under {heading}"


class _Resolution:
    def __init__(self, clause_ref, winning_block, confidence=0.9):
        self.id = uuid.uuid4()
        self.clause_ref = clause_ref
        self.winning_block_id = winning_block.id
        self.winning_document_version_id = winning_block.document_version_id
        self.confidence = confidence
        self.is_current = True


class _Session:
    """Returns blocks for the first query and resolutions for the second.

    `eligible_blocks` issues exactly those two, in that order.
    """

    def __init__(self, blocks, resolutions):
        self._returns = [blocks, resolutions]

    def execute(self, _statement):
        payload = self._returns.pop(0)

        class _Result:
            def scalars(self_inner):
                class _S:
                    def all(self_s):
                        return payload

                return _S()

        return _Result()


class _Ctx:
    org_id = uuid.uuid4()


BID = uuid.uuid4()


def test_two_clauses_sharing_a_number_are_both_eligible():
    """The Jadwa case, exactly. Business 3 (REIT forms) and Technical 3 (SSL)
    are different obligations; precedence used to keep one and drop the other."""
    version = uuid.uuid4()
    reit = _Block(1, "3", "Business Requirements", version)
    ssl = _Block(2, "3", "Technical Requirements", version)
    # A resolution exists for one of them under the qualified key.
    resolutions = [_Resolution("Business Requirements / 3", reit)]

    pairs = eligible_blocks(_Session([reit, ssl], resolutions), _Ctx(), bid_id=BID)

    assert {block.id for block, _ in pairs} == {reit.id, ssl.id}


def test_a_block_is_never_suppressed_by_another_block_in_the_same_document():
    """Even when a resolution names a different winner, a same-document sibling
    is not superseded -- one document's numbering restarting is not a dispute.
    This is the conjunct that makes the fix correct; drop it and the bug
    returns wearing a different query."""
    version = uuid.uuid4()
    first = _Block(1, "1", "Business Requirements", version)
    second = _Block(2, "1", "Business Requirements", version)
    resolutions = [_Resolution("Business Requirements / 1", first)]

    pairs = eligible_blocks(_Session([first, second], resolutions), _Ctx(), bid_id=BID)

    assert {block.id for block, _ in pairs} == {first.id, second.id}


def test_a_block_superseded_by_another_document_is_excluded():
    """The behaviour precedence exists for, and which must survive the fix: an
    addendum restating clause 6.1 governs, and the original's text must not
    become a second requirement."""
    original_version, addendum_version = uuid.uuid4(), uuid.uuid4()
    original = _Block(1, "6.1", "Scope", original_version)
    addendum = _Block(2, "6.1", "Scope", addendum_version)
    resolutions = [_Resolution("Scope / 6.1", addendum)]

    pairs = eligible_blocks(_Session([original, addendum], resolutions), _Ctx(), bid_id=BID)

    assert {block.id for block, _ in pairs} == {addendum.id}


def test_an_unnumbered_obligation_is_eligible():
    """No clause reference means no resolution, which used to mean invisible.
    Tenders state real obligations as prose."""
    version = uuid.uuid4()
    prose = _Block(1, None, "Submission", version)

    pairs = eligible_blocks(_Session([prose], []), _Ctx(), bid_id=BID)

    assert [block.id for block, _ in pairs] == [prose.id]
    assert pairs[0][1] is None, "an uncontested block has no resolution to record"


def test_a_low_confidence_resolution_does_not_supersede_anything():
    """Below MIN_CONFIDENCE the resolver declined to pick a winner and raised a
    clarification instead. A declined decision must not silently delete a clause
    -- the human has not answered yet."""
    v1, v2 = uuid.uuid4(), uuid.uuid4()
    a = _Block(1, "6.1", "Scope", v1)
    b = _Block(2, "6.1", "Scope", v2)
    # Filtered out by the query in the real code; modelled here as absent.
    pairs = eligible_blocks(_Session([a, b], []), _Ctx(), bid_id=BID)
    assert {block.id for block, _ in pairs} == {a.id, b.id}


def test_no_blocks_yields_no_pairs():
    assert eligible_blocks(_Session([], []), _Ctx(), bid_id=BID) == []
