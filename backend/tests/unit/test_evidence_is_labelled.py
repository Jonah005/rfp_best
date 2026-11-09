"""A blank form is not proof, and the system already knew that.

Asked whether the company had a person connected to a customer, the copilot
answered from twelve approved evidence items covering "CV update forms for
roles like Director, Software Tester and Team Lead, and financial pack
templates". Blank forms, attached to a mandatory requirement about key
personnel, presented in the prompt exactly like a signed reference letter.

`Evidence.evidence_type` had been stamped on every extracted excerpt since
ingestion was written, from a taxonomy whose own comment spells out the
distinction: "PREVIOUS_TENDER is what a customer asked for; PREVIOUS_PROPOSAL
is what this company answered. Only the second is evidence of the company's own
capability." Nothing read it -- not the matcher, not the scorer, not the
renderer, not the prompt. A signal recorded and never consulted is the same
bug as [R#] extracted with nowhere to land, and it produces the same class of
failure: a confident sentence with nothing behind it.

The fix is labelling, not exclusion. The closest material a company holds is
worth showing a drafter, and hiding it would leave someone staring at an empty
screen wondering why. What must not happen is presenting it as proof.
"""

from __future__ import annotations

import pytest

from app.gateway.registry import get as get_call_type
from app.models.enums import NOT_SELF_EVIDENCING, DocumentType, is_self_evidencing


@pytest.mark.parametrize(
    "document_type",
    [
        DocumentType.STANDARD_FORM,
        DocumentType.FORM,
        DocumentType.PRICING_TEMPLATE,
        DocumentType.COMMERCIAL_TEMPLATE,
        DocumentType.RESPONSE_TEMPLATE,
        DocumentType.QUESTIONNAIRE,
    ],
    ids=lambda t: t.value,
)
def test_a_blank_form_cannot_prove_a_capability(document_type: DocumentType):
    assert not is_self_evidencing(document_type)


def test_a_customers_old_tender_is_not_this_companys_record():
    """The taxonomy separates PREVIOUS_TENDER from PREVIOUS_PROPOSAL precisely
    so that a customer's wish list cannot be mined as capability."""
    assert not is_self_evidencing(DocumentType.PREVIOUS_TENDER)
    assert is_self_evidencing(DocumentType.PREVIOUS_PROPOSAL)


@pytest.mark.parametrize(
    "document_type",
    [
        DocumentType.CV,
        DocumentType.REFERENCE_LETTER,
        DocumentType.COMPLETION_CERTIFICATE,
        DocumentType.CONTRACT,
        DocumentType.CASE_STUDY,
        DocumentType.COMPLETED_QUESTIONNAIRE,
    ],
    ids=lambda t: t.value,
)
def test_real_evidence_is_not_swept_up_with_the_templates(document_type: DocumentType):
    """Over-broad exclusion would be the worse bug: a company with genuine
    proof would read as having none, and nobody would know why."""
    assert is_self_evidencing(document_type)


def test_an_unclassified_document_is_treated_as_evidence():
    """A type nobody has classified yet must not be silently downgraded --
    that would hide real material behind a classification gap."""
    assert is_self_evidencing(None)
    assert is_self_evidencing("SOME_FUTURE_TYPE")


class _Status:
    value = "CURRENT_VERIFIED"


class _Evidence:
    """Enough Evidence to format one line."""

    status = _Status()
    expiry_date = None
    excerpt = "Name: ______   Role: Software Tester"

    def __init__(self, evidence_type, *, is_substantiated=None, substantiation_note=None):
        self.evidence_type = evidence_type
        # Defaults mirror the column: NULL means nobody has judged this row.
        # The double carries the fields rather than the renderer reaching them
        # through getattr -- a default there would keep these tests green
        # through a rename of the real column, which is cover that is counted
        # and cannot fail.
        self.is_substantiated = is_substantiated
        self.substantiation_note = substantiation_note


def test_the_rendered_line_names_the_document_type():
    """The renderer showed status, expiry and role, and never the one field
    that distinguishes a template from a certificate.

    Asserted on the rendered string, not by grepping the function. The first
    version of this test read the source for a field name and kept passing
    after the field was dropped from the output -- cover that cannot fail is
    worse than none, because it is counted.
    """
    from app.services.context_builder import evidence_line

    line = evidence_line(1, _Evidence("REFERENCE_LETTER"), "PRIMARY")
    assert "[E1]" in line
    assert "REFERENCE_LETTER" in line


def test_a_template_is_marked_as_one_in_the_line_the_model_reads():
    from app.services.context_builder import evidence_line

    line = evidence_line(1, _Evidence("STANDARD_FORM"), "PRIMARY")
    assert "STANDARD_FORM" in line
    assert "BLANK FORM OR TEMPLATE" in line
    assert "not that the company has done anything" in line


def test_real_evidence_carries_no_warning():
    """The caveat has to be rare to be read. Attaching it to a signed
    reference letter would train people to skip it."""
    from app.services.context_builder import evidence_line

    line = evidence_line(1, _Evidence("COMPLETION_CERTIFICATE"), "PRIMARY")
    assert "BLANK FORM" not in line


def test_an_unclassified_excerpt_still_renders():
    """A missing type must not produce "[E1] (None, ..." or crash the whole
    context build on one unclassified row."""
    from app.services.context_builder import evidence_line

    line = evidence_line(1, _Evidence(None), "PRIMARY")
    assert "UNCLASSIFIED" in line
    assert "BLANK FORM" not in line


def test_a_template_is_never_admitted_unread():
    """A blank form shares its whole vocabulary with the requirement it was
    designed to answer, so it clears the lexical rule more easily than real
    evidence does. "Certain" is the wrong word for it: it is the case most in
    need of reading, and it was the case that skipped the reading."""
    import inspect

    from app.services import assessment

    source = inspect.getsource(assessment._needs_reading)
    assert "is_self_evidencing" in source


def test_the_prompt_says_a_form_is_not_proof():
    prompt = get_call_type("task_copilot").system_prompt.lower()
    assert "template" in prompt
    assert "shape of an answer" in prompt


def test_the_exclusion_set_is_not_silently_empty():
    """A frozenset that lost its contents would disable every check above
    while every other test kept passing."""
    assert len(NOT_SELF_EVIDENCING) >= 6


def test_a_register_row_about_a_real_certificate_is_still_flagged():
    """The case the type-based set cannot reach.

    A certification-register row naming ISO 27001 is correctly typed
    CERTIFICATE -- it IS about a certificate, and typing it FORM to force a
    downgrade would drop "ISO 27001" out of every shortlist that should surface
    it. `is_self_evidencing(CERTIFICATE)` is therefore True, and the drafter
    must still be told the certificate does not exist yet.
    """
    from app.services.context_builder import evidence_line

    line = evidence_line(
        1,
        _Evidence(
            "CERTIFICATE",
            is_substantiated=False,
            substantiation_note="Not substantiated: the register marks this credential "
            "NOT EVIDENCED.",
        ),
        "PRIMARY",
    )
    assert "CERTIFICATE" in line
    # Not the template caveat -- this is not a template, and saying so would be
    # false about what the row is.
    assert "BLANK FORM OR TEMPLATE" not in line
    assert "NOT SUBSTANTIATED" in line
    # The reason is quoted, because it names what somebody has to go and get.
    assert "NOT EVIDENCED" in line


def test_an_unjudged_row_is_not_called_a_placeholder():
    """NULL is not False.

    Every one of the 610 existing rows is NULL until a classifier reads it.
    Rendering those as unsubstantiated would put the warning on everything,
    and a warning on everything is read as decoration.
    """
    from app.services.context_builder import evidence_line

    line = evidence_line(1, _Evidence("REFERENCE_LETTER"), "PRIMARY")
    assert "NOT SUBSTANTIATED" not in line
