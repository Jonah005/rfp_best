"""Do unrelated clauses collide on the same bare reference?

The funnel showed 39 numbered blocks becoming 14 clause resolutions on the
Jadwa bid, while a document with unique numbering lost almost nothing (87 -> 70).
Resolutions are created one per distinct `clause_ref`, so that gap is only
explicable if many blocks share a reference.

Precedence exists to answer "two documents both address clause 6.1, which one
wins". That question is only meaningful when the two really are the same
clause. This tender restarts numbering inside every section, so "3" is Business
3, Integration 3 and Technical 3 -- three unrelated obligations grouped into one
contest, of which precedence keeps exactly one winner and discards the rest.

This prints every clause_ref carried by more than one block, with the heading
each block sits under, so the collision is visible rather than inferred. It also
shows what the reference would be if qualified by its section.

Nothing is written.

    python scripts/check_clause_keys.py
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
from app.models.enums import DocumentSide, DocumentVersionState  # noqa: E402


def main() -> int:
    from app.models import Bid, Document, DocumentBlock, DocumentVersion, Organization
    from app.services.requirements import qualified_clause_ref

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

        for bid in bids:
            rows = (
                session.execute(
                    select(DocumentBlock, DocumentVersion.id)
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
                        DocumentBlock.clause_ref.is_not(None),
                    )
                    .order_by(DocumentBlock.ordinal)
                )
                .all()
            )
            if not rows:
                continue

            bare: dict[str, list] = defaultdict(list)
            qualified: dict[str, list] = defaultdict(list)
            for block, version_id in rows:
                bare[block.clause_ref].append((block, version_id))
                qualified[qualified_clause_ref(block) or block.clause_ref].append(
                    (block, version_id)
                )

            name = ""
            facts = bid.tender_facts or {}
            if isinstance(facts, dict):
                name = str(facts.get("tender_name") or "")
            print(f"\n=== bid {str(bid.id)[:8]}  {name or bid.state}")
            print(f"  numbered blocks                      {len(rows):>5}")
            print(f"  distinct BARE refs (today's key)      {len(bare):>5}")
            print(f"  distinct QUALIFIED refs (section/ref) {len(qualified):>5}")

            # A collision is only a genuine precedence contest if the competing
            # blocks come from DIFFERENT document versions. Two blocks in the
            # same document sharing a reference are not two documents
            # disagreeing -- they are one document whose numbering restarts,
            # and precedence throws one of them away for no reason.
            same_doc_collisions = {
                ref: entries
                for ref, entries in bare.items()
                if len(entries) > 1 and len({v for _, v in entries}) == 1
            }
            lost = sum(len(entries) - 1 for entries in same_doc_collisions.values())
            print(
                f"\n  refs colliding INSIDE one document   {len(same_doc_collisions):>5}"
                f"\n  blocks discarded by those collisions  {lost:>5}   <-- not a real contest"
            )

            for ref, entries in sorted(same_doc_collisions.items())[:10]:
                print(f"\n  clause_ref \"{ref}\" is used by {len(entries)} blocks:")
                for block, _ in entries:
                    heading = " > ".join(h for h in (block.heading_path or []) if h) or "(no heading)"
                    text = " ".join((block.text or "").split())
                    print(f"      [{heading}]")
                    print(f"        {text[:88]}")
            if len(same_doc_collisions) > 10:
                print(f"\n  ... and {len(same_doc_collisions) - 10} more colliding refs")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
