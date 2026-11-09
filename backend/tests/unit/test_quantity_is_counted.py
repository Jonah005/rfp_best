"""Three reference projects, and a pack holding one.

R-006 on the fixture bid says:

    The supplier must provide three comparable projects delivered in the UAE or
    wider GCC region within the last three years.

The company pack holds one. The row read DRAFTABLE, with the rationale "Some
current evidence exists; the gaps must be filled during drafting." Every clause
of that is individually correct and the conclusion is false: two of the three
projects do not exist, and no amount of drafting will produce them. A proposal
would have gone out claiming three references and shown one.

## Why this is arithmetic and not a better prompt

The obvious repair was tried first: tell `evidence_relevance` that one completed
project is not partial proof of a demand for three. The verdicts began to flap
between runs, and the reason is that the instruction fights the meaning of its
own label. The one reference IS partial proof -- that is what PARTIAL means, and
the reader was right every time. It was being asked to encode an arithmetic
result in a word that means something else.

So the reading and the counting were split. `requirement_quantity` reads how
many the clause demands; this rule counts what the pack holds and compares. v3
of the relevance prompt now says so in as many words: "Quantity is NOT your
question... Counting is done elsewhere."

## What is counted, and the proxy being made explicit

Excerpts are not things. One project reference produces several rows, and
counting rows would let a single project satisfy a demand for three. The source
DOCUMENT is the nearest honest unit of "a separate item the company holds", and
it is a proxy: one document describing three projects counts as one, and
undercounts.

That is the direction to be wrong in. The rule only downgrades, a downgraded row
carries its count and its noun in the rationale where a person can see and
override it, and the failure it prevents is a proposal that claims three and
shows one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from app.schemas.evidence_relevance import RelevanceVerdict
from app.services.assessment import distinct_sources, quantity_shortfall


@dataclass
class _Version:
    required_count: int | None = None
    counted_noun: str | None = None


class _Evidence:
    def __init__(self, source_document_version_id):
        self.source_document_version_id = source_document_version_id


class _Candidate:
    def __init__(self, source, support=None):
        self.evidence = _Evidence(source)
        self.support = support


def _from(source, support=None, times=1):
    return [_Candidate(source, support) for _ in range(times)]


# --- counting the things, not the rows --------------------------------------


def test_several_excerpts_from_one_document_are_one_thing():
    """The whole reason rows cannot be counted.

    A single project reference is chunked into several excerpts, each one a
    candidate. Counting candidates would let one project answer a demand for
    three, which is the failure this rule exists to stop -- reproduced by the
    rule itself.
    """
    one_project = uuid.uuid4()
    assert distinct_sources(_from(one_project, times=6)) == 1


def test_separate_documents_are_separate_things():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert distinct_sources(_from(a) + _from(b) + _from(c)) == 3


def test_an_unrelated_excerpt_is_not_one_of_the_three():
    """UNRELATED means retrieval was wrong and the excerpt says nothing about
    this requirement. It cannot be one of the three projects, and counting it
    would let bad retrieval satisfy the tender."""
    real, noise = uuid.uuid4(), uuid.uuid4()
    candidates = _from(real, RelevanceVerdict.SUPPORTS) + _from(noise, RelevanceVerdict.UNRELATED)
    assert distinct_sources(candidates) == 1


def test_partial_and_unread_material_still_counts():
    """Generous on purpose, because the rule downgrades.

    PARTIAL is material a drafter can point at, and `support is None` means
    verification never ran -- absence of a check is not a finding. A shortfall
    should be claimed only when even the generous count falls short, so neither
    is excluded here.
    """
    a, b = uuid.uuid4(), uuid.uuid4()
    assert distinct_sources(_from(a, RelevanceVerdict.PARTIAL) + _from(b, None)) == 2


def test_nothing_at_all_counts_as_nothing():
    assert distinct_sources([]) == 0


# --- the shortfall itself ---------------------------------------------------


def test_one_project_against_a_demand_for_three_is_a_shortfall():
    """R-006, as a pure function."""
    version = _Version(required_count=3, counted_noun="comparable projects")
    found = quantity_shortfall(version, _from(uuid.uuid4(), times=4))
    assert found == (1, 3, "comparable projects")


def test_enough_is_not_a_shortfall():
    """Two OSCP CVs against a demand for two -- R-005, which must stay
    answerable. A rule that fires on a satisfied requirement is worse than no
    rule: it teaches people to ignore the state."""
    version = _Version(required_count=2, counted_noun="certified testers")
    two_cvs = _from(uuid.uuid4()) + _from(uuid.uuid4())
    assert quantity_shortfall(version, two_cvs) is None


def test_more_than_enough_is_not_a_shortfall():
    version = _Version(required_count=2, counted_noun="certified testers")
    assert quantity_shortfall(version, [_Candidate(uuid.uuid4()) for _ in range(5)]) is None


def test_an_unread_requirement_says_nothing():
    """NULL means "not judged", never "none required".

    This is the case that decides whether the rule is safe to ship. Every
    requirement in every existing bid has a NULL count until something reads it,
    and if NULL were treated as "the tender asks for zero" -- or worse, if a
    missing count were treated as a shortfall -- turning this on would block an
    entire matrix at once.
    """
    assert quantity_shortfall(_Version(), _from(uuid.uuid4())) is None
    assert quantity_shortfall(None, _from(uuid.uuid4())) is None


def test_a_requirement_read_and_found_to_have_no_count_says_nothing():
    """`required_count = 0` is what a NO verdict stores: read, and not a
    counting obligation. Indistinguishable in effect from unread, which is
    correct -- neither is a shortfall."""
    assert quantity_shortfall(_Version(required_count=0), []) is None


def test_the_shortfall_names_what_is_short():
    """The number alone is not actionable.

    "The tender asks for 3 and the pack evidences 1" leaves a reader to work out
    three of WHAT from a rationale that does not say. The noun comes from the
    clause's own words, so the message names the thing somebody has to go and
    find.
    """
    version = _Version(required_count=4, counted_noun="audited annual accounts")
    have, need, noun = quantity_shortfall(version, [])
    assert (have, need) == (0, 4)
    assert noun == "audited annual accounts"


@pytest.mark.parametrize("noun", [None, "", "   "])
def test_a_missing_noun_still_produces_a_readable_sentence(noun):
    """A verdict can come back with a count and no noun. The rationale is built
    by string interpolation, so `None` there would print "asks for 3 None"."""
    version = _Version(required_count=3, counted_noun=noun)
    _have, _need, filled = quantity_shortfall(version, [])
    assert filled == "of these"
    assert f"asks for 3 {filled}".endswith("of these")


def test_the_rule_can_only_ever_downgrade():
    """Its entire contract in one assertion.

    There is no input for which this reports a surplus, raises readiness, or
    says anything at all when the count is met. It returns a shortfall or it
    returns None -- so the worst a wrong reading can do is put a visible,
    explained BLOCKED on a row somebody can override.
    """
    plenty = [_Candidate(uuid.uuid4()) for _ in range(10)]
    for count in (None, 0, 2, 5, 10):
        assert quantity_shortfall(_Version(required_count=count), plenty) is None


# --- which clauses are even asked about -------------------------------------


NUMBERLESS = [
    "The supplier must describe its mobile application testing methodology.",
    "Testing must be carried out by suitably qualified personnel.",
    "The supplier shall provide a project schedule.",
]

NUMBERED = [
    "The supplier must provide three comparable projects.",
    "The supplier must propose at least two testers holding OSCP.",
    "The bidder shall perform penetration testing of 25 applications.",
    "Testing must occur between 01:00 and 05:00 Gulf Standard Time.",
    # A standard's NAME, which is why the filter cannot be the judge. Half the
    # certifications a tender names carry digits -- ISO 27001, IEC 62443, PCI
    # DSS 4.0 -- so a great many clauses reach the model and are told NO. That
    # is the cost of keeping the meaning judgement out of the regex, and it is
    # one batched call.
    "The supplier must hold current ISO/IEC 27001 certification.",
]


@pytest.mark.parametrize("text", NUMBERLESS)
def test_a_clause_with_no_number_never_reaches_the_model(text: str) -> None:
    """The filter that makes this nearly free.

    A clause with no numeral cannot demand three of something, so it is settled
    without spending anything -- and settled correctly, which is the difference
    between an optimisation and a silent skip. This project has paid for that
    difference six times.
    """
    from app.services.requirements import _NUMERAL

    assert not _NUMERAL.search(text)


@pytest.mark.parametrize("text", NUMBERED)
def test_every_clause_carrying_a_number_is_asked_about(text: str) -> None:
    """Including the ones whose number is not a count.

    "25 applications" is scope and "01:00" is a time, and neither is a counting
    obligation -- but that is a judgement about meaning, so it belongs to the
    model and not to a regex. The filter's only job is to be cheap without ever
    dropping a clause that might carry a count; deciding which numbers matter
    here would be exactly the guess the split was meant to avoid.
    """
    from app.services.requirements import _NUMERAL

    assert _NUMERAL.search(text)


def test_written_out_numbers_are_caught_as_well_as_figures():
    """Tenders say "three comparable projects" at least as often as "3", and a
    digit-only filter would have missed the requirement this rule was built
    for."""
    from app.services.requirements import _NUMERAL

    assert _NUMERAL.search("must provide three comparable projects")
    assert _NUMERAL.search("must provide 3 comparable projects")
    assert _NUMERAL.search("at least TWO certified testers")
