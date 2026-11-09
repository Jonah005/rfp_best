"""The two axes, tested as arithmetic.

`evidence_status` and `bid_readiness` answer different questions, and the whole
point of keeping them separate is that "we have proof" and "we can bid this"
are not the same claim. These tests pin that separation down: the cases that
matter most are the ones asserting the two axes *disagree*, because those are
exactly the cases a single collapsed field would lose.

The functions under test are pure, so no database is needed.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.models.enums import (
    BidReadiness,
    EvidenceStatus,
    KnowledgeStatus,
    RequirementApplicability,
    RequirementCategory,
)
from app.services.assessment import Candidate, _evidence_status, _readiness

TODAY = date(2026, 6, 1)
YESTERDAY = TODAY - timedelta(days=1)
NEXT_YEAR = TODAY + timedelta(days=365)


def candidate(
    status: KnowledgeStatus = KnowledgeStatus.CURRENT_VERIFIED,
    *,
    expiry: date | None = None,
    rank: int = 1,
) -> Candidate:
    return Candidate(
        evidence=SimpleNamespace(status=status, expiry_date=expiry, id="e"),
        score=1.0,
        role=None,
        rank=rank,
        note="",
    )


def requirement(
    *,
    mandatory: bool = True,
    category: RequirementCategory = RequirementCategory.TECHNICAL,
    applicability: RequirementApplicability = RequirementApplicability.ACTIVE,
) -> SimpleNamespace:
    return SimpleNamespace(is_mandatory=mandatory, category=category, applicability=applicability)


# --- evidence status --------------------------------------------------------


def test_no_candidates_is_none_not_a_guess():
    assert _evidence_status([], today=TODAY) is EvidenceStatus.NONE


def test_live_evidence_is_current_verified():
    status = _evidence_status(
        [candidate(KnowledgeStatus.CURRENT_VERIFIED, expiry=NEXT_YEAR)], today=TODAY
    )
    assert status is EvidenceStatus.CURRENT_VERIFIED


@pytest.mark.parametrize(
    "case",
    [
        # An expiry date in the past outranks whatever the stored status says:
        # a certificate that lapsed yesterday is not current, however it was
        # last labelled.
        candidate(KnowledgeStatus.CURRENT_VERIFIED, expiry=YESTERDAY),
        candidate(KnowledgeStatus.EXPIRED),
    ],
)
def test_expired_evidence_never_reads_as_current(case):
    assert _evidence_status([case], today=TODAY) is EvidenceStatus.EXPIRED


def test_historical_evidence_is_reported_as_historical_not_current():
    """Risk 3 from the evaluation: proof of the past presented as proof of now."""
    status = _evidence_status([candidate(KnowledgeStatus.HISTORICAL_VERIFIED)], today=TODAY)
    assert status is EvidenceStatus.HISTORICAL_VERIFIED
    assert status is not EvidenceStatus.CURRENT_VERIFIED


def test_a_mix_of_current_and_stale_is_partial_not_current():
    status = _evidence_status(
        [
            candidate(KnowledgeStatus.CURRENT_VERIFIED, expiry=NEXT_YEAR),
            candidate(KnowledgeStatus.HISTORICAL_VERIFIED, rank=2),
        ],
        today=TODAY,
    )
    assert status is EvidenceStatus.PARTIAL


def test_conflicting_evidence_wins_over_everything():
    """One contradiction taints the set; it is not averaged away by good proof."""
    status = _evidence_status(
        [
            candidate(KnowledgeStatus.CURRENT_VERIFIED, expiry=NEXT_YEAR),
            candidate(KnowledgeStatus.CONFLICTING, rank=2),
        ],
        today=TODAY,
    )
    assert status is EvidenceStatus.CONFLICTING


# --- readiness --------------------------------------------------------------


def test_current_evidence_on_a_technical_requirement_is_ready():
    """`proven` is what separates READY from DRAFTABLE here: current and
    approved describes an excerpt's lifecycle, and READY claims something about
    its content. See test_related_is_not_proof.py for the case that forced the
    distinction."""
    readiness, rationale = _readiness(
        requirement(),
        EvidenceStatus.CURRENT_VERIFIED,
        has_open_clarification=False,
        proven=True,
    )
    assert readiness is BidReadiness.READY
    assert rationale


def test_current_evidence_nobody_has_read_is_draftable_not_ready():
    readiness, rationale = _readiness(
        requirement(),
        EvidenceStatus.CURRENT_VERIFIED,
        has_open_clarification=False,
        proven=False,
    )
    assert readiness is BidReadiness.DRAFTABLE
    assert "has been judged proof" in rationale


def test_expired_evidence_yields_needs_refresh_not_blocked():
    """There *is* proof; it needs renewing. Calling it BLOCKED loses that."""
    readiness, _ = _readiness(requirement(), EvidenceStatus.EXPIRED, has_open_clarification=False)
    assert readiness is BidReadiness.NEEDS_REFRESH


def test_historical_evidence_yields_needs_refresh():
    readiness, rationale = _readiness(
        requirement(), EvidenceStatus.HISTORICAL_VERIFIED, has_open_clarification=False
    )
    assert readiness is BidReadiness.NEEDS_REFRESH
    assert "currency" in rationale.lower()


def test_mandatory_requirement_with_no_evidence_is_blocked():
    readiness, _ = _readiness(
        requirement(mandatory=True), EvidenceStatus.NONE, has_open_clarification=False
    )
    assert readiness is BidReadiness.BLOCKED


def test_optional_requirement_with_no_evidence_is_not_blocked():
    """A missing optional answer is work to do, not a reason to lose the bid."""
    readiness, _ = _readiness(
        requirement(mandatory=False), EvidenceStatus.NONE, has_open_clarification=False
    )
    assert readiness is BidReadiness.NEEDS_COMPANY_CONFIRMATION


def test_submission_category_with_current_evidence_still_requires_a_signature():
    """The axes disagreeing: fully evidenced, and still not answerable."""
    readiness, _ = _readiness(
        requirement(category=RequirementCategory.SUBMISSION),
        EvidenceStatus.CURRENT_VERIFIED,
        has_open_clarification=False,
    )
    assert readiness is BidReadiness.REQUIRES_SIGNATURE


def test_open_clarification_outranks_perfect_evidence():
    """We may have the proof and still not know what was asked."""
    readiness, _ = _readiness(
        requirement(), EvidenceStatus.CURRENT_VERIFIED, has_open_clarification=True
    )
    assert readiness is BidReadiness.NEEDS_CLIENT_CLARIFICATION


def test_not_applicable_requirement_is_never_a_gap():
    readiness, _ = _readiness(
        requirement(applicability=RequirementApplicability.NOT_APPLICABLE),
        EvidenceStatus.NONE,
        has_open_clarification=False,
    )
    assert readiness is BidReadiness.NOT_APPLICABLE


def test_conditional_pending_requires_confirmation_even_with_evidence():
    readiness, _ = _readiness(
        requirement(applicability=RequirementApplicability.CONDITIONAL_PENDING),
        EvidenceStatus.CURRENT_VERIFIED,
        has_open_clarification=False,
    )
    assert readiness is BidReadiness.NEEDS_COMPANY_CONFIRMATION


def test_partial_evidence_is_draftable_with_the_gap_named():
    readiness, rationale = _readiness(
        requirement(), EvidenceStatus.PARTIAL, has_open_clarification=False
    )
    assert readiness is BidReadiness.DRAFTABLE
    assert "gap" in rationale.lower()


def test_every_evidence_status_maps_to_a_readiness():
    """No status may fall through to an implicit default."""
    for status in EvidenceStatus:
        readiness, rationale = _readiness(requirement(), status, has_open_clarification=False)
        assert isinstance(readiness, BidReadiness)
        assert rationale.strip()


# --- terms you accept versus capabilities you prove -------------------------
#
# From a live run against a real RFP: six of seven "eligibility" requirements
# were contract boilerplate — "proposals become the property of ADX", "the ADX
# decision is final", "disputes go to arbitration after thirty days". Every one
# came back BLOCKED for want of evidence that could not exist, and the single
# genuine gate ("firms must be industry certified") was lost among them.


def test_a_contract_term_with_no_evidence_is_not_blocked():
    """Nothing in a company's filing cabinet evidences "we accept your terms"."""
    readiness, rationale = _readiness(
        requirement(category=RequirementCategory.CONTRACTUAL, mandatory=True),
        EvidenceStatus.NONE,
        has_open_clarification=False,
    )
    assert readiness is BidReadiness.NEEDS_COMPANY_CONFIRMATION
    assert "decision" in rationale.lower()


def test_a_legal_undertaking_with_no_evidence_is_not_blocked_either():
    readiness, _ = _readiness(
        requirement(category=RequirementCategory.LEGAL, mandatory=True),
        EvidenceStatus.NONE,
        has_open_clarification=False,
    )
    assert readiness is BidReadiness.NEEDS_COMPANY_CONFIRMATION


def test_a_real_eligibility_gate_with_no_evidence_is_still_blocked():
    """The distinction has to keep the genuine gap visible, not soften it."""
    readiness, _ = _readiness(
        requirement(category=RequirementCategory.ELIGIBILITY, mandatory=True),
        EvidenceStatus.NONE,
        has_open_clarification=False,
    )
    assert readiness is BidReadiness.BLOCKED


def test_a_contract_term_with_expired_evidence_still_needs_refreshing():
    """The exemption is only for absent evidence, not for stale evidence."""
    readiness, _ = _readiness(
        requirement(category=RequirementCategory.CONTRACTUAL, mandatory=True),
        EvidenceStatus.EXPIRED,
        has_open_clarification=False,
    )
    assert readiness is BidReadiness.NEEDS_REFRESH
