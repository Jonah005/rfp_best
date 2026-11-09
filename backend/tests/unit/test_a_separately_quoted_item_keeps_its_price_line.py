"""'(Qoute Separate)' is a commercial instruction, and it was being dropped.

The live tender heads two clauses "Signature System (Qoute Separate)" and
"Document Management System (Qoute Separate)". Both reached the requirement
matrix without the qualifier, which is how a separately-quoted system ends up
inside somebody's fixed price. The marker is read from the clause, not asked of
the model: it is one phrase, it is easy to miss, and missing it costs money.
"""

from __future__ import annotations

from app.services.requirements import priced_separately


class _Block:
    def __init__(self, text, heading_path=None):
        self.text = text
        self.heading_path = heading_path or []


def test_the_marker_is_found_however_the_tender_spells_it():
    for text in (
        "6. Signature System (Qoute Separate) Signature system is developed in house.",
        "Document Management System (Quote Separate)",
        "This module must be quoted separately.",
        "Provide a separate quote for the integration.",
        "Training is priced separately.",
        "Show this as a separate line item.",
    ):
        assert priced_separately(_Block(text)), text


def test_the_marker_is_found_in_the_clause_heading():
    block = _Block(
        "Upload client signature card document in Oracle database with Client ID",
        ["Integration Requirements", "6. Signature System (Qoute Separate)"],
    )
    assert priced_separately(block)


def test_ordinary_clauses_are_not_flagged():
    for text in (
        "The portal should support SSL configuration.",
        "Separate the admin site under an admin folder.",
        "Quote the overall fixed price for the project.",
    ):
        assert not priced_separately(_Block(text)), text
