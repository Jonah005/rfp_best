"""Text that announces it is not real.

Two different documents lie about themselves in the same way, so the detection
lives in one place.

A tender clause can arrive with the customer's own blanks still in it -- "TBD",
"[INSERT DATE]", a row of underscores. A requirement derived from one of those
is a requirement whose content nobody has decided yet, and answering it
confidently is how a bid commits to a number the customer never wrote.

A company document does something worse. The demo corpus, and every corpus
assembled in a hurry, contains templates and generated fillers that say so on
their face: "SAMPLE / NOT VALID", "SYNTHETIC / REPLACE", "DRAFT FOR APPROVAL",
"[REPLACE]", a rate card of invented figures. As extracted evidence those look
exactly like real proof -- an excerpt, a source, a confidence -- and once
approved they are quoted into a submitted proposal as though the company had
demonstrated something. That is the single most damaging output this system can
produce, and it happened: a blanket approval of 306 excerpts swept in at least
52 that say "not valid" in their own words.

So this module does not decide anything. It reports that a piece of text
disqualifies itself, and the caller decides what that means -- a clarification
for a tender clause, a refusal to approve for evidence.

Deliberately conservative. A false positive costs one excerpt a human look; a
false negative puts "AED 23,000,000 (illustrative)" in a bid.
"""

from __future__ import annotations

import re

# Unfilled blanks in a document that was meant to be completed.
PLACEHOLDER_RE = re.compile(
    r"(\bTBD\b|\bTBC\b|\bXXX+\b|\[[A-Z_ ]{3,}\]|<INSERT[^>]*>|_{5,}|\.{6,})",
    re.IGNORECASE,
)

# Text that states outright that it is not evidence of anything.
#
# Each of these was observed in the corpus. They are matched case-insensitively
# and as whole phrases, because the risk is not a stray word -- it is a document
# that labels itself a sample and is then quoted as proof.
NOT_EVIDENCE_RE = re.compile(
    r"("
    r"\bsample\s*/?\s*not\s+valid\b"
    r"|\bnot\s+valid\b"
    r"|\bsynthetic\b"
    r"|\bfor\s+illustration\b|\billustrative\b"
    r"|\bdraft\s+for\s+approval\b"
    r"|\bspecimen\b"
    r"|\bdummy\b"
    r"|\bplaceholder\b"
    r"|\breplace\s+(?:this|with)\b|\[replace\]"
    r"|\bto\s+be\s+filled\b|\bto\s+be\s+completed\b"
    r"|\bnot\s+supplied\b|\bnot\s+provided\b|\bnot\s+evidenced\b"
    r"|\bcertificates?\s+not\s+supplied\b"
    r"|\bexample\s+only\b|\btemplate\s+only\b"
    r")",
    re.IGNORECASE,
)


def placeholder_match(text: str | None) -> str | None:
    """The unfilled blank in this text, if any."""
    match = PLACEHOLDER_RE.search(text or "")
    return match.group(0) if match else None


def disqualifying_match(text: str | None) -> str | None:
    """The phrase by which this text disqualifies itself as proof, if any.

    Checks both patterns: a company excerpt still carrying "[INSERT VALUE]" is
    no more usable as evidence than one stamped SAMPLE.
    """
    body = text or ""
    match = NOT_EVIDENCE_RE.search(body) or PLACEHOLDER_RE.search(body)
    return match.group(0) if match else None
