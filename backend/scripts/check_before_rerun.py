"""What would change on the live bid if today's fixes were applied to it?

Everything built on 17 Sep was proven against `tests/fixtures/cybranytech/` --
seven clean documents, seventeen requirements, one tender with no amendments.
Jadwa is the messy input: 89 requirements, 610 evidence rows, a corpus where
half the documents were superseded by a re-upload. Three of today's changes
touch it directly and none has run against it.

Re-running the analysis would answer the question by doing it, and that is the
wrong order. Precedence re-resolution writes new `clause_resolution` rows and
retires the current ones; requirement extraction can create versions. This
project's first rule exists because of exactly that temptation: "Measure before
changing. Every wrong diagnosis here has been corrected by pasted output, never
by re-reading the code."

So this counts, and writes nothing.

## The three questions

**1. Does explicit supersession have anything to bite on?**
`_is_superseded` was matching a qualified key against a bare-keyed map and never
firing. Fixing it only changes Jadwa if there are superseding DocumentRelations
carrying clause refs. If there are none, the fix is correct and inert here, and
that is worth knowing before spending a re-run on it.

**2. How many clauses are contested at all?**
A clause with one candidate never reaches the supersession branch. The number of
clauses with two or more competing document versions is the ceiling on how much
precedence could possibly move.

**3. What will the quantity rule cost, and what could it block?**
Only requirements carrying a numeral reach the model. This reports how many that
is -- the whole cost of the rule on this bid -- and, of those, how many are
currently in a state the shortfall rule could downgrade. A requirement already
BLOCKED cannot be blocked further, so the exposure is narrower than the count.

Nothing is written. Read-only.

    python scripts/check_before_rerun.py
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

# Before any app import: `settings.vector_backend` is resolved once at import and
# chooses the embedding column type. See `_vector_backend` for the two rounds
# this cost.
from _vector_backend import align_vector_backend  # noqa: E402

align_vector_backend()

from sqlalchemy import select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.models.enums import RequirementLifecycle  # noqa: E402

BID = "0e20677e-9cc3-434e-ad88-c953d6676028"


def _short(text: str | None, width: int = 88) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def main() -> int:
    import uuid

    from app.models import (
        Document,
        DocumentBlock,
        DocumentRelation,
        DocumentVersion,
        Organization,
        Requirement,
        RequirementAssessment,
        RequirementVersion,
    )
    from app.models.enums import DocumentSide, DocumentVersionState
    from app.services.clause_identity import qualified_clause_ref
    from app.services.precedence import SUPERSEDING_RELATIONS
    from app.services.requirements import _NUMERAL

    bid_id = uuid.UUID(BID)
    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        # --- baseline, so the re-run has something to be compared against ----
        current = list(
            session.execute(
                select(RequirementAssessment).where(
                    RequirementAssessment.org_id == org.id,
                    RequirementAssessment.bid_id == bid_id,
                    RequirementAssessment.is_current.is_(True),
                )
            ).scalars()
        )
        readiness = Counter(
            str(getattr(a.bid_readiness, "value", a.bid_readiness)) for a in current
        )
        print("=" * 92)
        print("BASELINE -- record these numbers before re-running anything")
        print("=" * 92)
        print(f"current assessments: {len(current)}")
        for state, n in sorted(readiness.items(), key=lambda kv: -kv[1]):
            print(f"  {state:<32} {n:>4}")
        print()

        # --- 1. is there anything for the supersession fix to bite on? -------
        tender_versions = {
            row.id
            for row in session.execute(
                select(DocumentVersion)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    Document.org_id == org.id,
                    Document.bid_id == bid_id,
                    Document.side == DocumentSide.TENDER,
                    Document.withdrawn_at.is_(None),
                    DocumentVersion.state.in_(
                        [DocumentVersionState.INDEXED, DocumentVersionState.ACTIVE]
                    ),
                )
            ).scalars()
        }
        relations = [
            row
            for row in session.execute(
                select(DocumentRelation).where(DocumentRelation.org_id == org.id)
            ).scalars()
            if row.relation_type in SUPERSEDING_RELATIONS and row.from_version_id in tender_versions
        ]
        named_clauses = sorted(
            {
                (clause or "").strip()
                for row in relations
                for clause in (row.clause_refs or [])
                if (clause or "").strip()
            }
        )
        whole_document = [row for row in relations if not (row.clause_refs or [])]

        print("=" * 92)
        print("1. EXPLICIT SUPERSESSION -- does the fix have anything to act on here?")
        print("=" * 92)
        print(f"superseding relations on this bid's tender: {len(relations)}")
        print(f"  naming specific clauses: {len(named_clauses)} distinct ref(s)")
        print(f"  superseding a whole document: {len(whole_document)}")
        if named_clauses:
            print(f"  refs: {', '.join(named_clauses[:12])}")
        if not relations:
            print()
            print("  NOTHING TO ACT ON. The fix is correct and inert on this bid: no")
            print("  amendment here declares which clause it replaces, so precedence was")
            print("  never reaching the branch that was broken. Expect precedence to be")
            print("  UNCHANGED by the re-run -- and if it moves, that is a finding.")
        print()

        # --- 2. how much of precedence is contested at all? ------------------
        blocks = list(
            session.execute(
                select(DocumentBlock).where(
                    DocumentBlock.org_id == org.id,
                    DocumentBlock.document_version_id.in_(tender_versions or [None]),
                    DocumentBlock.clause_ref.is_not(None),
                )
            ).scalars()
        )
        by_key: dict[str, set] = {}
        for block in blocks:
            key = qualified_clause_ref(block) or block.clause_ref
            by_key.setdefault(key, set()).add(block.document_version_id)
        contested = {k: v for k, v in by_key.items() if len(v) > 1}

        print("=" * 92)
        print("2. CONTESTED CLAUSES -- the ceiling on how much precedence could move")
        print("=" * 92)
        print(f"clause keys across the tender: {len(by_key)}")
        print(f"addressed by more than one document version: {len(contested)}")
        for key in sorted(contested)[:10]:
            print(f"  {_short(key, 72)}  ({len(contested[key])} versions)")
        if not contested:
            print("  none. Every clause has exactly one candidate, so `_decide` returns")
            print("  early and no precedence method -- superseded or otherwise -- applies.")
        print()

        # --- 3. what will the quantity rule cost, and what can it reach? -----
        rows = list(
            session.execute(
                select(Requirement, RequirementVersion)
                .join(
                    RequirementVersion,
                    RequirementVersion.id == Requirement.current_version_id,
                )
                .where(
                    Requirement.org_id == org.id,
                    Requirement.bid_id == bid_id,
                    Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN,
                )
                .order_by(Requirement.code)
            ).all()
        )
        readiness_by_req = {
            a.requirement_id: str(getattr(a.bid_readiness, "value", a.bid_readiness))
            for a in current
        }
        # A row the rule could actually change. Anything already BLOCKED, not
        # applicable, or waiting on the customer is at or below the floor the
        # shortfall writes to, so it cannot be downgraded further.
        AT_RISK = {"DRAFTABLE", "READY", "NEEDS_REFRESH", "REQUIRES_SIGNATURE"}
        numbered = [(r, v) for r, v in rows if _NUMERAL.search(v.requirement_text or "")]
        exposed = [(r, v) for r, v in numbered if readiness_by_req.get(r.id) in AT_RISK]
        already_read = [v for _r, v in rows if v.quantity_by]

        print("=" * 92)
        print("3. QUANTITY RULE -- cost of the reading, and how far a shortfall could reach")
        print("=" * 92)
        print(f"live requirements: {len(rows)}")
        print(f"  carry a numeral, so reach the model: {len(numbered)}")
        print(f"  settled free (no numeral):           {len(rows) - len(numbered)}")
        print(f"  already carry a quantity verdict:    {len(already_read)}")
        print(f"  of the numbered rows, in a state a shortfall could downgrade: {len(exposed)}")
        print()
        print("  the rows a shortfall could actually move (worst case, all of them):")
        for req, version in exposed[:14]:
            print(
                f"    {req.code:<8} {readiness_by_req.get(req.id, '?'):<22} "
                f"{_short(version.requirement_text, 54)}"
            )
        if len(exposed) > 14:
            print(f"    ... and {len(exposed) - 14} more")
        print()
        print("=" * 92)
        print("Read this before deciding to re-run:")
        print()
        print("  * The quantity rule only DOWNGRADES, so the worst case is that all")
        print(f"    {len(exposed)} rows above move to BLOCKED. If that number is most of the")
        print("    matrix, read the clauses first -- a model calling every numeral a count")
        print("    would look exactly like this.")
        print("  * A requirement with NULL required_count is left alone, so any row the")
        print("    rule does not read keeps the readiness it has today.")
        print("  * Precedence and requirement identity are the reversible-in-principle,")
        print("    painful-in-practice part. Section 1 says whether they can move at all.")
        print("=" * 92)
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
