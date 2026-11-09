"""What does one classifiable unit of company evidence actually look like?

`check_document_classification.py` established that all 14 company-side versions
are unclassified and all 610 evidence rows fall back to `OTHER`. It also showed
something that changes what should be built: these files are REGISTERS, not
documents of a single kind.

    02_Legal_and_Registration_Register   trade licence + incorporation +
                                         tax registration + signatory authority
    12_Reusable_Company_Documents        product catalogue + methodology +
                                         security answers + standard forms

A classifier that types the DOCUMENT would stamp all 50 rows of file 02
`LEGAL_DOC`, and most of them would be wrong. The unit that carries a type is
`Evidence.evidence_type` -- one row per excerpt -- and that is the unit a
classifier has to read.

The covers raise a second question the taxonomy already knows about:

    03  "Evidence register for organizational credentials"
    06  "Bid-ready draft policies for governance approval"
    10  "Financial-evidence checklist, assumptions and controlled templates"
    MANIFEST  "synthetic/template fields must be replaced"

A register line reading "ISO 27001 -- to be obtained" is not a certificate, and
a draft policy awaiting approval is not an approved policy. `NOT_SELF_EVIDENCING`
exists for exactly this and is currently unreachable, because nothing sets a
type for it to read. So a classifier has to answer two questions per excerpt,
not one:

    1. what kind of material is this            -> DocumentType
    2. is this the thing, or a record of it     -> self-evidencing or not

This script prints excerpts so those two questions can be designed against real
text rather than against the covers of the files. Read-only, no model calls.

    python scripts/check_evidence_units.py                  # 3 per document
    python scripts/check_evidence_units.py --per 6 --chars 700
    python scripts/check_evidence_units.py --file 02        # one document
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
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
from app.models.enums import DocumentSide  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per", type=int, default=3, help="excerpts per document")
    parser.add_argument("--chars", type=int, default=500, help="characters per excerpt")
    parser.add_argument("--file", default=None, help="only documents whose filename contains this")
    args = parser.parse_args()

    from app.models import Document, DocumentVersion, Evidence, Organization

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        versions = list(
            session.execute(
                select(Document, DocumentVersion)
                .join(
                    DocumentVersion,
                    (DocumentVersion.document_id == Document.id)
                    & (DocumentVersion.org_id == Document.org_id),
                )
                .where(
                    Document.org_id == org.id,
                    Document.side == DocumentSide.COMPANY,
                    Document.withdrawn_at.is_(None),
                )
                .order_by(Document.original_filename)
            )
        )
        if args.file:
            needle = args.file.lower()
            versions = [
                pair for pair in versions if needle in (pair[0].original_filename or "").lower()
            ]
        if not versions:
            print("No matching company-side documents.")
            return 0

        shown = 0
        for document, version in versions:
            # Columns, not the entity. Loading an Evidence object pulls
            # `excerpt_vector`, and pgvector's decoder raises on the value
            # psycopg hands it -- a diagnostic that cannot read the table it
            # measures is worse than no diagnostic. Nothing here needs the
            # embedding.
            rows = list(
                session.execute(
                    select(
                        Evidence.excerpt,
                        Evidence.evidence_type,
                        Evidence.source_locator,
                        Evidence.approval_status,
                        Evidence.expiry_date,
                    )
                    .where(
                        Evidence.org_id == org.id,
                        Evidence.source_document_version_id == version.id,
                    )
                    .order_by(Evidence.created_at)
                ).all()
            )
            print("=" * 96)
            print(f"  {document.original_filename}   {len(rows)} evidence row(s)")
            print("=" * 96)
            if not rows:
                print("  (none)\n")
                continue

            # Spread the sample across the document rather than taking the
            # first N. The opening rows of a register are its preamble, and a
            # prompt designed against preambles would never see the entries.
            step = max(1, len(rows) // max(1, args.per))
            for excerpt, kind, locator, approval, expiry in rows[::step][: args.per]:
                locator = locator or {}
                where = " / ".join(
                    str(part)
                    for part in (
                        locator.get("clause_ref"),
                        locator.get("table_name"),
                        " > ".join(locator.get("heading_path") or []) or None,
                    )
                    if part
                )
                head = f"  [{getattr(kind, 'value', kind)}]"
                if where:
                    head += f"   {where}"
                head += (
                    f"   approval={getattr(approval, 'value', approval)}"
                    f"  expiry={expiry or '-'}"
                )
                print(head)
                print(
                    textwrap.fill(
                        " ".join((excerpt or "").split())[: args.chars],
                        width=92,
                        initial_indent="      ",
                        subsequent_indent="      ",
                    )
                )
                print()
                shown += 1

        print(
            f"{shown} excerpt(s) shown. Every one is typed OTHER, and every one reads as"
            "\nproof of capability to `is_self_evidencing()`. Nothing was changed."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
