"""Re-analysing an unchanged document must not mint new requirements.

Observed live: a bid went from 38 requirement rows to 47 across two runs of the
same analysis on the same document, and not one of the nine was a new obligation.
The model had simply re-worded what it already produced --

    "using existing components such as"  ->  "reusing existing components including"
    "the RFP requirements"               ->  "the RFP content"
    "Jadwa Investment Company"           ->  "Jadwa Investment Company management"

-- and identity for an unnumbered block was the wording, so nothing matched.
Each duplicate then attracted its own evidence links and its own assessment,
inflating every count in the matrix and making the coverage picture read better
than the document supported.

The fix is to stop asking the model where a requirement came from. A block is a
stable coordinate -- same document version, same character span -- and the model
cannot perturb it. Wording is then used only to separate the several obligations
one block can legitimately decompose into.

These tests drive `_find_existing` against a fake session, so they assert the
identity decision rather than the SQL that fetches candidates.
"""

from __future__ import annotations

import uuid

from app.services.requirements import (
    _SAME_OBLIGATION,
    _comparable,
    _find_existing,
    _similarity,
)

# Verbatim from the live bid. One block (document version d1b106ba,
# characters 8216-9413) produced all six of these across three analysis runs:
# two obligations, each re-worded twice. Hand-written paraphrases missed the
# defect that mattered, because they were short enough not to trip difflib's
# autojunk heuristic.
DESIGN = {
    "R-016": (
        "The vendor must follow a design approach similar to the current design of "
        "www.jadwa.com and create a new design for this system, potentially using "
        "existing components such as page design, header, fonts, colors, menu, main "
        "parent page layout, and disclaimer."
    ),
    "R-046": (
        "The vendor must follow a design approach similar to the current design of "
        "www.jadwa.com and create a new design for this system, potentially reusing "
        "existing components including page design, header, fonts (English/Arabic), "
        "color, menu, main parent page layout, and disclaimer."
    ),
    "R-048": (
        "The bidder must follow the design approach of the current www.jadwa.com "
        "website and create a new design for the portal, potentially reusing existing "
        "components such as page design, header, fonts, colors, menu, layout, and "
        "disclaimer."
    ),
}

SCREENS = {
    "R-017": (
        "The vendor must develop screens including user login, email/SMS templates, "
        "SMS verification, information gathering, final agreement generation, document "
        "upload, dependents information, back office tasks list, approval screens, "
        "audit report, dashboard, and reports to implement a successful portal with "
        "high UI/UX."
    ),
    "R-047": (
        "The vendor must develop screens including user login, email/SMS templates, "
        "SMS verification, information gathering with stages, final agreement "
        "generation, document upload, add dependents/quota, back office task list, "
        "approval screens, audit report for single request, dashboard for statistics, "
        "and reports."
    ),
    "R-049": (
        "The bidder must develop screens including user login, email/SMS templates, "
        "SMS verification, information gathering, final agreement generation, document "
        "upload, dependent information, back-office task lists, approval screens, "
        "audit reports, dashboards, and reports."
    ),
}


# Three obligations the RFP states in one block, sharing a long boilerplate
# stem and differing only at the end. Unweighted similarity scored these 0.796
# -- higher than a genuine restatement elsewhere at 0.720 -- so a cleanup built
# on that metric proposed withdrawing two of them as duplicates of the first.
TRADENET = {
    "R-024": (
        "The system must integrate with Tradenet Core OMS System to create beneficiary accounts."
    ),
    "R-025": (
        "The system must integrate with Tradenet Core OMS System to create "
        "electronic trading accounts with defined settings."
    ),
    "R-044": (
        "The system must integrate with Tradenet Core OMS System for auto approval "
        "of compliance based on new system approval."
    ),
}


class _Block:
    def __init__(self, char_start, char_end, version_id, clause_ref=None, heading=None):
        self.id = uuid.uuid4()
        self.clause_ref = clause_ref
        self.heading_path = [heading] if heading else []
        self.document_version_id = version_id
        self.char_start = char_start
        self.char_end = char_end


class _Requirement:
    def __init__(self, code):
        self.id = uuid.uuid4()
        self.code = code


class _Session:
    """Returns the rows `_requirements_from_block` reads.

    Five fields now, not three. The query grew `source_document_version_id` and
    `clause_ref` when candidate scoping stopped being "this exact block in this
    exact document version" -- under which an amendment could never find the
    requirement it amends, and created a second live one instead.
    """

    def __init__(self, rows):
        self.rows = rows

    def execute(self, _statement):
        rows = self.rows

        class _Result:
            def all(self_inner):
                return rows

            def scalar_one_or_none(self_inner):
                return None

        return _Result()


class _Ctx:
    org_id = uuid.uuid4()


BID = uuid.uuid4()
VERSION = uuid.uuid4()


def locator(block):
    return {"char_start": block.char_start, "char_end": block.char_end}


def row(requirement, text, block):
    """One candidate row as the real query returns it, for a rerun on the same
    document: same version, same clause, same span."""
    from app.services.clause_identity import qualified_clause_ref

    return (
        requirement,
        text,
        locator(block),
        block.document_version_id,
        qualified_clause_ref(block),
    )


def test_a_reworded_requirement_from_the_same_block_is_the_same_requirement():
    """The exact drift seen live."""
    block = _Block(1200, 1480, VERSION)
    existing = _Requirement("R-016")
    first = (
        "The vendor must follow a design approach similar to the current design of "
        "www.jadwa.com and create a new design for this system, potentially using "
        "existing components such as page design and headers."
    )
    reworded = (
        "The vendor must follow a design approach similar to the current design of "
        "www.jadwa.com and create a new design for this system, potentially reusing "
        "existing components including page design and headers."
    )
    session = _Session([row(existing, first, block)])

    # `normalized` and `match_text` carry the same string in these fixtures on
    # purpose: the drift being modelled is in the CLAUSE, which is the text
    # identity is now judged on. `match_text` is required, so a test cannot
    # silently fall back to scoring the restatement.
    found = _find_existing(
        session, _Ctx(), bid_id=BID, block=block, normalized=reworded, match_text=reworded
    )
    assert found is existing, "a re-worded restatement created a duplicate requirement"


def test_two_different_obligations_from_one_block_stay_separate():
    """One paragraph often states several obligations. Matching by block alone
    would collapse them, which is the opposite failure and just as silent."""
    block = _Block(2000, 2600, VERSION)
    existing = _Requirement("R-024")
    session = _Session(
        [
            row(
                existing,
                "The system must integrate with Tradenet Core OMS System to create "
                "beneficiary accounts.",
                block,
            )
        ]
    )

    unrelated = (
        "The vendor must configure IIS application pool recycling, compression and "
        "output caching on the production servers."
    )
    assert (
        _find_existing(
            session,
            _Ctx(),
            bid_id=BID,
            block=block,
            normalized=unrelated,
            match_text=unrelated,
        )
        is None
    )


def test_an_obligation_from_a_different_block_is_a_different_requirement():
    """Same words, different place in the document. The candidate query is
    scoped by character span, so nothing is offered to compare against."""
    block = _Block(5000, 5200, VERSION)
    session = _Session([])  # no requirement carries this block's locator
    assert (
        _find_existing(
            session,
            _Ctx(),
            bid_id=BID,
            block=block,
            normalized="The bidder must treat the RFP as confidential.",
            match_text="The bidder must treat the RFP as confidential.",
        )
        is None
    )


def test_identity_ignores_punctuation_and_case_but_not_meaning():
    assert _comparable("The RFP's requirements -- confidential!") == _comparable(
        "the rfp s requirements confidential"
    )
    assert _comparable("Working hours 9am-4pm") != _comparable("Vacation and launch dates")


def test_a_block_with_a_clause_reference_does_not_take_this_path():
    """Numbered clauses keep their reference-based identity, which survives a
    re-wording on its own. This test exists so a future change to the
    no-reference branch cannot quietly capture the numbered one."""
    block = _Block(10, 90, VERSION, clause_ref="3", heading="Business Requirements")
    session = _Session([])
    # Reaches the clause-ref branch and finds nothing, rather than consulting
    # the block-locator candidates.
    assert (
        _find_existing(
            session,
            _Ctx(),
            bid_id=BID,
            block=block,
            normalized="anything",
            match_text="anything",
        )
        is None
    )


def test_no_two_different_obligations_ever_score_high_enough_to_merge():
    """The rule that must never break.

    A duplicate row is visible and a person can merge it. An obligation folded
    into another is gone, and nothing downstream can tell that the bid stopped
    covering it. So the only hard requirement on this metric is that genuinely
    different obligations never reach the threshold.

    TRADENET is the hardest case in the corpus: one shared stem, different
    tails. Those scored 0.796 -- the ceiling across all 12 labelled
    different-obligation pairs.
    """
    codes = list(TRADENET)
    for i, a in enumerate(codes):
        for b in codes[i + 1 :]:
            score = _similarity(TRADENET[a], TRADENET[b])
            assert score < _SAME_OBLIGATION, (
                f"{a} and {b} scored {score:.3f}; merging them deletes a real obligation"
            )
    for a in DESIGN.values():
        for b in SCREENS.values():
            assert _similarity(a, b) < _SAME_OBLIGATION


def test_the_clearest_restatements_do_merge():
    """The metric still has to earn its place. These are the pairs a rerun
    produces most often -- the same sentence with a word or two moved.

    The screen-list pair at 0.888 used to be in this list and has moved to the
    test below. The bar rose to 0.90 because two DISTINCT obligations reached
    0.848 -- the SMS clause, once it was decomposed properly -- and 0.85 left
    two thousandths between a real obligation and being discarded. Losing the
    0.888 echo to a visible duplicate is the price of that margin.
    """
    assert _similarity(DESIGN["R-016"], DESIGN["R-046"]) >= _SAME_OBLIGATION
    assert _similarity(TRADENET["R-024"], TRADENET["R-024"]) >= _SAME_OBLIGATION


def test_the_screen_list_restatement_is_now_kept_not_merged():
    """0.888: a real echo that the raised bar no longer collapses.

    Recorded as a deliberate cost rather than left to be rediscovered. If the
    threshold is ever lowered again, this is the pair that starts merging first
    -- and the SMS obligations at 0.848 are what it would approach.
    """
    score = _similarity(SCREENS["R-017"], SCREENS["R-049"])
    assert 0.848 < score < _SAME_OBLIGATION


def test_a_restatement_below_the_threshold_is_kept_not_merged():
    """R-016 and R-048 are the same obligation and score 0.848 -- under the bar.

    That is the trade, written down rather than discovered: this pair stays as
    two rows for a person to merge. Accepting eight visible duplicates is the
    price of never silently deleting an obligation, and if the threshold is ever
    lowered to catch them, this test is where the reasoning lives.
    """
    score = _similarity(DESIGN["R-016"], DESIGN["R-048"])
    assert 0.79 < score < _SAME_OBLIGATION


def test_the_similarity_metric_survives_long_prose():
    """difflib's autojunk drops any element in over 1% of a sequence longer
    than 200 characters. Every requirement text is longer than that, so with it
    on the matcher was degraded exactly where it was needed: R-016 against
    R-048 scored 0.089. This fails if anyone builds a SequenceMatcher here
    without autojunk=False."""
    from difflib import SequenceMatcher

    a, b = _comparable(DESIGN["R-016"]), _comparable(DESIGN["R-048"])
    assert len(a) > 200, "the fixture must be long enough to trigger autojunk"
    assert SequenceMatcher(None, a, b).ratio() < 0.2
    assert _similarity(DESIGN["R-016"], DESIGN["R-048"]) > 0.8


# --- rerun reconciliation ----------------------------------------------------


class _Proposal:
    def __init__(self, ordinal, text):
        self.source_block_ordinal = ordinal
        self.requirement_text = text
        self.normalized_requirement = text


def _reconcile(candidates, proposals, block):
    from app.services.requirements import _reconcile_against_existing

    rows = [row(requirement, text, block) for requirement, text in candidates]
    return _reconcile_against_existing(
        _Session(rows),
        _Ctx(),
        bid_id=BID,
        proposals=proposals,
        block_by_ordinal={block.char_start: (block, None)},
    )


def test_a_block_that_yields_no_more_than_before_creates_nothing_new():
    """The count carries information the score does not.

    R-051 and R-052 appeared on a rerun immediately after a cleanup removed ten
    duplicates: restatements scoring 0.81, deliberately under the 0.85 bar, so
    each became a second row. If a block held two requirements and proposes two,
    it has not gained an obligation whatever the wording.
    """
    block = _Block(8216, 9413, VERSION)
    existing = [(_Requirement("R-015"), DESIGN["R-016"]), (_Requirement("R-017"), SCREENS["R-017"])]
    proposals = [_Proposal(8216, DESIGN["R-048"]), _Proposal(8216, SCREENS["R-049"])]

    assigned = _reconcile(existing, proposals, block)

    matched = [assigned[id(p)] for p in proposals]
    assert all(m is not None for m in matched), "a rerun created new rows for old obligations"
    # And matched to the RIGHT one: design to design, screens to screens.
    assert matched[0].code == "R-015"
    assert matched[1].code == "R-017"


def test_a_block_that_genuinely_gains_an_obligation_still_creates_one():
    """Saturation is the whole safeguard. Three proposals against two existing
    requirements means one is new, and it must not displace either."""
    block = _Block(8216, 9413, VERSION)
    existing = [(_Requirement("R-015"), DESIGN["R-016"]), (_Requirement("R-017"), SCREENS["R-017"])]
    proposals = [
        _Proposal(8216, DESIGN["R-048"]),
        _Proposal(8216, SCREENS["R-049"]),
        _Proposal(8216, "The vendor must provide 24x7 support with a four-hour response SLA."),
    ]

    assigned = _reconcile(existing, proposals, block)

    assert sum(1 for p in proposals if assigned[id(p)] is None) == 1
    assert assigned[id(proposals[2])] is None, "the unrelated obligation must be the new one"


def test_an_unrelated_proposal_is_never_forced_onto_an_existing_requirement():
    """Saturated does not mean "match anything". Below the positional floor the
    two are not even loosely related, and versioning one onto the other would
    silently change what an existing code means."""
    block = _Block(8216, 9413, VERSION)
    existing = [(_Requirement("R-015"), DESIGN["R-016"])]
    proposals = [
        _Proposal(8216, "Payment shall be made in three milestones of 20, 70 and 10 percent.")
    ]

    assigned = _reconcile(existing, proposals, block)

    assert assigned[id(proposals[0])] is None


def test_a_numbered_clause_is_reconciled_like_any_other():
    """Reconciliation used to skip numbered clauses, and that exemption is what
    made two thirds of this tender unstable.

    The reasoning was that a clause reference is a stable identity of its own.
    It is -- for a CLAUSE. It is not one for an obligation, and a numbered
    clause here routinely states several. Every proposal from "Business
    Requirements / 4" fell through to `_find_existing`, matched the one
    requirement carrying that reference and wrote a new version of it, so the
    clause's five SMS bullets became one requirement holding whichever bullet
    came last. R-020 reached version 95 rotating between them; R-018 alternated
    between "Verify Information" and "Get applicant related more information".

    Worse than the rotation: the other four bullets never became requirements at
    all. The SMS functions, the SQL Server database, the IIS configuration, the
    XSS controls and the load test were all absent from the matrix for this
    reason -- each collided with a sibling bullet on the same clause number.

    So the reference scopes the candidates and no longer decides identity.
    """
    from app.services.requirements import _UNASSIGNED

    block = _Block(10, 90, VERSION, clause_ref="3", heading="Business Requirements")
    proposals = [_Proposal(10, "anything at all")]
    assigned = _reconcile([(_Requirement("R-001"), "x")], proposals, block)
    assert assigned.get(id(proposals[0]), _UNASSIGNED) is not _UNASSIGNED, (
        "a numbered block must be reconciled, not handed to the reference matcher"
    )


def test_a_numbered_clause_with_several_bullets_keeps_them_all():
    """The recovery this buys, stated as the case that was failing.

    Five proposals from one numbered clause, one existing requirement. The cap
    lets that requirement claim ONE of them; the other four are new. Under the
    old rule all five matched the same row and four obligations vanished.
    """
    block = _Block(10, 900, VERSION, clause_ref="4", heading="Business Requirements")
    existing = [(_Requirement("R-020"), "To send validation SMS")]
    bullets = [
        "To send validation SMS",
        "To send other annoucement SMS to applicants",
        "Develop engine to prompty send SMS",
        "Ensure Arabic SMS is supported",
        "Easy to re-configure the SMS service credentials if changed",
    ]
    proposals = [_Proposal(10, text) for text in bullets]

    assigned = _reconcile(existing, proposals, block)

    claimed = [p for p in proposals if assigned.get(id(p)) is not None]
    assert len(claimed) == 1, "one existing requirement can absorb exactly one bullet"
    assert assigned[id(claimed[0])].code == "R-020"
    # The bullet it keeps is the one it already stated, so the code does not
    # change meaning between runs.
    assert claimed[0].requirement_text == "To send validation SMS"
    # And nothing else is silently folded into it.
    assert sum(1 for p in proposals if assigned.get(id(p)) is None) == 4


# --- the model repeating itself inside one answer ----------------------------


def test_one_answer_stating_the_same_obligation_twice_yields_one_requirement():
    """The failure that put five requirements on a two-obligation paragraph.

    Reconciliation compared each proposal against requirements already stored
    and never against the other proposals in the same reply. So a model that
    said the design obligation twice produced two rows, and once the existing
    requirements were claimed the surplus became R-053 and R-054.

    Measured on that block: the repeats scored 0.884 and 0.958 against their
    twins, while the three genuinely different Tradenet obligations from another
    block scored 0.804 and 0.745 and must survive.
    """
    from app.services.requirements import _ECHO_OF

    block = _Block(8216, 9413, VERSION)
    proposals = [
        _Proposal(8216, DESIGN["R-016"]),
        _Proposal(8216, DESIGN["R-046"]),  # 0.938 against the first — an echo
        _Proposal(8216, SCREENS["R-017"]),
    ]
    assigned = _reconcile([], proposals, block)

    assert assigned[id(proposals[1])] is _ECHO_OF, "a restatement became a second requirement"
    assert assigned[id(proposals[0])] is not _ECHO_OF
    assert assigned[id(proposals[2])] is not _ECHO_OF, "the screen list is a separate obligation"


def test_two_obligations_sharing_a_stem_are_not_treated_as_echoes():
    """The safety direction. Dropping a proposal loses an obligation outright,
    so the bar is the high standalone one — the Tradenet trio at 0.804 and
    0.745 stays three requirements."""
    from app.services.requirements import _ECHO_OF

    block = _Block(13144, 13289, VERSION)
    proposals = [_Proposal(13144, TRADENET[code]) for code in ("R-024", "R-025", "R-044")]
    assigned = _reconcile([], proposals, block)

    assert all(assigned[id(p)] is not _ECHO_OF for p in proposals)


def test_an_echo_is_skipped_rather_than_persisted():
    """The decision has to reach the write path. Marking a proposal an echo and
    then persisting it anyway would be the same defect this codebase keeps
    producing: a value that decides nothing."""
    import inspect

    from app.services import requirements

    source = inspect.getsource(requirements.extract_requirements)
    assert "is _ECHO_OF" in source
    assert "continue" in source.split("is _ECHO_OF")[1][:300]
    assert "echoes_dropped" in source, "a dropped proposal must be counted, not vanish"


# --- an amendment is a new version, not a second requirement ----------------


def _candidates(rows, block):
    from app.services.requirements import _requirements_from_block

    return _requirements_from_block(_Session(rows), _Ctx(), bid_id=BID, block=block)


def test_an_amendment_finds_the_requirement_it_amends():
    """The defect that made the amendment lifecycle a duplicate factory.

    Candidates were scoped to `source_document_version_id == block.document_version_id`.
    When an amendment supersedes a clause the winning block moves into the
    amendment's version, while the requirement's current version still points at
    the RFP's -- so nothing matched, the proposal looked new, and a SECOND live
    requirement appeared on the same clause: "Test 25 applications" and "Test 35
    applications", both current, one of them retracted by the customer.

    The qualified clause reference bridges the two versions, because it is a
    property of the clause rather than of the file it arrived in.
    """
    from app.services.clause_identity import qualified_clause_ref

    rfp_version, amendment_version = uuid.uuid4(), uuid.uuid4()
    # Same clause, same heading, different document and different span -- an
    # amendment restates the clause, it does not reproduce the RFP's byte offsets.
    in_rfp = _Block(1200, 1480, rfp_version, clause_ref="4.2", heading="4.2 Testing scope")
    in_amendment = _Block(10, 90, amendment_version, clause_ref="4.2", heading="4.2 Testing scope")
    existing = _Requirement("R-001")

    stored = (
        existing,
        "Test 25 applications.",
        locator(in_rfp),
        rfp_version,
        qualified_clause_ref(in_rfp),
    )
    found = _candidates([stored], in_amendment)
    assert [r for r, _t in found] == [existing], (
        "the amendment could not see the requirement it amends, so it would create a "
        "second live requirement for the same clause"
    )


def test_an_unnumbered_block_does_not_bridge_versions():
    """The half of the rule that stops it over-matching.

    `qualified_clause_ref` is None for a block with no reference, and None ==
    None would gather every unnumbered requirement in the bid into one contest
    -- the same collapse qualification was introduced to prevent, arrived at
    from the other direction. Only a real reference bridges.
    """
    one, two = uuid.uuid4(), uuid.uuid4()
    unnumbered_here = _Block(1200, 1480, one)
    unnumbered_there = _Block(10, 90, two)
    existing = _Requirement("R-002")

    stored = (existing, "Some prose obligation.", locator(unnumbered_here), one, None)
    assert _candidates([stored], unnumbered_there) == []


def test_the_same_block_still_matches_without_a_clause_reference():
    """And the original path is untouched: a rerun on the very same unnumbered
    block still finds what it produced last time, by span."""
    version = uuid.uuid4()
    block = _Block(1200, 1480, version)
    existing = _Requirement("R-003")
    stored = (existing, "Some prose obligation.", locator(block), version, None)
    assert [r for r, _t in _candidates([stored], block)] == [existing]


def test_a_requirement_is_offered_once_even_when_both_rules_match():
    """Same block AND same clause is the ordinary rerun case. Returning the row
    twice would let one requirement absorb two proposals in the caller's greedy
    assignment, which is exactly the collapse it caps against."""
    from app.services.clause_identity import qualified_clause_ref

    version = uuid.uuid4()
    block = _Block(1200, 1480, version, clause_ref="4.2", heading="4.2 Testing scope")
    existing = _Requirement("R-004")
    stored = (
        existing,
        "Test 25 applications.",
        locator(block),
        version,
        qualified_clause_ref(block),
    )
    assert len(_candidates([stored, stored], block)) == 1
