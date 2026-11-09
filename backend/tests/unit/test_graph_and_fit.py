"""The third retrieval path, and what the model is allowed to conclude from it.

Two additions are covered here. The knowledge graph gives retrieval an axis that
is not the requirement's vocabulary at all — extraction has already resolved the
corpus into typed entities, so a requirement naming CREST can walk to the
certificate that proves it without sharing a word with it. And the model, having
read the shortlist, writes the side-by-side comparison a bid manager actually
wants: what was asked, what the company has, what is missing.

Both are useful only if they stay inside their limits, which is what these
assert: the graph must not attach on a shared common word, and the comparison
must not become the verdict.
"""

from __future__ import annotations

import pytest

from app.models.enums import EvidenceStatus
from app.schemas.evidence_relevance import Coverage, RequirementFit
from app.services.assessment import (
    GRAPH_BASE_SCORE,
    MIN_ATTACH_SCORE,
    MIN_ENTITY_NAME_COVERAGE,
    MIN_SEMANTIC_SIMILARITY,
    _dissent,
    _tokens,
    names_entity,
)


def _requirement(text: str) -> set[str]:
    return set(_tokens(text))


# --- does the requirement name the entity? ----------------------------------


def test_an_entity_the_requirement_names_is_matched():
    assert names_entity("CREST", _requirement("Bidders must hold CREST accreditation"))


def test_a_multi_word_entity_needs_its_parts():
    present = _requirement("certified to ISO/IEC 27001:2022")
    assert names_entity("ISO 27001", present)


def test_a_generic_entity_does_not_attach_on_one_shared_word():
    """The failure that would make graph expansion worse than useless.

    "Information Security Policy" shares "security" with half a tender. Matching
    the generous way round would link it to every one of those requirements, and
    a structural link reads as more authoritative than a similarity score, not
    less.
    """
    present = _requirement("The supplier shall maintain security of the data centre")
    assert not names_entity("Information Security Policy", present)


def test_a_named_entity_is_matched_despite_decoration():
    """The stopword list exists so "certificate" is not what carries a match."""
    present = _requirement("Provide evidence of CREST accreditation")
    assert names_entity("CREST certificate", present)


def test_an_entity_named_only_by_stopwords_matches_nothing():
    assert not names_entity("The Company", _requirement("the company shall provide"))


def test_the_coverage_bar_admits_a_partial_phrase_but_not_a_single_word():
    """Between 1.0 (too strict for real names) and low (matches on one word)."""
    assert 0.5 < MIN_ENTITY_NAME_COVERAGE < 1.0


# --- where a graph hit sits in the ranking ----------------------------------


def test_a_graph_hit_clears_the_attachment_floor():
    """A stored link is a reason to attach on its own."""
    assert GRAPH_BASE_SCORE > MIN_ATTACH_SCORE


def test_a_strong_paraphrase_still_outranks_a_structural_link():
    """Evidence that answers the requirement beats evidence merely connected."""
    assert GRAPH_BASE_SCORE < MIN_SEMANTIC_SIMILARITY


# --- the comparison, and the limits on it -----------------------------------


def _fit(coverage: Coverage, gap: str = "no CREST scope for web applications"):
    return RequirementFit(asked="CREST", offered="ISO 27001", gap=gap, coverage=coverage)


def test_agreement_produces_no_dissent():
    assert _dissent(_fit(Coverage.FULLY_COVERED), EvidenceStatus.CURRENT_VERIFIED) is None


def test_disagreement_is_surfaced_with_the_reason():
    """The most informative thing in a run: arithmetic says covered, the reader
    says the excerpts are about something else."""
    message = _dissent(_fit(Coverage.NOT_COVERED), EvidenceStatus.CURRENT_VERIFIED)
    assert message is not None
    assert "not covered" in message
    assert "CREST scope" in message


def test_expiry_is_not_treated_as_a_disagreement():
    """The model is never shown dates and is told not to judge currency, so its
    silence about an expired certificate is not dissent."""
    assert _dissent(_fit(Coverage.FULLY_COVERED), EvidenceStatus.EXPIRED) is None


def test_no_comparison_means_no_dissent():
    assert _dissent(None, EvidenceStatus.NONE) is None


def test_a_dissent_without_detail_still_reads_as_a_sentence():
    message = _dissent(
        RequirementFit(coverage=Coverage.NOT_COVERED), EvidenceStatus.CURRENT_VERIFIED
    )
    assert message is not None and message.endswith(".")


@pytest.mark.parametrize(
    "status",
    [EvidenceStatus.PARTIAL, EvidenceStatus.CONFLICTING],
)
def test_partial_statuses_agree_with_partial_coverage(status):
    assert _dissent(_fit(Coverage.PARTIALLY_COVERED), status) is None


def test_coverage_is_not_the_evidence_status_vocabulary():
    """The model must not be able to answer in terms it cannot check.

    EXPIRED and HISTORICAL_VERIFIED are conclusions about dates it is never
    shown; offering them as answers would put a model opinion about currency in
    a field computed from `expiry_date`.
    """
    assert {member.value for member in Coverage}.isdisjoint(
        {member.value for member in EvidenceStatus}
    )
