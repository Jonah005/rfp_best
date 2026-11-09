"""A clause that binds the customer is recorded, not worked.

Eleven requirements owned by the CUSTOMER read NEEDS_COMPANY_CONFIRMATION or
NEEDS_CLIENT_CLARIFICATION and one produced a company task, though the bidder
has nothing to confirm, evidence or ask about "Jadwa controls the negotiation".
NOT_APPLICABLE was the wrong label (that means a condition does not apply), so
the row gets a state of its own.

Second defect, same export: an excerpt marked unsubstantiated carried the note
"Model check: supports this requirement". Both statements were true, of
different things, and read as a contradiction.
"""

from __future__ import annotations

import inspect

from app.models.enums import BidReadiness
from app.services import assessment, readiness, tasks


def test_the_state_exists():
    assert BidReadiness.CUSTOMER_OBLIGATION.value == "CUSTOMER_OBLIGATION"


def test_a_customer_clause_outranks_an_open_question_about_it():
    source = inspect.getsource(assessment.assess_requirement)
    assert "readiness = BidReadiness.CUSTOMER_OBLIGATION" in source
    # Only a positive label, never an unsure one.
    assert "ObligationOwner.CUSTOMER" in source
    assert "customer_hands_something_over(text_query)" in source
    assert "RequirementApplicability.ACTIVE" in source


def test_nothing_is_attached_to_a_customer_clause():
    source = inspect.getsource(assessment.assess_requirement)
    assert "or binds_only_the_customer" in source


def test_a_reading_may_not_move_it():
    assert BidReadiness.CUSTOMER_OBLIGATION in assessment._READINESS_FLOOR


def test_it_is_not_counted_as_work_or_as_a_gap_in_coverage():
    source = inspect.getsource(readiness)
    assert "summary.customer_obligations += 1" in source
    assert BidReadiness.CUSTOMER_OBLIGATION not in readiness.ACTION_STATES
    assert BidReadiness.CUSTOMER_OBLIGATION not in readiness.CRITICAL_STATES


def test_no_task_is_generated_for_it():
    source = inspect.getsource(tasks.generate_tasks)
    assert "RequirementAssessment.bid_readiness == BidReadiness.CUSTOMER_OBLIGATION" in source


def test_a_task_ignores_it_when_deciding_its_own_state():
    source = inspect.getsource(tasks)
    assert "r is not BidReadiness.CUSTOMER_OBLIGATION" in source


def test_a_note_does_not_say_supports_for_evidence_that_cannot_prove():
    source = inspect.getsource(assessment)
    assert "relevant to this requirement, but inadmissible as proof" in source
    assert "if evidence.is_substantiated is False:" in source


def test_the_four_clauses_that_hand_something_over_are_recognised_and_the_rest_are_not():
    """The real Jadwa texts, not a description of them."""
    hands_over = [
        "Jadwa will manage the subscription of Al-Elm services for following purposes: "
        "Verify Information; Get applicant related more information",
        "Jadwa will provide access to Core OMS system for the following purpose: Check "
        "client already exist; Create client account by adding all collected information",
        "Jadwa will provide SMS service connection details to send validation SMS to applicant.",
        "Jadwa will provide vendor service and credentials details to access the existing "
        "email service. It will be used for following purpose: To send applicant email "
        "verification",
    ]
    rights = [
        "All terms and conditions will be subject to negotiation by Jadwa.",
        "Jadwa anticipates selecting at least two vendors to have more in-depth discussions.",
        "Jadwa holds the right to cancel the project without awarding it to any of the vendors.",
        "Jadwa will rate proposals based on the following factors: Responsiveness; Cost",
        "Jadwa reserves the right to award to the bidder that presents the best value.",
        "Jadwa will bear the cost of SSL certificate",
        "Jadwa IT team will assist if required.",
    ]
    for text in hands_over:
        assert assessment.customer_hands_something_over(text), text
    for text in rights:
        assert not assessment.customer_hands_something_over(text), text


def test_the_gaps_sheet_agrees_with_the_matrix_about_what_is_our_work():
    """Four rows created tasks and asked for confirmation on the Matrix and were
    absent from Gaps, because Gaps read the owner label and the Matrix read the
    assessment. Both now read the assessment."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from _export_queries import is_a_gap

    common = dict(mandatory=True, applicability="ACTIVE")
    # The handover clause: labelled CUSTOMER, held for a person.
    assert is_a_gap(owner="CUSTOMER", readiness="NEEDS_COMPANY_CONFIRMATION", **common)
    assert is_a_gap(owner="CUSTOMER", readiness="NEEDS_CLIENT_CLARIFICATION", **common)
    # A clause that only binds the customer.
    assert not is_a_gap(owner="CUSTOMER", readiness="CUSTOMER_OBLIGATION", **common)
    # Unchanged behaviour for bidder rows.
    assert is_a_gap(owner="BIDDER", readiness="BLOCKED", **common)
    assert is_a_gap(owner="BIDDER", readiness=None, **common)
    assert not is_a_gap(owner="BIDDER", readiness="READY", **common)
    assert not is_a_gap(
        owner="BIDDER", readiness="BLOCKED", mandatory=False, applicability="ACTIVE"
    )
    assert not is_a_gap(
        owner="BIDDER", readiness="BLOCKED", mandatory=True, applicability="NOT_APPLICABLE"
    )


def test_the_decomposition_prompt_splits_bulleted_purposes_of_a_handover():
    from app.gateway.registry import REQUIREMENT_DECOMPOSITION

    assert REQUIREMENT_DECOMPOSITION.prompt_version == "v12"
    prompt = REQUIREMENT_DECOMPOSITION.system_prompt
    assert "is FOUR requirements" in prompt
    assert "bulleted list under" in prompt
    assert "When in doubt, split" in prompt
    # The sentence that let the model leave a bulleted handover as one row.
    assert "it stays one customer requirement." not in prompt.replace(
        "does it stay one customer requirement.", ""
    )
