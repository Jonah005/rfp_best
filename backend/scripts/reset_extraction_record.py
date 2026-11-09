"""Show, and optionally clear, the record of which prompt read which block.

`block_extraction` is what makes a rerun a no-op: a block already read by the
current prompt version is not sent again. That is right when the code has not
changed, and wrong after a change to how blocks are matched, because then the
same prompt should read them again.

This exists because the obvious one-liner does not work and does not say so.
Every table here has row-level security FORCEd, so a session that has not set
`app.current_org` matches no rows -- and `DELETE FROM block_extraction` then
reports success having deleted nothing. That is how a "cleared" message came to
sit above an analysis run that skipped every block.

    python scripts/reset_extraction_record.py             # what is recorded
    python scripts/reset_extraction_record.py --detail    # per block, in document order
    python scripts/reset_extraction_record.py --clear     # forget it, per bid

Clearing loses no requirements and no history. It only means the next analysis
re-reads the document, which is exactly what is wanted after the matching rules
change.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
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

from sqlalchemy import delete, select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402


def main() -> int:
    clear = "--clear" in sys.argv
    only = None
    for index, value in enumerate(sys.argv):
        if value == "--bid" and index + 1 < len(sys.argv):
            only = sys.argv[index + 1].lower()

    from app.models import BlockExtraction, Organization

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        # The line the one-liner was missing. Without it every statement below
        # silently addresses an empty table.
        apply_tenant_context(session, ctx)

        rows = session.execute(
            select(
                BlockExtraction.bid_id,
                BlockExtraction.extracted_by,
                BlockExtraction.found,
                BlockExtraction.document_version_id,
                BlockExtraction.char_start,
                BlockExtraction.char_end,
            ).where(BlockExtraction.org_id == org.id)
        ).all()

        if not rows:
            print("block_extraction is empty: the next analysis will read every block.")
            if clear:
                print("Nothing to clear.")
            return 0

        counts: Counter = Counter()
        produced: Counter = Counter()
        for bid_id, extracted_by, found, _v, _s, _e in rows:
            counts[(str(bid_id), extracted_by)] += 1
            produced[(str(bid_id), extracted_by)] += found or 0

        print(f"{len(rows)} recorded reading(s):\n")
        for (bid_id, extracted_by), blocks in sorted(counts.items()):
            print(
                f"  bid {bid_id[:8]}  {extracted_by}  "
                f"{blocks} block(s), {produced[(bid_id, extracted_by)]} proposal(s)"
            )

        if "--detail" in sys.argv:
            # How many obligations each block yielded, in document order.
            #
            # The question this answers: decomposition is rich for the first
            # numbered clauses and returns exactly one requirement for every
            # clause after Business 7. A cutoff that sharp is either the model
            # tiring within a batch or the response hitting max_output_tokens --
            # and both would show here as a run of 1s starting at a particular
            # position, rather than as a property of the clauses themselves.
            from app.models import DocumentBlock

            blocks = {
                (b.document_version_id, b.char_start, b.char_end): b
                for b in session.execute(
                    select(DocumentBlock).where(DocumentBlock.org_id == org.id)
                ).scalars()
            }
            detailed = []
            for _bid_id, extracted_by, found, version_id, start, end in rows:
                block = blocks.get((version_id, start, end))
                detailed.append((block.ordinal if block else -1, found, block, extracted_by))
            detailed.sort()
            print("\nper block, in document order:")
            for ordinal, found, block, _stamp in detailed:
                ref = block.clause_ref if block else "?"
                size = len(block.text or "") if block else 0
                bar = "#" * min(found, 20)
                print(
                    f"  ordinal {ordinal:>3}  clause_ref={str(ref):<6}  "
                    f"{size:>5} chars  found {found:>2}  {bar}"
                )

        if not clear:
            print(
                "\nThese blocks will be SKIPPED by the next analysis under the same prompt\n"
                "version. Add --clear (optionally --bid <prefix>) to have them read again."
            )
            return 0

        statement = delete(BlockExtraction).where(BlockExtraction.org_id == org.id)
        if only:
            matching = {
                row[0] for row in rows if str(row[0]).lower().startswith(only)
            }
            if len(matching) != 1:
                print(f"{len(matching)} bids match {only!r}; name one exactly.")
                return 1
            statement = statement.where(BlockExtraction.bid_id == next(iter(matching)))

        deleted = session.execute(statement).rowcount
        session.commit()
        # The count is reported because "cleared" on its own is what made the
        # failing one-liner look like it had worked.
        print(f"\nDeleted {deleted} row(s). The next analysis will read those blocks again.")
        if deleted == 0:
            print(
                "  Zero is suspicious given the listing above -- check that the tenant\n"
                "  context applies to the same organization the rows belong to."
            )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
