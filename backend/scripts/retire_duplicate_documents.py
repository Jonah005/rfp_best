"""Retire the older copy when the same company document was uploaded twice.

Re-uploading a file does not create a new version of it. `_resolve_document`
looks the document up only when the caller passes an explicit `document_id`,
and otherwise creates a new one -- so uploading the same pack again produced a
second, independent Document with its own version 1. Nothing marks either as
the real one. Both are approved, both are current, and both answer the same
question, which is why a corpus of 303 excerpts reads as 610.

That is a real problem after a parser fix, because the two copies are not
equivalent: the older excerpts came from the broken parse, where every table
inherited the document's last heading.

What this does, per title, keeping the NEWEST document:

  * marks the older document withdrawn, with a reason
  * marks its evidence SUPERSEDED

The second is the one that changes behaviour. `withdrawn_at` is read by the
document lists and by precedence, but neither retrieval nor assessment consults
it -- admissibility is decided by `Evidence.approval_status` and
`Evidence.status` alone, and SUPERSEDED is not in the admissible set. Setting
only the document flag would look tidy and change nothing.

Nothing is deleted. A superseded excerpt keeps its text, its source and its
approval history; it simply stops being offered as an answer. That distinction
matters if anyone later asks what the system believed last week.

    python scripts/retire_duplicate_documents.py            # show what it would do
    python scripts/retire_duplicate_documents.py --apply    # do it
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
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

from sqlalchemy import select, update  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.models.enums import KnowledgeStatus  # noqa: E402

REASON = "Superseded by a newer upload of the same document."


def main() -> int:
    apply_changes = "--apply" in sys.argv

    from app.models import Document, DocumentVersion, Evidence, Organization

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        documents = (
            session.execute(
                select(Document)
                .where(
                    Document.org_id == org.id,
                    Document.side == "COMPANY",
                    Document.withdrawn_at.is_(None),
                )
                .order_by(Document.title, Document.created_at)
            )
            .scalars()
            .all()
        )

        by_title: dict[str, list] = defaultdict(list)
        for document in documents:
            by_title[document.title].append(document)

        duplicates = {t: d for t, d in by_title.items() if len(d) > 1}
        if not duplicates:
            print("No company document appears twice. Nothing to retire.")
            return 0

        total_evidence = 0
        plan = []
        for title, group in sorted(duplicates.items()):
            # Newest wins: it came from the current parser.
            keep = group[-1]
            retire = group[:-1]
            for document in retire:
                version_ids = (
                    session.execute(
                        select(DocumentVersion.id).where(
                            DocumentVersion.org_id == org.id,
                            DocumentVersion.document_id == document.id,
                        )
                    )
                    .scalars()
                    .all()
                )
                evidence_ids = (
                    session.execute(
                        select(Evidence.id).where(
                            Evidence.org_id == org.id,
                            Evidence.source_document_version_id.in_(version_ids),
                            Evidence.status != KnowledgeStatus.SUPERSEDED,
                        )
                    )
                    .scalars()
                    .all()
                )
                total_evidence += len(evidence_ids)
                plan.append((title, document, keep, evidence_ids))

        print(f"{'document':52} {'retiring':>10} {'keeping':>10} {'evidence':>9}")
        print("-" * 86)
        for title, document, keep, evidence_ids in plan:
            print(
                f"{title[:52]:52} {str(document.id)[:8]:>10} {str(keep.id)[:8]:>10} "
                f"{len(evidence_ids):>9}"
            )
        print("-" * 86)
        print(f"documents to retire  {len(plan)}")
        print(f"evidence to supersede {total_evidence}")

        if not apply_changes:
            print("\nDry run. Nothing was changed. Re-run with --apply to make it so.")
            return 0

        now = datetime.now(timezone.utc)
        for _title, document, _keep, evidence_ids in plan:
            document.withdrawn_at = now
            document.withdrawn_reason = REASON
            if evidence_ids:
                session.execute(
                    update(Evidence)
                    .where(Evidence.org_id == org.id, Evidence.id.in_(evidence_ids))
                    .values(status=KnowledgeStatus.SUPERSEDED)
                )
        session.commit()
        print(
            f"\nRetired {len(plan)} document(s) and superseded {total_evidence} evidence rows.\n"
            "Nothing was deleted: each excerpt keeps its text, source and approval history,\n"
            "and simply stops being offered as an answer.\n\n"
            "Re-run scripts/check_corpus.py to confirm, then re-run the bid analysis so the\n"
            "assessments are recomputed against the remaining evidence."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
