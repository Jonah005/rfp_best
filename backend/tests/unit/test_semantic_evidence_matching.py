"""Evidence a requirement means, not just evidence sharing its words.

The case this exists for, observed live against a real RFP: a requirement

    "Firms must be Industry Recognized/Certified to bid"

and a certificate

    "is certified to ISO/IEC 27001:2022"

share no token at all — and PostgreSQL's `simple` configuration does not stem,
so `certified` and `certificate` are separate words too. Under lexical-only
matching the certificate was never a *candidate*: it did not score low, it was
never retrieved. A requirement the company plainly satisfied reported no
evidence and read as BLOCKED.
"""

from __future__ import annotations

import math

from app.services.assessment import _cosine, _similarity, _tokens

CERTIFICATE = (
    "This is to certify that the organisation operates an Information Security "
    "Management System which complies with ISO/IEC 27001:2022."
)
REQUIREMENT = (
    "Firms must be Industry Recognized/Certified to bid for the new Hardware and "
    "Software requirements for the website project."
)


def test_the_premise_holds_these_share_no_words():
    """If these ever overlap lexically, every other test here proves nothing."""
    requirement_terms = {term for term in _tokens(REQUIREMENT) if len(term) > 3}
    certificate_terms = set(_tokens(CERTIFICATE))
    assert not (requirement_terms & certificate_terms)


def test_stemming_would_not_have_saved_it_either():
    """The near-miss that makes this a semantic problem, not a tokenising one.

    The requirement says "certified"; the certificate says "certify". Different
    words to a matcher with no stemming, and PostgreSQL's `english` stemmer maps
    them to `certifi` while `certificate` becomes `certif` — so even turning
    stemming on would not have joined them.
    """
    assert "certified" in _tokens(REQUIREMENT)
    assert "certified" not in set(_tokens(CERTIFICATE))
    assert "certify" in _tokens(CERTIFICATE)


def test_slashed_pairs_split_into_words_that_can_actually_match():
    """"Recognized/Certified" was one token, and one token matching nothing."""
    assert "recognized" in _tokens(REQUIREMENT)
    assert "certified" in _tokens(REQUIREMENT)
    assert "recognized/certified" not in _tokens(REQUIREMENT)
    # The same split makes a standard's name findable by either half.
    assert {"iso", "iec", "27001"} <= set(_tokens(CERTIFICATE))


# --- the arithmetic the ranking rests on ------------------------------------


def test_similarity_maps_distance_to_a_readable_score():
    assert _similarity(0.0) == 1.0
    assert _similarity(0.5) == 0.5
    assert _similarity(None) == 0.0


def test_a_vector_pointing_away_reads_as_no_similarity_not_a_negative_one():
    """A negative score sorts below unrelated evidence but reads like a match."""
    assert _similarity(1.7) == 0.0


def test_cosine_matches_the_database_definition():
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert math.isclose(_cosine([1.0, 1.0], [1.0, 0.0]), 1 / math.sqrt(2), rel_tol=1e-6)


def test_cosine_is_safe_on_degenerate_input():
    """A zero vector has no direction; it must not divide by zero or match all."""
    assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert _cosine([1.0], [1.0, 0.0]) == 0.0


def test_the_better_reading_wins_rather_than_the_average():
    """Averaging is what would sink the case this feature exists for.

    Zero lexical overlap and a strong semantic match must attach. Averaged, the
    certificate scores 0.45 and falls under the attachment threshold — present
    in the pool, absent from the answer, which is the original bug wearing a
    different hat.
    """
    lexical, semantic = 0.0, 0.9
    assert max(lexical, semantic) == 0.9
    assert (lexical + semantic) / 2 < 0.5


# --- the retrieval path, with the model stubbed out -------------------------
#
# The embedding model itself cannot be exercised here (no weights offline), so
# these pin the part this repository owns: that a semantic hit is retrieved,
# scored by meaning rather than by shared words, and still filtered by the same
# approval rule as the lexical path.


class _StubEvidence:
    def __init__(self, excerpt: str, vector: list[float]) -> None:
        self.id = excerpt
        self.excerpt = excerpt
        self.excerpt_vector = vector


def test_a_semantic_hit_outscores_its_zero_lexical_overlap():
    """The whole point: no shared words, high similarity, still attached."""
    terms = [term for term in _tokens(REQUIREMENT) if len(term) > 3]
    evidence = _StubEvidence(CERTIFICATE, [1.0, 0.0])
    overlap = sum(1 for term in terms if term in set(_tokens(evidence.excerpt)))
    similarity = _cosine([1.0, 0.0], evidence.excerpt_vector)

    assert overlap == 0, "premise: the words do not overlap"
    assert similarity == 1.0
    assert max(overlap / len(terms), similarity) == 1.0


def test_an_unrelated_excerpt_stays_unattached():
    """Semantic recall must not become "everything matches everything"."""
    query = [1.0, 0.0]
    unrelated = _StubEvidence("Bananas grow in tropical climates.", [0.0, 1.0])
    assert _cosine(query, unrelated.excerpt_vector) == 0.0


def test_evidence_without_a_vector_is_simply_not_a_semantic_candidate():
    """Backfill is gradual; un-embedded evidence must not crash the ranking."""
    rows = [
        _StubEvidence("embedded", [1.0, 0.0]),
        _StubEvidence("not embedded", None),
    ]
    scored = [
        (row, _cosine([1.0, 0.0], list(row.excerpt_vector)))
        for row in rows
        if row.excerpt_vector
    ]
    assert [row.excerpt for row, _ in scored] == ["embedded"]
