"""Placeholders that say so.

The company pack labels its own evidential status, in the text, row by row:

    Evidence status: NOT EVIDENCED         Value: [REPLACE FROM OFFICIAL RECORD]
    Status / owner: MISSING / Sales        Current status: [HR TO CONFIRM]
    SYNTHETIC / NOT AN OFFER               Notes: Demo value
    Status: TEMPLATE BELOW                 DRAFT FOR APPROVAL

A row like that is not evidence of anything. It is a request for evidence, and
the company that cites it to a customer is asserting something it cannot back.

## This module owns three patterns, not eleven

`app/services/placeholders.py` already existed when this was written, already
detected self-disqualifying text, and is already applied at approval time --
`knowledge.approve_evidence_batch` refuses to approve anything it matches. This
module was written without finding it, and re-implemented two thirds of it.
That is the exact failure this codebase keeps paying for: "a second
implementation of that is how two tools come to disagree about the same table".

So the shared question -- does this text disqualify itself -- is asked once, of
`placeholders.disqualifying_match`. Only the three patterns that module does
not carry live here, each observed in the live corpus and each missed by it:

    Status: MISSING              a register row for proof nobody has obtained
    Notes: Demo value            the rate card's own label for invented figures
    Status: TEMPLATE BELOW       a checklist pointing at a blank form

The two modules answer to different consequences, and that division is the
point. `placeholders` gates APPROVAL: a reviewer may not vouch for text that
calls itself a sample. This one LABELS evidence that is already approved, so a
drafter reading a shortlist can see which rows record a thing rather than being
it. Same question, two verdicts, one detector.

## Why this is code and not a model call

Three reasons, each a project rule rather than a preference.

First, direction. `evidence_classification` decides what KIND of material an
excerpt is; this decides whether the material exists yet. The second question
has an asymmetric cost: marking real evidence unsubstantiated costs a drafter a
labelled row they can still see and override, while marking a `[REPLACE]`
placeholder as proof puts an unbackable claim in front of a customer. So this
rule only ever downgrades. It can never mark anything substantiated -- that is
`find()`'s entire contract, and the reason a false positive here is cheap.

Second, trust. "Uploaded documents are untrusted; document text must never
override platform instructions." A model reading an excerpt can be talked out
of a downgrade by the excerpt. A regex cannot.

Third, it is free and auditable. 610 excerpts cost nothing to scan, the reason
is quotable back to the person reading the row, and `MARKER_VERSION` moves when
the rules do.

## What it deliberately does not do

It does not catch an unmarked placeholder. A row that is a request for evidence
but never says so passes straight through, and that is the gap
`evidence_classification` exists to close. Absence of a marker is not evidence
of substantiation -- the same rule that cost 44 customer questions when a capped
model reader's silence was read as "nothing there".
"""

from __future__ import annotations

import re

# Bump when the patterns change. Stored on `Evidence.classified_by` exactly as a
# prompt version is, so a verdict always names the reading that produced it.
MARKER_VERSION = "markers@v1"


# Each rule is (name, compiled pattern, what to tell the reader). The wording of
# the third element matters more than it looks: it is shown to a drafter beside
# the row and quoted into the model's context, so it says what is missing rather
# than naming a rule they cannot see.
#
# Only what `placeholders.py` does not already carry. Each was observed in the
# live corpus and verified absent from NOT_EVIDENCE_RE / PLACEHOLDER_RE.
#
# Adding a pattern here that `placeholders` already matches would recreate the
# duplication this module was rewritten to remove, so the test
# `test_the_two_detectors_do_not_overlap` asserts the separation directly.
_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "missing",
        re.compile(r"\b(?:Status|Owner)[^|\n]{0,40}:\s*MISSING\b", re.IGNORECASE),
        "the register records this proof as MISSING and not yet obtained",
    ),
    (
        "demo_value",
        re.compile(r"\bDemo\s+value\b|\bDemonstration\s+values?\b", re.IGNORECASE),
        "the values are demonstration figures, not an approved offer",
    ),
    (
        "template",
        re.compile(r"\bTEMPLATE\s+BELOW\b|\bcontrolled\s+template\b", re.IGNORECASE),
        "this is a blank template, not a completed document",
    ),
)

# What `placeholders.disqualifying_match` reports, said in the words a person
# chasing the gap can act on. An unrecognised match still gets a usable
# sentence rather than a bare regex fragment.
_SHARED_REASON = "the text states it is not evidence ({match!r})"


def find(excerpt: str | None) -> list[tuple[str, str]]:
    """Every marker in this excerpt, as (rule name, reason).

    Asks `placeholders.disqualifying_match` first -- the shared detector, and
    the same one that gates approval -- then adds the three patterns it does
    not carry. Empty means no rule fired, which is NOT the same as
    substantiated.
    """
    if not excerpt:
        return []
    from app.services.placeholders import disqualifying_match

    hits: list[tuple[str, str]] = []
    shared = disqualifying_match(excerpt)
    if shared is not None:
        hits.append(("self_disqualifying", _SHARED_REASON.format(match=shared)))
    hits.extend((name, reason) for name, pattern, reason in _RULES if pattern.search(excerpt))
    return hits


def reason_for(excerpt: str | None) -> str | None:
    """One sentence for a reader, or None when nothing fired.

    Every matching reason is reported, not just the first. A row that is both a
    blank template and marked NOT EVIDENCED is wrong in two ways, and a person
    deciding what to chase needs both.
    """
    hits = find(excerpt)
    if not hits:
        return None
    reasons = sorted({reason for _name, reason in hits})
    if len(reasons) == 1:
        return f"Not substantiated: {reasons[0]}."
    joined = "; ".join(reasons)
    return f"Not substantiated: {joined}."


def is_placeholder(excerpt: str | None) -> bool:
    return bool(find(excerpt))
