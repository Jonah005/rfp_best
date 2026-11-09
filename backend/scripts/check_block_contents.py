"""Print each crowded block's own text beside the requirements drawn from it.

A rerun on an unchanged document took the bid from 43 live requirements to 51.
The eight new rows are not duplicates -- every pairwise score is well below the
assignment floor -- so the clause-vs-restatement defect is not what produced
them. Something else multiplies requirements on rerun, and every hypothesis
about it is guesswork until the block text is on the screen.

One arithmetic fact says the answer is here rather than in the matcher. Block
ordinal 26 spans characters 13144-13289: one hundred and forty-five characters.
It carries eight requirements, and R-050's clause alone is longer than the whole
span. Either the stored locator does not describe the text the model was shown,
or the model is producing obligations the block does not contain. Those are
different faults with different fixes, and the block text distinguishes them
immediately.

For every block holding more than one live requirement this prints:

  * the block's own stored text, in full, with its true character length beside
    the length its locator claims -- a mismatch there means requirements are
    filed against coordinates that do not describe their source;
  * every live requirement from that block, with its clause and how much of
    that clause can be found in the block text at all.

The last column is the one to read. A requirement whose words are largely absent
from its own source block was not extracted from it.

Nothing is written.

    python scripts/check_block_contents.py [BID_PREFIX]
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
    """How much of the clause actually appears in the block.

    Not a similarity score -- the block is longer than any one clause, so
    symmetric similarity would read low for a perfectly good extraction. This
    asks only: of the clause's words, what fraction occur in the block? A
    faithful extraction is near 1.0 whatever else the block contains.
    """
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
                Requirement,
                RequirementVersion.requirement_text,
                RequirementVersion.source_document_version_id,
                RequirementVersion.source_locator,
                RequirementVersion.extracted_by,
            )
            .join(RequirementVersion, RequirementVersion.requirement_id == Requirement.id)
            .where(
                Requirement.org_id == org.id,
                Requirement.bid_id == bid.id,
                Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN,
                RequirementVersion.superseded_at.is_(None),
            )
        ).all()

        by_block: dict[tuple, list] = defaultdict(list)
        for requirement, text, version_id, locator, extracted_by in rows:
            locator = locator if isinstance(locator, dict) else {}
            key = (version_id, locator.get("char_start"), locator.get("char_end"))
            by_block[key].append((requirement, text or "", extracted_by or ""))

        crowded = {k: v for k, v in by_block.items() if len(v) > 1}
        print(f"bid {str(bid.id)[:8]}: {len(rows)} live requirements, "
              f"{len(crowded)} block(s) holding more than one\n")

        for (version_id, char_start, char_end), entries in sorted(
            crowded.items(), key=lambda item: -len(item[1])
        ):
            block = session.execute(
                select(DocumentBlock).where(
                    DocumentBlock.org_id == org.id,
                    DocumentBlock.document_version_id == version_id,
                    DocumentBlock.char_start == char_start,
                    DocumentBlock.char_end == char_end,
                )
            ).scalars().first()

            span = (char_end or 0) - (char_start or 0)
            print("=" * 78)
            print(f"block chars {char_start}-{char_end}  (span {span} chars)  "
                  f"{len(entries)} requirements")
            if block is None:
                print("  NO BLOCK ROW at this locator.")
                continue

            text = block.text or ""
            print(f"  ordinal={block.ordinal}  clause_ref={block.clause_ref!r}  "
                  f"heading={block.heading_path!r}")
            # The span the locator claims against the text actually stored. If
            # these disagree, the coordinate that identity rests on is not
            # describing the text the model saw.
            flag = "" if len(text) == span else "   <== LENGTH DISAGREES WITH LOCATOR SPAN"
            print(f"  stored text is {len(text)} chars{flag}")
            print("  ---- block text ----")
            for line in text.splitlines() or [""]:
                print(f"  | {line}")
            print("  --------------------")

            print(f"\n  {len(entries)} requirement(s) filed against this block:")
            for requirement, clause, extracted_by in sorted(entries, key=lambda e: e[0].code):
                covered = _coverage(clause, text)
                mark = "" if covered >= 0.8 else "   <== NOT IN THIS BLOCK"
                print(f"\n    {requirement.code}  words found in block: {covered:.0%}{mark}")
                print(f"      extracted_by: {extracted_by}")
                print(f"      {' '.join(clause.split())[:160]}")
            print()

        # A whole-document check on the same question, because a block that
        # produced exactly one requirement is not exempt from this fault -- it
        # is only harder to notice.
        print("=" * 78)
        worst = []
        for (version_id, char_start, char_end), entries in by_block.items():
            block = session.execute(
                select(DocumentBlock).where(
                    DocumentBlock.org_id == org.id,
                    DocumentBlock.document_version_id == version_id,
                    DocumentBlock.char_start == char_start,
                    DocumentBlock.char_end == char_end,
                )
            ).scalars().first()
            if block is None:
                continue
            for requirement, clause, _ in entries:
                worst.append((_coverage(clause, block.text or ""), requirement.code))
        worst.sort()
        print("lowest coverage across ALL live requirements (clause words present in "
              "their own block):")
        for score, code in worst[:12]:
            print(f"  {code}  {score:.0%}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
