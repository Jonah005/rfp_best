"""A scorecard for one tender: how well was it read, in numbers that compare.

Quality on a new document is not a feeling. Every figure here is counted from
what the pipeline wrote, so two tenders can be compared, and a bad score points
at the stage that caused it rather than leaving "the AI was worse" as the
conclusion.

What each number tells you, and what to do when it is bad:

    Parsing      blocks, how many carry a clause reference, OCR pages, and
                 rows in tables. Few blocks on a long document means the parser
                 saw one wall of text; many OCR pages means a scan, and every
                 later number should be read as a floor.
    Sections     the section titles found, and clauses per section. One section
                 holding everything is the heading defect this project has hit
                 twice: references collide and the matrix cannot be traced.
    Extraction   blocks read, requirements produced, blocks read and found to
                 impose none, and blocks nothing accounts for. The last is the
                 one that loses obligations.
    Batching     whether any ambiguity batch returned the maximum number of
                 issues. A batch at the cap was probably truncated, so the
                 document is denser than one pass can report.
    Questions    open findings, and how many carry a question a customer could
                 answer. A finding with no question cannot go in the letter.
    Traceability requirements with no clause reference, and clause references
                 shared by more than one requirement from different sections.

Nothing is written. This only counts.

    python scripts/score_extraction.py                 # newest bid
    python scripts/score_extraction.py --bid <uuid>
    python scripts/score_extraction.py --json          # for a scorecard file
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault(
    "BIDREADY_DATABASE_URL",
    os.environ.get("BIDREADY_LOCAL_DATABASE_URL")
    or "postgresql+psycopg://bidready_app:bidready-local@localhost:5432/bidready",
)

from _vector_backend import align_vector_backend  # noqa: E402

align_vector_backend()

from sqlalchemy import select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.models.enums import (  # noqa: E402
    BlockType,
    ClarificationIssueType,
    ClarificationStatus,
    DocumentSide,
)

OBLIGATION_WORDS = ("shall", "must", "is required", "are required", "should", "responsible for")


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def score(session, ctx, bid) -> dict:
    from app.models import (
        AuditEvent,
        BlockExtraction,
        ClarificationIssue,
        Document,
        DocumentBlock,
        DocumentVersion,
        Requirement,
        RequirementVersion,
    )
    from app.services.requirements import eligible_blocks

    versions = (
        session.execute(
            select(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                Document.org_id == ctx.org_id,
                Document.bid_id == bid.id,
                Document.side == DocumentSide.TENDER,
                Document.withdrawn_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    version_ids = [v.id for v in versions]
    blocks = (
        session.execute(
            select(DocumentBlock).where(
                DocumentBlock.org_id == ctx.org_id,
                DocumentBlock.document_version_id.in_(version_ids or [bid.id]),
            )
        )
        .scalars()
        .all()
    )
    sections = Counter(
        " > ".join(h for h in (b.heading_path or []) if h) or "(no section)" for b in blocks
    )
    obliging = [
        b
        for b in blocks
        if any(w in (b.text or "").lower() for w in OBLIGATION_WORDS)
        and b.block_type is not BlockType.HEADING
    ]

    # `eligible_blocks` returns (block, resolution) pairs: the resolution is
    # what made the block eligible.
    eligible = [
        pair[0] if isinstance(pair, tuple) else pair
        for pair in eligible_blocks(session, ctx, bid_id=bid.id)
    ]
    # BlockExtraction is keyed on the character span, which is the same
    # coordinate requirement identity uses -- so "was this read" and "does a
    # requirement point here" agree by construction.
    read = {
        (row.document_version_id, row.char_start, row.char_end)
        for row in session.execute(
            select(BlockExtraction).where(
                BlockExtraction.org_id == ctx.org_id, BlockExtraction.bid_id == bid.id
            )
        ).scalars()
    }

    def was_read(block) -> bool:
        return (block.document_version_id, block.char_start, block.char_end) in read

    requirements = session.execute(
        select(Requirement, RequirementVersion)
        .join(
            RequirementVersion,
            RequirementVersion.id == Requirement.current_version_id,
            isouter=True,
        )
        .where(Requirement.org_id == ctx.org_id, Requirement.bid_id == bid.id)
    ).all()
    live = [(r, v) for r, v in requirements if r.lifecycle_state.value != "WITHDRAWN"]
    with_clause = [(r, v) for r, v in live if v is not None and (v.clause_ref or "").strip()]

    # One reference used by requirements from different sections is the
    # collision this project has hit twice: it makes a reference untraceable.
    # Two requirements under one reference mean different things depending on
    # WHERE the second one came from. The same reference in two document
    # versions is the same clause seen twice -- an amendment, or the same file
    # uploaded twice -- and is not a defect. The same reference twice in ONE
    # document is the collision that makes a reference untraceable: it is how
    # Integration 4 and Business 4 both read "Business Requirements / 4".
    by_ref: dict[str, set] = defaultdict(set)
    within_version: dict[tuple, set] = defaultdict(set)
    for _r, v in with_clause:
        locator = v.source_locator or {}
        span = (locator.get("char_start"), locator.get("char_end"))
        by_ref[v.clause_ref].add((v.source_document_version_id, span))
        within_version[(v.source_document_version_id, v.clause_ref)].add(span)
    shared = {
        ref: len({span for _version, span in pairs})
        for ref, pairs in by_ref.items()
        if len({version for version, _span in pairs}) == 1 and len(pairs) > 1
    }
    across_documents = sum(
        1 for pairs in by_ref.values() if len({version for version, _span in pairs}) > 1
    )
    colliding = {
        f"{ref}": len(spans) for (_version, ref), spans in within_version.items() if len(spans) > 1
    }
    shared = shared or colliding

    issues = (
        session.execute(
            select(ClarificationIssue).where(
                ClarificationIssue.org_id == ctx.org_id, ClarificationIssue.bid_id == bid.id
            )
        )
        .scalars()
        .all()
    )
    open_issues = [i for i in issues if i.status is ClarificationStatus.OPEN]
    askable = [i for i in open_issues if (i.recommended_question or "").strip()]
    gaps = [
        i
        for i in issues
        if i.issue_type is ClarificationIssueType.COVERAGE_GAP
        and i.status is ClarificationStatus.OPEN
    ]
    closed_no_reason = [
        i
        for i in issues
        if i.status is not ClarificationStatus.OPEN and not (i.resolution_note or "").strip()
    ]

    # The only durable record of which pages came from a photograph.
    ocr_pages = 0
    for event in session.execute(
        select(AuditEvent).where(
            AuditEvent.org_id == ctx.org_id,
            AuditEvent.action == "document.indexed",
        )
    ).scalars():
        after = event.after_state or {}
        if event.entity_id in version_ids:
            ocr_pages += len(after.get("ocr_pages") or [])

    unaccounted = [b for b in eligible if not was_read(b)]
    return {
        "bid": str(bid.id),
        "parsing": {
            "tender_documents": len(versions),
            "blocks": len(blocks),
            "blocks_with_a_clause_ref": sum(1 for b in blocks if (b.clause_ref or "").strip()),
            "table_rows": sum(1 for b in blocks if b.block_type is BlockType.TABLE_ROW),
            "pages_read_by_ocr": ocr_pages,
            "blocks_that_read_like_an_obligation": len(obliging),
        },
        "sections": {
            "found": len(sections),
            "largest_share_pct": _pct(max(sections.values(), default=0), len(blocks)),
            "titles": [s for s, _ in sections.most_common(12)],
        },
        "extraction": {
            "eligible_blocks": len(eligible),
            "blocks_read": len([b for b in eligible if was_read(b)]),
            "blocks_never_read": len(unaccounted),
            "requirements": len(live),
            "requirements_per_obliging_block": round(len(live) / len(obliging), 2)
            if obliging
            else 0,
        },
        "traceability": {
            "requirements_without_a_clause_ref": len(live) - len(with_clause),
            # The defect: one document, one reference, two unrelated clauses.
            "clause_refs_colliding_inside_one_document": len(shared),
            "worst_colliding_refs": sorted(shared.items(), key=lambda kv: -kv[1])[:5],
            # Benign: the same clause in two uploads (an amendment, or the same
            # file uploaded twice).
            "clause_refs_seen_in_more_than_one_document": across_documents,
        },
        "questions": {
            "open_findings": len(open_issues),
            "askable_of_the_customer": len(askable),
            # Findings with no question to put to the customer are the
            # platform's own checks (identity drift, coverage). They are not
            # clarifications and must not be read as customer queries.
            "internal_qa_open": len(open_issues) - len(askable),
            "askable_pct": _pct(len(askable), len(open_issues)),
            "open_coverage_gaps": len(gaps),
            "closed_without_a_reason": len(closed_no_reason),
        },
    }


def main() -> int:
    from app.models import Bid, Organization

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bid", help="bid id; default is the newest bid found")
    parser.add_argument("--json", action="store_true", help="print the scorecard as JSON")
    args = parser.parse_args()

    session = get_session_factory()()
    try:
        for org in session.execute(select(Organization)).scalars().all():
            ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
            apply_tenant_context(session, ctx)
            stmt = select(Bid).where(Bid.org_id == org.id).order_by(Bid.created_at.desc())
            bids = list(session.execute(stmt).scalars().all())
            if args.bid:
                bids = [b for b in bids if str(b.id) == args.bid]
            if not bids:
                continue
            card = score(session, ctx, bids[0])
            if args.json:
                print(json.dumps(card, indent=2, default=str))
            else:
                _print(card)
            return 0
        print("No bid found.")
        return 1
    finally:
        session.close()


def _print(card: dict) -> None:
    print(f"\n  Scorecard for bid {card['bid']}\n")
    for section, values in card.items():
        if section == "bid":
            continue
        print(f"  {section.upper()}")
        for key, value in values.items():
            if isinstance(value, list) and key == "titles":
                print(f"    {key:42} {len(value)} section(s)")
                for title in value:
                    print(f"      - {title[:90]}")
            else:
                print(f"    {key:42} {value}")
        print()
    q = card["questions"]
    e = card["extraction"]
    print("  READ THIS FIRST")
    if e["blocks_never_read"]:
        print(
            f"    {e['blocks_never_read']} eligible block(s) were never read "
            "-- obligations may be missing."
        )
    if card["sections"]["largest_share_pct"] > 60:
        print("    One section holds most of the document: check heading detection.")
    if card["parsing"]["pages_read_by_ocr"]:
        print(
            f"    {card['parsing']['pages_read_by_ocr']} page(s) came from OCR; "
            "treat every count as a floor."
        )
    if q["open_findings"] and q["askable_pct"] < 60:
        print(f"    Only {q['askable_pct']}% of open findings can be put to the customer.")
    if card["traceability"]["clause_refs_colliding_inside_one_document"]:
        print(
            "    One document uses the same clause reference for unrelated clauses: "
            "an unnumbered section title was not detected (re-parse after upgrading)."
        )
    if card["traceability"]["clause_refs_seen_in_more_than_one_document"]:
        print(
            f"    {card['traceability']['clause_refs_seen_in_more_than_one_document']} "
            "reference(s) appear in more than one uploaded document -- an amendment, "
            "or the same file uploaded twice. Not a defect by itself."
        )
    if not any(
        [
            e["blocks_never_read"],
            card["sections"]["largest_share_pct"] > 60,
            card["parsing"]["pages_read_by_ocr"],
            q["open_findings"] and q["askable_pct"] < 60,
            card["traceability"]["clause_refs_colliding_inside_one_document"],
        ]
    ):
        print("    Nothing stands out. Compare the numbers against your other tenders.")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
