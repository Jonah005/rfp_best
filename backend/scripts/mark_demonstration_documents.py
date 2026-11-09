"""Apply the demonstration-document rule to a corpus already ingested.

`ingestion.parse_and_index` runs `demonstration_documents.mark_version` on every
company upload from now on. Documents loaded before that existed were never
checked, and on the live corpus that is the whole evidence pack: one excerpt per
file carried the "FICTIONAL TEST FIXTURE" sentence and was marked, while the
rest of each file stayed unjudged and a requirement went READY on seven of them.

Nothing is deleted, withdrawn or hidden. The excerpts stay searchable and stay
attached to the requirements they match. What changes is that they cannot back
a claim, and cannot make a requirement READY.

    python scripts/mark_demonstration_documents.py            # show what it would do
    python scripts/mark_demonstration_documents.py --apply    # do it
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

from _vector_backend import align_vector_backend  # noqa: E402

align_vector_backend()

from sqlalchemy import func, select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.models.enums import DocumentSide  # noqa: E402
from app.services import demonstration_documents  # noqa: E402


def main() -> int:
    from app.models import Document, DocumentChunk, DocumentVersion, Evidence, Organization

    apply = "--apply" in sys.argv
    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        versions = session.execute(
            select(DocumentVersion.id, Document.original_filename)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                Document.org_id == org.id,
                Document.side == DocumentSide.COMPANY,
                Document.withdrawn_at.is_(None),
            )
            .order_by(Document.original_filename)
        ).all()
        if not versions:
            print("No live company documents.")
            return 0

        found = []
        for version_id, filename in versions:
            kind = None
            for (text,) in session.execute(
                select(DocumentChunk.text).where(
                    DocumentChunk.org_id == org.id,
                    DocumentChunk.document_version_id == version_id,
                )
            ).all():
                kind = demonstration_documents.declaration_in(text)
                if kind:
                    break
            if not kind:
                continue
            excerpts = session.execute(
                select(func.count(Evidence.id)).where(
                    Evidence.org_id == org.id,
                    Evidence.source_document_version_id == version_id,
                )
            ).scalar_one()
            already = session.execute(
                select(func.count(Evidence.id)).where(
                    Evidence.org_id == org.id,
                    Evidence.source_document_version_id == version_id,
                    Evidence.is_substantiated.is_(False),
                )
            ).scalar_one()
            found.append((version_id, filename, kind, excerpts, already))

        if not found:
            print("No document declares itself demonstration material.")
            return 0

        print(f"{'document':52} {'declares':>14} {'excerpts':>9} {'already':>8}")
        print("-" * 88)
        for _v, filename, kind, excerpts, already in found:
            print(f"{(filename or '?')[:52]:52} {kind:>14} {excerpts:>9} {already:>8}")
        outstanding = sum(e - a for _v, _f, _k, e, a in found)
        print(f"\n{len(found)} document(s); {outstanding} excerpt(s) would be marked.")
        if not apply:
            print("Nothing was changed. Re-run with --apply to do it.")
            return 0

        total = 0
        for version_id, filename, _kind, _e, _a in found:
            marked = demonstration_documents.mark_version(session, ctx, version_id=version_id)
            total += marked
            print(f"  marked   {filename} ({marked} excerpts)")
        session.commit()
        print(f"\n{total} excerpt(s) marked. Re-run the assessment for this to reach the matrix.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
