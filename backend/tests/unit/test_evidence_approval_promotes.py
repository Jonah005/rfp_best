"""Approving evidence must make it usable, not merely mark it approved.

The failure this pins was total and silent. Extraction records every excerpt as
PROPOSED — a claim awaiting a human. Assessment only draws on verified states.
And nothing anywhere in the system moved evidence out of PROPOSED: approving it
set the approval flag and left the status alone.

So a company could upload its entire corpus, watch extraction produce 304
excerpts, approve all of them, and still have every requirement report "no
supporting evidence" — with no error, no warning, and nothing in any log to say
why. The whole company side of the product could not work.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.models.enums import ApprovalStatus, KnowledgeStatus
from app.services.assessment import _admissible
from app.services.knowledge import _initial_status

TODAY = date.today()


def test_proposed_is_not_an_admissible_state():
    """The premise: verified states only, which is what made the gap fatal."""
    clause = _admissible()[1]
    admitted = {status.value for status in clause.right.value}
    assert KnowledgeStatus.PROPOSED.value not in admitted
    assert KnowledgeStatus.CURRENT_VERIFIED.value in admitted


def test_approval_derives_currentness_from_the_dates():
    """Approval verifies the claim; the dates decide whether it is current."""
    assert _initial_status(None, None) is KnowledgeStatus.CURRENT_VERIFIED
    assert (
        _initial_status(None, TODAY + timedelta(days=365))
        is KnowledgeStatus.CURRENT_VERIFIED
    )


def test_evidence_whose_validity_has_ended_is_verified_as_historical():
    """Approving a lapsed certificate must not make it read as current."""
    assert (
        _initial_status(None, TODAY - timedelta(days=1))
        is KnowledgeStatus.HISTORICAL_VERIFIED
    )


def test_approved_and_verified_are_different_axes():
    """A fact can be approved and expired; neither implies the other.

    Conflating them is what would tempt a fix that simply drops the status
    filter from admissibility — which would let unreviewed extraction output
    back a customer-facing claim.
    """
    assert ApprovalStatus.APPROVED.value != KnowledgeStatus.CURRENT_VERIFIED.value
    assert KnowledgeStatus.EXPIRED.value not in {ApprovalStatus.APPROVED.value}
