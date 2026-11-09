"""Nothing in the report pipeline may depend on one customer's tender."""

from __future__ import annotations

from types import SimpleNamespace

from app.services import report_attachments as ra
from app.services import report_extras as rx


def test_evaluation_factors_found_for_any_customer(monkeypatch):
    text = (
        "Northwind Bank shall evaluate all proposals against the following criteria: "
        "Price  Delivery plan  Team experience  Security approach  "
        "Northwind Bank reserves the right to reject any bid."
    )
    block = SimpleNamespace(text=text)
    monkeypatch.setattr("app.services.tender_analysis.tender_blocks", lambda *a, **k: [block])
    assert rx._evaluation_factors(None, None, None) == [
        "Price",
        "Delivery plan",
        "Team experience",
        "Security approach",
    ]


def test_attachments_follow_what_the_tender_asks_for():
    asks = lambda kind, t: bool(ra.ASKED_FOR[kind].search(t))  # noqa: E731
    tender = "Submit résumés of key staff, your standard terms and conditions and past performance."
    assert asks("CV", tender) and asks("LEGAL_DOC", tender) and asks("PROJECT_REFERENCE", tender)
    assert not asks("LOAD_TEST", tender)
    assert asks("LOAD_TEST", "Provide the results of a performance test.")
    assert not asks("PROJECT_REFERENCE", "Quote the RFP reference number on every page.")


def test_paragraphs_with_fixture_only_facts_are_withheld():
    from app.services.release import WITHHELD, _withhold_fixture_claims

    facts = {"Nabil Farouk": "cvs", "14,200": "ref"}
    parts = [
        "We will build the portal.\n\nNabil Farouk led it and 14,200 applications ran.\n\nWe test "
        "it.",
        "Nabil Farouk handled 14,200 more.",
    ]
    out, n = _withhold_fixture_claims(parts, facts)
    assert n == 2 and out == ["We will build the portal.", WITHHELD, "We test it.", WITHHELD]


def test_a_single_fixture_name_or_an_approved_claim_is_withheld_too():
    from app.services.release import WITHHELD, _withhold_fixture_claims

    facts = {"Al Waha Capital": "ref"}
    out, n = _withhold_fixture_claims(
        ["We know Al Waha Capital.\n\nOur approved evidence covers this.\n\nWe test it."], facts
    )
    assert n == 2 and out == [WITHHELD, "We test it."]
