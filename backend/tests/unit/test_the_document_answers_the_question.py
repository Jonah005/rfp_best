"""A question the tender itself answers is not asked.

`analyze_ambiguity` shows the model ten blocks at a time. Told it can only see
its own window, it reports what it cannot find -- faithfully, and often about
material sitting a few pages further on. The severity rule already stops such a
claim from BLOCKING, but a MAJOR clarification is still a question somebody has
to put to the customer, and on the live Jadwa run two of the fifty-nine open
findings were of exactly this kind:

    "The Submission Guidelines & Requirements section is introduced but the
     actual submission instructions are not present in the visible text."

    "The table of contents lists numerous Business, Integration and Technical
     requirements (e.g. Al-Elm Services, Tradenet Core OMS, B2B Service, SSL
     Certificates, Load Test Support) but the text is not present."

Both name what they say is missing, and both name things the document contains.
So the check is deterministic and needs no second model call: take the terms the
description itself quotes, lists or capitalises, and look for them in the blocks
this call could NOT see. Found there, the claim is about a window and the
finding is dropped.

Text inside the same batch never counts. The model read that; if it still said
something was missing it meant something else, and that finding stands.
"""

from __future__ import annotations

from app.models.enums import ClarificationIssueType
from app.services.tender_analysis import (
    ABSENCE_TYPES,
    _absence_is_refuted,
    _absence_subjects,
    _normalize,
)

SUBMISSION = (
    "The Submission Guidelines & Requirements section is introduced but the actual "
    "submission instructions are not present in the visible text."
)
TOC = (
    "The table of contents lists numerous Business, Integration and Technical "
    "requirements (e.g. Al-Elm Services, Tradenet Core OMS, B2B Service, "
    "SSL Certificates, Load Test Support) but the text is not present."
)


def test_the_subject_of_an_absence_claim_is_read_from_its_own_words():
    assert "submission guidelines & requirements" in _absence_subjects(SUBMISSION)
    listed = _absence_subjects(TOC)
    assert "al-elm services" in listed
    assert "tradenet core oms" in listed


def test_a_single_word_is_never_a_subject():
    """"Email" or "SSL" matches half a tender by accident. Dropping a real
    question on that would be worse than keeping a false one."""
    subjects = _absence_subjects(
        'The list mentions "SSL" and Email but the detail is not present.'
    )
    assert all(len(s.split()) >= 2 for s in subjects)


def test_a_claim_is_dropped_when_the_rest_of_the_document_holds_what_it_names():
    unseen = _normalize(
        "5. Submission Guidelines & Requirements. Bids shall be delivered in two "
        "sealed envelopes to the address in clause 1.2 before the deadline."
    )
    refuted, found = _absence_is_refuted(SUBMISSION, unseen_text=unseen)
    assert refuted
    assert "submission guidelines & requirements" in found


def test_half_a_list_found_elsewhere_refutes_the_list():
    unseen = _normalize(
        "Al-Elm Services integration shall use the Tradenet Core OMS adapter. "
        "The B2B Service is exposed over TLS."
    )
    refuted, _ = _absence_is_refuted(TOC, unseen_text=unseen)
    assert refuted


def test_a_claim_stands_when_the_document_really_is_silent():
    unseen = _normalize(
        "Annex C - Price Schedule. Bidders shall complete every line of the table below."
    )
    refuted, found = _absence_is_refuted(SUBMISSION, unseen_text=unseen)
    assert not refuted
    assert found == []


def test_material_inside_the_same_batch_does_not_refute_anything():
    """The caller passes only the text this call could not see. A term the
    model read and still called missing is not this rule's business."""
    refuted, _ = _absence_is_refuted(SUBMISSION, unseen_text="")
    assert not refuted


def test_a_finding_that_does_not_claim_absence_is_left_alone():
    """Contradictions and undefined fields describe text in view. They are
    checkable by a reader and must survive whatever words they use."""
    contradiction = (
        "Clause 4.2 gives the Submission Guidelines & Requirements deadline as "
        "20 March while clause 1.1 gives 30 March."
    )
    refuted, _ = _absence_is_refuted(
        contradiction,
        unseen_text=_normalize("Submission Guidelines & Requirements ... 30 March"),
    )
    assert not refuted


def test_only_the_two_absence_types_are_filtered():
    assert set(ABSENCE_TYPES) == {
        ClarificationIssueType.BLANK,
        ClarificationIssueType.INCOMPLETE_SCOPE,
    }


def test_the_service_applies_the_rule():
    """Pins the policy tests above to the code that runs: the filter must sit in
    the batch loop, and before the fingerprint is recorded, so a false finding
    raised by an earlier run is one this run does not repeat and the retirement
    pass closes it."""
    import inspect

    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.analyze_ambiguity)
    assert "_absence_is_refuted(" in source
    assert source.index("_absence_is_refuted(") < source.index("seen_fingerprints.add(")
    assert "unseen_text=unseen_text" in source


def test_a_plural_does_not_make_a_second_submission():
    """"Signed proposal by authorized representative" and "Signed proposals by
    an authorized representative" are one signature. The fingerprint used the
    name as typed, so the live export listed both, and a submission checklist
    that asks for the same document twice cannot tell a bid manager which line
    is the real obligation."""
    from app.services.tender_analysis import _deliverable_key

    assert _deliverable_key("Signed proposal by authorized representative") == _deliverable_key(
        "Signed proposals by an authorized representative"
    )
    # Still distinguishes deliverables that differ in substance.
    assert _deliverable_key("Technical proposal") != _deliverable_key("Financial proposal")
    assert _deliverable_key("") == ""


def test_the_deliverable_fingerprint_uses_that_key():
    import inspect

    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.extract_deliverables)
    assert "_deliverable_key(proposal.name)" in source


def test_a_finding_can_be_linked_by_the_codes_it_names():
    """Twelve open questions reached no row at all.

    `concerned_requirements` bounds the model to requirements drawn from the
    blocks the finding CITES -- and when the cited block is a heading, a
    contents entry or a preamble, it produced none, so there was nothing to
    link and the question was invisible from every matrix row. Most of those
    findings named their requirements in the description: "the penetration
    testing requirement (R-072) does not state...".

    A named code is the model's own explicit answer to what the finding is
    about. It is checked against the bid's live requirements before anything is
    linked, so a code that does not exist links nothing.
    """
    from app.services.tender_analysis import codes_named_in

    named = codes_named_in(
        "The penetration testing requirement (R-072) and r-53 leave the scope open."
    )
    assert named == ["R-072", "R-053"], "zero-padded and de-duplicated, whatever the spelling"
    assert codes_named_in("No codes here at all") == []
    # Not every R- is a requirement code.
    assert codes_named_in("RFP-2017 and R2D2 and R-") == []


def test_the_fallback_only_runs_when_nothing_else_linked():
    """The bounded path stays the primary one: a code named in prose is weaker
    evidence than a requirement drawn from the block the finding cites."""
    import inspect

    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.analyze_ambiguity)
    assert "if not linked_ids:" in source
    assert source.index("concerned_requirements(candidates") < source.index("if not linked_ids:")
    # A hallucinated code links nothing.
    assert "if code in code_index" in source
    assert "linked_by_the_codes_they_name=linked_by_name" in source
