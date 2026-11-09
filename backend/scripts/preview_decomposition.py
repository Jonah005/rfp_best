"""Run the decomposition prompt and show what it WOULD do. Write nothing.

Prompt v6 rewrote 33 live requirements into fragments of themselves before
anybody saw a single one of its outputs. The obligation text was recoverable
only because versions exist; the hours spent finding out were not. Since then
every prompt change has been a change made blind, and a harness to preview one
has been agreed and postponed repeatedly.

This is that harness. It calls the CURRENTLY REGISTERED
`requirement_decomposition` prompt against the tender's stored blocks, exactly as
`extract_requirements` does -- same payload, same batching, same
`continues_from` context -- and prints the proposals beside the requirements
that block already has. Nothing is written, no version is superseded, no
requirement is created. The session is rolled back at the end even though
nothing should have staged, because "should have" is not a guarantee worth
betting live data on.

Reading the output, per block:

    KEEPS      a proposal matching an existing requirement closely
    NEW        a proposal with no counterpart -- what the change adds
    ABSORBED   an existing requirement whose every word survives in a proposal
    LOSES      an existing requirement whose words do NOT survive anywhere

LOSES is the column that matters, and ABSORBED exists because the first version
of this harness could not tell the two apart: a prompt that puts fragments back
together shows every fragment as unmatched, and reporting that as loss argues
against exactly the change the harness was built to validate.

The comparison is character similarity against the same helper reconciliation
uses, so this and the pipeline cannot disagree about what "already there" means.
It is a reading aid, not a verdict: proposals are printed in full so a person
judges the wording.

    python scripts/preview_decomposition.py                  # whole tender
    python scripts/preview_decomposition.py --clause 10,11   # just these
    python scripts/preview_decomposition.py --limit 6        # first N blocks
    python scripts/preview_decomposition.py --match Tradenet,Al-Elm   # blocks by their text

Costs one model call per batch, so `--clause` or `--limit` while iterating on a
prompt, and the full run once before applying it.
"""

from __future__ import annotations

import argparse
import json
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
from app.gateway.gateway import build_gateway  # noqa: E402
from app.gateway.registry import get as get_call_type  # noqa: E402
from app.gateway.registry import wrap_untrusted  # noqa: E402
from app.models.enums import DocumentSide  # noqa: E402
from app.schemas.tender_extraction import RequirementDecompositionResult  # noqa: E402
from app.services.requirements import (  # noqa: E402
    _SAME_OBLIGATION,
    _preceding_text,
    _requirements_from_block,
    _similarity,
)

BID = "0e20677e-9cc3-434e-ad88-c953d6676028"

# How close a proposal must be to an existing requirement to count as the same
# one. Deliberately the pipeline's own standalone bar, so "KEEPS" here means
# what reconciliation would call a match.
SAME = _SAME_OBLIGATION


STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "for",
    "with",
    "by",
    "at",
    "from",
    "as",
    "that",
    "this",
    "these",
    "those",
    "it",
    "its",
    "is",
    "are",
    "be",
    "will",
    "shall",
    "should",
    "must",
    "may",
    "can",
    "such",
    "their",
    "they",
    "we",
    "our",
    "any",
    "all",
    "like",
    "other",
}


def _terms(text: str) -> set[str]:
    """Content words, for the absorption test.

    Word-level rather than character similarity, because absorption is a
    containment question -- did every word of this requirement survive
    somewhere -- and `_similarity` answers a resemblance question, which a
    combined row and one of its fragments fail by construction.
    """
    words = re.findall(r"[a-z0-9][a-z0-9\-/.]*", (text or "").lower())
    return {w.strip("-/.") for w in words if len(w) > 2 and w not in STOP}


def _wrap(text: str, indent: str) -> str:
    return textwrap.fill(
        " ".join((text or "").split()),
        width=98,
        initial_indent=indent,
        subsequent_indent=" " * len(indent),
    )


def main() -> int:
    import uuid

    parser = argparse.ArgumentParser()
    parser.add_argument("--clause", default="", help="comma-separated clause refs to preview")
    parser.add_argument(
        "--match",
        default="",
        help="comma-separated words; keep blocks whose TEXT contains any of them",
    )
    parser.add_argument("--limit", type=int, default=0, help="preview only the first N blocks")
    parser.add_argument("--batch", type=int, default=6, help="blocks per model call (6)")
    args = parser.parse_args()

    from app.models import Document, DocumentBlock, DocumentVersion, Organization

    call_type = get_call_type("requirement_decomposition")
    stamp = f"llm:{call_type.name}@{call_type.prompt_version}"
    print(f"previewing {stamp} -- nothing will be written\n")

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)
        bid_id = uuid.UUID(BID)

        versions = [
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
        blocks = list(
            session.execute(
                select(DocumentBlock)
                .where(
                    DocumentBlock.org_id == org.id,
                    DocumentBlock.document_version_id.in_(versions),
                )
                .order_by(DocumentBlock.char_start)
            ).scalars()
        )
        wanted = {c.strip() for c in args.clause.split(",") if c.strip()}

        def names(block) -> set[str]:
            """Every way a person might write this block's reference.

            The exports show "Integration Requirements / 4", but the block
            stores only "4" and keeps the section in its heading path, and
            "4" alone matches a block in every section of the tender.
            """
            ref = (block.clause_ref or "").strip()
            found = {ref} if ref else set()
            for heading in reversed(block.heading_path or []):
                if ref and heading:
                    found.add(f"{heading.strip()} / {ref}")
                    break
            return found

        everything = blocks
        if wanted:
            blocks = [b for b in blocks if names(b) & wanted]
        needles = [m.strip().lower() for m in args.match.split(",") if m.strip()]
        if needles:
            blocks = [b for b in blocks if any(n in (b.text or "").lower() for n in needles)]
        if args.limit:
            blocks = blocks[: args.limit]
        if not blocks:
            print("No blocks selected. References this tender has:")
            for b in everything:
                shown = sorted(names(b) - {(b.clause_ref or "").strip()}) or [b.clause_ref or "-"]
                print(f"  {shown[0]}")
            return 1

        gateway = build_gateway()
        # `_preceding_text` expects the (block, resolution) pairs extraction
        # builds. Resolution is irrelevant to the prompt, so None stands in --
        # the point is to reproduce the payload, not the write path.
        pairs = [(b, None) for b in blocks]
        preceding = _preceding_text(session, ctx, pairs=pairs)

        total_new = total_lost = total_kept = total_absorbed = 0
        for start in range(0, len(blocks), args.batch):
            batch = blocks[start : start + args.batch]
            payload = {
                "clause_blocks": [
                    {
                        "ordinal": b.ordinal,
                        "clause_ref": b.clause_ref,
                        "heading_path": b.heading_path or [],
                        "block_type": b.block_type.value,
                        "page_from": b.page_from,
                        "text": b.text,
                        **(
                            {"continues_from": preceding[b.ordinal]}
                            if b.ordinal in preceding
                            else {}
                        ),
                    }
                    for b in batch
                ]
            }
            result = gateway.generate_structured(
                "requirement_decomposition",
                wrap_untrusted(json.dumps(payload, ensure_ascii=False, default=str)),
                ctx=ctx,
                session=session,
            )
            if not isinstance(result, RequirementDecompositionResult):
                print("requirement_decomposition returned an unexpected schema.")
                return 1

            by_ordinal: dict[int, list] = {}
            for proposal in result.requirements:
                by_ordinal.setdefault(proposal.source_block_ordinal, []).append(proposal)

            for block in batch:
                existing = _requirements_from_block(session, ctx, bid_id=bid_id, block=block)
                proposals = by_ordinal.get(block.ordinal, [])
                clause = block.clause_ref or f"block {block.ordinal}"
                head = " > ".join(block.heading_path or []) or "(no heading)"
                print("=" * 100)
                print(f"{clause:10} {head}")
                print(f"  now: {len(existing)} requirement(s)   proposed: {len(proposals)}")
                if block.ordinal in result.no_requirement_block_ordinals:
                    print("  the model says this block imposes no obligation")

                matched_existing: set[int] = set()
                for proposal in proposals:
                    text = proposal.requirement_text
                    best_index, best = None, 0.0
                    for index, (_requirement, current) in enumerate(existing):
                        score = _similarity(text, current)
                        if score > best:
                            best_index, best = index, score
                    if best >= SAME and best_index is not None:
                        matched_existing.add(best_index)
                        code = existing[best_index][0].code
                        print(_wrap(text, f"  KEEPS {code} ({best:.0%}) "))
                        total_kept += 1
                    else:
                        near = (
                            f" closest {existing[best_index][0].code} {best:.0%}"
                            if best_index is not None
                            else ""
                        )
                        print(_wrap(text, f"  NEW{near} "))
                        total_new += 1

                for index, (requirement, current) in enumerate(existing):
                    if index in matched_existing:
                        continue
                    # Absorbed is not lost.
                    #
                    # A prompt that coalesces fragments back into the clause they
                    # came from shows every fragment as unmatched, because none
                    # of them is 90% similar to the combined row. On the timing
                    # block that printed three LOSES for rows whose every word
                    # survives in one proposal -- alongside a fourth item the
                    # fragments never had. Reported as loss, it argues against
                    # exactly the change it was built to validate.
                    #
                    # So the question for an unmatched requirement is not "does a
                    # proposal resemble it" but "do its words survive somewhere".
                    holder = None
                    for proposal in proposals:
                        if not _terms(current) - _terms(proposal.requirement_text):
                            holder = proposal
                            break
                    if holder is not None:
                        print(_wrap(current, f"  ABSORBED {requirement.code} "))
                        total_absorbed += 1
                        continue
                    print(_wrap(current, f"  LOSES {requirement.code} "))
                    total_lost += 1

        print("=" * 100)
        print(
            f"\nacross {len(blocks)} block(s): {total_kept} kept, {total_new} new, "
            f"{total_absorbed} absorbed into a proposal, {total_lost} LOST."
        )
        print(
            "\nLOST is the number to read, and only that one. ABSORBED means an existing\n"
            "requirement's every word survives inside a proposal -- fragments being put\n"
            "back together, which is a gain and reads as loss only to a count that cannot\n"
            "tell a superset from a deletion.\n\n"
            "Anything under LOST is wording the bid would stop carrying. v6 would have\n"
            "shown 33 of them and showed nobody."
        )
        return 0
    finally:
        # Nothing above writes, and the rollback is here anyway: a preview that
        # can alter live requirements is not a preview.
        session.rollback()
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
