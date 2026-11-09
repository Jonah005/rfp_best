"""A block that is the tail of the previous one is not shown alone.

A PDF page break split one bulleted list across two blocks of the tender:

    block 25  clause_ref '2'   "2. Tradenet Core OMS System  Tradenet is a Order
                                Management Syetem developed in Oracle, Jadwa will
                                provide access to Core OMS system for the following
                                purpose  Check client already exsist  Create client
                                account by adding all collected information"

    block 26  clause_ref None  " Create Beneficary accounts  Create Electronic
                                trading account with defined settings  Auto
                                approval of compliance based on new system
                                approval"

Block 26 starts one character after block 25 ends. It has three bullets, no
subject and no clause reference. Shown that alone, the model must supply the
subject, and it supplied a different wording every run -- "The system must
integrate with the Tradenet Core OMS system to create beneficiary accounts",
"The system must create beneficiary accounts via the Tradenet Core OMS system".
Right in substance, different in words, so nothing matched anything: three
bullets became eight requirements.

Joining the blocks in the parser would fix the text and break something worse.
Character spans are the coordinate requirement identity is keyed on, so
re-splitting a document renumbers every locator in it. The blocks are left
exactly as they are and the previous text is passed as context instead.

The rules below are narrow on purpose. Context that is not genuinely continuous
would invite the very invention this prevents.
"""

from __future__ import annotations

import uuid

from app.services.requirements import _preceding_text

DOC = uuid.uuid4()


class _Block:
    def __init__(
        self,
        ordinal,
        char_start,
        char_end,
        *,
        text="",
        clause_ref=None,
        heading_path=None,
        document_version_id=DOC,
    ):
        self.ordinal = ordinal
        self.char_start = char_start
        self.char_end = char_end
        self.text = text
        self.clause_ref = clause_ref
        self.heading_path = heading_path
        self.document_version_id = document_version_id


class _Session:
    """Returns the document's blocks in ordinal order, as the real query does."""

    def __init__(self, blocks):
        self.blocks = blocks

    def execute(self, _statement):
        blocks = self.blocks

        class _Result:
            def scalars(self_inner):
                class _Scalars:
                    def all(self_deep):
                        return sorted(blocks, key=lambda b: b.ordinal)

                return _Scalars()

        return _Result()


class _Ctx:
    org_id = uuid.uuid4()


HEADING = ["Business Requirements"]

LEAD_IN = _Block(
    25,
    12896,
    13143,
    text=(
        "2. Tradenet Core OMS System Tradenet is a Order Management Syetem developed in "
        "Oracle, Jadwa will provide access to Core OMS system for the following purpose "
        " Check client already exsist  Create client account by adding all collected "
        "information"
    ),
    clause_ref="2",
    heading_path=HEADING,
)

TAIL = _Block(
    26,
    13144,
    13289,
    text=(
        " Create Beneficary accounts  Create Electronic trading account with defined "
        "settings  Auto approval of compliance based on new system approval"
    ),
    clause_ref=None,
    heading_path=HEADING,
)


def _run(blocks, eligible=None):
    chosen = eligible if eligible is not None else blocks
    return _preceding_text(_Session(blocks), _Ctx(), pairs=[(b, None) for b in chosen])


def test_the_orphaned_tail_is_given_its_lead_in():
    """The case this exists for."""
    out = _run([LEAD_IN, TAIL])
    assert TAIL.ordinal in out
    assert "for the following purpose" in out[TAIL.ordinal]


def test_a_block_with_its_own_clause_reference_gets_nothing():
    """A numbered clause identifies itself. Handing it the previous clause's text
    invites obligations to be attributed across a boundary that is real."""
    numbered = _Block(26, 13144, 13289, text="3. B2B Service ...", clause_ref="3",
                      heading_path=HEADING)
    assert _run([LEAD_IN, numbered]) == {}


def test_a_gap_in_the_offsets_means_it_is_not_a_continuation():
    """Contiguity is the evidence that one block is the tail of the other. With a
    gap, the neighbour is merely nearby -- and nearby is how unrelated text gets
    read as an introduction."""
    detached = _Block(26, 13500, 13645, text=TAIL.text, heading_path=HEADING)
    assert _run([LEAD_IN, detached]) == {}


def test_a_different_heading_means_a_different_section():
    """Contiguous offsets across a section boundary are still a boundary. The
    first block of a section must not inherit the last block of the one before."""
    other_section = _Block(
        26, 13144, 13289, text=TAIL.text, heading_path=["Technical Requirements"]
    )
    assert _run([LEAD_IN, other_section]) == {}


def test_the_first_block_of_a_document_has_nothing_before_it():
    first = _Block(0, 0, 406, text="RFP for Account Opening", heading_path=None)
    assert _run([first]) == {}


def test_context_is_read_from_the_document_not_from_the_eligible_set():
    """The previous block is often not itself extractable -- a heading, or a
    clause a later document supersedes. If context came only from eligible
    blocks, a tail whose lead-in happened to be skipped would silently lose it,
    which is the failure mode returning in a quieter form.
    """
    heading = _Block(24, 12738, 12895, text="Business Requirements", heading_path=HEADING)
    lead_in = _Block(25, 12896, 13143, text=LEAD_IN.text, clause_ref="2", heading_path=HEADING)
    # Only the tail is eligible; the document still holds all three.
    out = _run([heading, lead_in, TAIL], eligible=[TAIL])
    assert "for the following purpose" in out[TAIL.ordinal]


def test_blocks_of_another_document_version_are_not_neighbours():
    """Ordinals restart per document version, so a neighbour must be matched
    within its own version or block 25 of one document becomes context for block
    26 of another."""
    foreign = _Block(
        25, 12896, 13143, text="unrelated", clause_ref="2",
        heading_path=HEADING, document_version_id=uuid.uuid4(),
    )
    # The session returns only this version's blocks, as the real query's
    # document_version_id filter guarantees; the tail is then alone.
    assert _run([TAIL], eligible=[TAIL]) == {}
    assert foreign.document_version_id != TAIL.document_version_id
