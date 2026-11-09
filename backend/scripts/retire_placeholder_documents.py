"""Retire company documents that are blank templates rather than evidence.

The first company pack was a scaffold generated from an old proposal: every
field reads [REPLACE FROM OFFICIAL RECORD], every rate is marked SYNTHETIC, and
every CV availability line says [HR TO CONFIRM]. Extraction judged it
correctly -- 261 of 610 excerpts came back unsubstantiated -- but judged is not
the same as gone. Those excerpts are still offered to the matcher, still
shortlisted against requirements, and still counted in every total, so a real
pack uploaded beside them competes with them.

A document whose content is a form waiting to be filled in is not evidence of
anything. This finds them and retires them, on the same two-step the duplicate
retirement uses:

  * the document is marked withdrawn, with a reason
  * its evidence is marked SUPERSEDED, which is what actually removes it from
    retrieval -- admissibility is decided by Evidence.status, and a document
    flag alone would look tidy and change nothing

Nothing is deleted. A superseded excerpt keeps its text, its source and its
approval history. It stops being offered as an answer.

Detection is by density, not by a single match: a real document may quote a
template marker once while explaining its own process. A document is a template
when placeholder markers appear in at least MIN_HITS excerpts AND in at least
MIN_SHARE of them.

Density is deliberately conservative, and on the live corpus it caught only 4 of
the 13 documents in a pack that was ENTIRELY generated: a register of things
the company could not supply reads as prose, not as a form, however little it
proves. Guessing harder is the wrong answer -- a rule tuned until it happens to
catch one known pack will mis-fire on the next one. So there is a second
selector that does not guess at all: retire what was uploaded before a date.
"A pack uploaded today supersedes the pack uploaded in August" is a fact the
person running this knows and the script cannot infer.

    python scripts/retire_placeholder_documents.py            # show what it would do
    python scripts/retire_placeholder_documents.py --apply    # retire the templates
    python scripts/retire_placeholder_documents.py --uploaded-before 2026-09-01
    python scripts/retire_placeholder_documents.py --uploaded-before 2026-09-01 --apply
    python scripts/retire_placeholder_documents.py --apply --title "11_Rate_Card.docx"
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault(
    "BIDREADY_DATABASE_URL",
    os.environ.get("BIDREADY_LOCAL_DATABASE_URL")
    or "postgresql+psycopg://bidready_app:bidready-local@localhost:5432/bidready",
)

from _vector_backend import align_vector_backend  # noqa: E402

align_vector_backend()

from sqlalchemy import select, update  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.models.enums import KnowledgeStatus  # noqa: E402

# The markers this generator left behind, and the ones any template leaves.
PLACEHOLDER = re.compile(
    r"\[REPLACE[^\]]*\]|\[QTY\]|\[UNIT\]|\[PRICE[^\]]*\]|\[HR TO CONFIRM\]"
    r"|\[RESOURCE MANAGER TO CONFIRM\]|\[APPROVAL DATE[^\]]*\]|\[TTS\]|\[[A-Z][A-Z /-]{3,}\]"
    r"|\bSYNTHETIC\b|\bDRAFT FOR APPROVAL\b|\bREPLACE BEFORE USE\b|\bDEMO VALUE\b"
    r"|\bNOT EVIDENCED\b|\bworkflow test only\b",
    re.IGNORECASE,
)
# A document needs this many placeholder excerpts, and this share of them,
# before it is called a template. One quoted marker in a real document is not
# enough, and neither is one marker in a two-excerpt file.
MIN_HITS = 3
MIN_SHARE = 0.34


def main() -> int:
    from app.models import Document, DocumentVersion, Evidence, Organization

    apply = "--apply" in sys.argv
    only = None
    if "--title" in sys.argv:
        only = sys.argv[sys.argv.index("--title") + 1]
    before = None
    if "--uploaded-before" in sys.argv:
        raw = sys.argv[sys.argv.index("--uploaded-before") + 1]
        try:
            before = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"Not a date: {raw}. Use YYYY-MM-DD.")
            return 1

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        rows = session.execute(
            select(
                Document.id,
                Document.original_filename,
                Document.created_at,
                Evidence.id,
                Evidence.excerpt,
                Evidence.status,
            )
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .join(
                Evidence,
                (Evidence.source_document_version_id == DocumentVersion.id)
                & (Evidence.org_id == Document.org_id),
            )
            .where(
                Document.org_id == org.id,
                Document.side == "COMPANY",
                Document.withdrawn_at.is_(None),
            )
        ).all()

        if not rows:
            print("No live company documents with evidence.")
            return 0

        totals: dict[tuple, list] = {}
        uploaded: dict[tuple, object] = {}
        for document_id, filename, created_at, evidence_id, excerpt, status in rows:
            key = (document_id, filename)
            totals.setdefault(key, []).append(
                (evidence_id, bool(PLACEHOLDER.search(excerpt or "")), status)
            )
            uploaded[key] = created_at

        doomed = []
        print(f"{'document':56} {'uploaded':>11} {'excerpts':>9} {'placeholder':>12} {'share':>7}")
        print("-" * 100)
        for key, items in sorted(totals.items(), key=lambda kv: (uploaded[kv[0]], kv[0][1] or "")):
            document_id, filename = key
            hits = sum(1 for _id, hit, _s in items if hit)
            share = hits / len(items)
            when = uploaded[key]
            template = hits >= MIN_HITS and share >= MIN_SHARE
            why = "template" if template else ""
            if before is not None and when is not None and when < before:
                template = True
                why = "older pack" if not why else "template, older pack"
            if only:
                template = filename == only
                why = "named" if template else ""
            mark = f"  <-- {why}" if template else ""
            stamp = when.date().isoformat() if when else "?"
            print(
                f"{(filename or '?')[:56]:56} {stamp:>11} {len(items):>9} {hits:>12} "
                f"{share:>6.0%}{mark}"
            )
            if template:
                doomed.append((document_id, filename, [i for i, _h, _s in items]))

        if not doomed:
            print("\nNothing selected. Nothing to do.")
            if before is None:
                print(
                    "Density only catches documents that LOOK like forms. To retire a whole "
                    "earlier pack, use --uploaded-before YYYY-MM-DD."
                )
            return 0

        excerpts = sum(len(ids) for _d, _f, ids in doomed)
        print(f"\n{len(doomed)} document(s), {excerpts} excerpt(s) would be retired.")
        if not apply:
            print("Nothing was changed. Re-run with --apply to do it.")
            return 0

        now = datetime.now(timezone.utc)
        for document_id, filename, evidence_ids in doomed:
            session.execute(
                update(Document)
                .where(Document.org_id == org.id, Document.id == document_id)
                .values(withdrawn_at=now)
            )
            session.execute(
                update(Evidence)
                .where(Evidence.org_id == org.id, Evidence.id.in_(evidence_ids))
                .values(status=KnowledgeStatus.SUPERSEDED)
            )
            print(f"  retired  {filename} ({len(evidence_ids)} excerpts superseded)")
        session.commit()
        print("\nDone. Re-run the assessment for the change to reach the matrix.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
