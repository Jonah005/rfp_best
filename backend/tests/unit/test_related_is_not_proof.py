"""Evidence the model called "related" must not be reported as proof.

Observed on the real ADX bid, first run with verification live. The model read
684 retrieved candidates and called exactly **two** of them proof; 66 more were
"related, not proof on its own"; it discarded the other 616. Counting those 66
as proof produced roughly twenty rows reading "current verified / ready" whose
own rationale said, one sentence later, that nothing attached actually answered
the requirement:

    R-006  These terms and conditions will also form a part of the contract.
           current verified / ready / 1 item
           "Current, approved evidence supports this requirement. The model
            reads the attached evidence as not covered: no excerpt directly
            addresses the requirement..."

A matrix cannot say "ready" and "no evidence of this" about one row and be
useful to anyone. Two things follow, and they land on the two axes separately —
which is the point, not a technicality.
"""

from __future__ import annotations

import uuid
from datetime import date

from app.models.enums import (
    AssessmentEvidenceRole,
    BidReadiness,
    EvidenceStatus,
    KnowledgeStatus,
    RequirementApplicability,
    RequirementCategory,
)
from app.models.knowledge import Evidence
from app.schemas.evidence_relevance import Coverage, RelevanceVerdict, RequirementFit
from app.services.assessment import Candidate, _evidence_status

TODAY = date(2026, 9, 1)


def _candidate(support: RelevanceVerdict | None, *, expiry: date | None = None):
    return Candidate(
        evidence=Evidence(
            id=uuid.uuid4(),
            excerpt="text",
            status=KnowledgeStatus.CURRENT_VERIFIED,
            expiry_date=expiry,
        ),
        score=0.8,
        role=AssessmentEvidenceRole.SUPPORTING,
        rank=1,
        note="note",
        support=support,
    )


# --- axis one: what the evidence shows --------------------------------------


def test_a_shortlist_of_related_material_is_not_current_verified():
    """The bug, in one assertion. This is what produced R-006."""
    candidates = [_candidate(RelevanceVerdict.PARTIAL) for _ in range(3)]
    assert _evidence_status(candidates, today=TODAY) is EvidenceStatus.PARTIAL


def test_one_piece_of_real_proof_is_enough():
    """A requirement is answered by the excerpt that answers it, not by a count."""
    candidates = [
        _candidate(RelevanceVerdict.PARTIAL),
        _candidate(RelevanceVerdict.SUPPORTS),
        _candidate(RelevanceVerdict.PARTIAL),
    ]
    assert _evidence_status(candidates, today=TODAY) is EvidenceStatus.CURRENT_VERIFIED


def test_unjudged_evidence_behaves_exactly_as_before():
    """Verification off, unavailable, or skipped: the old arithmetic, unchanged.

    This is what keeps the change safe. `support` is None whenever nothing read
    the pair, and a missing reading must never be treated as a bad one — that
    would make a provider outage look like a company with no evidence.
    """
    candidates = [_candidate(None) for _ in range(3)]
    assert _evidence_status(candidates, today=TODAY) is EvidenceStatus.CURRENT_VERIFIED


def test_a_mixed_judged_and_unjudged_shortlist_needs_real_proof():
    """One unread excerpt does not launder a shortlist of related material."""
    candidates = [_candidate(RelevanceVerdict.PARTIAL), _candidate(None)]
    assert _evidence_status(candidates, today=TODAY) is EvidenceStatus.PARTIAL


def test_dates_still_outrank_the_reading():
    """An expired excerpt the model loved is still expired.

    The reading answers "is this proof of the ask"; it never answers "is this
    still true", which is computed from the date and is not the model's business.
    """
    expired = _candidate(RelevanceVerdict.SUPPORTS, expiry=date(2020, 1, 1))
    assert _evidence_status([expired], today=TODAY) is EvidenceStatus.EXPIRED


def test_nothing_attached_is_still_nothing():
    assert _evidence_status([], today=TODAY) is EvidenceStatus.NONE


# --- axis two: what happens next --------------------------------------------


def _assess_readiness(coverage: Coverage, *, category, status, mandatory=True, proven=True):
    """The readiness decision as `assess_requirement` makes it, isolated.

    Mirrors the ordering in the service: compute readiness from the evidence
    status, then let a NOT_COVERED reading move it to a person.
    """
    from app.services.assessment import _readiness

    class _Req:
        applicability = RequirementApplicability.ACTIVE
        is_conditional = False

    _Req.is_mandatory = mandatory
    _Req.category = category
    from app.services.assessment import _READINESS_FLOOR

    readiness, _ = _readiness(_Req(), status, has_open_clarification=False, proven=proven)
    fit = RequirementFit(coverage=coverage)
    if fit is not None and readiness not in _READINESS_FLOOR:
        if fit.coverage is Coverage.NOT_COVERED:
            readiness = BidReadiness.NEEDS_COMPANY_CONFIRMATION
        elif fit.coverage is Coverage.PARTIALLY_COVERED and readiness is BidReadiness.READY:
            readiness = BidReadiness.DRAFTABLE
    return readiness


def test_not_covered_sends_the_requirement_to_a_person():
    """ "Ready" is a claim about the bid, and this row cannot support one."""
    assert (
        _assess_readiness(
            Coverage.NOT_COVERED,
            category=RequirementCategory.TECHNICAL,
            status=EvidenceStatus.CURRENT_VERIFIED,
        )
        is BidReadiness.NEEDS_COMPANY_CONFIRMATION
    )


def test_full_coverage_leaves_readiness_alone():
    assert (
        _assess_readiness(
            Coverage.FULLY_COVERED,
            category=RequirementCategory.TECHNICAL,
            status=EvidenceStatus.CURRENT_VERIFIED,
        )
        is BidReadiness.READY
    )


def test_ready_needs_a_positive_reading_not_merely_the_absence_of_a_negative_one():
    """Current and approved is a statement about an excerpt's LIFECYCLE.

    R-019 on the live bid went READY on seven excerpts that were current,
    approved, the right type -- and never read for whether they prove bilingual
    delivery. That is the whole claim READY makes: a drafter has nothing left to
    resolve. So an unread shortlist is DRAFTABLE, which is true of it, rather
    than READY, which is not.
    """
    assert (
        _assess_readiness(
            Coverage.FULLY_COVERED,
            category=RequirementCategory.TECHNICAL,
            status=EvidenceStatus.CURRENT_VERIFIED,
            proven=False,
        )
        is BidReadiness.DRAFTABLE
    )


def test_a_blocked_requirement_is_not_softened():
    """NOT_COVERED must only ever move a row toward more attention, never less."""
    assert (
        _assess_readiness(
            Coverage.NOT_COVERED,
            category=RequirementCategory.TECHNICAL,
            status=EvidenceStatus.NONE,
        )
        is BidReadiness.BLOCKED
    )


def test_partial_coverage_is_draftable_not_ready():
    """R-033 on the live bid: one excerpt called proof, but proof of a slightly
    different commitment. "Ready" claims a drafter has nothing left to resolve.
    """
    assert (
        _assess_readiness(
            Coverage.PARTIALLY_COVERED,
            category=RequirementCategory.TECHNICAL,
            status=EvidenceStatus.CURRENT_VERIFIED,
        )
        is BidReadiness.DRAFTABLE
    )


def test_partial_coverage_never_upgrades_a_weaker_state():
    """The rule only ever moves a row toward more attention."""
    assert (
        _assess_readiness(
            Coverage.PARTIALLY_COVERED,
            category=RequirementCategory.TECHNICAL,
            status=EvidenceStatus.NONE,
        )
        is BidReadiness.BLOCKED
    )


# --- the skip rule that let an unchecked row read "ready" -------------------


def test_a_short_requirement_cannot_be_certified_by_one_word():
    """Found live on R-001, "Training for 2 seats is required."

    Three terms over three characters, so a single shared common word scored
    0.33 and cleared the fraction bar alone. Every excerpt mentioning training
    counted as obviously right, the shortlist looked unambiguous, and the
    requirement was never read — it reported "ready" on eight unchecked
    attachments.
    """
    from app.services.assessment import _lexically_certain

    assert not _lexically_certain(overlap=1, term_count=3)


def test_a_broad_overlap_on_a_short_requirement_is_still_certain():
    """The bar must not become unreachable for genuinely strong matches."""
    from app.services.assessment import _lexically_certain

    assert _lexically_certain(overlap=3, term_count=3)


def test_one_word_of_a_long_requirement_was_never_certain():
    """The fraction already handled this case; the count must not weaken it."""
    from app.services.assessment import _lexically_certain

    assert not _lexically_certain(overlap=1, term_count=20)


def test_both_conditions_are_required():
    """Three shared words out of forty is a wide overlap and a thin share."""
    from app.services.assessment import _lexically_certain

    assert not _lexically_certain(overlap=3, term_count=40)
