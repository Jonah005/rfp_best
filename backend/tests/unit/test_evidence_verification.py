"""The model checks retrieval; it does not get to decide anything else.

A cosine floor has to separate proof from noise with one scalar and nothing
downstream to catch it when it is wrong. Verification replaces that guess with a
reading — and everything here exists to keep the reading inside its lane: it may
prune and reorder a shortlist the permission-filtered query already returned,
and it may not introduce evidence, survive its own failure, or empty a matrix
because a provider had a bad minute.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.knowledge import Evidence
from app.schemas.evidence_relevance import (
    CandidateVerdict,
    EvidenceRelevanceResult,
    RelevanceVerdict,
)
from app.services import assessment as assess
from app.services.assessment import (
    MIN_SEMANTIC_SIMILARITY,
    MIN_SEMANTIC_SIMILARITY_RECALL,
    RelevanceVerifier,
    VerificationUnavailable,
)


class _FakeGateway:
    """Returns a canned result, or raises, and records what it was asked."""

    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.prompts: list[str] = []
        self.last_call_cached = False

    def generate_structured(self, call_type, prompt, **kwargs):
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        assert call_type == "evidence_relevance"
        return self.result


def _use(monkeypatch, gateway):
    monkeypatch.setattr(assess, "build_gateway", lambda: gateway)


def _shortlist(*texts: str) -> list[tuple[float, Evidence, str]]:
    # Descending retrieval scores, so any reordering in the result is the
    # verifier's doing rather than the input's.
    # Ids are assigned here rather than left to the database default, so the
    # "no identifiers in the prompt" test is checking a real uuid string and not
    # the word "None".
    return [
        (0.9 - 0.1 * index, Evidence(id=uuid.uuid4(), excerpt=text), "retrieved")
        for index, text in enumerate(texts)
    ]


def _verdicts(*pairs: tuple[int, RelevanceVerdict]) -> EvidenceRelevanceResult:
    return EvidenceRelevanceResult(
        verdicts=[
            CandidateVerdict(candidate=number, verdict=verdict, reason="because")
            for number, verdict in pairs
        ]
    )


def _prune(verifier, shortlist):
    return verifier.prune(None, None, requirement_text="must hold CREST", shortlist=shortlist)


# --- the point of the exercise ----------------------------------------------


def test_an_unrelated_candidate_is_dropped(monkeypatch):
    """The false positive the thresholds could not catch."""
    _use(
        monkeypatch,
        _FakeGateway(_verdicts((1, RelevanceVerdict.UNRELATED), (2, RelevanceVerdict.SUPPORTS))),
    )
    verifier = RelevanceVerifier()
    kept = _prune(verifier, _shortlist("proposals become customer property", "CREST certified"))

    assert [evidence.excerpt for _, evidence, _ in kept] == ["CREST certified"]
    assert verifier.dropped == 1
    assert verifier.supported == 1


def test_proof_outranks_related_whatever_retrieval_thought(monkeypatch):
    """Ranking follows the verdict, or a demoted excerpt stays PRIMARY."""
    _use(
        monkeypatch,
        _FakeGateway(_verdicts((1, RelevanceVerdict.PARTIAL), (2, RelevanceVerdict.SUPPORTS))),
    )
    kept = _prune(RelevanceVerifier(), _shortlist("security policy", "CREST certificate"))

    assert [evidence.excerpt for _, evidence, _ in kept] == [
        "CREST certificate",
        "security policy",
    ]


def test_roles_are_assigned_after_pruning():
    """`_rank` runs last, so promotion follows removal."""
    ranked = assess._rank(_shortlist("first", "second"))
    assert ranked[0].rank == 1
    assert ranked[0].role.value == "PRIMARY"
    assert ranked[1].role.value == "SUPPORTING"


# --- what it is not allowed to do -------------------------------------------


def test_a_verdict_about_something_not_shown_is_discarded(monkeypatch):
    """An out-of-range number means the model answered about nothing visible."""
    _use(
        monkeypatch,
        _FakeGateway(_verdicts((1, RelevanceVerdict.SUPPORTS), (99, RelevanceVerdict.UNRELATED))),
    )
    kept = _prune(RelevanceVerifier(), _shortlist("CREST certificate"))
    assert len(kept) == 1


def test_the_prompt_carries_no_evidence_identifiers(monkeypatch):
    """Ids in a prompt invite invention; positions cannot be hallucinated."""
    gateway = _FakeGateway(_verdicts((1, RelevanceVerdict.SUPPORTS)))
    _use(monkeypatch, gateway)
    shortlist = _shortlist("CREST certificate")
    _prune(RelevanceVerifier(), shortlist)

    prompt = gateway.prompts[0]
    assert "[1]" in prompt
    assert str(shortlist[0][1].id) not in prompt
    # Excerpts are third-party text and must arrive delimited as data.
    assert "<untrusted_document_content>" in prompt


def test_excerpts_are_truncated_in_the_prompt(monkeypatch):
    gateway = _FakeGateway(_verdicts((1, RelevanceVerdict.SUPPORTS)))
    _use(monkeypatch, gateway)
    _prune(RelevanceVerifier(), _shortlist("x" * 5000))
    assert "x" * (assess.VERIFICATION_EXCERPT_CHARS + 1) not in gateway.prompts[0]


# --- failure must degrade, never block --------------------------------------


def test_a_provider_failure_falls_back_and_latches_off(monkeypatch):
    """ "The vendor is down" must never be why a bid cannot be assessed."""
    _use(monkeypatch, _FakeGateway(error=RuntimeError("502 from provider")))
    verifier = RelevanceVerifier()

    with pytest.raises(VerificationUnavailable):
        _prune(verifier, _shortlist("CREST certificate"))

    assert verifier.available is False
    assert verifier.failures == 1


def test_an_empty_reply_is_a_failure_not_a_rejection(monkeypatch):
    """Judging nothing is not the same as finding nothing relevant."""
    _use(monkeypatch, _FakeGateway(EvidenceRelevanceResult(verdicts=[])))
    verifier = RelevanceVerifier()

    with pytest.raises(VerificationUnavailable):
        _prune(verifier, _shortlist("CREST certificate"))
    assert verifier.available is False


def test_an_unjudged_candidate_is_kept_and_said_so(monkeypatch):
    """Losing real proof to a formatting slip is the worse error."""
    _use(monkeypatch, _FakeGateway(_verdicts((1, RelevanceVerdict.SUPPORTS))))
    verifier = RelevanceVerifier()
    kept = _prune(verifier, _shortlist("CREST certificate", "ISO 27001 certificate"))

    assert len(kept) == 2
    assert verifier.unjudged == 1
    assert "Not checked" in kept[1][2]


def test_an_empty_shortlist_costs_no_call(monkeypatch):
    gateway = _FakeGateway(_verdicts())
    _use(monkeypatch, gateway)
    assert _prune(RelevanceVerifier(), []) == []
    assert gateway.prompts == []


# --- the thresholds the two modes use ---------------------------------------


def test_the_recall_floor_is_lower_than_the_deciding_floor():
    """Verification changes what the number has to do: shortlist, not verdict."""
    assert MIN_SEMANTIC_SIMILARITY_RECALL < MIN_SEMANTIC_SIMILARITY


def test_the_recall_floor_still_excludes_unrelated_text():
    """A miss is unrecoverable — an excerpt not retrieved is never judged — but
    the floor cannot drop so far that every document is a candidate."""
    assert MIN_SEMANTIC_SIMILARITY_RECALL > 0.7


def test_the_shortlist_is_wider_than_what_can_attach():
    """Pruning is the point; a shortlist at the attach limit prunes nothing."""
    assert assess.VERIFICATION_SHORTLIST > assess.CANDIDATE_LIMIT


def test_verification_can_be_switched_off(monkeypatch):
    """Cost is real: one call per requirement. It has to be a setting."""

    class _Off:
        assessment_llm_verification = False

    monkeypatch.setattr(assess, "get_settings", lambda: _Off())
    assert RelevanceVerifier().available is False
