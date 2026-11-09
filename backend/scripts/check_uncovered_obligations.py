"""Which sentences in the tender did not become a requirement?

Five obligations are named in the source PDF and absent from the 104 extracted
requirements:

    Back Office Checker (the final-verification role)
    Working days, among the configurable timing parameters
    "Get applicant related more information", the second Al-Elm purpose
    "Develop engine to promptly send SMS"
    "Quote Separate" for the Signature and Document Management systems

Each was found by a person reading the PDF against the workbook. That does not
scale and it does not tell us WHY they are missing, and the why decides the fix:
a prompt that skips a bullet is a different problem from a parser that never
put the text in a block, which is different again from a batch that ran out of
room.

So this reads every block, splits it into candidate obligations, and asks
whether any requirement drawn from that block covers each one. What comes back
is the list of spans the extractor saw and did not turn into anything --
including, if the theory holds, those five.

The coverage checker in `extract_requirements` asks a coarser question: did this
BLOCK produce anything at all. Every one of these five sits in a block that
produced several requirements, so that check was always going to pass. This is
the finer question it cannot ask.

Coverage is judged on content words, not on wording: a requirement covers a
sentence when it contains most of what the sentence is about. That is a
heuristic and it will be wrong at the edges -- so it prints the sentence and the
requirements beside it, and a person decides. It is a way of narrowing 43 blocks
to a readable list, not a verdict.

Nothing is written. Read-only.

    python scripts/check_uncovered_obligations.py
    python scripts/check_uncovered_obligations.py --covered 0.5   # stricter
    python scripts/check_uncovered_obligations.py --all           # every sentence
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault(
    "BIDREADY_DATABASE_URL",
    os.environ.get("BIDREADY_LOCAL_DATABASE_URL")
    or "postgresql+psycopg://bidready_app:bidready-local@localhost:5432/bidready",
)

# Before any app import: `settings.vector_backend` chooses the embedding column
# type and is resolved once at import. run_local.py sets it; a script run
# directly does not, so the ORM builds pgvector columns against a real[] schema
# and dies on "malformed array literal" or "'list' object has no attribute
# 'split'". Both have cost a debugging round. Detected from the live schema.
from _vector_backend import align_vector_backend  # noqa: E402

align_vector_backend()

from sqlalchemy import select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.services.requirements import _requirements_from_block  # noqa: E402

BID = "0e20677e-9cc3-434e-ad88-c953d6676028"

# The five a person found by reading the PDF. Checked by name as well as by the
# general sweep, because a sweep that misses a known-missing item is a sweep
# that cannot be trusted on the ones nobody has found yet.
# Spelled as the TENDER spells them, typos included. "promptly" and "Quote
# Separate" both reported NOT IN ANY BLOCK on the first run and both were my
# spelling: the document says "prompty" and "Qoute". A checker that normalises
# the source to what it expects to find will report a parser failure that is
# really its own assumption.
KNOWN_MISSING = [
    "Back Office Checker",
    "Working days",
    "Get applicant related more information",
    "engine to prompty send SMS",
    "Qoute Separate",
]

# A sentence worth checking says somebody has to do something. Without this the
# report is mostly headings and narrative, and the real gaps are lost in it.
OBLIGATION = re.compile(
    r"\b(shall|must|should|will|required|require|requires|provide|develop|ensure|"
    r"support|deliver|submit|implement|maintain|integrate|avail|create|configure|"
    r"vendor|bidder|supplier)\b",
    re.IGNORECASE,
)

TOC_LEADER = re.compile(r"\.{5,}")

STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "by",
    "at", "from", "as", "that", "this", "these", "those", "it", "its", "is",
    "are", "was", "were", "be", "been", "will", "would", "shall", "should",
    "must", "may", "can", "could", "do", "does", "did", "not", "any", "all",
    "such", "which", "their", "they", "we", "our", "us", "you", "your", "if",
    "there", "have", "has", "had", "also", "other", "following", "based",
}


def _terms(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9\-/]*", (text or "").lower())
    return {w.strip("-/") for w in words if len(w) > 2 and w not in STOP}


def _covered(sentence: set[str], requirement: set[str]) -> float:
    """Share of the sentence's content words the requirement carries."""
    if not sentence:
        return 1.0
    return len(sentence & requirement) / len(sentence)


def _sentences(text: str) -> list[str]:
    """Split a block into candidate obligations.

    Bullets in this tender survive extraction as runs of two or more spaces
    rather than as list markup, so splitting on sentence enders alone keeps a
    whole eight-item role list as one "sentence" -- and a requirement covering
    any one item then looks like it covers them all. That is precisely how a
    missing bullet hides.
    """
    parts = re.split(r"(?<=[.;:])\s+|\s{2,}|\n+|(?=•)", text or "")
    out: list[str] = []
    for part in parts:
        part = " ".join(part.split()).strip(" •-")
        # Short is not the same as unimportant.
        #
        # A 25-character minimum looked like sensible noise control and threw
        # away "Working days" -- twelve characters, one of the five obligations
        # this script exists to find, sitting in a bullet list beside "Working
        # hours 9am-4pm" and "Vacation". The length of a bullet says nothing
        # about whether it binds anybody.
        #
        # What actually separates a fragment from an obligation is whether it
        # carries meaning at all, so the test is content words rather than
        # characters.
        if len(part) < 6 or not _terms(part):
            continue
        # A table-of-contents line is a heading, a run of dot leaders and a page
        # number. It is not an obligation, it never becomes a requirement, and
        # the contents pages alone produced over a hundred entries on the first
        # run -- "Footer ......", "SMS ......", "Contact Us ......" -- which is
        # enough noise to make the real findings unreadable.
        if TOC_LEADER.search(part):
            continue
        out.append(part)
    return out


def main() -> int:
    import uuid

    parser = argparse.ArgumentParser()
    # 0.85, not the 0.6 this started at.
    #
    # Calibrated against the one case already known to be missing. "Back Office
    # Checker (For final application verification)" scores 67% against the
    # merged role requirement, because `back`, `office`, `application` and
    # `verification` all appear in the Back Office MAKER bullet beside it. Only
    # `checker` and `final` are absent -- and those two words are the whole
    # obligation. A loose threshold does not merely miss such an item, it misses
    # exactly the items most worth finding: the ones that look like their
    # neighbours.
    parser.add_argument("--covered", type=float, default=0.85,
                        help="share of a sentence's words a requirement must carry (0.85)")
    parser.add_argument("--all", action="store_true",
                        help="include sentences with no obligation verb")
    args = parser.parse_args()

    from app.models import (
        Bid,
        Document,
        DocumentBlock,
        DocumentVersion,
        Organization,
        RequirementVersion,
    )
    from app.models.enums import DocumentSide

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)
        bid_id = uuid.UUID(BID)
        if session.execute(
            select(Bid).where(Bid.org_id == org.id, Bid.id == bid_id)
        ).scalar_one_or_none() is None:
            print("Bid not visible under this tenant context.")
            return 1

        # Only this bid's TENDER text, and only the versions requirements were
        # actually drawn from.
        #
        # The first run swept every block in the organization, so most of its
        # output was the company knowledge pack -- CVs, the legal evidence
        # register, rate cards -- none of which is a tender obligation and all
        # of which correctly produced no requirement. The real findings were
        # buried under it.
        #
        # The version filter matters just as much. The same clause appears in
        # more than one DocumentVersion of the tender, and requirements attach
        # to one of them, so the other copy reports "produced NOTHING" for text
        # that is fully covered:
        #
        #   block 10  Design Requirements ...   produced: NOTHING
        #   block 10  Design Requirements ...   produced: R-058, R-060, R-069 ...
        #
        # Reporting that as a gap would send someone hunting for an obligation
        # that is already in the matrix.
        versions = {
            version_id
            for (version_id,) in session.execute(
                select(RequirementVersion.source_document_version_id).where(
                    RequirementVersion.org_id == org.id
                )
            ).all()
        }
        tender_versions = [
            v.id
            for v in session.execute(
                select(DocumentVersion)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    DocumentVersion.org_id == org.id,
                    Document.side == DocumentSide.TENDER,
                    Document.bid_id == bid_id,
                )
            ).scalars()
        ]
        wanted = [v for v in tender_versions if v in versions] or tender_versions
        if not wanted:
            print("No tender document versions found for this bid.")
            return 1
        blocks = (
            session.execute(
                select(DocumentBlock)
                .where(
                    DocumentBlock.org_id == org.id,
                    DocumentBlock.document_version_id.in_(wanted),
                )
                .order_by(DocumentBlock.char_start)
            )
            .scalars()
            .all()
        )
        print(
            f"{len(tender_versions)} tender document version(s) for this bid; "
            f"{len(wanted)} carry the extracted requirements.\n"
        )

        total = checked = uncovered = 0
        findings: list[tuple] = []
        for block in blocks:
            produced = _requirements_from_block(session, ctx, bid_id=bid_id, block=block)
            covering = [(r.code, _terms(text)) for r, text in produced]
            # The obligation test applies to the BLOCK, not to each sentence.
            #
            # Per sentence it discards exactly the items worth finding. "Back
            # Office Checker (For final application verification)" is a noun
            # phrase with no verb in it -- the duty lives in the lead-in above,
            # "Admins site should contain functions to create users and assign
            # them roles". A bullet inherits the obligation of the sentence
            # introducing it, and filtering bullet by bullet drops every list
            # item in the tender.
            block_binds = bool(OBLIGATION.search(block.text or ""))
            for sentence in _sentences(block.text or ""):
                total += 1
                if not args.all and not block_binds:
                    continue
                checked += 1
                terms = _terms(sentence)
                best_code, best, best_terms = None, 0.0, set()
                for code, req_terms in covering:
                    score = _covered(terms, req_terms)
                    if score > best:
                        best_code, best, best_terms = code, score, req_terms
                if best >= args.covered:
                    continue
                uncovered += 1
                findings.append(
                    (
                        block,
                        sentence,
                        best_code,
                        best,
                        [c for c, _ in covering],
                        # The words the closest requirement does not carry. This
                        # is what makes a report readable: "missing: checker,
                        # final" identifies the gap at a glance, where a
                        # percentage only says something is off.
                        sorted(terms - best_terms),
                    )
                )

        print(f"{len(blocks)} block(s), {total} candidate span(s), "
              f"{checked} carrying an obligation verb.")
        print(f"{uncovered} not covered by any requirement from their own block "
              f"(at {args.covered:.0%}).\n")

        for block, sentence, best_code, best, codes, missing in findings:
            head = " > ".join(block.heading_path or []) or "(no heading)"
            clause = block.clause_ref or f"block {block.ordinal}"
            print("=" * 100)
            print(f"{clause:10} {head}")
            print(textwrap.fill(sentence, width=96, initial_indent="  ",
                                subsequent_indent="  "))
            if best_code:
                print(f"  closest: {best_code} at {best:.0%}")
            if missing:
                print(f"  missing from it: {', '.join(missing[:12])}")
            print(f"  block produced: {', '.join(codes) if codes else 'NOTHING'}")

        print("=" * 100)

        # The vocabulary diff: words in a block that appear in NONE of the
        # requirements drawn from it.
        #
        # Proportional coverage cannot find a missing bullet, and that is the
        # failure this whole script exists to catch. "Back Office Checker (For
        # final application verification)" sits inside a sixty-word roles list;
        # two words of it are absent from the merged requirement, the span
        # scores in the nineties, and it passes. Same for "Qoute Separate".
        # Lowering the threshold far enough to catch them floods the report with
        # every ordinary rewording.
        #
        # So this asks a question with no threshold in it at all: which words
        # did the tender use here that no requirement from this block repeats?
        # A dropped bullet always leaves its own words behind, and they are
        # exactly what a reader needs to see.
        print("\nWORDS THE TENDER USES THAT NO REQUIREMENT FROM THAT BLOCK REPEATS")
        print("A dropped obligation leaves its words behind. Ordinary rewording")
        print("leaves few and dull ones; a lost bullet leaves its own nouns.\n")
        orphan_words: set[str] = set()
        for block in blocks:
            produced = _requirements_from_block(session, ctx, bid_id=bid_id, block=block)
            if not produced:
                continue
            covered_terms: set[str] = set()
            for _requirement, text in produced:
                covered_terms |= _terms(text)
            source_terms = _terms(block.text or "")
            orphans = sorted(source_terms - covered_terms)
            if not orphans:
                continue
            orphan_words |= set(orphans)
            clause = block.clause_ref or f"block {block.ordinal}"
            head = " > ".join(block.heading_path or []) or "(no heading)"
            print(f"  {clause:10} {head}")
            print(f"      {', '.join(orphans)}")
        print()

        print("=" * 100)
        print("\nthe five found by reading the PDF -- does this sweep see them?")
        text_of = {b.id: (b.text or "") for b in blocks}
        for phrase in KNOWN_MISSING:
            in_source = any(phrase.lower() in t.lower() for t in text_of.values())
            by_span = any(phrase.lower() in f[1].lower() for f in findings)
            # A phrase counts as found when its own distinctive words are among
            # the orphans, because that is how the vocabulary diff reports it --
            # "checker, final" rather than the whole sentence. Requiring the
            # span check alone is what made this report MISSED for two of the
            # five while the words were sitting in the list above.
            by_words = bool(_terms(phrase) & orphan_words)
            if not in_source:
                verdict = "NOT IN ANY BLOCK -- the parser never captured it"
            elif by_span and by_words:
                verdict = "flagged by both the span check and the word diff"
            elif by_words:
                verdict = "flagged by the word diff (the span check missed it)"
            elif by_span:
                verdict = "flagged by the span check only"
            else:
                verdict = "in the source, and this sweep MISSED it"
            print(f"  {phrase:42} {verdict}")
        print(
            "\n'NOT IN ANY BLOCK' means the text never reached extraction and the fix is\n"
            "in parsing, not in the prompt. 'MISSED it' means this sweep's threshold is\n"
            "too loose to be trusted on the gaps nobody has found by hand."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
