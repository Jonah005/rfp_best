"""Thirty-one document versions. How many are actually different documents?

`show_blocks.py` reported "31 document versions" in passing while looking for
something else. That number has been implied for a while -- `_resolve_document`
was noted as creating a document rather than versioning an existing one -- but
it has never been counted, and a count is what decides whether it matters.

The question is not how many rows exist. It is how many are RE-UPLOADS of the
same file: same document, ingested again, each time producing a fresh version
with its own blocks at its own character offsets. That matters concretely,
because requirement identity is keyed on (document version, character span).
Re-ingest a document and every requirement from the previous version is
stranded: its coordinates still exist, but the blocks the next analysis reads
belong to a different version, so nothing matches and the bid re-extracts from
scratch alongside the rows it already had.

Grouped by filename, since that is what a re-upload has in common. For each
group:

  * every version, with its block count and how many live requirements point at
    it;
  * whether the block texts are identical between versions -- the difference
    between "the customer sent an amended document" and "the same file was
    uploaded twice".

The second is the one that decides what to do. Genuine amendments are what
versioning is for. Byte-identical re-ingests are the defect.

Nothing is written.

    python scripts/check_document_versions.py
"""

from __future__ import annotations

import hashlib
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


def main() -> int:
    from app.models import (
        Document,
        DocumentBlock,
        DocumentVersion,
        Organization,
        Requirement,
        RequirementVersion,
    )

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        versions = (
            session.execute(
                select(DocumentVersion, Document)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(DocumentVersion.org_id == org.id)
            )
            .all()
        )

        block_counts: dict = defaultdict(int)
        digests: dict = {}
        for version_id, text in session.execute(
            select(DocumentBlock.document_version_id, DocumentBlock.text).where(
                DocumentBlock.org_id == org.id
            )
        ).all():
            block_counts[version_id] += 1
            # Order-independent digest of the block texts, so two ingests of the
            # same file agree even if block ordering shifted.
            digest = digests.setdefault(version_id, hashlib.sha256())
            digest.update(hashlib.sha256((text or "").encode("utf-8")).digest())

        requirement_counts: dict = defaultdict(int)
        for version_id in session.execute(
            select(RequirementVersion.source_document_version_id)
            .join(Requirement, Requirement.id == RequirementVersion.requirement_id)
            .where(
                Requirement.org_id == org.id,
                Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN,
                RequirementVersion.superseded_at.is_(None),
            )
        ).scalars():
            requirement_counts[version_id] += 1

        by_name: dict = defaultdict(list)
        for version, document in versions:
            name = f"{document.original_filename}   [{document.side.value}]"
            by_name[name].append((version, document))

        print(f"{len(versions)} document version(s) across {len(by_name)} filename(s)\n")

        repeated = 0
        identical = 0
        for name, entries in sorted(by_name.items(), key=lambda item: -len(item[1])):
            if len(entries) > 1:
                repeated += len(entries) - 1
            print("=" * 78)
            print(f"{name}   {len(entries)} version(s)")
            seen: dict = {}
            for version, _document in sorted(
                entries, key=lambda pair: getattr(pair[0], "created_at", None) or 0
            ):
                digest = digests.get(version.id)
                fingerprint = digest.hexdigest()[:12] if digest else "(no blocks)"
                created = getattr(version, "created_at", None)
                stamp = created.isoformat(timespec="seconds") if created else "?"
                mark = ""
                if fingerprint in seen:
                    mark = f"   <== SAME TEXT as {seen[fingerprint]}"
                    identical += 1
                else:
                    seen[fingerprint] = str(version.id)[:8]
                print(
                    f"  {str(version.id)[:8]}  {stamp}  "
                    f"{block_counts.get(version.id, 0):>4} blocks  "
                    f"{requirement_counts.get(version.id, 0):>3} live requirements  "
                    f"text={fingerprint}{mark}"
                )
            print()

        print("=" * 78)
        print(
            f"{repeated} version(s) beyond the first for their filename; "
            f"{identical} of them carry text identical to an earlier version.\n"
            "An identical re-ingest is not an amendment. It strands every requirement\n"
            "extracted from the previous version, because identity is keyed on\n"
            "(document version, character span) and the span now belongs to a version\n"
            "nothing reads."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
