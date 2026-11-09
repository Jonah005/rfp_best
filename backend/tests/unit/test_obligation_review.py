"""A second opinion, on only the clauses that need one.

Decomposition answers a dozen questions about a clause at once and the owner is
one field among them. On the live ADX bid that produced a split result: the
customer's rights in the contract section came back CUSTOMER, and the same class
of clause among the submission instructions came back BIDDER. Same tender, same
kind of sentence, different answer depending on which part of the document it
was printed in. Not a hard judgement — a crowded one.

So the clauses a deterministic rule can settle never reach a model, and the rest
go to a call that asks one question about each. Batched, so a whole tender is
one request: about a tenth of a cent, against roughly sixty calls for the
evidence check that follows.

Everything here is about the two limits: it must not spend a call on a decided
question, and it must not be able to excuse an obligation the text plainly
assigns to the bidder.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.enums import ObligationOwner
from app.schemas.tender_extraction import (
    ObligationReviewResult,
    ObligationVerdict,
)
from app.services import requirements as module

# Verbatim from the ADX RFP, in the register this tender actually uses.
SETTLED_BY_TEXT = [
    "The Firm must notify ADX of their intention to respond no later than one week "
    "following the date of RFP receipt.",
    "The selected Bidder must inform the Abu Dhabi Securities Exchange in writing of "
    "any changes to the proposed team.",
    "The Contractor shall include all relevant business references for which "
    "contractor have performed work of similar size and scope.",
    "The supplier is requested to maintain the System Document File (SDF).",
]

NEEDS_A_READING = [
    "ADX may, at its discretion, waive any minor non-conformity or minor irregularity.",
    "Any subsequent discussions shall be at the discretion of ADX.",
    "The ADX has the right to disqualify the Bidder whose clarification is found not "
    "suitable to the proposed project.",
    # Passive voice: nobody is named as the actor at all.
    "All communication for changes must be done in writing by authorized personnel.",
]


# --- triage: do not buy an answer to a decided question ---------------------


@pytest.mark.parametrize("text", SETTLED_BY_TEXT)
def test_a_clause_naming_the_bidder_needs_no_call(text):
    assert module._BIDDER_ACTOR.search(text)


@pytest.mark.parametrize("text", NEEDS_A_READING)
def test_a_clause_naming_nobody_or_the_customer_goes_to_the_model(text):
    assert not module._BIDDER_ACTOR.search(text)


def test_the_word_this_tender_actually_uses_is_recognised():
    """ "The Firm must…" is how half this RFP words its obligations, and the
    first version of the pattern did not know the word — so the clauses most
    plainly the bidder's were the ones it could not vouch for."""
    assert module._BIDDER_ACTOR.search("The Firm must prepare the number of copies.")


# --- the review itself ------------------------------------------------------


class _Version:
    def __init__(self, text):
        self.requirement_text = text


class _Requirement:
    def __init__(self, text, owner=ObligationOwner.BIDDER):
        self.id = uuid.uuid4()
        self.code = "R-001"
        self.current_version_id = uuid.uuid4()
        self.obligation_owner = owner
        self._text = text


class _Session:
    """Just enough session for the review: it reads versions and flushes."""

    def __init__(self, requirements):
        self._requirements = requirements
        self.flushed = 0

    def execute(self, _statement):
        rows = self._requirements

        class _Result:
            def scalars(self_inner):
                return rows

        return _Result()

    def get(self, _model, _id):
        for requirement in self._requirements:
            if requirement.current_version_id == _id:
                return _Version(requirement._text)
        return None

    def flush(self):
        self.flushed += 1


class _Gateway:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.prompts = []

    def generate_structured(self, call_type, prompt, **kwargs):
        assert call_type == "obligation_owner_review"
        # A deterministic read over fixed clause text: re-analysing an unchanged
        # tender must not buy the same answer twice.
        assert kwargs.get("cache") is True
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.result


def _review(requirements, result=None, error=None):
    session = _Session(requirements)
    gateway = _Gateway(result, error)
    summary = module.review_obligation_owners(session, _ctx(), bid_id=uuid.uuid4(), gateway=gateway)
    return summary, gateway


def _ctx():
    from app.core.context import ExecutionContext

    return ExecutionContext(org_id=uuid.uuid4())


def test_only_the_ambiguous_clauses_are_sent(monkeypatch):
    settled = _Requirement(SETTLED_BY_TEXT[0])
    ambiguous = _Requirement(NEEDS_A_READING[0])
    result = ObligationReviewResult(
        verdicts=[ObligationVerdict(clause=1, owner=ObligationOwner.CUSTOMER)]
    )

    summary, gateway = _review([settled, ambiguous], result)

    assert summary["settled_without_a_call"] == 1
    assert summary["reviewed"] == 1
    # One request for the whole tender, not one per clause.
    assert summary["calls"] == 1
    assert len(gateway.prompts) == 1
    assert "[1]" in gateway.prompts[0] and "[2]" not in gateway.prompts[0]
    assert ambiguous.obligation_owner is ObligationOwner.CUSTOMER


def test_the_review_cannot_excuse_a_clause_the_text_assigns():
    """The backstop runs on the answer, not only on the first label."""
    bidder = _Requirement("Bidder is liable for compensations to ADX.")
    result = ObligationReviewResult(
        verdicts=[ObligationVerdict(clause=1, owner=ObligationOwner.CUSTOMER)]
    )
    _review([bidder], result)
    assert bidder.obligation_owner is ObligationOwner.BIDDER


def test_a_clause_the_review_ignores_keeps_its_label():
    """Silence is not a verdict."""
    ambiguous = _Requirement(NEEDS_A_READING[0], owner=ObligationOwner.BIDDER)
    summary, _ = _review([ambiguous], ObligationReviewResult(verdicts=[]))
    assert ambiguous.obligation_owner is ObligationOwner.BIDDER
    assert summary["relabelled"] == 0


def test_an_out_of_range_verdict_is_discarded():
    ambiguous = _Requirement(NEEDS_A_READING[1])
    result = ObligationReviewResult(
        verdicts=[ObligationVerdict(clause=99, owner=ObligationOwner.CUSTOMER)]
    )
    _review([ambiguous], result)
    assert ambiguous.obligation_owner is ObligationOwner.BIDDER


def test_a_provider_failure_leaves_every_label_alone():
    """A review that cannot run must never be read as "all bidder obligations
    are excused", nor as a reason to fail the analysis."""
    ambiguous = _Requirement(NEEDS_A_READING[0], owner=ObligationOwner.CUSTOMER)
    summary, _ = _review([ambiguous], error=RuntimeError("502"))
    assert ambiguous.obligation_owner is ObligationOwner.CUSTOMER
    assert summary["calls"] == 0


def test_a_settled_clause_is_corrected_without_a_call():
    """Decomposition said CUSTOMER; the text says "the Contractor shall"."""
    wrong = _Requirement(SETTLED_BY_TEXT[2], owner=ObligationOwner.CUSTOMER)
    summary, gateway = _review([wrong], ObligationReviewResult(verdicts=[]))
    assert wrong.obligation_owner is ObligationOwner.BIDDER
    assert gateway.prompts == []
    assert summary["calls"] == 0


# --- the cost shape ---------------------------------------------------------


def test_the_batch_is_large_enough_to_be_one_call_for_a_real_tender():
    """58 requirements, of which roughly half are ambiguous: one request."""
    assert module.OBLIGATION_REVIEW_BATCH >= 40


def test_clause_text_is_truncated_in_the_prompt():
    """Who acts is settled by the opening words of a clause."""
    long_clause = "It is hereby noted that " + "x" * 5000
    requirement = _Requirement(long_clause)
    _, gateway = _review([requirement], ObligationReviewResult(verdicts=[]))
    assert "x" * (module.OBLIGATION_REVIEW_CHARS + 1) not in gateway.prompts[0]
