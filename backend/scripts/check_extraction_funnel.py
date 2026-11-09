"""Where do the tender's obligations go between parsing and extraction?

A tender a human reads as ~39 obligations produced 14 requirements. This counts
the funnel stage by stage so the narrow stage is measured rather than guessed.

What it found, and what was fixed:

    Requirement extraction read `eligible_blocks`, which returned only blocks
    that WON a current `ClauseResolution`. Resolutions are created one per
    distinct clause reference, and the reference was the bare `clause_ref`. This
    tender restarts numbering in every section, so 39 numbered blocks collapsed
    into 14 contests; precedence kept one winner each and the other 25 were
    discarded as losers of a dispute that never existed. Blocks with no
    reference had no resolution and were never eligible at all.

    `eligible_blocks` now reads every tender block and excludes one only when a
    different DOCUMENT VERSION supersedes it, and precedence keys on the
    qualified reference so unrelated clauses no longer contest.

`blocks eligible for extraction` calls the real `eligible_blocks`. An earlier
version of this script hand-copied the old query instead, which meant the
diagnostic reimplemented its own subject and kept reporting the pre-fix number
after the fix landed -- it read as proof the fix had done nothing.

Nothing is written. This only counts.

    python scripts/check_extraction_funnel.py
"""

from __future__ import annotations

import os
import sys
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

from sqlalchemy import func, select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.models.enums import DocumentSide, DocumentVersionState  # noqa: E402

# Words that make a block an obligation in nearly every tender written in
# English. Deliberately crude: this is a floor on how many obligations exist,
# not a replacement extractor. If the floor already exceeds the requirement
# count, the pipeline is losing obligations and the exact number does not
# matter to that conclusion.
OBLIGATION_WORDS = ("shall", "must", "is required", "are required", "responsible for")


def main() -> int:
    from app.models import (
        Bid,
        ClauseResolution,
        Document,
        DocumentBlock,
        DocumentVersion,
        Organization,
        Requirement,
    )
    from app.services.requirements import eligible_blocks

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        bids = (
            session.execute(select(Bid).where(Bid.org_id == org.id).order_by(Bid.created_at))
            .scalars()
            .all()
        )
        if not bids:
            print("No bids.")
            return 1

        for bid in bids:
            requirements = session.execute(
                select(func.count(Requirement.id)).where(
                    Requirement.org_id == org.id, Requirement.bid_id == bid.id
                )
            ).scalar_one()

            # Every block of every indexed tender document on this bid.
            tender_blocks = (
                session.execute(
                    select(DocumentBlock)
                    .join(DocumentVersion, DocumentVersion.id == DocumentBlock.document_version_id)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .where(
                        DocumentBlock.org_id == org.id,
                        Document.bid_id == bid.id,
                        Document.side == DocumentSide.TENDER,
                        Document.withdrawn_at.is_(None),
                        DocumentVersion.state.in_(
                            [DocumentVersionState.INDEXED, DocumentVersionState.ACTIVE]
                        ),
                    )
                )
                .scalars()
                .all()
            )
            if not tender_blocks:
                continue

            with_ref = [b for b in tender_blocks if b.clause_ref]
            without_ref = [b for b in tender_blocks if not b.clause_ref]

            def obliging(blocks):
                return [
                    b
                    for b in blocks
                    if any(word in (b.text or "").lower() for word in OBLIGATION_WORDS)
                ]

            resolutions = session.execute(
                select(func.count(ClauseResolution.id)).where(
                    ClauseResolution.org_id == org.id,
                    ClauseResolution.bid_id == bid.id,
                    ClauseResolution.is_current.is_(True),
                )
            ).scalar_one()
            # THE REAL FUNCTION, not a copy of it.
            #
            # The first version of this script hand-wrote the old eligibility
            # query here -- the join through ClauseResolution.winning_block_id.
            # That made the diagnostic reimplement the exact logic it exists to
            # measure, so after `eligible_blocks` was rewritten the script went
            # on reporting the pre-fix number and looked like proof the fix had
            # done nothing.
            #
            # A measurement that reproduces its subject measures itself. If this
            # count is ever wrong now, it is wrong because the pipeline is
            # wrong, which is the only kind of wrong worth reporting.
            eligible = len(eligible_blocks(session, ctx, bid_id=bid.id))

            name = ""
            facts = bid.tender_facts or {}
            if isinstance(facts, dict):
                name = str(facts.get("tender_name") or "")
            print(f"\n=== bid {str(bid.id)[:8]}  {name or bid.state}")
            print(f"  tender blocks parsed               {len(tender_blocks):>5}")
            print(
                f"    with a clause_ref                {len(with_ref):>5}"
                f"   (these can be contested by another document)"
            )
            print(
                f"    with NO clause_ref               {len(without_ref):>5}"
                f"   (uncontestable, and still extracted)"
            )
            print(f"  current clause resolutions         {resolutions:>5}")
            print(f"  blocks eligible for extraction     {eligible:>5}")
            print(f"  requirements created               {requirements:>5}")

            # The number that decides whether this is a real loss or a tidy
            # pipeline. An unnumbered block that says "shall" is an obligation
            # the system cannot see.
            lost = obliging(without_ref)
            # Kept as a check, not an alarm: these were invisible before the
            # fix. If `blocks eligible for extraction` equals `tender blocks
            # parsed`, none of them are being lost any more.
            print(
                f"\n  unnumbered blocks containing shall/must/required   {len(lost):>5}"
            )
            print(
                f"  numbered blocks containing shall/must/required     "
                f"{len(obliging(with_ref)):>5}"
            )
            for block in lost[:12]:
                text = " ".join((block.text or "").split())
                print(f"    - {text[:96]}")
            if len(lost) > 12:
                print(f"    ... and {len(lost) - 12} more")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
