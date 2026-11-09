"""The company-side filing rules, which are about meaning rather than tidiness.

Two of these distinctions do real work:

* A tender-side label on a company upload would file a customer's document as
  company knowledge, where the retrieval layer could mine a customer's old wish
  list as though it were this company's own capability.
* PREVIOUS_TENDER and PREVIOUS_PROPOSAL are separate kinds for the same reason.
  The tender records what was asked; only the response records what was offered.
"""

from __future__ import annotations

import pytest

from app.api.v1.knowledge import TENDER_ONLY_TYPES, _company_document_type
from app.core.errors import ValidationError
from app.models.enums import DocumentType


@pytest.mark.parametrize(
    "label",
    ["RFP", "RFQ", "AMENDMENT", "BOQ", "TERMS_AND_CONDITIONS", "QUESTIONNAIRE"],
)
def test_a_tender_label_is_refused_on_the_company_side(label):
    with pytest.raises(ValidationError) as exc:
        _company_document_type(label)
    assert "tender document" in str(exc.value)


def test_a_past_tender_is_allowed_because_history_is_company_material():
    """Refusing this would leave nowhere to file the other half of a bid pair."""
    assert _company_document_type("PREVIOUS_TENDER") is DocumentType.PREVIOUS_TENDER


def test_what_was_asked_and_what_was_offered_are_different_kinds():
    asked = _company_document_type("PREVIOUS_TENDER")
    offered = _company_document_type("PREVIOUS_PROPOSAL")
    assert asked is not offered


def test_an_unknown_label_is_refused_rather_than_stored():
    with pytest.raises(ValidationError) as exc:
        _company_document_type("SOMETHING_INVENTED")
    assert "not a document type" in str(exc.value)


def test_no_type_at_all_is_allowed():
    """Classification stays optional: an unfiled document is still ingestible."""
    assert _company_document_type(None) is None
    assert _company_document_type("") is None


def test_every_tender_only_label_really_is_a_document_type():
    """Guards against a typo silently making the refusal list shorter."""
    for document_type in TENDER_ONLY_TYPES:
        assert isinstance(document_type, DocumentType)


@pytest.mark.parametrize(
    "label",
    [
        "TRADE_LICENCE",
        "INCORPORATION_CERTIFICATE",
        "SIGNATORY_AUTHORITY",
        "INSURANCE_CERTIFICATE",
        "ACCREDITATION",
        "PROFESSIONAL_CERTIFICATE",
        "SLA_DOC",
        "ESCALATION_MATRIX",
        "COMPLETION_CERTIFICATE",
        "REFERENCE_LETTER",
        "FINANCIAL_STATEMENT",
        "BANK_LETTER",
        "RATE_CARD",
        "COMPLETED_QUESTIONNAIRE",
    ],
)
def test_the_twelve_categories_are_all_accepted(label):
    """Every kind the upload screen offers must be storable."""
    assert _company_document_type(label) is DocumentType(label)


# --- correcting a document already on file ---------------------------------
#
# A bulk upload whose folders matched nothing leaves everything ingested but
# unclassified, and the checklist then reads "not provided" for all 44 kinds
# while the corpus sits there fully uploaded. Without in-place correction the
# only remedy is to upload every file again, duplicating the corpus to fix a
# label.


def test_classify_accepts_the_same_kinds_as_upload():
    """The correction path must not be narrower than the upload path."""
    for label in ("CERTIFICATE", "CV", "POLICY", "PREVIOUS_TENDER", "RATE_CARD"):
        assert _company_document_type(label) is DocumentType(label)


def test_classify_refuses_a_tender_label_too():
    """Otherwise the rule could be walked around by uploading then correcting."""
    with pytest.raises(ValidationError):
        _company_document_type("RFP")
