"""What is actually sitting in the approval queue, printed so a person can read it.

"Approve all 108 for reuse" is one click and a real signature: a claim in a
submitted proposal traces back to whoever approved the excerpt behind it. The
screen shows the excerpts a page at a time, which is the wrong shape for
deciding about a hundred of them at once.

So this prints the queue grouped by source document, and flags the four kinds
of excerpt that should not be approved without a second look:

    BLANK      a form's empty fields -- proves the shape of an answer, nothing
               more (underscores, dotted leaders, "N/A" rows)
    UNSUBSTANTIATED  extraction already marked it as not proving what it says
    EXPIRED    an expiry date in the past: true once, not true now
    TENDER?    wording that belongs to a customer's demand, not our capability
               ("the bidder shall", "must provide"), which is how a previous
               tender gets mined as though it were our own claim

Nothing is written. Approving stays a human act on the screen.

    python scripts/review_pending_evidence.py            # flagged rows only
    python scripts/review_pending_evidence.py --all      # every pending row
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault(
    "BIDREADY_DATABASE_URL",
    os.environ.get("BIDREADY_LOCAL_DATABASE_URL")
    or "postgresql+psycopg://bidready_app:bidready-local@localhost:5432/bidready",
)

from _vector_backend import align_vector_backend  # noqa: E402

align_vector_backend()

from sqlalchemy import select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.models.enums import ApprovalStatus, is_self_evidencing  # noqa: E402

BLANK = re.compile(r"_{3,}|\.{5,}|\[\s*\]|\bTBD\b|\bTBA\b|\bto be (?:filled|completed)\b", re.I)
DEMAND = re.compile(
    r"\b(?:the\s+)?(?:bidder|tenderer|vendor|supplier|contractor)s?\s+(?:shall|must|should|will|is\s+required)"
    r"|\bshall\s+(?:provide|submit|comply|furnish)\b"
    r"|\bmandatory\s+requirement\b",
    re.I,
)


def main() -> int:
    from app.models import Document, DocumentVersion, Evidence, Organization

    show_all = "--all" in sys.argv
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
                Evidence.excerpt,
                Evidence.is_substantiated,
                Evidence.expiry_date,
                Evidence.evidence_type,
                Document.original_filename,
                Document.side,
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
            .where(
                Evidence.org_id == org.id,
                Evidence.approval_status != ApprovalStatus.APPROVED,
                Document.withdrawn_at.is_(None),
            )
            .order_by(Document.original_filename, Evidence.created_at)
        ).all()

        if not rows:
            print("Nothing is waiting for approval.")
            return 0

        today = date.today()
        by_document: dict[str, list] = {}
        counts: dict[str, int] = {}
        for excerpt, substantiated, expiry, evidence_type, filename, side in rows:
            flags = []
            text = " ".join((excerpt or "").split())
            if BLANK.search(text):
                flags.append("BLANK")
            if substantiated is False:
                flags.append("UNSUBSTANTIATED")
            if expiry and expiry < today:
                flags.append("EXPIRED")
            if DEMAND.search(text):
                flags.append("TENDER?")
            if getattr(side, "value", side) != "COMPANY":
                flags.append("NOT-A-COMPANY-DOC")
            for flag in flags:
                counts[flag] = counts.get(flag, 0) + 1
            by_document.setdefault(filename or "(no filename)", []).append(
                (evidence_type, text, flags)
            )

        print(f"{len(rows)} excerpt(s) awaiting approval, from {len(by_document)} document(s).\n")
        print("FLAG SUMMARY")
        if counts:
            for flag, count in sorted(counts.items(), key=lambda kv: -kv[1]):
                print(f"  {flag:18} {count}")
        else:
            print("  none")
        print()

        for filename, items in by_document.items():
            flagged = [i for i in items if i[2]]
            head = f"{filename}  --  {len(items)} excerpt(s)"
            if flagged:
                head += f", {len(flagged)} flagged"
            print(head)
            typed = {t for t, _text, _f in items if t is not None}
            proves = any(is_self_evidencing(t) for t in typed)
            names = sorted(getattr(t, "value", str(t)) for t in typed) or ["(untyped)"]
            print(f"    typed: {', '.join(names)}{'  (counts as proof)' if proves else ''}")
            for _evidence, text, flags in items:
                if flags or show_all:
                    mark = ("[" + ",".join(flags) + "] ") if flags else ""
                    print(f"    - {mark}{text[:220]}")
            print()

        print(
            "Approve what you recognise as your own, current material. A BLANK or "
            "TENDER? row is the kind that makes a draft claim something the company "
            "never said."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
