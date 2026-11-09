"""Evidence the model called "not proof" must not make a row draftable.

The verifier asks the model, for every shortlisted excerpt, whether it proves
the requirement or is merely related to it. On the live bid it answered 198
times: **5 supports, 193 "related, not proof on its own"**, each with a reason
of its own -- "shows a historical bid record containing a technical proposal,
but not the project experience itself".

The matrix then reported 41 rows DRAFTABLE. For **40 of them the model had
judged every single attachment not-proof**. The verdict existed on every row and
readiness never asked for it, so the matrix invited a drafter to write a
positive answer to a customer backed by material the system itself had said does
not answer the requirement.

`_evidence_status` was already reading the verdict correctly -- that is why
those rows are PARTIAL rather than CURRENT_VERIFIED. The defect was that PARTIAL
had come to mean two different things:

    some proof, plus some stale or historical material   -> draftable
    material attached, none of it proof                  -> nothing to draft from

and the readiness rule treated both as the first. The second is now routed
exactly as "nothing was found", because in the sense that matters nothing was.
The excerpts stay attached and visible -- "here is the closest thing we hold" is
worth showing a drafter -- they simply stop counting as a claim's backing.
"""

from __future__ import annotations

import uuid

from app.models.enums import (
    BidReadiness,
    EvidenceStatus,
    ObligationOwner,
    RequirementApplicability,
    RequirementCategory,
)
from app.schemas.evidence_relevance import RelevanceVerdict
from app.services.assessment import _nothing_attached_is_proof, _readiness


class _Requirement:
    def __init__(
        self,
        *,
        mandatory=True,
        category=RequirementCategory.TECHNICAL,
        owner=ObligationOwner.BIDDER,
    ):
        self.id = uuid.uuid4()
        self.is_mandatory = mandatory
        self.category = category
        self.obligation_owner = owner
        self.applicability = RequirementApplicability.ACTIVE


class _Candidate:
    """Only the field the rule reads. `support` is None when the model never
    judged this excerpt -- a provider outage, or a shortlist it did not answer
    for."""

    def __init__(self, support):
        self.support = support


SUPPORTS = _Candidate(RelevanceVerdict.SUPPORTS)
RELATED = _Candidate(RelevanceVerdict.PARTIAL)
UNJUDGED = _Candidate(None)


def test_material_judged_related_only_is_not_draftable():
    """The 40 rows. Mandatory, so this lands on BLOCKED rather than quietly
    becoming somebody's optional follow-up."""
    readiness, rationale = _readiness(
        _Requirement(),
        EvidenceStatus.PARTIAL,
        has_open_clarification=False,
        nothing_is_proof=True,
    )
    assert readiness is BidReadiness.BLOCKED
    assert "mandatory" in rationale.lower()


def test_real_partial_evidence_is_still_draftable():
    """The case PARTIAL was invented for must survive: some proof, plus stale or
    historical material alongside it. Over-correcting here would bury the rows a
    drafter can actually work on."""
    readiness, rationale = _readiness(
        _Requirement(),
        EvidenceStatus.PARTIAL,
        has_open_clarification=False,
        nothing_is_proof=False,
    )
    assert readiness is BidReadiness.DRAFTABLE
    assert "gaps" in rationale.lower()


def test_the_rationale_says_what_is_actually_attached():
    """"No supporting evidence was found" would be false on these rows --
    something was found, and a reader who opens the matrix can see it. The
    sentence has to distinguish "we have nothing" from "we have things that do
    not prove it", or it teaches people to distrust the column."""
    _, rationale = _readiness(
        _Requirement(mandatory=False),
        EvidenceStatus.PARTIAL,
        has_open_clarification=False,
        nothing_is_proof=True,
    )
    assert "related material is attached" in rationale.lower()
    assert "none of it proof" in rationale.lower()


def test_a_customer_owned_clause_is_still_not_the_bidder_s_problem():
    """The existing exemptions apply to both routes into this state. A clause
    binding the customer has nothing for the bidder to prove whether the
    shortlist was empty or merely unhelpful."""
    readiness, _ = _readiness(
        _Requirement(owner=ObligationOwner.CUSTOMER),
        EvidenceStatus.PARTIAL,
        has_open_clarification=False,
        nothing_is_proof=True,
    )
    assert readiness is BidReadiness.NEEDS_COMPANY_CONFIRMATION


def test_a_term_to_accept_is_not_blocked_by_related_material():
    readiness, _ = _readiness(
        _Requirement(category=RequirementCategory.CONTRACTUAL),
        EvidenceStatus.PARTIAL,
        has_open_clarification=False,
        nothing_is_proof=True,
    )
    assert readiness is BidReadiness.NEEDS_COMPANY_CONFIRMATION


def test_an_unchecked_shortlist_is_not_a_finding():
    """Absence of a verdict is not a verdict.

    When verification does not run -- a provider outage, a model that skipped a
    number -- every candidate carries `support=None`. Reading that as "nothing
    is proof" would turn an outage into a bid-wide wall of BLOCKED rows, which
    is a different lie from the one being fixed.
    """
    assert _nothing_attached_is_proof([UNJUDGED, UNJUDGED]) is False
    assert _nothing_attached_is_proof([]) is False


def test_one_piece_of_proof_is_enough_to_keep_drafting():
    assert _nothing_attached_is_proof([RELATED, RELATED, SUPPORTS]) is False
    assert _nothing_attached_is_proof([RELATED, UNJUDGED]) is True
    assert _nothing_attached_is_proof([RELATED]) is True
