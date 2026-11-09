"""What kinds of material a copilot answer may cite, declared once.

Three bugs in this area had one cause, and it was not any of the three places
they showed up.

    * Approved evidence was rendered *above* the "AUTHORIZED CONTEXT" heading,
      so the model quoted it and then refused to use it.
    * [E#] was emitted in a format the citation extractor did not read, so a
      well-grounded answer reported "no sources cited".
    * [R#] was extracted but had no column to land in, so an answer resting
      entirely on a colleague's written reply reported the same thing.
    * And the first bug was never actually fixed for requirements, which kept
      being rendered above the heading. Asked whether the company had a Jadwa
      contact, the copilot answered that the context held no information about
      any contact person -- two inches below R-002, which requires notifying
      one. Same bug as the first, same function, second kind of material.

Each time, two correct pieces met and nobody owned the joint. The taxonomy
lived in four places that had to agree by hand: the system prompt that names
the prefixes, the context builder that renders the sections, the regex that
extracts them, and the columns that record them. Nothing made them agree, and
nothing failed when they stopped agreeing -- the symptom was always a quietly
wrong sentence in the interface.

So the taxonomy is declared here, once, and the other four derive from it. The
regex is built from these rows. The mapping from a citation to the column that
records it is built from these rows. The contract tests assert that the prompt
names every row and that the builder renders every row inside the authorized
heading. Adding a fourth kind of material and forgetting half the wiring is now
a test failure rather than a paragraph of confident prose with nothing behind
it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ContextKind:
    """One kind of material an answer may draw on, and cite."""

    key: str
    # The citation the model writes, as a regex fragment: [E1], [R3], [R-002].
    pattern: str
    # How a person refers to it, used in the prompt and in the interface.
    label: str
    # The section heading it is rendered under, inside AUTHORIZED CONTEXT.
    heading: str
    # Where it came from, in one line, for the prompt.
    provenance: str
    # The ChatMessage column recording which ids an answer cited, or None.
    records_to: str | None
    # Required when records_to is None. A kind that records nowhere must say
    # why, so that "we forgot" can never look the same as "deliberately not".
    why_unrecorded: str = ""
    # Whether material of this kind can support an answer. Requirements are
    # authorized and citable but state the QUESTION, so a task holding nothing
    # but requirements still has nothing to answer from. That distinction used
    # to live in two unrelated expressions -- the "nothing admissible" marker
    # and the out-of-scope test -- which is precisely how they drifted apart.
    supports_answer: bool = True

    def __post_init__(self) -> None:
        if self.records_to is None and not self.why_unrecorded:
            raise ValueError(
                f"Context kind {self.key!r} records no cited ids and gives no reason. "
                "Either name the column, or say why this kind is not recorded -- "
                "a silent omission here is exactly how [R#] went missing."
            )


CONTEXT_KINDS: tuple[ContextKind, ...] = (
    ContextKind(
        key="requirement",
        # Requirements carry their own codes, so they are cited by code rather
        # than by position. The hyphen also keeps [R-002] clear of [R2]: a
        # requirement and a cross-department answer would otherwise collide on
        # the same letter, and the model would have no way to mean one and not
        # the other.
        pattern=r"R-\d+",
        label="requirement",
        heading="-- Requirements this task must satisfy --",
        provenance="the tender's own words, as extracted and confirmed for this task",
        records_to=None,
        supports_answer=False,
        why_unrecorded=(
            "A task's requirements are already linked to it in task_requirement. "
            "Recording them again per message would duplicate that relation "
            "without answering a question nobody can already ask."
        ),
    ),
    ContextKind(
        key="evidence",
        pattern=r"E\d+",
        label="approved evidence",
        heading="-- Evidence already approved against these requirements --",
        provenance="company material that passed evidence approval",
        records_to="cited_evidence_ids",
    ),
    ContextKind(
        key="resolution",
        pattern=r"R\d+",
        label="cross-department answer",
        heading="-- Answers received from other departments --",
        provenance="a named colleague's written reply to a request from this task",
        records_to="cited_request_ids",
    ),
    ContextKind(
        key="retrieved",
        pattern=r"S\d+",
        label="corpus chunk",
        heading="-- Retrieved from the company corpus for this query --",
        provenance="raw corpus text this task is entitled to see",
        records_to="cited_chunk_ids",
    ),
)

# Longest-first so that [R-002] is never matched as [R] + junk, and so a new
# kind cannot silently shadow an existing one by ordering accident.
CITATION = re.compile(
    "\\[(" + "|".join(sorted((k.pattern for k in CONTEXT_KINDS), key=len, reverse=True)) + ")\\]"
)

_BY_KEY = {kind.key: kind for kind in CONTEXT_KINDS}

# The kinds that can actually answer something, as opposed to stating what was
# asked. Read by both the "nothing admissible" marker and the refusal test, so
# the two cannot disagree about whether a task has anything to work with.
ANSWERING_KEYS: frozenset[str] = frozenset(
    kind.key for kind in CONTEXT_KINDS if kind.supports_answer
)


def kind_for(ref: str) -> ContextKind | None:
    """Which kind a citation belongs to. `None` for a ref nothing declares."""
    for kind in sorted(CONTEXT_KINDS, key=lambda k: len(k.pattern), reverse=True):
        if re.fullmatch(kind.pattern, ref):
            return kind
    return None


def refs_in(text: str) -> set[str]:
    """Every citation an answer actually wrote.

    Ranges are not expanded, and must not be: "[E1] through [E12]" contains two
    citations, and treating it as twelve would invent eleven attributions the
    model never made. The prompt forbids writing ranges for exactly this
    reason -- the honest fix is at the point of writing, not of counting.
    """
    return set(CITATION.findall(text or ""))


def recorded_columns(refs: set[str], resolve: dict[str, dict]) -> dict[str, list]:
    """Map cited refs onto the columns that record them.

    `resolve` maps a kind key to that kind's {ref: id} lookup. Every kind with
    a column gets an entry, even an empty one, so a caller can never
    accidentally leave a column unwritten by forgetting the kind exists.
    """
    out: dict[str, list] = {
        kind.records_to: [] for kind in CONTEXT_KINDS if kind.records_to is not None
    }
    for ref in refs:
        kind = kind_for(ref)
        if kind is None or kind.records_to is None:
            continue
        found = resolve.get(kind.key, {}).get(ref)
        if found is not None:
            out[kind.records_to].append(found)
    return out


def prompt_taxonomy() -> str:
    """The prefixes and their provenance, for the system prompt to quote.

    The prompt stays hand-written and versioned -- generating it would let the
    text change without the version changing, and an unversioned prompt change
    is unauditable. This exists so the contract test can assert that the
    hand-written prompt still names every kind.
    """
    return "\n".join(
        f"  [{kind.pattern}] {kind.label} -- {kind.provenance}" for kind in CONTEXT_KINDS
    )


__all__ = [
    "CITATION",
    "CONTEXT_KINDS",
    "ContextKind",
    "kind_for",
    "prompt_taxonomy",
    "recorded_columns",
    "refs_in",
    "_BY_KEY",
]
