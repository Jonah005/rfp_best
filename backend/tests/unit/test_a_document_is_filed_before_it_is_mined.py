"""Typing a company document, so an excerpt can say what it is.

Measured on the live corpus: 610 evidence records, every one `OTHER`. The
copilot is told to treat a blank form as the shape of an answer rather than
proof of anything -- a rule it could not apply, because nothing had ever
written a type. `evidence_type` was stamped from the document's type, and the
document's type was only ever set when a person picked one at upload.
"""

from __future__ import annotations

from app.gateway.registry import get
from app.models.enums import DocumentType, is_self_evidencing
from app.services.document_classification import COMPANY_TYPES, _type_for


def test_an_invented_type_becomes_other():
    assert _type_for("PROCUREMENT_FILE") is DocumentType.OTHER
    assert _type_for("") is DocumentType.OTHER


def test_a_type_is_read_however_it_is_written():
    assert _type_for("reference letter") is DocumentType.REFERENCE_LETTER
    assert _type_for("Previous-Proposal") is DocumentType.PREVIOUS_PROPOSAL


def test_the_customers_own_document_types_are_never_proposed():
    """A company file typed RFP would be read as a customer's wish list."""
    codes = {t.value for t in COMPANY_TYPES}
    assert "RFP" not in codes and "BOQ" not in codes and "TECHNICAL_SPECIFICATION" not in codes
    assert "CV" in codes and "REFERENCE_LETTER" in codes and "STANDARD_FORM" in codes


def test_the_types_that_prove_nothing_stay_that_way():
    """The whole point of typing: a form is not a record of anything done."""
    assert not is_self_evidencing(DocumentType.STANDARD_FORM)
    assert not is_self_evidencing(DocumentType.PREVIOUS_TENDER)
    assert is_self_evidencing(DocumentType.REFERENCE_LETTER)
    assert is_self_evidencing(DocumentType.PREVIOUS_PROPOSAL)


def test_the_prompt_names_the_distinctions_that_change_what_can_be_claimed():
    prompt = get("document_classification").system_prompt
    assert "blank or unfilled template" in prompt
    assert "PREVIOUS_TENDER" in prompt and "PREVIOUS_PROPOSAL" in prompt
    assert "return OTHER with a low" in prompt, "guessing must be the worse option"


def test_the_drafting_call_is_told_what_each_source_is():
    """Typing the corpus is pointless if the call that writes to the customer
    never sees the type."""
    prompt = get("draft_statement_generation").system_prompt
    assert "can_prove_capability" in prompt
    assert "never cite it for an EVIDENCE_BACKED_FACT" in prompt


def test_the_decomposition_call_knows_what_a_matrix_row_is():
    prompt = get("requirement_decomposition").system_prompt
    assert "table_header" in prompt
    assert "compliance" in prompt.lower()
    assert "no_requirement_block_ordinals" in prompt
