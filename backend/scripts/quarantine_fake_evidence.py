"""Un-approve evidence that says, in its own words, that it is not evidence.

The approval gate now refuses these, but a gate only stops what has not happened
yet. A bulk approval already moved 306 extracted excerpts to APPROVED /
CURRENT_VERIFIED, and a large share of them are templates, samples and generated
fillers that announce themselves: "SAMPLE / NOT VALID", "SYNTHETIC / REPLACE",
"DRAFT FOR APPROVAL", "certificates not supplied", invented revenue, a rate card
of made-up numbers.

Approved and current, they are indistinguishable from real proof to retrieval,
to assessment, and to a drafted statement. This walks what is already there and
puts them back where they belong.

What it does, per matching row:

  * approval_status -> PENDING_APPROVAL, approved_for_reuse -> False
  * status          -> NEEDS_CONFIRMATION

Not SUPERSEDED and not deleted: nothing about the excerpt has been replaced, and
the company may hold a real version of the same document. NEEDS_CONFIRMATION is
the honest state -- a human has to look, and until they do it cannot back a
claim.

    python scripts/quarantine_fake_evidence.py            # show what it would do
    python scripts/quarantine_fake_evidence.py --apply    # do it

The dry run prints the matched phrase beside the excerpt, because the only way
to trust an automated judgement about text is to read the text.
"""

from __future__ import annotations

import os
import sys
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

from sqlalchemy import select, update  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.models.enums import ApprovalStatus, KnowledgeStatus  # noqa: E402
from app.services.placeholders import disqualifying_match  # noqa: E402


def main() -> int:
    apply_changes = "--apply" in sys.argv

    from app.models import Evidence, Organization

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        # NAMED COLUMNS, never `select(Evidence)`.
        #
        # `Evidence.excerpt_vector` picks its column type at import time from
        # settings.vector_backend. Loading whole entities makes pgvector's
        # converter run over every row, and against the portable backend it
        # receives a list and calls .split() on it: AttributeError, from a
        # script that only wanted to read text. This bit once already in
        # export_matrix.py and was written again here.
        #
        # Reading three scalar columns also means the excerpt text is all that
        # crosses the wire, for 600-odd rows.
        rows = session.execute(
            select(
                Evidence.id,
                Evidence.excerpt,
                Evidence.approval_status,
            ).where(
                Evidence.org_id == org.id,
                # A SUPERSEDED excerpt from the retired upload is already out of
                # play and there is nothing to fix.
                Evidence.status != KnowledgeStatus.SUPERSEDED,
            )
        ).all()

        matched = []
        for evidence_id, excerpt, approval_status in rows:
            phrase = disqualifying_match(excerpt)
            if phrase is not None:
                matched.append((evidence_id, excerpt, approval_status, phrase))

        print(f"live evidence            {len(rows)}")
        print(f"says it is not evidence  {len(matched)}")
        if not matched:
            print("\nNothing to quarantine.")
            return 0

        approved = [m for m in matched if m[2] == ApprovalStatus.APPROVED]
        print(f"  of which APPROVED      {len(approved)}   <-- currently usable as proof")

        by_phrase = Counter(m[3].lower() for m in matched)
        print("\nmatched phrases:")
        for phrase, count in by_phrase.most_common():
            print(f"  {count:>4}  {phrase!r}")

        print("\nexcerpts:")
        for _id, excerpt, approval_status, phrase in matched[:25]:
            text = " ".join((excerpt or "").split())
            state = getattr(approval_status, "value", approval_status)
            print(f"  [{phrase}] {str(state):16} {text[:88]}")
        if len(matched) > 25:
            print(f"  ... and {len(matched) - 25} more")

        if not apply_changes:
            print("\nDry run. Nothing was changed. Re-run with --apply to quarantine these.")
            return 0

        # An UPDATE statement, for the same reason: assigning to ORM
        # attributes would require loading the entities this query avoided.
        session.execute(
            update(Evidence)
            .where(
                Evidence.org_id == org.id,
                Evidence.id.in_([m[0] for m in matched]),
            )
            .values(
                approval_status=ApprovalStatus.PENDING_APPROVAL,
                approved_for_reuse=False,
                status=KnowledgeStatus.NEEDS_CONFIRMATION,
            )
        )
        session.commit()
        print(
            f"\nQuarantined {len(matched)} excerpt(s): PENDING_APPROVAL / NEEDS_CONFIRMATION.\n"
            "Nothing was deleted. They stay on the Evidence list for review, and the\n"
            "approval gate now refuses them individually and skips them in a batch, so a\n"
            "bulk approve cannot sweep them back in.\n\n"
            "Re-run the bid assessment: any requirement that was PARTIAL on the strength\n"
            "of one of these should lose that support."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
