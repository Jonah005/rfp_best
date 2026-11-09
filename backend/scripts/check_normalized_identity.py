"""Do the matcher and the matrix score the same words?

`check_reconciliation.py` established that R-050 and R-055 share a block, that
the candidate lookup returns both, and that they score 1.000 against each other
-- yet a rerun created the second instead of matching it. Every explanation that
turns on the lookup or the thresholds is now excluded by measurement.

What is left is that the two comparisons are made on different fields.
Reconciliation scores `normalized_requirement`, the model's own restatement of
the obligation; the export, and the script above, score `requirement_text`, the
quoted clause. The clause is lifted from the document and is stable across runs.
The restatement is written fresh each time, and nothing constrains the model to
word it the same way twice.

If that is what happened, two proposals from one block carried the SAME quoted
clause and DIFFERENT restatements. They were not echoes of each other, because
echo collapse read the restatements. Only one of them could claim R-050,
because an existing requirement can be claimed once. The other had nothing left
to match and became a new requirement -- and then displayed as an exact
duplicate, because the field that is identical is the one no comparison looked
at.

So this prints both scores side by side for every block holding more than one
requirement, plus when each row was created and how many versions it carries.
A pair that reads 1.000 on the clause and well under the bar on the restatement
is the case above. A pair high on both is a different bug and this rules the
explanation out rather than confirming it.

Nothing is written.

    python scripts/check_normalized_identity.py
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
from app.models.enums import RequirementLifecycle  # noqa: E402
from app.services.requirements import (  # noqa: E402
    _POSITIONAL_FLOOR,
    _SAME_OBLIGATION,
    _similarity,
)


def main() -> int:
    from app.models import Bid, Organization, Requirement, RequirementVersion

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)
        print(
            f"echo bar {_SAME_OBLIGATION}, assignment floor {_POSITIONAL_FLOOR}\n"
            "clause    = requirement_text      (what the export and the matrix show)\n"
            "restated  = normalized_requirement (what reconciliation actually scores)\n"
        )

        bids = (
            session.execute(select(Bid).where(Bid.org_id == org.id).order_by(Bid.created_at))
            .scalars()
            .all()
        )

        for bid in bids:
            rows = session.execute(
                select(
                    Requirement,
                    RequirementVersion.requirement_text,
                    RequirementVersion.normalized_requirement,
                    RequirementVersion.source_document_version_id,
                    RequirementVersion.source_locator,
                )
                .join(RequirementVersion, RequirementVersion.requirement_id == Requirement.id)
                .where(
                    Requirement.org_id == org.id,
                    Requirement.bid_id == bid.id,
                    Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN,
                    RequirementVersion.superseded_at.is_(None),
                )
            ).all()
            if not rows:
                continue

            by_block: dict[tuple, list] = defaultdict(list)
            for requirement, text, normalized, version_id, locator in rows:
                locator = locator if isinstance(locator, dict) else {}
                key = (version_id, locator.get("char_start"), locator.get("char_end"))
                by_block[key].append((requirement, text or "", normalized or ""))

            crowded = {k: v for k, v in by_block.items() if len(v) > 1}
            if not crowded:
                continue

            print(f"=== bid {str(bid.id)[:8]}")
            for (version_id, char_start, char_end), entries in sorted(
                crowded.items(), key=lambda item: -len(item[1])
            ):
                print(f"\n  block {str(version_id)[:8]} chars {char_start}-{char_end}")
                for requirement, text, normalized in sorted(entries, key=lambda e: e[0].code):
                    versions = session.execute(
                        select(func.count(RequirementVersion.id)).where(
                            RequirementVersion.requirement_id == requirement.id
                        )
                    ).scalar_one()
                    created = getattr(requirement, "created_at", None)
                    stamp = created.isoformat(timespec="seconds") if created else "?"
                    print(f"    {requirement.code}  created {stamp}  {versions} version(s)")
                    # An empty restatement is its own finding: every score
                    # against it is 0, so such a row can never be matched and a
                    # rerun must duplicate it.
                    if not normalized.strip():
                        print("      restated: (EMPTY -- nothing to match on)")
                    else:
                        print(f"      restated: {' '.join(normalized.split())[:100]}")
                    print(f"      clause:   {' '.join(text.split())[:100]}")

                print("\n    pairwise, clause vs restatement:")
                ordered = sorted(entries, key=lambda e: e[0].code)
                for i, (a, text_a, norm_a) in enumerate(ordered):
                    for b, text_b, norm_b in ordered[i + 1 :]:
                        clause = _similarity(text_a, text_b)
                        restated = _similarity(norm_a, norm_b)
                        gap = clause - restated
                        # The signature of the split: identical in the document,
                        # different in the model's words. Flagged only when the
                        # clause is at or above the echo bar and the restatement
                        # is below it, because that is precisely the band where
                        # the two fields disagree about whether a row is a
                        # duplicate.
                        mark = ""
                        if clause >= _SAME_OBLIGATION and restated < _SAME_OBLIGATION:
                            mark = "  <== SPLIT: same clause, restatement missed the bar"
                        elif abs(gap) >= 0.20:
                            mark = "  <== fields disagree"
                        print(
                            f"      {a.code} vs {b.code}: "
                            f"clause {clause:.3f}   restated {restated:.3f}{mark}"
                        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
