"""How often does the parser separate a lead-in sentence from what it introduces?

Blocks are split on blank lines (`re.split(r"\\n\\s*\\n", text)`). A paragraph
that ends in a colon is not a statement -- it is the subject of the list that
follows, and the blank line between them puts the two in different blocks.

Block 26 of the Jadwa tender is the consequence. Its whole text is:

    Create Beneficary accounts
    Create Electronic trading account with defined settings
    Auto approval of compliance based on new system approval

Three bullets, no subject. The sentence that supplies it -- "Jadwa will provide
access to Core OMS system for the following purpose:" -- is a different block.
Asked what this block obliges, the model has to guess, and it guessed
differently on every run: eight requirements, scoring 0.50 against each other,
none of them quoting the block.

Before changing how documents are split, this counts the pattern. One orphaned
lead-in is a row to fix by hand. Thirty is a parser change, and the character
spans it would move are the coordinates requirement identity rests on -- so the
number decides whether that migration is worth writing.

Reported per tender document version:

  * blocks whose text ends in a colon, with the block that follows;
  * whether the follower has a clause reference of its own (if it does, it can
    stand alone and the split costs nothing);
  * how many requirements are currently filed against each follower, and their
    average word-coverage against it -- the orphans are where extraction has
    been inventing subjects.

Nothing is written.

    python scripts/check_orphan_leadins.py [BID_PREFIX]
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

DEFAULT_BID_PREFIX = "0e20677e"


def _coverage(clause: str, block_text: str) -> float:
    words = [w for w in clause.lower().split() if len(w) > 3]
    if not words:
        return 1.0
    haystack = block_text.lower()
    return sum(1 for word in words if word.strip(".,;:()") in haystack) / len(words)


def main() -> int:
    prefix = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BID_PREFIX).lower()

    from app.models import Bid, DocumentBlock, Organization, Requirement, RequirementVersion

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        bids = [
            bid
            for bid in session.execute(select(Bid).where(Bid.org_id == org.id)).scalars().all()
            if str(bid.id).lower().startswith(prefix)
        ]
        if len(bids) != 1:
            print(f"{len(bids)} bids match {prefix!r}; name one exactly.")
            return 1
        bid = bids[0]

        rows = session.execute(
            select(
                Requirement.code,
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
        by_span: dict[tuple, list] = defaultdict(list)
        for code, text, version_id, locator in rows:
            locator = locator if isinstance(locator, dict) else {}
            by_span[(version_id, locator.get("char_start"), locator.get("char_end"))].append(
                (code, text or "")
            )

        version_ids = sorted({key[0] for key in by_span if key[0] is not None}, key=str)
        total_orphans = 0

        for version_id in version_ids:
            blocks = (
                session.execute(
                    select(DocumentBlock)
                    .where(
                        DocumentBlock.org_id == org.id,
                        DocumentBlock.document_version_id == version_id,
                    )
                    .order_by(DocumentBlock.ordinal)
                )
                .scalars()
                .all()
            )
            if not blocks:
                continue

            print("=" * 78)
            print(f"document version {str(version_id)[:8]}: {len(blocks)} blocks")

            orphans = []
            for index, block in enumerate(blocks[:-1]):
                if not text_of(block).endswith(":"):
                    continue
                follower = blocks[index + 1]
                orphans.append((block, follower))

            print(f"  {len(orphans)} block(s) end in a colon\n")
            total_orphans += len(orphans)

            for block, follower in orphans:
                filed = by_span.get(
                    (version_id, follower.char_start, follower.char_end), []
                )
                # A follower with its own clause reference is self-identifying;
                # the split costs nothing there. The damage is confined to
                # followers that are bare continuations.
                standalone = follower.clause_ref is not None
                note = "  (follower has its own clause_ref -- harmless)" if standalone else ""
                print(f"  ordinal {block.ordinal} -> {follower.ordinal}{note}")
                print(f"    lead-in : {' '.join(text_of(block).split())[:110]}")
                print(f"    follower: {' '.join(text_of(follower).split())[:110]}")
                if filed:
                    scores = [_coverage(clause, follower.text or "") for _, clause in filed]
                    average = sum(scores) / len(scores)
                    flag = "   <== subjects being invented" if average < 0.8 else ""
                    print(
                        f"    {len(filed)} requirement(s) filed against the follower, "
                        f"average coverage {average:.0%}{flag}"
                    )
                    print(f"      {', '.join(code for code, _ in filed)}")
                else:
                    print("    no requirements filed against the follower")
                print()

        print("=" * 78)
        print(
            f"{total_orphans} lead-in/follower pair(s) in total.\n"
            "A handful is a hand-fix. Many is a parser change -- and that moves character\n"
            "spans, which are the coordinate requirement identity is keyed on, so it needs\n"
            "a migration that remaps existing locators rather than an edit to the splitter."
        )
        return 0
    finally:
        session.close()


def text_of(block) -> str:
    return (block.text or "").strip()


if __name__ == "__main__":
    sys.exit(main())
