"""A row that asks for evidence is not evidence.

Every excerpt below is real text from the company pack, copied from a run of
`scripts/check_evidence_units.py` rather than written for the test. Two tests in
this project once passed against invented fixtures while the bug they covered
was untouched, so the strings here are the strings in the database.

The two directions are not equally important. A missed downgrade lets an
unbackable claim reach a customer; a spurious one costs a drafter a labelled row
they can see and overrule. So the false-positive cases below are as load-bearing
as the true positives -- they are what stops the rule creeping outward until it
downgrades the policy manual.
"""

from __future__ import annotations

import pytest

from app.services import evidence_markers

# --- real placeholders, all of which must be caught -------------------------

PLACEHOLDERS = [
    pytest.param(
        "Table: Certification register Columns: Credential | Scope / owner | Certificate no. "
        "| Issue / expiry | Evidence status Credential: ISO/IEC 27001 Information Security | "
        "Scope / owner: [REPLACE scope] | Certificate no.: [REPLACE] | Issue / expiry: "
        "[REPLACE] | Evidence status: NOT EVIDENCED",
        id="iso-27001-that-does-not-exist-yet",
    ),
    pytest.param(
        "Table: Trade licence Columns: Field | Value Field: Issuing authority | Value: "
        "[REPLACE FROM OFFICIAL RECORD]",
        id="trade-licence-replacement-sheet",
    ),
    pytest.param(
        "Table: Wilma Vineetha Dmello Columns: Proposed role | Senior Software Developer "
        "Proposed role: Current status | Senior Software Developer: [HR TO CONFIRM]",
        id="cv-field-awaiting-hr",
    ),
    pytest.param(
        "Table: Synthetic role-based rates SYNTHETIC / NOT AN OFFER Demonstration values "
        "only. Replace from an approved commercial model and apply bid-specific discounts "
        "through documented approval.",
        id="synthetic-rate-card",
    ),
    pytest.param(
        "Table: Synthetic role-based rates Columns: Role | Day rate AED | Hour rate AED | "
        "Minimum unit | Notes Role: Security Specialist | Day rate AED: 5,000 | Hour rate "
        "AED: 670 | Minimum unit: 1 day | Notes: Demo value",
        id="demo-day-rate",
    ),
    pytest.param(
        "Table: Priority project evidence register Columns: Project / customer claim | "
        "Historical source | Preferred proof | Status / owner Project / customer claim: ADX "
        "secure real-time subscription processing / ADX-eKtetab | Historical source: pp. "
        "130, 133 | Preferred proof: Contract extract; acceptance; approved reference "
        "letter | Status / owner: MISSING / Sales + Legal",
        id="project-proof-not-obtained",
    ),
    pytest.param(
        "Table: Financial evidence checklist Columns: Evidence | Minimum content | Status | "
        "Owner Evidence: Bank / financial-capability letter | Minimum content: Bank "
        "letterhead, account standing/capability wording, recent date | Status: MISSING | "
        "Owner: CFO / Bank",
        id="bank-letter-missing",
    ),
    pytest.param(
        "Table: Table 1 DRAFT FOR APPROVAL These policies were created because the supplied "
        "proposal did not contain a complete current policy set. They are operational "
        "drafts, not evidence of adopted practice until approved and implemented.",
        id="policy-manual-cover-says-so",
    ),
    pytest.param(
        "Table: Pricing schedule template Columns: Line | Deliverable / service | Qty | Unit "
        "| Unit price | Total | Assumption / evidence Line: 1 | Deliverable / service: "
        "[REPLACE] | Qty: [QTY] | Unit: [UNIT] | Unit price: [PRICE] | Total: [TOTAL] | "
        "Assumption / evidence: [LINK]",
        id="blank-pricing-template",
    ),
    pytest.param(
        "Table: Certification register Columns: Credential | Scope / owner | Certificate no. "
        "| Issue / expiry | Evidence status Credential: Tadawul tradable-rights "
        "certification | Scope / owner: Historical product processing/support claim | "
        "Certificate no.: Not provided | Issue / expiry: Not provided | Evidence status: "
        "SOURCE — HISTORICAL; verify",
        id="certificate-not-provided",
    ),
    pytest.param(
        "Table: Table 2 Columns: Document owner | Bid / Proposal Management Document owner: "
        "Approval state | Bid / Proposal Management: Historical claims — certificates not "
        "supplied",
        id="certificates-not-supplied",
    ),
]


@pytest.mark.parametrize("excerpt", PLACEHOLDERS)
def test_a_row_that_asks_for_evidence_is_marked(excerpt: str) -> None:
    assert evidence_markers.is_placeholder(excerpt), excerpt[:80]
    reason = evidence_markers.reason_for(excerpt)
    assert reason and reason.startswith("Not substantiated:")


# --- real evidence, none of which may be downgraded -------------------------

SUBSTANTIATED = [
    pytest.param(
        "1. Information security policy TTS will protect the confidentiality, integrity and "
        "availability of company and customer information through risk-based controls "
        "proportionate to data sensitivity and service criticality. Maintain an asset "
        "inventory and assigned owners. Apply least privilege, role-based access, MFA where "
        "supported, joiner/mover/leaver controls and periodic access reviews.",
        id="an-actual-policy",
    ),
    pytest.param(
        "4. Business continuity policy Identify critical services, dependencies, maximum "
        "tolerable downtime and recovery objectives. Maintain response, communication, "
        "recovery and return-to-normal procedures. Back up critical information; test "
        "restoration and protect copies from common failure modes.",
        id="continuity-policy",
    ),
    pytest.param(
        "Table: Historical incident targets Columns: Severity | Historical definition "
        "summary | Callback | Resolution commitment Severity: Critical | Historical "
        "definition summary: Serious impact on a significant portion of the software and "
        "major user impact | Callback: 15 minutes | Resolution commitment: Within 2 hours",
        id="stated-sla-targets",
    ),
    pytest.param(
        "Table: Table 2 Columns: Document owner | Bid / Proposal Management Document owner: "
        "Company | Bid / Proposal Management: Total Technologies and Solutions FZ-LLC",
        id="document-header-furniture",
    ),
    pytest.param(
        "Table: Amjad Nawaz Sheikh Columns: Proposed role | Team Lead / Senior Architect "
        "Proposed role: Profile summary | Team Lead / Senior Architect: Enterprise "
        "architecture, Java/.NET engineering, training and delivery leadership",
        id="a-filled-in-cv-row",
    ),
    pytest.param(
        "Offer Management / ADX-eKtetab Centralized public-offering workflow for exchanges, "
        "issuers, lead managers and receiving entities. Role-based participant access "
        "Electronic-channel and participant integration Data validation and verification",
        id="product-summary",
    ),
]


@pytest.mark.parametrize("excerpt", SUBSTANTIATED)
def test_real_material_is_left_alone(excerpt: str) -> None:
    assert not evidence_markers.is_placeholder(excerpt), evidence_markers.find(excerpt)
    assert evidence_markers.reason_for(excerpt) is None


def test_a_citation_is_not_a_placeholder_field() -> None:
    """`[3]` and `[a]` are not unfilled fields.

    The bracket rule is the broadest of the eight and the one most likely to
    creep. It requires an upper-case opener and three characters, so ordinary
    citations and list markers do not trip it.
    """
    assert not evidence_markers.is_placeholder(
        "Source: Winning Proposal [3], pp. 150-156 [a] and the appendix [ii]."
    )


def test_every_reason_is_reported_not_just_the_first() -> None:
    """A row can be wrong in more than one way, and a person chasing it needs to
    know all of them.

    The example spans BOTH detectors on purpose. `placeholders.py` owns the
    shared patterns and reports one representative match -- that is its
    contract, and it is not worth changing for this -- so a row failing two
    shared patterns yields one hit. A row that is both self-disqualifying AND
    matches a rule only this module carries yields two, which is the case where
    the second reason actually tells somebody something new.
    """
    excerpt = "Certificate no.: [REPLACE] | Status: MISSING | Owner: Compliance"
    hits = evidence_markers.find(excerpt)
    assert len(hits) >= 2, hits
    names = {name for name, _reason in hits}
    assert "self_disqualifying" in names
    assert "missing" in names
    reason = evidence_markers.reason_for(excerpt)
    assert "MISSING" in reason
    assert "not evidence" in reason


def test_the_two_detectors_do_not_overlap() -> None:
    """The duplication this module was rewritten to remove must not creep back.

    `evidence_markers` originally re-implemented two thirds of
    `placeholders.py` -- written without finding it, which is the exact failure
    this codebase keeps paying for. Every pattern kept here must be one
    `placeholders` does NOT already catch; otherwise the two drift and
    disagree about the same excerpt.
    """
    from app.services.placeholders import disqualifying_match

    # One string per local rule, chosen to match that rule and nothing shared.
    local_only = {
        "missing": "Status: MISSING | Owner: CFO",
        "demo_value": "Day rate AED: 5,000 | Notes: Demo value",
        "template": "Evidence: Pricing schedule | Status: TEMPLATE BELOW",
    }
    covered = {name for name, _pattern, _reason in evidence_markers._RULES}
    assert covered == set(local_only), "a rule was added or renamed without a case here"

    for name, text in local_only.items():
        assert disqualifying_match(text) is None, (
            f"{name!r} is already covered by placeholders.py; delete it here rather "
            "than maintaining the same pattern twice"
        )
        assert any(hit == name for hit, _reason in evidence_markers.find(text))


def test_the_shared_detector_is_actually_consulted() -> None:
    """Delegation, asserted on behaviour rather than on an import.

    If `find` stopped calling `placeholders.disqualifying_match`, every string
    below would go unmarked while the three local rules kept passing -- the
    module would look healthy and would have silently lost two thirds of its
    coverage.
    """
    for text in (
        "Evidence status: NOT EVIDENCED",
        "Value: [REPLACE FROM OFFICIAL RECORD]",
        "SYNTHETIC / NOT AN OFFER",
        "DRAFT FOR APPROVAL",
        "Certificate no.: Not provided",
    ):
        hits = evidence_markers.find(text)
        assert hits, text
        assert any(name == "self_disqualifying" for name, _reason in hits), text


def test_nothing_here_can_mark_something_substantiated() -> None:
    """The whole contract of this module in one assertion.

    It returns markers or it returns nothing. There is no path through it that
    concludes an excerpt IS proof -- that judgment needs the model call, and an
    excerpt with no marker is simply one this rule has nothing to say about.
    """
    assert evidence_markers.find("") == []
    assert evidence_markers.find(None) == []
    assert evidence_markers.reason_for("anything at all") is None


def test_an_old_claim_is_not_an_absent_one():
    """The rule that had to be removed after one dry run.

    "Historical proposal claim; verify current status before external use" was
    a marker for exactly one run. It matched 41 rows including the company
    profile and the service-lines section -- the drafter's best material -- and
    marking those "never cite as proof" would have been badly wrong: they are
    backed by a real 2015 proposal, and what is in doubt is their currency, not
    their existence.

    Age and absence are two axes. `EvidenceStatus` already separates
    HISTORICAL_VERIFIED from CURRENT_VERIFIED, and folding the first into this
    column is the same error as collapsing evidence_status into bid_readiness.
    """
    profile = (
        "Capability statement Total Technologies and Solutions FZ-LLC (TTS) is presented "
        "in the supplied proposal as a Dubai-based financial-technology and software-"
        "services company founded in March 2004. Source: Winning Proposal — ADX-eKtetab "
        "Technical Proposal (26 July 2015), pp. 112, 140–141. Historical proposal claim; "
        "verify current status before external use."
    )
    assert not evidence_markers.is_placeholder(profile), evidence_markers.find(profile)


def test_a_historical_row_that_is_ALSO_missing_is_still_caught():
    """Removing the age rule must not take the absence rule with it. This row
    is both old and never supplied, and the second is what matters."""
    excerpt = (
        "Credential: Tadawul tradable-rights certification | Scope / owner: Historical "
        "product processing/support claim | Certificate no.: Not provided | Issue / "
        "expiry: Not provided | Evidence status: SOURCE — HISTORICAL; verify"
    )
    assert evidence_markers.is_placeholder(excerpt)
