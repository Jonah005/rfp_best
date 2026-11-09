"""The model is paid to resolve ambiguity, not to agree with a threshold.

One call per requirement is the honest price of having a reader instead of a
number — and on a sixty-requirement bid, re-run every time more evidence is
approved, it is also the largest recurring cost in the product. Two rules bring
it down without giving up the reading:

* a shortlist where nothing is in doubt is not sent at all, and
* an identical question asked twice is answered once.

Both are only defensible if they never change what the model would have said.
That is what these cases check.
"""

from __future__ import annotations

import uuid

from app.models.knowledge import Evidence
from app.services.assessment import (
    MIN_ATTACH_SCORE,
    MIN_CONFIDENT_LEXICAL,
    MIN_SEMANTIC_SIMILARITY,
    RelevanceVerifier,
    _needs_reading,
)


def _item(score: float = 0.6):
    return (score, Evidence(id=uuid.uuid4(), excerpt="text"), "note")


# --- when the model is worth calling ----------------------------------------


def test_an_uncertain_candidate_earns_a_call():
    """The band the verification stage exists for.

    A candidate admitted by the widened recall floor that would not have cleared
    the strict rule is exactly the kind that used to attach as confident
    nonsense. It gets read.
    """
    uncertain = _item()
    assert _needs_reading([uncertain], certain=set())


def test_one_uncertain_candidate_among_certain_ones_still_earns_a_call():
    """Skipping is all-or-nothing per requirement, and it should be.

    The shortlist goes to the model in a single call, so as soon as one item
    needs reading the call is being made anyway; excluding the others would save
    nothing and would leave part of the shortlist unjudged.
    """
    certain, uncertain = _item(0.9), _item(0.6)
    assert _needs_reading([certain, uncertain], certain={certain[1].id})


def test_a_shortlist_of_certain_candidates_is_not_sent():
    """Paying to be told a hard lexical match is a match."""
    a, b = _item(0.9), _item(0.8)
    assert not _needs_reading([a, b], certain={a[1].id, b[1].id})


def test_an_empty_shortlist_is_not_sent():
    """Nothing to judge; the requirement reports no evidence either way."""
    assert not _needs_reading([], certain=set())


# --- what counts as certain -------------------------------------------------


def test_the_confidence_bar_is_far_above_the_attachment_floor():
    """Two different questions: worth looking at, versus obviously right."""
    assert MIN_CONFIDENT_LEXICAL > MIN_ATTACH_SCORE


def test_the_confidence_bar_is_not_so_high_that_nothing_clears_it():
    """A requirement of twenty terms sharing five of them is not a judgement call."""
    assert MIN_CONFIDENT_LEXICAL <= 0.25


def test_the_strict_similarity_floor_still_means_certain():
    """Above the old deciding threshold, the thresholds were already trusted."""
    assert MIN_SEMANTIC_SIMILARITY > MIN_CONFIDENT_LEXICAL


# --- the counters a cost decision needs to be visible ------------------------


def test_a_fresh_verifier_reports_zero_cost():
    counters = RelevanceVerifier().counters()
    assert counters["calls"] == 0
    assert counters["cache_hits"] == 0
    assert counters["skipped_requirements"] == 0


def test_cost_counters_are_reported_separately_from_outcome_counters():
    """What was paid for and what was found are different questions.

    Collapsing them is how an optimisation stops being maintained: if `calls`
    were merged into `checked`, a change that quietly doubled spend would look
    identical to one that quietly doubled recall.
    """
    counters = RelevanceVerifier().counters()
    assert {"calls", "cache_hits", "skipped_requirements"} <= set(counters)
    assert {"checked", "supported", "dropped", "disagreements"} <= set(counters)
