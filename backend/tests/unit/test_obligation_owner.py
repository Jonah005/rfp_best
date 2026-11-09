"""Who a clause binds, and why getting it wrong in one direction is worse.

Nineteen rows of the first real bid reported BLOCKED — "a mandatory requirement
with no supporting evidence" — and only four were anything of the kind. Eight
were ADX reserving rights for itself:

    5.14  ADX may, at its discretion, waive any minor non-conformity.
    5.17  The ADX shall be under no obligation to accept the lowest offer.
    8.23  ADX will not be liable to pay any amounts other than the grand total.

No company document can evidence a clause like that. Assessed as a bidder
obligation it sits at BLOCKED forever, and the performance bond, the
service-interruption liability, the System Document File and the list of other
commitments — the four that actually put the bid at risk — are one fifth of a
wall of red nobody can act on.

**The asymmetry is the whole design.** Calling a customer right a bidder
obligation costs a cluttered review list. Calling a bidder obligation the
customer's takes a genuine no-bid risk off that list, silently. So every
uncertain case resolves toward BIDDER, and the cases below that matter most are
the ones asserting a bidder obligation can never be excused by accident.
"""

from __future__ import annotations

import pytest

from app.models.enums import (
    BidReadiness,
    EvidenceStatus,
    ObligationOwner,
    RequirementApplicability,
    RequirementCategory,
)
from app.schemas.tender_extraction import ExtractedRequirement
from app.services.assessment import _obligation_owner, _readiness
from app.services.requirements import resolved_obligation_owner


class _Req:
    """A requirement, at the level `_readiness` reads one."""

    def __init__(
        self,
        *,
        owner=ObligationOwner.BIDDER,
        category=RequirementCategory.TECHNICAL,
        mandatory=True,
    ):
        self.obligation_owner = owner
        self.category = category
        self.is_mandatory = mandatory
        self.applicability = RequirementApplicability.ACTIVE
        self.is_conditional = False


def _readiness_of(req):
    return _readiness(req, EvidenceStatus.NONE, has_open_clarification=False)[0]


# --- the point: customer rights stop looking like bidder failures ------------


def test_a_customer_right_is_not_a_blocker():
    """R-015: "ADX may waive any minor non-conformity." Nothing to evidence."""
    assert (
        _readiness_of(_Req(owner=ObligationOwner.CUSTOMER))
        is BidReadiness.NEEDS_COMPANY_CONFIRMATION
    )


def test_a_customer_right_is_still_put_in_front_of_a_person():
    """Noted, never hidden. It remains a term somebody should read and accept."""
    readiness, rationale = _readiness(
        _Req(owner=ObligationOwner.CUSTOMER),
        EvidenceStatus.NONE,
        has_open_clarification=False,
    )
    assert readiness is not BidReadiness.NOT_APPLICABLE
    assert "binds the customer" in rationale


# --- the failure that must be impossible by accident ------------------------


def test_a_bidder_obligation_is_still_blocked():
    """R-040, the 15% performance bond. This must stay red."""
    assert _readiness_of(_Req(owner=ObligationOwner.BIDDER)) is BidReadiness.BLOCKED


def test_a_mutual_clause_is_still_blocked():
    """MUTUAL binds the bidder too, so it is not excused."""
    assert _readiness_of(_Req(owner=ObligationOwner.MUTUAL)) is BidReadiness.BLOCKED


def test_a_requirement_with_no_owner_at_all_is_treated_as_the_bidders():
    """Extracted before the column existed, or missing for any other reason.

    The default lives in the model, in the database, in the extraction schema
    AND here. Four places for one value is not redundancy — it is the decision
    that a missing label must never excuse an obligation.
    """

    class _Legacy:
        category = RequirementCategory.TECHNICAL
        is_mandatory = True
        applicability = RequirementApplicability.ACTIVE
        is_conditional = False

    assert _obligation_owner(_Legacy()) is ObligationOwner.BIDDER
    assert _readiness_of(_Legacy()) is BidReadiness.BLOCKED


def test_the_extraction_schema_defaults_to_bidder():
    """A model that omits the field must not thereby excuse the bidder."""
    proposal = ExtractedRequirement(
        requirement_text="The supplier shall submit a performance bond.",
        category=RequirementCategory.COMMERCIAL,
        is_mandatory=True,
        source_block_ordinal=1,
        confidence=0.9,
    )
    assert proposal.obligation_owner is ObligationOwner.BIDDER


# --- the label only ever relabels an empty row ------------------------------


def test_the_label_cannot_touch_a_requirement_that_has_evidence():
    """Proof outranks the label, in both directions.

    A customer-owned clause that somehow attracted current evidence still
    reports what the evidence says. The label answers "can this ever be
    evidenced", not "what does the evidence show".
    """
    readiness, _ = _readiness(
        _Req(owner=ObligationOwner.CUSTOMER),
        EvidenceStatus.CURRENT_VERIFIED,
        has_open_clarification=False,
        proven=True,
    )
    assert readiness is BidReadiness.READY


def test_an_open_clarification_still_outranks_the_label():
    readiness, _ = _readiness(
        _Req(owner=ObligationOwner.CUSTOMER),
        EvidenceStatus.NONE,
        has_open_clarification=True,
    )
    assert readiness is BidReadiness.NEEDS_CLIENT_CLARIFICATION


def test_a_non_applicable_requirement_is_unaffected():
    req = _Req(owner=ObligationOwner.CUSTOMER)
    req.applicability = RequirementApplicability.NOT_APPLICABLE
    assert _readiness_of(req) is BidReadiness.NOT_APPLICABLE


# --- saying accurately why a submission row is blocked ----------------------


def test_a_submission_action_says_it_is_an_action():
    """R-002: notify ADX within a week. No file was ever the answer."""
    _, rationale = _readiness(
        _Req(category=RequirementCategory.SUBMISSION),
        EvidenceStatus.NONE,
        has_open_clarification=False,
    )
    assert "submission action" in rationale
    assert "no supporting evidence" not in rationale


def test_a_submission_action_is_still_blocked():
    """Renaming it must not soften it — it genuinely has not been done."""
    assert _readiness_of(_Req(category=RequirementCategory.SUBMISSION)) is BidReadiness.BLOCKED


def test_a_capability_gap_keeps_the_evidence_wording():
    """R-021, the System Document File: this one really is a missing document."""
    _, rationale = _readiness(
        _Req(category=RequirementCategory.TECHNICAL),
        EvidenceStatus.NONE,
        has_open_clarification=False,
    )
    assert "no supporting evidence" in rationale


# --- the deterministic backstop, on the real clauses ------------------------
#
# The classifier is a model, and a model can be wrong. This is the check that
# does not depend on one: a clause naming the bidder as the actor is the
# bidder's obligation, whatever the label says. It only ever moves a label back
# TOWARD the bidder — nothing here promotes a clause to CUSTOMER, because a
# regular expression is not a good enough reason to stop asking a person about
# an obligation.


BIDDER_CLAUSES = [
    # The four that actually put this bid at risk.
    "Upon awarding project to bidder, the bidder shall submit an automatically "
    "renewable performance bond.",
    "Bidder is liable for compensations to ADX for any service interruption to ADX "
    "business due to the project implementation.",
    "The supplier is requested to maintain the System Document File (SDF) and make "
    "sure that it is updated, and periodically provide an update to ADX.",
    "Contractor shall also provide a list of its other commitments during the same "
    "time frame as the aforementioned project duration.",
    # And two written in a softer register, which must not excuse them.
    "The Bidder should describe in detail, the Approach for knowledge transfer.",
    "Bidder/Contractor shall supply samples, brochures, and detailed specifications.",
]

CUSTOMER_CLAUSES = [
    "ADX may, at its discretion, waive any minor non-conformity or minor irregularity in an offer.",
    "The ADX shall be under no obligation to accept the lowest or any other offer.",
    "The Abu Dhabi Securities Exchange will not be liable to pay any amounts other "
    "than the amount stated as the grand total.",
    "Any subsequent discussions shall be at the discretion of ADX.",
    "The ADX has the right to disqualify the Bidder whose clarification is found "
    "not suitable to the proposed project.",
    "At the discretion of the ADX, review and acceptance would be carried out by a "
    "team appointed by the ADX.",
]


@pytest.mark.parametrize("text", BIDDER_CLAUSES)
def test_a_clause_naming_the_bidder_can_never_be_excused(text):
    """Verbatim from the ADX RFP. If any of these is ever labelled CUSTOMER, a
    real no-bid risk drops off the red list — so the label is overridden."""
    assert resolved_obligation_owner(text, ObligationOwner.CUSTOMER) is ObligationOwner.BIDDER


@pytest.mark.parametrize("text", CUSTOMER_CLAUSES)
def test_a_genuine_customer_right_survives_the_backstop(text):
    """Verbatim from the ADX RFP. The backstop must not be so eager that it
    undoes the distinction it exists to protect — several of these name the
    Bidder as the OBJECT of the customer's right."""
    assert resolved_obligation_owner(text, ObligationOwner.CUSTOMER) is ObligationOwner.CUSTOMER


def test_the_backstop_never_promotes_a_clause_to_customer():
    """One direction only. Nothing here can excuse an obligation."""
    for text in CUSTOMER_CLAUSES + BIDDER_CLAUSES:
        assert resolved_obligation_owner(text, ObligationOwner.BIDDER) is ObligationOwner.BIDDER


# --- re-extraction has to actually apply the label --------------------------


def test_re_extracting_unchanged_wording_still_refreshes_the_classification():
    """The bug that made the whole feature a silent no-op.

    A requirement whose wording has not changed gets no new version — correctly,
    since the text is the same. But the early return skipped the classification
    too, so shipping `obligation_owner` and re-running the analysis cost a full
    batch of model calls and left all 58 rows on the default. Wording and
    classification are different questions, and only the first needs a version.
    """
    from app.services.requirements import _refresh_classification

    class _Session:
        def __init__(self):
            self.flushed = 0

        def flush(self):
            self.flushed += 1

    class _Existing:
        category = RequirementCategory.SUBMISSION
        is_mandatory = True
        is_scored = False
        obligation_owner = ObligationOwner.BIDDER

    class _Proposal:
        requirement_text = "ADX may, at its discretion, waive any minor non-conformity."
        category = RequirementCategory.CONTRACTUAL
        is_mandatory = True
        is_scored = False
        obligation_owner = ObligationOwner.CUSTOMER

    session, existing = _Session(), _Existing()
    _refresh_classification(session, existing, _Proposal())

    assert existing.obligation_owner is ObligationOwner.CUSTOMER
    assert existing.category is RequirementCategory.CONTRACTUAL
    # autoflush is off in this application's sessions, so an unflushed update is
    # invisible to the next SELECT in the same transaction.
    assert session.flushed == 1


def test_the_refresh_runs_the_backstop_too():
    """Re-extraction is a second chance to get it wrong, and the same guard applies."""
    from app.services.requirements import _refresh_classification

    class _Session:
        def flush(self):
            pass

    class _Existing:
        category = RequirementCategory.COMMERCIAL
        is_mandatory = True
        is_scored = False
        obligation_owner = ObligationOwner.BIDDER

    class _Proposal:
        requirement_text = "The bidder shall submit an automatically renewable performance bond."
        category = RequirementCategory.COMMERCIAL
        is_mandatory = True
        is_scored = False
        obligation_owner = ObligationOwner.CUSTOMER

    existing = _Existing()
    _refresh_classification(_Session(), existing, _Proposal())
    assert existing.obligation_owner is ObligationOwner.BIDDER


# --- re-extraction is not an amendment --------------------------------------


def test_a_re_extraction_is_not_recorded_as_a_superseding_document():
    """Observed live, and the worst kind of bug: quiet and in the audit trail.

    Re-running the analysis on unchanged documents rewrote R-003 from
    "responses to clarifications would be annexed to the contract" to "the Firm
    must prepare the number of copies as indicated" — a different obligation
    under a code that assessments, tasks and artifact statements already cite —
    and logged it as an amendment by a superseding document that does not exist.

    `_find_existing` matches on clause reference alone in its fallback, with no
    text comparison, which is right for a genuine amendment (the customer
    rewrote 6.1; it is still requirement 6.1) and wrong for a second pass over
    the same bytes.
    """
    import inspect

    from app.services import requirements as module

    source = inspect.getsource(module._persist_requirement)
    assert "current.source_document_version_id == block.document_version_id" in source
    assert "Re-extracted from the same source document version" in source
    assert "_flag_identity_drift" in source


def test_identity_drift_is_raised_for_review_rather_than_hidden():
    """It does not block the re-extraction — a second pass is often better than
    the first — it makes the drift something a person has to look at."""
    import inspect

    from app.services import requirements as module

    source = inspect.getsource(module._flag_identity_drift)
    assert "may now" in source and "different obligation" in source
    # Fingerprinted so re-running the analysis does not pile up duplicates.
    assert "fingerprint=" in source and "hashlib.sha256" in source


def test_the_prompt_says_the_section_does_not_decide_the_owner():
    """The live failure: CUSTOMER in the contract section, BIDDER for the same
    class of clause among the submission instructions."""
    from app.gateway.registry import get

    prompt = get("requirement_decomposition").system_prompt
    assert "The SECTION a clause sits in does not decide this" in prompt
    assert "acted upon" in prompt


def test_the_decomposition_prompt_version_moved_with_its_guidance():
    """A changed prompt is a changed question, and the history has to say so.

    Asserted as "past the version that introduced this guidance" rather than as
    an exact string. Pinning the exact value made every later prompt change fail
    here, and a test that fails for a correct change teaches people to edit the
    number rather than think about it -- which is how the one real signal in it
    gets lost.
    """
    from app.gateway.registry import get

    version = get("requirement_decomposition").prompt_version
    assert version not in {"v1", "v2"}, (
        "obligation_owner guidance arrived in v3; an earlier version means it is gone"
    )


def test_a_drift_note_never_says_the_bid_is_waiting_on_the_customer():
    """The failure this caused: 51 of 59 rows reading "needs client clarification".

    A drift note is a statement about our own extraction — there is nothing to
    ask the customer — and putting it in the waiting-on-the-customer channel
    silenced the entire matrix in one run.

    The guarantee is unchanged; what enforces it has moved. It used to be
    enforced by NOT LINKING the note to its requirement, which also made it
    invisible on the row it concerned. Readiness now gates on
    `recommended_question` — whether there is something a customer could answer
    — so the note is linked and visible, and still cannot claim the bid is
    blocked on anybody. Raising a question here would put it straight back into
    that channel, which is what this test exists to prevent.
    """
    import inspect

    from app.services import requirements as module

    body = inspect.getsource(module._flag_identity_drift).split('"""')[2]
    assert "recommended_question=None" in body, (
        "a drift note must carry no customer question, or it will gate the row"
    )
    assert "requirement_ids=[requirement.id]" in body, (
        "it must reach the row it concerns, or the finding is unreachable"
    )
    assert "detected_by=" in body, "it must stay identifiable for cleanup"


def test_stale_drift_notes_are_retired_by_the_analysis():
    """The 51 already in the database have to be closed somewhere.

    A standalone script cannot reach them: the local stack sets the constrained
    role's password to a constant of its own and exports the finished URL only
    into the process it launches, so nothing outside that process can read the
    working credential. The analysis already holds it, and the tenant context
    with it.
    """
    import inspect

    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.analyze_bid)
    assert "close_detector_issues" in source
    assert "requirement_identity_drift@v1" in source


def test_retiring_a_detector_unlinks_as_well_as_closes():
    """A closed issue that still holds its link is a trap for the next reader,
    and for any query that counts links rather than statuses."""
    import inspect

    from app.services import clarifications

    source = inspect.getsource(clarifications.close_detector_issues)
    assert "delete(ClarificationRequirement)" in source
    assert "ClarificationIssue.detected_by == detected_by" in source
