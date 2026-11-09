"""Retire the assessments that outlived their requirements.

`check_current_assessments.py` measured it on the live bid:

    current assessments on this bid: 158
      DRAFT_EXTRACTED   89     <- live
      WITHDRAWN         69     <- retired, still claiming to be current

    state                          all   live   diff
    BLOCKED                         81     59     22
    NEEDS_CLIENT_CLARIFICATION      43     21     22
    NEEDS_COMPANY_CONFIRMATION      33      9     24
    DRAFTABLE                        1      0      1

Nothing is corrupt. `is_current` holds -- exactly one per requirement -- and
that is what makes it dangerous, because every number a person has actually seen
is right: the export joins to the requirement and filters. The trap was set for
whatever queried `is_current` next without knowing it had to, and the first
thing that did was a read-only diagnostic written to answer a different
question. It reported BLOCKED as 81 against a true figure of 59.

Look at the DRAFTABLE row too. The only draftable requirement in the entire
matrix is a retired one. Unfiltered, the bid appears to have one answerable
requirement; it has none.

## What this does, and does not

Sets `is_current = False` on assessments whose requirement is WITHDRAWN. That is
all. The rows are kept -- a retired requirement's last verdict is part of how
the bid reached its current state, and `is_current = False` is exactly how every
superseded assessment is already stored.

The cause is fixed separately, in `requirements.withdraw`, which now does all
three parts of a withdrawal in one place. This is the backfill for rows written
before that existed.

Dry run by default. `--apply` to write.

    python scripts/repair_withdrawn_assessments.py
    python scripts/repair_withdrawn_assessments.py --apply
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

from _vector_backend import align_vector_backend  # noqa: E402

align_vector_backend()

from sqlalchemy import select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.models.enums import RequirementLifecycle  # noqa: E402


def main() -> int:
    apply_changes = "--apply" in sys.argv

    from app.models import Organization, Requirement, RequirementAssessment

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        # Scoped to WITHDRAWN requirements and nothing else. A live
        # requirement's current assessment is the matrix, and clearing one by
        # accident would empty a row silently -- a far worse failure than the
        # one being repaired.
        rows = list(
            session.execute(
                select(RequirementAssessment, Requirement.code, Requirement.bid_id)
                .join(Requirement, Requirement.id == RequirementAssessment.requirement_id)
                .where(
                    RequirementAssessment.org_id == org.id,
                    RequirementAssessment.is_current.is_(True),
                    Requirement.lifecycle_state == RequirementLifecycle.WITHDRAWN,
                )
                .order_by(Requirement.code)
            ).all()
        )

        if not rows:
            print("Nothing to repair: no withdrawn requirement carries a current assessment.")
            return 0

        by_bid = Counter(str(bid_id) for _a, _code, bid_id in rows)
        by_readiness = Counter(
            str(getattr(a.bid_readiness, "value", a.bid_readiness)) for a, _c, _b in rows
        )

        print("=" * 88)
        print(f"{len(rows)} assessment(s) of a WITHDRAWN requirement still marked current")
        print("=" * 88)
        print("\nby readiness -- this is what an unfiltered count is over-reporting:")
        for state, n in sorted(by_readiness.items(), key=lambda kv: -kv[1]):
            print(f"  {state:<32} {n:>4}")
        print("\nby bid:")
        for bid_id, n in by_bid.most_common():
            print(f"  {bid_id}  {n}")
        print("\nfirst 15 rows:")
        for assessment, code, _bid in rows[:15]:
            state = str(getattr(assessment.bid_readiness, "value", assessment.bid_readiness))
            print(f"  {code:<10} {state}")
        if len(rows) > 15:
            print(f"  ... and {len(rows) - 15} more")

        if not apply_changes:
            print()
            print("#" * 88)
            print("DRY RUN. Nothing written. Re-run with --apply to retire these.")
            print("The rows are kept either way; --apply only clears is_current.")
            print("#" * 88)
            return 0

        for assessment, _code, _bid in rows:
            assessment.is_current = False
        session.commit()
        print()
        print(f"Retired {len(rows)} assessment(s). No row was deleted.")
        print("Re-run scripts/check_current_assessments.py: current should now equal live.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
