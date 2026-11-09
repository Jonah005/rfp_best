"""Dump the exact texts behind the identity bug, for use as test fixtures.

The regression test for the clause-vs-restatement defect has to reproduce the
real score -- 0.748 between R-050 and R-055 -- or it is not testing the bug. A
hand-written paraphrase does not: the first attempt scored 0.877 and passed a
test that should have failed, which is exactly how the difflib `autojunk`
defect survived its own unit test earlier in this codebase.

So the fixture is taken from the database rather than typed. Prints JSON: for
each code, the full requirement_text and normalized_requirement, plus the
scores that matter, so the numbers quoted in the test can be checked against
the ones the live rows actually produce.

    python scripts/dump_identity_fixture.py > fixture.json
"""

from __future__ import annotations

import json
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

from sqlalchemy import select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.services.requirements import _similarity  # noqa: E402

CODES = ["R-050", "R-055", "R-024", "R-025"]

# Requirement codes restart per bid, so R-024 exists in all three. The first
# version of this script selected on code alone and took `.first()`, and
# returned another bid's R-024 and R-025 -- two unrelated clauses scoring 0.239
# -- while looking exactly as authoritative as the rows that were wanted. The
# bid is now required, and every row prints the bid it came from so the output
# carries its own proof.
DEFAULT_BID_PREFIX = "0e20677e"


def main() -> int:
    prefix = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BID_PREFIX).lower()

    from app.models import Bid, Organization, Requirement, RequirementVersion

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        bids = [
            bid
            for bid in session.execute(select(Bid).where(Bid.org_id == org.id)).scalars().all()
            if str(bid.id).lower().startswith(prefix)
        ]
        if len(bids) != 1:
            print(f"{len(bids)} bids match {prefix!r}; name one exactly.")
            return 1
        bid = bids[0]

        texts: dict[str, dict] = {}
        for code in CODES:
            row = session.execute(
                select(RequirementVersion.requirement_text, RequirementVersion.normalized_requirement)
                .join(Requirement, Requirement.id == RequirementVersion.requirement_id)
                .where(
                    Requirement.org_id == org.id,
                    Requirement.bid_id == bid.id,
                    Requirement.code == code,
                    RequirementVersion.superseded_at.is_(None),
                )
            ).first()
            if row is None:
                continue
            clause, restated = row
            texts[code] = {"bid": str(bid.id)[:8], "clause": clause, "restated": restated}

        scores = {}
        if "R-050" in texts and "R-055" in texts:
            scores["cross_field_R050_clause_vs_R055_restated"] = round(
                _similarity(texts["R-050"]["clause"], texts["R-055"]["restated"] or ""), 4
            )
            scores["clause_vs_clause_R050_R055"] = round(
                _similarity(texts["R-050"]["clause"], texts["R-055"]["clause"]), 4
            )
        if "R-024" in texts and "R-025" in texts:
            scores["clause_vs_clause_R024_R025"] = round(
                _similarity(texts["R-024"]["clause"], texts["R-025"]["clause"]), 4
            )
            scores["restated_vs_restated_R024_R025"] = round(
                _similarity(
                    texts["R-024"]["restated"] or "", texts["R-025"]["restated"] or ""
                ),
                4,
            )

        print(json.dumps({"texts": texts, "scores": scores}, indent=2, ensure_ascii=False))
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
