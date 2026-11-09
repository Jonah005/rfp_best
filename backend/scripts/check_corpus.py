"""Is the company corpus what you think it is, or is it two of everything?

Re-uploading a file does not add a version to the document it matches. Ingestion
looks a document up only when the caller passes an explicit id, so a second
upload of the same pack becomes a second, independent Document with its own
version 1 -- and nothing records that one replaces the other. Both are approved,
both are admissible, and the corpus quietly holds two of everything.

That matters most straight after a parser fix, which is exactly when someone
re-uploads: the earlier excerpts came from the parse that was wrong.

    python scripts/check_corpus.py

Reports every company document with its upload time, block and chunk counts, and
how much evidence hangs off it. Where a title appears twice, the later upload is
marked NEWEST -- by `created_at`, which is the only thing that actually orders
them.

An earlier version of this script ordered by version_no and called the last row
"CURRENT". Both copies are version 1, so that ordering had no tiebreaker and the
label was whatever the database returned first. It contradicted the retire
script, and the contradiction is the only reason anyone noticed. A column that
looks authoritative and is arbitrary is worse than no column.

Nothing is written or deleted. This only counts.
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

from sqlalchemy import func, select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.models.enums import ApprovalStatus, KnowledgeStatus  # noqa: E402


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

        # Evidence counted per source version, in one query rather than per row.
        evidence_by_version = dict(
            session.execute(
                select(Evidence.source_document_version_id, func.count(Evidence.id))
                .where(Evidence.org_id == org.id)
                .group_by(Evidence.source_document_version_id)
            ).all()
        )

        # Ordered by when the DOCUMENT was created, not by version_no.
        #
        # The first version of this script ordered by (title, version_no) and
        # then labelled the last row of each title CURRENT. Re-uploading creates
        # a second document rather than a second version, so both copies are
        # version 1 and that ordering has no tiebreaker: the label was decided
        # by whatever order the database happened to return. It disagreed with
        # the retire script, which orders by created_at -- and the disagreement
        # was the only reason anyone noticed the label was arbitrary.
        #
        # There is no "current" between two independent documents anyway. What
        # a reader actually needs is which one arrived first.
        rows = session.execute(
            select(Document, DocumentVersion)
            .join(
                DocumentVersion,
                (DocumentVersion.document_id == Document.id)
                & (DocumentVersion.org_id == Document.org_id),
            )
            .where(Document.org_id == org.id, Document.side == "COMPANY")
            .order_by(Document.title, Document.created_at, DocumentVersion.version_no)
        ).all()

        by_document: dict = defaultdict(list)
        for document, version in rows:
            by_document[document.title].append((document, version))

        newest = oldest = withdrawn = 0
        print(
            f"{'document':44} {'doc':>9} {'uploaded':>17} {'blocks':>7} "
            f"{'chunks':>7} {'evidence':>9}  which"
        )
        print("-" * 108)
        for title, entries in sorted(by_document.items()):
            for index, (document, version) in enumerate(entries):
                count = evidence_by_version.get(version.id, 0)
                # Last in created_at order is the newest upload. With one copy
                # it is simply the only one.
                is_newest = index == len(entries) - 1
                if document.withdrawn_at is not None:
                    withdrawn += count
                elif is_newest:
                    newest += count
                else:
                    oldest += count
                stamp = (
                    document.created_at.strftime("%m-%d %H:%M:%S")
                    if document.created_at
                    else "unknown"
                )
                label = "NEWEST" if is_newest else "earlier"
                if len(entries) == 1:
                    label = "only copy"
                # Whether this document is withdrawn is the state that decides
                # what the retire script will act on, and this report used to
                # omit it entirely -- so a corpus that had already been cleaned
                # still read as two of everything here while retire correctly
                # found nothing to do. A report that hides the field its
                # companion filters on will always eventually disagree with it.
                if document.withdrawn_at is not None:
                    label += " WITHDRAWN"
                print(
                    f"{title[:44]:44} {str(document.id)[:8]:>9} {stamp:>17} "
                    f"{version.block_count or 0:>7} {version.chunk_count or 0:>7} "
                    f"{count:>9}  {label}"
                )

        total_titles = len(by_document)
        total_documents = sum(len(v) for v in by_document.values())
        print("-" * 108)
        print(f"distinct titles         {total_titles}")
        print(f"document rows           {total_documents}")
        print(f"evidence on newest      {newest}")
        print(f"evidence on earlier     {oldest}")
        print(f"evidence on withdrawn   {withdrawn}")
        print(f"evidence total          {newest + oldest + withdrawn}")

        # The number that actually decides what the copilot can answer from.
        #
        # `withdrawn_at` is read by the document lists and by precedence, and by
        # NEITHER retrieval nor assessment -- admissibility is decided by
        # approval_status and status alone. Retiring a document without moving
        # its evidence to SUPERSEDED would look tidy in the table above and
        # change nothing at all, so the count is reported here rather than
        # inferred from the tidiness.
        admissible = session.execute(
            select(func.count(Evidence.id)).where(
                Evidence.org_id == org.id,
                Evidence.approval_status == ApprovalStatus.APPROVED,
                Evidence.status.in_(
                    [
                        KnowledgeStatus.CURRENT_VERIFIED,
                        KnowledgeStatus.HISTORICAL_VERIFIED,
                        KnowledgeStatus.EXPIRED,
                        KnowledgeStatus.CONFLICTING,
                    ]
                ),
            )
        ).scalar_one()
        superseded = session.execute(
            select(func.count(Evidence.id)).where(
                Evidence.org_id == org.id,
                Evidence.status == KnowledgeStatus.SUPERSEDED,
            )
        ).scalar_one()
        print(f"\nadmissible to retrieval {admissible}")
        print(f"superseded              {superseded}")
        if withdrawn and superseded < withdrawn:
            print(
                "\nWARNING: documents are withdrawn but their evidence is still "
                "admissible.\nWithdrawing a document does not remove its excerpts from "
                "matching -- run\nscripts/retire_duplicate_documents.py --apply, which "
                "moves them to SUPERSEDED."
            )

        # Duplicate TITLES are a different problem from multiple versions: two
        # documents with the same name are two filing decisions, and retrieval
        # cannot tell which one a person meant.
        # A withdrawn copy is not a live duplicate. Counting it as one is what
        # made this report contradict the retire script.
        duplicate_titles = [
            t
            for t, v in by_document.items()
            if len([1 for d, _ in v if d.withdrawn_at is None]) > 1
        ]
        print()
        if not duplicate_titles:
            print("Every company document appears exactly once.")
            return 0

        print(f"{len(duplicate_titles)} title(s) exist as more than one document:")
        for title in duplicate_titles:
            print(f"  - {title}")
        print(
            "\nThese are separate documents, not versions of one. Re-uploading a file "
            "does not\nadd a version unless the caller names the document to add it to, "
            "so both copies are\nversion 1 and NEITHER is marked as replacing the other. "
            "Both are approved, both are\nadmissible, and both answer the same question."
        )
        print(
            f"\n{oldest} evidence rows come from the earlier upload. After a parser fix "
            "those are\nthe ones derived from the parse you replaced. Compare the chunk "
            "counts above: where\nthey differ between two copies of a title, the parser "
            "genuinely changed the result."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
