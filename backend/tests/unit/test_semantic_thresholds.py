"""Two scores that are not the same kind of thing must not share a threshold.

Observed live, and the worst failure this system can produce. A single
`MIN_ATTACH_SCORE = 0.05` was applied to both a lexical overlap fraction and a
cosine similarity. The first is genuinely weak at 0.05; the second is not — a
sentence encoder puts two unrelated business documents around 0.75, so the floor
admitted everything.

Every requirement in a real bid came back with the same eight excerpts and read
"current verified / ready", including "proposals become the property of the
customer" — which no company document can evidence. A matrix that claims
everything is proven is more dangerous than one that finds nothing: the product
exists to make a claim defensible, and this made every claim look defended.
"""

from __future__ import annotations

from app.services.assessment import (
    MAX_SEMANTIC_ATTACHMENTS,
    MIN_ATTACH_SCORE,
    MIN_SEMANTIC_SIMILARITY,
)


def test_the_semantic_bar_is_far_above_the_lexical_one():
    """The bug in one assertion: these cannot be the same number."""
    assert MIN_SEMANTIC_SIMILARITY > MIN_ATTACH_SCORE
    assert MIN_SEMANTIC_SIMILARITY >= 0.8, (
        "unrelated business text sits near 0.75 cosine; a lower bar admits everything"
    )


def test_typical_unrelated_similarity_is_rejected():
    """The number that caused the false positives, checked directly."""
    unrelated_similarity = 0.75
    assert unrelated_similarity < MIN_SEMANTIC_SIMILARITY


def test_a_lexical_hit_is_still_admitted_on_a_weak_overlap():
    """Tightening the semantic bar must not silently break lexical recall."""
    shares_one_term_in_ten = 0.10
    assert shares_one_term_in_ten >= MIN_ATTACH_SCORE


def test_semantic_attachments_are_capped():
    """A threshold that drifts with a model change can only mislead a little."""
    assert 1 <= MAX_SEMANTIC_ATTACHMENTS <= 5


def test_the_cap_is_below_the_candidate_limit():
    """Otherwise the cap is decorative and one path can fill the whole answer."""
    from app.services.assessment import CANDIDATE_LIMIT

    assert MAX_SEMANTIC_ATTACHMENTS < CANDIDATE_LIMIT


# --- the admission rule itself ----------------------------------------------


def _admit(lexical: float, similarity: float, already_attached: int) -> bool:
    """The rule as `_candidates_for` applies it, isolated for testing."""
    lexical_ok = lexical >= MIN_ATTACH_SCORE
    semantic_ok = (
        similarity >= MIN_SEMANTIC_SIMILARITY and already_attached < MAX_SEMANTIC_ATTACHMENTS
    )
    return lexical_ok or semantic_ok


def test_unrelated_evidence_is_not_attached():
    """No shared words and ordinary similarity: this is the false positive."""
    assert not _admit(lexical=0.0, similarity=0.75, already_attached=0)


def test_a_genuine_paraphrase_is_attached():
    """The case semantic matching exists for: zero overlap, high similarity."""
    assert _admit(lexical=0.0, similarity=0.90, already_attached=0)


def test_a_lexical_hit_needs_no_semantic_support():
    """Sharing the requirement's words is its own reason to attach."""
    assert _admit(lexical=0.30, similarity=0.10, already_attached=0)


def test_the_semantic_cap_stops_admitting_past_its_limit():
    assert not _admit(
        lexical=0.0, similarity=0.99, already_attached=MAX_SEMANTIC_ATTACHMENTS
    )


def test_the_cap_never_blocks_a_lexical_hit():
    """The cap bounds an uncertain signal; it must not suppress a certain one."""
    assert _admit(lexical=0.50, similarity=0.99, already_attached=MAX_SEMANTIC_ATTACHMENTS)
