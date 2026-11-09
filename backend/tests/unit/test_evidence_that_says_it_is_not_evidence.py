"""Text stamped SAMPLE must never become approved proof of capability.

This is the worst thing this system has actually done. A bulk approval moved 306
extracted excerpts to APPROVED / CURRENT_VERIFIED in one click, and at least 52
of them announce in their own words that they are not real: "SAMPLE / NOT
VALID", "SYNTHETIC / REPLACE", "DRAFT FOR APPROVAL", "certificates not
supplied", plus invented revenue figures and a rate card of made-up numbers.

Approved and current, those are indistinguishable from real evidence to
retrieval, to assessment, and to a drafted statement. The end state of that path
is a submitted proposal claiming demonstrated capability on the strength of a
template.

Nothing in the approval gate looked at the text. The gate checked expiry, which
is arithmetic, and stopped there.

These tests assert the refusal, and just as importantly assert what must NOT be
refused -- a company whose real documents mention the word "sample" in passing
must still be able to approve them.
"""

from __future__ import annotations

import pytest

from app.core.errors import ValidationError
from app.services.placeholders import disqualifying_match

REAL_MARKERS = [
    "ISO 27001 certificate SAMPLE / NOT VALID - replace before submission",
    "SYNTHETIC / REPLACE: annual revenue AED 23,000,000",
    "Bank capability letter [REPLACE] with signed original",
    "Audited financial statement DRAFT FOR APPROVAL",
    "CREST accreditation - certificates not supplied",
    "Reference project value: AED 23,000,000 (illustrative)",
    "Insurance certificate [TO BE FILLED]",
    "Rate card - example only, not contractual",
]


@pytest.mark.parametrize("text", REAL_MARKERS)
def test_text_that_disqualifies_itself_is_detected(text):
    assert disqualifying_match(text) is not None, text


REAL_EVIDENCE = [
    "ISO/IEC 27001:2022 certificate no. 12345, issued 2024-03-01, valid to 2027-02-28.",
    "The company has delivered 14 portal implementations for banking clients since 2019.",
    "Wilma Vineetha Dmello, Senior Software Developer, 9 years ASP.NET and SQL Server.",
    "Our standard support model provides 24x7 coverage with a 4-hour response SLA.",
    "We sample 10% of transactions for quality assurance during UAT.",
    "The solution supports English and Arabic across all screens.",
]


@pytest.mark.parametrize("text", REAL_EVIDENCE)
def test_real_evidence_is_not_refused(text):
    """The cost of a false positive is a real certificate a human cannot approve.

    "We sample 10% of transactions" is the case worth keeping honest about: the
    word appears, and the excerpt is genuine evidence of a QA practice.
    """
    assert disqualifying_match(text) is None, text


class _Evidence:
    def __init__(self, excerpt, status, approval_status):
        import uuid

        self.id = uuid.uuid4()
        self.org_id = "org"
        self.excerpt = excerpt
        self.status = status
        self.approval_status = approval_status
        self.approved_for_reuse = False
        self.last_verified_at = None
        self.effective_date = None
        self.expiry_date = None


def test_approving_a_sample_excerpt_is_refused_with_the_reason():
    """Refused, not silently downgraded. A person asked for this to be approved
    and is owed the answer, and the phrase that decided it -- otherwise the only
    way to learn why is to read the source."""
    from app.core.context import ExecutionContext
    from app.models.enums import ApprovalStatus, KnowledgeStatus
    from app.services import knowledge

    evidence = _Evidence(
        "ISO 27001 certificate SAMPLE / NOT VALID",
        KnowledgeStatus.PROPOSED,
        ApprovalStatus.PENDING_APPROVAL,
    )

    class _Session:
        def get(self, _model, _id):
            return evidence

        def flush(self):
            pass

    import uuid

    ctx = ExecutionContext(org_id="org", actor_kind="USER", actor_user_id=uuid.uuid4())
    with pytest.raises(ValidationError) as raised:
        knowledge.approve_evidence(
            _Session(), ctx, evidence_id=evidence.id, reason="Reviewed in bulk."
        )

    message = str(raised.value)
    assert "SAMPLE / NOT VALID" in message, "the deciding phrase must be quoted back"
    assert evidence.approval_status is ApprovalStatus.PENDING_APPROVAL
    assert evidence.approved_for_reuse is False


def test_the_gate_reads_the_text_not_only_the_dates():
    """The specific hole: approval checked expiry and nothing else, so an
    excerpt with no dates at all sailed through whatever it said.

    Asserted on behaviour rather than source, but the point is that a
    PROPOSED excerpt with no expiry -- the shape every extracted row has -- must
    still be refused on its wording.
    """
    from app.models.enums import ApprovalStatus, KnowledgeStatus

    evidence = _Evidence(
        "Annual revenue AED 23,000,000 (illustrative)",
        KnowledgeStatus.PROPOSED,
        ApprovalStatus.PENDING_APPROVAL,
    )
    assert evidence.expiry_date is None
    assert evidence.status is not KnowledgeStatus.EXPIRED
    assert disqualifying_match(evidence.excerpt) is not None
