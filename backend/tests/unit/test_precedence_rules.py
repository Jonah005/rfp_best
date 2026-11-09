"""Precedence decision logic, exercised without a database.

These cases are the reason precedence is resolved rather than assumed. A
hardcoded ``amendment > clarification > original`` ordering passes the easy ones
and gets the interesting ones silently wrong.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

import pytest

from app.models.enums import DocumentType, PrecedenceMethod
from app.services.precedence import MIN_CONFIDENCE, ClauseCandidate, _decide, _label_key


@dataclass
class _Stub:
    id: uuid.UUID


@dataclass
class _BlockStub:
    """A block as `collect_candidates` produces one.

    It carries a clause reference because the real query filters
    `clause_ref IS NOT NULL` -- a block without one never enters a precedence
    contest. The stub used to omit it, which was harmless while supersession
    was matched against `_decide`'s `clause_ref` argument and became a silent
    no-match once it was matched against the candidate's own block. Modelling
    the row faithfully is cheaper than remembering which fields the code reads.
    """

    id: uuid.UUID
    clause_ref: str | None = None
    heading_path: list[str] | None = None


def candidate(
    doc_type: DocumentType,
    *,
    issue_date: date | None = None,
    version_label: str | None = None,
    version_id: uuid.UUID | None = None,
    clause_ref: str = "6.1",
) -> ClauseCandidate:
    vid = version_id or uuid.uuid4()
    return ClauseCandidate(
        version=_Stub(id=vid),  # type: ignore[arg-type]
        block=_BlockStub(id=uuid.uuid4(), clause_ref=clause_ref),  # type: ignore[arg-type]
        document_type=doc_type,
        issue_date=issue_date,
        version_label=version_label,
    )


def test_a_single_candidate_wins_uncontested():
    only = candidate(DocumentType.RFP, issue_date=date(2026, 7, 14))
    outcome = _decide("6.1", [only], superseded={}, stated_priority=None)
    assert outcome.winner is only
    assert not outcome.unresolved


def test_explicit_supersession_beats_a_later_issue_date():
    """A relation naming the clause is stronger evidence than a date."""
    original = candidate(DocumentType.RFP, issue_date=date(2026, 9, 1))
    amendment = candidate(DocumentType.AMENDMENT, issue_date=date(2026, 8, 11))
    outcome = _decide(
        "6.1",
        [original, amendment],
        superseded={"6.1": {original.version.id}},
        stated_priority=None,
    )
    assert outcome.winner is amendment
    assert outcome.method is PrecedenceMethod.EXPLICIT_SUPERSESSION
    assert outcome.confidence >= MIN_CONFIDENCE


def test_a_later_clarification_beats_an_earlier_amendment():
    """The case the hardcoded ordering gets wrong."""
    amendment = candidate(DocumentType.AMENDMENT, issue_date=date(2026, 8, 11))
    clarification = candidate(DocumentType.CLARIFICATION, issue_date=date(2026, 8, 19))
    outcome = _decide("6.1", [amendment, clarification], superseded={}, stated_priority=None)
    assert outcome.winner is clarification
    assert outcome.method is PrecedenceMethod.ISSUE_DATE


def test_an_earlier_clarification_does_not_beat_a_later_amendment():
    """And the mirror image: recency is what decides, not document type."""
    clarification = candidate(DocumentType.CLARIFICATION, issue_date=date(2026, 7, 20))
    amendment = candidate(DocumentType.AMENDMENT, issue_date=date(2026, 8, 11))
    outcome = _decide("9", [clarification, amendment], superseded={}, stated_priority=None)
    assert outcome.winner is amendment


def test_a_customer_stated_rule_outranks_issue_date():
    later_amendment = candidate(DocumentType.AMENDMENT, issue_date=date(2026, 9, 1))
    earlier_spec = candidate(DocumentType.TECHNICAL_SPECIFICATION, issue_date=date(2026, 7, 1))
    outcome = _decide(
        "4.2",
        [later_amendment, earlier_spec],
        superseded={},
        stated_priority=["TECHNICAL_SPECIFICATION", "AMENDMENT"],
    )
    assert outcome.winner is earlier_spec
    assert outcome.method is PrecedenceMethod.CUSTOMER_STATED_RULE


def test_version_label_decides_when_dates_are_absent():
    v1 = candidate(DocumentType.RFP, version_label="v1")
    v2 = candidate(DocumentType.RFP, version_label="v2.1")
    outcome = _decide("3", [v1, v2], superseded={}, stated_priority=None)
    assert outcome.winner is v2
    assert outcome.method is PrecedenceMethod.VERSION_LABEL


def test_document_type_is_only_the_last_resort():
    original = candidate(DocumentType.RFP)
    amendment = candidate(DocumentType.AMENDMENT)
    outcome = _decide("7", [original, amendment], superseded={}, stated_priority=None)
    assert outcome.winner is amendment
    assert outcome.method is PrecedenceMethod.DOCUMENT_TYPE_DEFAULT
    assert outcome.confidence < MIN_CONFIDENCE + 0.2, (
        "a type-only decision must not present itself as confident"
    )


def test_indistinguishable_candidates_produce_no_winner():
    """Declining to choose is the correct outcome, not picking arbitrarily."""
    a = candidate(DocumentType.AMENDMENT)
    b = candidate(DocumentType.AMENDMENT)
    outcome = _decide("6.1", [a, b], superseded={}, stated_priority=None)
    assert outcome.winner is None
    assert outcome.unresolved
    assert outcome.confidence == 0.0
    assert outcome.conflict_pair is not None and len(outcome.conflict_pair) == 2


def test_equal_issue_dates_fall_through_rather_than_coin_flip():
    same = date(2026, 8, 11)
    a = candidate(DocumentType.AMENDMENT, issue_date=same)
    b = candidate(DocumentType.AMENDMENT, issue_date=same)
    outcome = _decide("6.1", [a, b], superseded={}, stated_priority=None)
    assert outcome.unresolved


def test_a_dated_version_beats_an_undated_one_but_only_weakly():
    dated = candidate(DocumentType.AMENDMENT, issue_date=date(2026, 8, 11))
    undated = candidate(DocumentType.AMENDMENT)
    outcome = _decide("6.1", [dated, undated], superseded={}, stated_priority=None)
    assert outcome.winner is dated
    assert outcome.confidence >= MIN_CONFIDENCE
    assert outcome.confidence < 0.8


def test_document_wide_supersession_applies_to_every_clause():
    original = candidate(DocumentType.RFP, issue_date=date(2026, 7, 1))
    replacement = candidate(DocumentType.AMENDMENT, issue_date=date(2026, 7, 2))
    outcome = _decide(
        "12.4",
        [original, replacement],
        superseded={"*": {original.version.id}},
        stated_priority=None,
    )
    assert outcome.winner is replacement
    assert outcome.method is PrecedenceMethod.EXPLICIT_SUPERSESSION


@pytest.mark.parametrize(
    ("lower", "higher"),
    [("v1", "v2"), ("1.0", "1.1"), ("v1.9", "v1.10"), ("A", "B"), ("draft", "v1")],
)
def test_version_labels_order_numerically_where_possible(lower, higher):
    assert _label_key(lower) < _label_key(higher)
