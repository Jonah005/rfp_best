"""What state is the evidence actually in?

`check_corpus.py` reports one number -- how much evidence is admissible to
retrieval -- and it came back zero straight after a re-upload. Zero has two very
different explanations that a single count cannot tell apart: extraction
proposes evidence and a human approves it, so brand-new evidence is *supposed*
to be unapproved; or the retirement superseded more than it should have.

So this reports the full cross-tab instead -- every (approval_status, status)
pair, split by whether the source document is withdrawn. Nothing is written.

    python scripts/evidence_states.py
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

from sqlalchemy import case, func, select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.models.enums import KnowledgeStatus  # noqa: E402


def main() -> int:
    from app.models import Document, DocumentVersion, Evidence, Organization

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        withdrawn = case(
            (Document.withdrawn_at.is_(None), "live"), else_="withdrawn"
        ).label("doc")

        rows = session.execute(
            select(
                withdrawn,
                Evidence.approval_status,
                Evidence.status,
                func.count(Evidence.id),
            )
            .join(
                DocumentVersion,
                (DocumentVersion.id == Evidence.source_document_version_id)
                & (DocumentVersion.org_id == Evidence.org_id),
            )
            .join(
                Document,
                (Document.id == DocumentVersion.document_id)
                & (Document.org_id == DocumentVersion.org_id),
            )
            .where(Evidence.org_id == org.id)
            .group_by(withdrawn, Evidence.approval_status, Evidence.status)
            .order_by(withdrawn, Evidence.approval_status, Evidence.status)
        ).all()

        print(f"{'source doc':>11}  {'approval_status':24} {'status':24} {'rows':>6}")
        print("-" * 70)
        for doc, approval, status, count in rows:
            print(
                f"{doc:>11}  {str(getattr(approval, 'value', approval)):24} "
                f"{str(getattr(status, 'value', status)):24} {count:>6}"
            )

        # Embedded or not, counted only over the evidence that can actually be
        # matched.
        #
        # The backfill endpoint is capped at 200 rows per call and returns
        # `remaining` so it can be pressed until that reaches zero -- but the
        # UI reported only "completed" for a long time, so the honest way to
        # know whether the corpus is fully embedded is to count it here.
        #
        # `is_(None)` is deliberate: it asks the database, so nothing depends on
        # how the vector column deserializes in Python. Selecting the column
        # itself is what broke an earlier script, because its type is chosen at
        # import time from settings.vector_backend.
        admissible_no_vector = session.execute(
            select(func.count(Evidence.id)).where(
                Evidence.org_id == org.id,
                Evidence.status != KnowledgeStatus.SUPERSEDED,
                Evidence.excerpt_vector.is_(None),
            )
        ).scalar_one()
        admissible_total = session.execute(
            select(func.count(Evidence.id)).where(
                Evidence.org_id == org.id,
                Evidence.status != KnowledgeStatus.SUPERSEDED,
            )
        ).scalar_one()
        print(
            f"\nlive evidence with a vector    "
            f"{admissible_total - admissible_no_vector} / {admissible_total}"
        )
        if admissible_no_vector:
            print(
                f"{admissible_no_vector} live excerpt(s) have no vector and can only be found by\n"
                "shared words. Press 'Embed evidence for semantic search' again -- the\n"
                "endpoint embeds at most 200 per call."
            )

        orphans = session.execute(
            select(func.count(Evidence.id)).where(
                Evidence.org_id == org.id,
                Evidence.source_document_version_id.is_(None),
            )
        ).scalar_one()
        if orphans:
            print(f"\n{orphans} evidence row(s) have no source document version.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
