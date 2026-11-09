"""Print a range of parsed blocks exactly as they are stored.

Two hypotheses about block 26 have now died: that its requirements were filed
under a mismatched locator, and that a colon-terminated lead-in was split from
its list. The second died to its own measurement -- the whole document contains
one block ending in a colon, and that one is harmless.

Which means the sentence that gives block 26's bullets their subject ("Jadwa
will provide access to Core OMS system for the following purpose:") is neither
in block 26 nor in a block of its own. It is somewhere, and no amount of
inference has located it. So this prints the blocks around it verbatim -- type,
clause reference, heading path, offsets and full text -- and the structure can
be read rather than guessed at.

    python scripts/show_blocks.py 20 32           # ordinals 20..32 inclusive
    python scripts/show_blocks.py 20 32 --grep "Core OMS"

With --grep, every block whose text contains the string is printed regardless of
the range, which is the quick way to find where a sentence actually lives.

Nothing is written.
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

from sqlalchemy import select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402


def _argument(flag: str) -> str | None:
    for index, value in enumerate(sys.argv):
        if value == flag and index + 1 < len(sys.argv):
            return sys.argv[index + 1]
        if value.startswith(f"{flag}="):
            return value.split("=", 1)[1]
    return None


def main() -> int:
    positional = [a for a in sys.argv[1:] if not a.startswith("-") and a.isdigit()]
    first = int(positional[0]) if positional else 20
    last = int(positional[1]) if len(positional) > 1 else first + 12
    needle = _argument("--grep")

    from app.models import DocumentBlock, Organization

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        # The tender document version the requirements point at. Taken from the
        # blocks themselves rather than named here, so this cannot drift out of
        # step with the rest of the diagnostics.
        # Which document version to show.
        #
        # The first version of this script used "whichever has the most blocks".
        # That is a guess dressed as a default, and it picked an unrelated ADX
        # RFP template out of the thirty-one versions in this database, then
        # printed its block 26 under the same heading the Jadwa investigation
        # was using. `--grep "Core OMS"` returned zero because it was reading
        # the wrong document, and nothing in the output said so.
        #
        # The version is now taken from the requirements themselves -- the same
        # rule the other diagnostics use, so they cannot disagree about which
        # document is under discussion.
        from app.models import Requirement, RequirementVersion

        requested = _argument("--version")
        counts: dict = {}
        for row in session.execute(
            select(DocumentBlock.document_version_id).where(DocumentBlock.org_id == org.id)
        ).scalars():
            counts[row] = counts.get(row, 0) + 1
        if not counts:
            print("No document blocks.")
            return 1

        in_use = {
            row
            for row in session.execute(
                select(RequirementVersion.source_document_version_id)
                .join(Requirement, Requirement.id == RequirementVersion.requirement_id)
                .where(
                    Requirement.org_id == org.id,
                    RequirementVersion.superseded_at.is_(None),
                )
            ).scalars()
        }

        if requested:
            matches = [key for key in counts if str(key).lower().startswith(requested.lower())]
            if len(matches) != 1:
                print(f"{len(matches)} document versions start with {requested!r}.")
                return 1
            version_id = matches[0]
        elif len(in_use) == 1:
            version_id = next(iter(in_use))
        else:
            print(
                f"{len(counts)} document versions exist; "
                f"{len(in_use)} of them have requirements filed against them:"
            )
            for key in sorted(in_use, key=str):
                print(f"  {str(key)[:8]}   {counts.get(key, 0)} blocks")
            print("\nName one with --version <prefix>.")
            return 1

        note = "" if version_id in in_use else "   (NO requirements reference this version)"
        print(
            f"{len(counts)} document versions in this database; showing "
            f"{str(version_id)[:8]}{note}"
        )

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
        print(f"document version {str(version_id)[:8]}: {len(blocks)} blocks\n")

        if needle:
            chosen = [b for b in blocks if needle.lower() in (b.text or "").lower()]
            print(f"--grep {needle!r}: {len(chosen)} block(s)\n")
        else:
            chosen = [b for b in blocks if first <= b.ordinal <= last]

        for block in chosen:
            text = block.text or ""
            print("=" * 78)
            print(
                f"ordinal {block.ordinal}   chars {block.char_start}-{block.char_end}"
                f"   ({len(text)} chars)"
            )
            print(
                f"  type={getattr(block.block_type, 'value', block.block_type)!r}   "
                f"clause_ref={block.clause_ref!r}   table={block.table_name!r}"
            )
            print(f"  heading_path={block.heading_path!r}")
            print("  ----")
            for line in text.splitlines() or [""]:
                print(f"  | {line}")
            print()
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
