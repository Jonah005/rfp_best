"""What does the company corpus actually contain, and what is it filed as?

Zero requirements reach DRAFTABLE. The handover names the cause: 610 evidence
records are all typed `OTHER` and all read as "proves capability", so the
shortlist a drafter sees cannot tell a signed reference letter from a blank CV
template. This script measures that claim before anything is built on it.

## Why OTHER, mechanically

`DocumentClassification` already exists, one row per document version, with a
`classified_by` stamp. `ingestion.register_version` writes it when the uploader
STATES a type -- recorded as `human:<user_id>`, because a filing decision by a
person outranks anything a classifier would infer. When no type is stated, no
row is written at all, and every reader falls back:

    knowledge_extraction.py:121   ... .scalar_one_or_none() or DocumentType.OTHER

That fallback then travels: `_evidence_ids` stamps `evidence_type=document_type`
onto every excerpt it creates. So one missing classification row at upload
becomes hundreds of evidence rows asserting a type nobody chose, and
`is_self_evidencing()` -- which treats an unknown type as evidence on purpose,
so a new type is never silently downgraded -- passes all of them through.

Nothing here is broken. The classifier was never built.

## What this prints

Per company-side document version: what it is filed as and by whom, how many
evidence rows carry that type, and the opening of its text. The last part is
the point -- the corpus has to be looked at before a prompt is written for it,
and the taxonomy has 48 company-side types that a prompt has to choose between.

Read-only. Makes no model calls and writes nothing.

    python scripts/check_document_classification.py
    python scripts/check_document_classification.py --sample 600
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
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

from sqlalchemy import func, select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.models.enums import DocumentSide, DocumentType, is_self_evidencing  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sample", type=int, default=320, help="characters of text to show per document"
    )
    args = parser.parse_args()

    from app.models import (
        Document,
        DocumentChunk,
        DocumentClassification,
        DocumentVersion,
        Evidence,
        Organization,
    )

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        rows = list(
            session.execute(
                select(Document, DocumentVersion, DocumentClassification)
                .join(
                    DocumentVersion,
                    (DocumentVersion.document_id == Document.id)
                    & (DocumentVersion.org_id == Document.org_id),
                )
                .outerjoin(
                    DocumentClassification,
                    (DocumentClassification.document_version_id == DocumentVersion.id)
                    & (DocumentClassification.org_id == DocumentVersion.org_id),
                )
                .where(
                    Document.org_id == org.id,
                    Document.side == DocumentSide.COMPANY,
                    Document.withdrawn_at.is_(None),
                )
                .order_by(Document.original_filename, DocumentVersion.version_no)
            )
        )
        if not rows:
            print("No company-side documents.")
            return 0

        # Evidence counts per source version, in one query rather than one per
        # document: a per-row count here is how a diagnostic comes to take
        # longer than the thing it measures.
        evidence_counts = dict(
            session.execute(
                select(Evidence.source_document_version_id, func.count(Evidence.id))
                .where(Evidence.org_id == org.id)
                .group_by(Evidence.source_document_version_id)
            ).all()
        )
        evidence_types = Counter(
            row[0]
            for row in session.execute(
                select(Evidence.evidence_type).where(Evidence.org_id == org.id)
            ).all()
        )

        # First chunk of each version, for the text sample.
        openings: dict = {}
        for version_id, text in session.execute(
            select(DocumentChunk.document_version_id, DocumentChunk.text)
            .where(DocumentChunk.org_id == org.id, DocumentChunk.ordinal == 0)
        ).all():
            openings[version_id] = text

        stamps: Counter = Counter()
        filed: Counter = Counter()
        unclassified = 0
        no_text = []

        print(f"{len(rows)} company-side document version(s).\n")
        for document, version, classification in rows:
            kind = classification.document_type if classification else None
            by = classification.classified_by if classification else "-- no row --"
            stamps[by.split(":", 1)[0] if classification else "unclassified"] += 1
            filed[getattr(kind, "value", kind) or "(falls back to OTHER)"] += 1
            if classification is None:
                unclassified += 1

            print("=" * 96)
            print(f"  {document.original_filename}  (v{version.version_no})")
            print(f"  title       {document.title}")
            print(
                f"  filed as    {getattr(kind, 'value', kind) or 'nothing -- reads as OTHER'}"
                f"   by {by}"
            )
            print(
                f"  evidence    {evidence_counts.get(version.id, 0)} row(s)"
                f"   chunks {version.chunk_count}   blocks {version.block_count}"
                f"   state {getattr(version.state, 'value', version.state)}"
            )
            opening = openings.get(version.id)
            if not opening:
                no_text.append(document.original_filename)
                print("  text        -- no chunk 0; nothing to classify from --")
                continue
            print(
                textwrap.fill(
                    " ".join(opening.split())[: args.sample],
                    width=92,
                    initial_indent="  text        ",
                    subsequent_indent="              ",
                )
            )
        print("=" * 96)

        print("\nfiled as:")
        for name, count in filed.most_common():
            print(f"  {count:>4}  {name}")
        print("\nclassified by:")
        for name, count in stamps.most_common():
            print(f"  {count:>4}  {name}")

        print("\nevidence rows by type (whole org, both sides):")
        total_evidence = sum(evidence_types.values())
        for kind, count in evidence_types.most_common():
            name = getattr(kind, "value", kind)
            flag = "" if is_self_evidencing(kind) else "   [not self-evidencing]"
            print(f"  {count:>4}  {name}{flag}")

        print(
            f"\n{unclassified} of {len(rows)} version(s) have no classification row, so their"
            f"\nevidence is stamped OTHER by the fallback in knowledge_extraction.py:121."
            f"\n{total_evidence} evidence row(s) in total."
        )
        if no_text:
            print(
                f"\n{len(no_text)} version(s) have no chunk 0. A classifier reads text, so"
                " these\nneed a parse before they can be typed:"
            )
            for name in no_text[:10]:
                print(f"  {name}")
        print(
            f"\nThe taxonomy offers {len(list(DocumentType))} types. Nothing was changed."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
