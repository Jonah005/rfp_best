"""Why did a rerun create a requirement instead of matching one?

Reconciliation is supposed to make re-analysing an unchanged document
idempotent: proposals from a block are assigned to the requirements that block
already produced, and only the surplus is new. It held at 42 rows for one run
and then let two duplicates through -- R-053 scoring 0.884 against R-016, R-054
scoring 0.958 against R-052, both far above the 0.75 assignment floor, and both
from the same block (d1b106ba, characters 8216-9413).

Every step of that decision is reachable from here, so this replays it rather
than reasoning about it. For each block that produced more than one live
requirement it prints:

  * what `_requirements_from_block` returns -- the candidate lookup, which is
    the step most likely to be silently empty, because it matches on a JSONB
    locator and a mismatch there looks exactly like "nothing to match against";
  * the stored locator beside the block's own offsets, so a type or key
    difference is visible rather than inferred;
  * the similarity of every requirement to every other from that block, against
    the two thresholds the pipeline uses.

Nothing is written.

    python scripts/check_reconciliation.py
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
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
from app.models.enums import RequirementLifecycle  # noqa: E402
from app.services.requirements import (  # noqa: E402
    _POSITIONAL_FLOOR,
    _SAME_OBLIGATION,
    _requirements_from_block,
    _similarity,
)


def main() -> int:
    from app.models import Bid, DocumentBlock, Organization, Requirement, RequirementVersion

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)
        print(f"thresholds: assignment floor {_POSITIONAL_FLOOR}, standalone {_SAME_OBLIGATION}\n")

        bids = (
            session.execute(select(Bid).where(Bid.org_id == org.id).order_by(Bid.created_at))
            .scalars()
            .all()
        )

        for bid in bids:
            rows = session.execute(
                select(
                    Requirement,
                    RequirementVersion.requirement_text,
                    RequirementVersion.source_document_version_id,
                    RequirementVersion.source_locator,
                )
                .join(RequirementVersion, RequirementVersion.requirement_id == Requirement.id)
                .where(
                    Requirement.org_id == org.id,
                    Requirement.bid_id == bid.id,
                    Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN,
                    RequirementVersion.superseded_at.is_(None),
                )
            ).all()
            if not rows:
                continue

            by_block: dict[tuple, list] = defaultdict(list)
            for requirement, text, version_id, locator in rows:
                locator = locator if isinstance(locator, dict) else {}
                key = (version_id, locator.get("char_start"), locator.get("char_end"))
                by_block[key].append((requirement, text or "", locator))

            crowded = {k: v for k, v in by_block.items() if len(v) > 1}

            print(f"=== bid {str(bid.id)[:8]}   {len(rows)} live requirements")
            if not crowded:
                print("  no block holds more than one requirement")
            for (version_id, char_start, char_end), entries in sorted(
                crowded.items(), key=lambda item: -len(item[1])
            ):
                codes = ", ".join(r.code for r, _, _ in entries)
                print(f"\n  block {str(version_id)[:8]} chars {char_start}-{char_end}: {codes}")
                print(f"    locator types: char_start={type(char_start).__name__} "
                      f"char_end={type(char_end).__name__}")

                # THE STEP THAT DECIDES. If this returns fewer rows than the
                # block actually holds, reconciliation had nothing to match
                # against and every proposal became a new requirement -- which
                # is the failure, and it leaves no trace anywhere else.
                block = session.execute(
                    select(DocumentBlock).where(
                        DocumentBlock.org_id == org.id,
                        DocumentBlock.document_version_id == version_id,
                        DocumentBlock.char_start == char_start,
                        DocumentBlock.char_end == char_end,
                    )
                ).scalars().first()
                if block is None:
                    print("    NO DOCUMENT BLOCK matches this locator.")
                    print("    Requirements point at a span the block table does not have, so")
                    print("    the candidate lookup finds nothing and every rerun starts fresh.")
                    continue
                print(f"    block row: ordinal={block.ordinal} "
                      f"chars {block.char_start}-{block.char_end} "
                      f"clause_ref={block.clause_ref!r}")

                candidates = _requirements_from_block(session, ctx, bid_id=bid.id, block=block)
                found = ", ".join(r.code for r, _ in candidates) or "(none)"
                print(f"    _requirements_from_block -> {len(candidates)}: {found}")
                if len(candidates) < len(entries):
                    print("    ^^ FEWER CANDIDATES THAN REQUIREMENTS FROM THIS BLOCK.")
                    print("       Reconciliation cannot match what it cannot see.")

                print("    pairwise similarity:")
                for i, (a, text_a, _) in enumerate(entries):
                    for b, text_b, _ in entries[i + 1 :]:
                        score = _similarity(text_a, text_b)
                        verdict = (
                            "MERGE" if score >= _SAME_OBLIGATION
                            else "assign" if score >= _POSITIONAL_FLOOR
                            else "distinct"
                        )
                        print(f"      {a.code} vs {b.code}: {score:.3f}  {verdict}")

            # Twins that DO NOT share a block key.
            #
            # The loop above only prints blocks holding more than one
            # requirement, so a duplicate whose two rows sit under DIFFERENT
            # keys is invisible to it -- each key looks like a quiet block of
            # one. That is not a cosmetic gap: the key is exactly what the
            # candidate lookup matches on, so a split key IS the failure. If
            # R-050 and R-055 read the same and their keys differ, nothing was
            # wrong with the thresholds or the assignment; reconciliation was
            # handed an empty candidate list and every proposal was new by
            # construction.
            #
            # So this compares every live requirement to every other regardless
            # of block, and prints the pairs that read alike but are filed
            # apart, with both keys in full.
            flat = [
                (requirement, text, locator, key)
                for key, entries in by_block.items()
                for requirement, text, locator in entries
            ]
            split: list[tuple] = []
            for i, (a, text_a, _loc_a, key_a) in enumerate(flat):
                for b, text_b, _loc_b, key_b in flat[i + 1 :]:
                    if key_a == key_b:
                        continue
                    score = _similarity(text_a, text_b)
                    if score >= _POSITIONAL_FLOOR:
                        split.append((a, key_a, b, key_b, score))
            if split:
                print(
                    f"\n  {len(split)} pair(s) read alike but are filed under DIFFERENT block "
                    "keys.\n  The candidate lookup matches on this key, so these rows could "
                    "never\n  have been compared to each other, whatever the similarity says:"
                )
                for a, key_a, b, key_b, score in sorted(split, key=lambda row: -row[-1]):
                    (va, sa, ea), (vb, sb, eb) = key_a, key_b
                    print(f"\n    ({score:.3f})  {a.code} vs {b.code}")
                    print(f"      {a.code}: doc {str(va)[:8]} chars {sa}-{ea}")
                    print(f"      {b.code}: doc {str(vb)[:8]} chars {sb}-{eb}")
                    if va != vb:
                        print("      ^^ DIFFERENT DOCUMENT VERSIONS. The same document was")
                        print("         ingested twice, so the second analysis matched against")
                        print("         nothing the first produced.")
                    else:
                        # Two spans in one document are two places in it. This
                        # line used to say "the block boundaries moved between
                        # runs", which was a guess and a wrong one: clause 2 and
                        # clause 3 of the live tender both contain the sentence
                        # "The workflow can be governed by implementing business
                        # validation rules...", so their requirements score 1.000
                        # and are both correct. A tender may state the same duty
                        # for two different things on purpose, and merging those
                        # would lose a citation.
                        print("      ^^ two places in the same document. Often legitimate:")
                        print("         a tender may state the same duty twice, for two")
                        print("         different subjects. Read both clauses before merging.")
            else:
                print("\n  No look-alike pairs across different block keys.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
