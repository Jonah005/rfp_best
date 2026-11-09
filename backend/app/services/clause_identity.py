"""What makes two pieces of tender text the same clause.

This question is asked in two places that must agree, and for most of this
project they did not. `precedence` asks it to decide which document governs a
clause; `requirements` asks it to decide whether an obligation is new or a new
version of one already recorded. Each had its own answer, and both answers were
the bare `DocumentBlock.clause_ref`.

A bare reference is not an identity. Tenders restart numbering in every section,
so on one real document "3" was Business 3 (REIT subscription forms),
Business 3 again (B2B bank integration) and Technical 3 (SSL support). Grouping
them made precedence hold a contest between three unrelated obligations, keep
one winner, and silently discard the other two -- and since requirement
extraction only ever sees resolution winners, 25 of 39 numbered clauses never
reached the model at all. The tender read as 14 requirements.

Fixing that in `requirements` alone left the same defect in `precedence`. So
the definition lives here, once, and both import it. A test asserts that
neither module keys on a bare reference again.

The reference is *displayed* as well as matched, so it has to stay readable:
"Business Requirements / 3" says what "3" never did.
"""

from __future__ import annotations

import hashlib
import re

# Wide enough for a real section heading plus a reference, and bounded so a
# polluted heading cannot overflow the column. `clause_ref` is varchar(200) in
# both `clause_resolution` and `requirement_version` (migration 0018); it was
# varchar(64), which a qualified reference on a document with long headings
# would have exceeded on insert.
MAX_CLAUSE_REF = 200

# How much of the budget the heading may take, leaving room for " / <ref>".
_MAX_HEADING = 140

# Page furniture that parsers fold into heading text.
#
# One real heading arrived as:
#   "8.11 Appendix I Technical Proposal Format Page 19 of 32|<RFP Template.docx> <date>"
# where everything from "Page 19 of 32" on is the running header of the page the
# heading happened to sit on. Left in, it makes the identity depend on
# pagination: the same clause in a re-exported PDF lands on page 20 and becomes
# a different clause. It is stripped here rather than in the parser because the
# parser's output is also shown to people, and "which page was this on" is
# worth keeping for a reader even when it must not affect matching.
_FIELD_ARTIFACT = re.compile(r"\s*\|.*$")
_PAGE_MARKER = re.compile(r"\s*\bpage\s+\d+\s+of\s+\d+\b.*$", re.IGNORECASE)


def normalized_heading(heading_path: list[str] | None) -> str | None:
    """The nearest enclosing section, cleaned of page furniture.

    Returns None when the block sits under no heading at all, which is a real
    state and not an error: a document may open with prose.
    """
    headings = [h for h in (heading_path or []) if h and h.strip()]
    if not headings:
        return None
    heading = headings[-1]
    heading = _FIELD_ARTIFACT.sub("", heading)
    heading = _PAGE_MARKER.sub("", heading)
    heading = " ".join(heading.split())
    return heading or None


def qualified_clause_ref(block) -> str | None:
    """The reference to match on and to show, or None if the block has none.

    None is returned only when the block genuinely carries no reference. That
    matters: `clause_ref == None` compiles to `IS NULL` in SQLAlchemy, so a
    caller that passes an absent reference into an equality filter matches every
    other block that also lacks one. Callers must branch on None rather than
    comparing it -- see `requirements._find_existing`.
    """
    ref = (getattr(block, "clause_ref", None) or "").strip()
    if not ref:
        return None
    heading = normalized_heading(getattr(block, "heading_path", None))
    if not heading:
        return _bounded(ref)
    return _bounded(f"{heading} / {ref}")


def _bounded(value: str) -> str:
    """Keep the reference inside the column, without merging two clauses.

    Plain truncation would be wrong: two long headings sharing a prefix would
    truncate to the same string and re-create the collision this module exists
    to prevent. So an over-long value keeps a readable prefix and gains a short
    digest of the whole, which stays unique.
    """
    if len(value) <= MAX_CLAUSE_REF:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    keep = MAX_CLAUSE_REF - len(digest) - 2
    return f"{value[:keep]}~{digest}"


def heading_for_display(heading_path: list[str] | None) -> str:
    """The section as a person should read it. Never used for matching."""
    return normalized_heading(heading_path) or "(no section)"
