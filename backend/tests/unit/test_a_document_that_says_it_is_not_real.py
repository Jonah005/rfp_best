"""A disclaimer on page one is about the document, not about page one.

A test pack was uploaded whose every file opened with:

    FICTIONAL TEST FIXTURE. TTS Technology Solutions LLC does not exist. The
    people, customers, certificate numbers and registrar named here are
    invented.

`evidence_markers` reads one excerpt at a time, which is right for a register
whose rows declare their own status. Here it marked the one excerpt carrying
the sentence and left the rest of the file alone -- clean paragraphs about
encryption, load testing and bilingual portals -- so R-019 went READY on seven
of them.

The shape is not peculiar to test fixtures. Vendors circulate specimen
certificates, sample proposals, training copies and "for illustration only"
rate cards, and each says so once, at the top. A corpus that mines page four of
a specimen certificate as proof of certification is the failure this product
exists to prevent.

So the scope of the finding follows the scope of the claim: one declaration
anywhere in a document version marks every excerpt drawn from it.
"""

from __future__ import annotations

import inspect

from app.services import demonstration_documents as dd

DECLARED = [
    "FICTIONAL TEST FIXTURE. TTS Technology Solutions LLC does not exist. The people, "
    "customers, certificate numbers and registrar named here are invented, and this "
    "document is not a record of any real organisation.",
    "This is a specimen certificate issued for illustration purposes only.",
    "Sample proposal - not for use in real bids.",
    "This pack contains only dummy data.",
    "A fictional company used in our onboarding course.",
]

REAL = [
    "AES-256 column encryption for identity numbers, dates of birth and bank account "
    "numbers. Keys are held in a hardware-backed key vault.",
    "At 300 concurrent hits and sessions no request returned a server busy, overloaded "
    "or timeout response.",
    "ISO/IEC 27001:2022 Information Security Management, certificate IS-2023-41188, "
    "expires 8 May 2026.",
    # The near misses. A real document may discuss test data or demonstrations
    # without being a specimen itself, and a rule that cannot tell the
    # difference disqualifies working evidence.
    "Testing is conducted against a seeded database of sample records so that no "
    "customer data leaves production.",
    "We gave a demonstration of the portal to the client's steering committee in March.",
    "The training environment is refreshed nightly from an anonymised extract.",
]


def test_a_document_that_disowns_itself_is_recognised():
    for text in DECLARED:
        assert dd.declaration_in(text) is not None, text


def test_a_real_document_that_merely_mentions_testing_is_not():
    for text in REAL:
        assert dd.declaration_in(text) is None, text


def test_the_whole_version_is_marked_not_just_the_declaring_excerpt():
    """The bug, stated as the rule that fixes it."""
    source = inspect.getsource(dd.mark_version)
    # Chunks are searched; evidence is updated without a text filter on it.
    assert "DocumentChunk.text" in source
    assert "Evidence.source_document_version_id == version_id" in source
    assert "Evidence.excerpt" not in source


def test_it_reads_the_chunks_rather_than_the_excerpts():
    """Whether an excerpt happened to be drawn from the first paragraph is an
    accident of chunking, and the declaration is usually in the first
    paragraph."""
    assert "Reads the CHUNKS, not the excerpts" in (dd.mark_version.__doc__ or "")


def test_it_only_ever_writes_the_safe_value():
    source = inspect.getsource(dd.mark_version)
    assert "is_substantiated=False" in source
    assert "is_substantiated=True" not in source


def test_a_human_judgement_outranks_it():
    source = inspect.getsource(dd.mark_version)
    assert 'startswith("human:")' in source


def test_the_verdict_names_the_reading_that_made_it():
    assert dd.MARKER_VERSION.startswith("code:demonstration_documents@")
    assert "classified_by=MARKER_VERSION" in inspect.getsource(dd.mark_version)


def test_the_note_says_what_is_wrong_in_words_a_person_can_act_on():
    reason = dd.REASON.format(kind="fictional")
    assert "states of itself" in reason
    assert "proves anything" in reason


def test_it_runs_at_ingestion_after_extraction():
    """It marks the excerpts extraction has just produced, so it cannot run
    before them."""
    from app.services import ingestion

    source = inspect.getsource(ingestion.parse_and_index)
    assert "demonstration_documents.mark_version(" in source
    assert source.index("extract_company_knowledge(") < source.index(
        "demonstration_documents.mark_version("
    )


def test_nothing_is_withdrawn_or_hidden():
    """A demonstration document is still the right thing to read when writing a
    first draft. What changes is that it cannot back a claim."""
    source = inspect.getsource(dd.mark_version)
    for forbidden in ("withdrawn_at", "delete(", "SUPERSEDED"):
        assert forbidden not in source


def test_evidence_that_cannot_prove_anything_is_not_draftable():
    """Twelve requirements read DRAFTABLE on shortlists where every item was
    marked "the document states of itself that it is fictional material".

    DRAFTABLE says there is something to write from. There was something to
    READ, which is a different thing. What happens next is that a person
    decides whether the company can claim this at all.
    """
    from app.services import assessment

    source = inspect.getsource(assessment._everything_attached_is_disqualified)
    # `is False`, never falsy: an unjudged row is not a disqualified one, the
    # same rule that stops silence being read as a finding.
    assert "c.evidence.is_substantiated is False" in source
    assert "bool(candidates) and all(" in source

    applied = inspect.getsource(assessment.assess_requirement)
    assert "_everything_attached_is_disqualified(candidates)" in applied
    assert "BidReadiness.NEEDS_COMPANY_CONFIRMATION" in applied
    # An open customer question still outranks it.
    assert "readiness not in _READINESS_FLOOR" in applied


def test_a_fact_cannot_cite_evidence_that_proves_nothing():
    """The payload already told the model `can_prove_capability` per source and
    the prompt already said never to cite a false one for a fact. Both were
    true while a requirement went READY on seven excerpts from a document that
    calls itself fictional. An instruction a model can ignore is not a control.

    Reclassified, not dropped: what the company would have to DO to make the
    sentence true is exactly a PROPOSED_COMMITMENT, which needs a named
    approver and is never presented as a fact about the company.
    """
    from app.services import drafting

    # The rule lives in `map_proposals`, which the first draft and a diagram
    # write-up both go through; `generate` must not have a way around it.
    assert "map_proposals(" in inspect.getsource(drafting.generate)
    source = inspect.getsource(drafting.map_proposals)
    assert "unprovable = [" in source
    assert "is_self_evidencing(e.evidence_type) and e.is_substantiated is not False" in source
    assert "StatementClass.PROPOSED_COMMITMENT.value" in source
    assert "required_approver_role" in source
    # And the reader is told why the sentence changed shape.
    assert "COMMITMENT_NOTE" in source
    assert "Written as a commitment, not as a fact" in drafting.COMMITMENT_NOTE


def test_a_fact_with_no_citation_at_all_is_also_reclassified():
    from app.services import drafting

    source = inspect.getsource(drafting.map_proposals)
    assert "if unprovable or not cited:" in source


def test_a_signature_requirement_is_not_waiting_on_proof():
    """REQUIRES_SIGNATURE does not mean "the evidence proves this".

    It means the requirement is satisfied by someone signing the submission,
    and no excerpt was ever going to be the thing that satisfies it. The
    disqualification rule overrode it on its first run and sent R-009 --
    proposals must be signed by an authorised representative -- to
    NEEDS_COMPANY_CONFIRMATION, asking a person to decide whether the company
    can claim something it does not need to claim.
    """
    from app.services import assessment

    source = inspect.getsource(assessment.assess_requirement)
    assert "_NOT_WAITING_ON_PROOF = _READINESS_FLOOR | {BidReadiness.REQUIRES_SIGNATURE}" in source
    assert "readiness not in _NOT_WAITING_ON_PROOF" in source
