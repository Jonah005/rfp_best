"""158 current assessments for 89 live requirements.

`check_before_rerun.py` was written to answer three questions about today's
fixes. It answered all three -- and printed a fourth number nobody had asked
about:

    current assessments: 158
    live requirements:    89

`is_current` is supposed to mean "this is THE assessment of this requirement
right now". One per requirement. 158 of them across 89 live rows is not a
rounding error, and there are only two shapes it can have:

  * **Benign but misleading.** The extra 69 belong to requirements that have
    since been WITHDRAWN -- the fragmented rows coalesced 129 -> 104, and the
    ones `retire_superseded_extractions.py` retired. Their assessments were
    never cleared, so anything that counts readiness over `is_current` without
    joining to a live requirement counts retired work. The export filters, which
    is why the handover's numbers look sane; a dashboard, a metric or the next
    script that does not filter would be quietly wrong.

  * **A real integrity defect.** Two or more rows with `is_current = True` for
    the SAME requirement. `assess_requirement` retires the previous one before
    inserting, so this would mean a path that writes an assessment without going
    through it -- and then "the current readiness of R-042" has two answers and
    which one you see depends on row order.

Those want opposite fixes, so this counts rather than argues. It also checks the
third possibility -- an assessment whose requirement row is gone entirely, which
would be a broken foreign key rather than a lifecycle question.

Nothing is written. Read-only.

    python scripts/check_current_assessments.py
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

BID = "0e20677e-9cc3-434e-ad88-c953d6676028"


def main() -> int:
    import uuid

    from app.models import Organization, Requirement, RequirementAssessment

    bid_id = uuid.UUID(BID)
    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        # Outer join on purpose: an assessment whose requirement row is missing
        # entirely is a different problem from one whose requirement is
        # withdrawn, and an inner join would hide the first inside the count.
        rows = session.execute(
            select(
                RequirementAssessment.id,
                RequirementAssessment.requirement_id,
                RequirementAssessment.bid_readiness,
                Requirement.code,
                Requirement.lifecycle_state,
            )
            .outerjoin(Requirement, Requirement.id == RequirementAssessment.requirement_id)
            .where(
                RequirementAssessment.org_id == org.id,
                RequirementAssessment.bid_id == bid_id,
                RequirementAssessment.is_current.is_(True),
            )
        ).all()

        print("=" * 92)
        print(f"current assessments on this bid: {len(rows)}")
        print("=" * 92)

        # --- 1. whose requirements are they? ---------------------------------
        lifecycles = Counter(
            "(requirement row missing)" if state is None else str(getattr(state, "value", state))
            for _id, _rid, _r, _code, state in rows
        )
        print("\nby the lifecycle of the requirement they assess:")
        for state, n in sorted(lifecycles.items(), key=lambda kv: -kv[1]):
            print(f"  {state:<32} {n:>4}")

        live = [
            r for r in rows if r[4] is not None and str(getattr(r[4], "value", r[4])) != "WITHDRAWN"
        ]
        print(f"\nassessments of a LIVE requirement: {len(live)}")
        print(f"assessments of a withdrawn or missing one: {len(rows) - len(live)}")

        # --- 2. is any requirement carrying two? -----------------------------
        per_requirement = Counter(rid for _id, rid, _r, _code, _s in rows)
        duplicated = [(rid, n) for rid, n in per_requirement.items() if n > 1]
        print("\n" + "=" * 92)
        print("INTEGRITY: requirements carrying more than one CURRENT assessment")
        print("=" * 92)
        if not duplicated and len(rows) == len(live):
            print("  none, and there is no surplus either: every current assessment")
            print("  belongs to a live requirement. `is_current` means what it says.")
        elif not duplicated:
            print("  none. `is_current` holds -- one per requirement, as intended.")
            print("  So the surplus is a LIFECYCLE question, not a corruption one:")
            print("  retired requirements kept their assessment when they were withdrawn.")
            print("  Run scripts/repair_withdrawn_assessments.py.")
        else:
            print(f"  {len(duplicated)} requirement(s) have two or more. This is the bad case:")
            print("  'the current readiness of this row' has more than one answer, and which")
            print("  one a reader sees depends on row order.")
            codes = {rid: code for _id, rid, _r, code, _s in rows}
            for rid, n in sorted(duplicated, key=lambda kv: -kv[1])[:12]:
                print(f"    {codes.get(rid) or rid}: {n} current rows")

        # --- 3. what the readiness numbers look like once filtered -----------
        print("\n" + "=" * 92)
        print("READINESS, unfiltered vs live-only -- the gap is what an unfiltered")
        print("dashboard would be reporting")
        print("=" * 92)
        everything = Counter(str(getattr(r[2], "value", r[2])) for r in rows)
        live_only = Counter(str(getattr(r[2], "value", r[2])) for r in live)
        states = sorted(set(everything) | set(live_only))
        print(f"  {'state':<32} {'all':>6} {'live':>6} {'diff':>6}")
        for state in states:
            a, b = everything.get(state, 0), live_only.get(state, 0)
            print(f"  {state:<32} {a:>6} {b:>6} {a - b:>6}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
