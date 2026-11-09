"""Close coverage findings that deny the requirements they link to.

    "Clause 12 governs this tender but produced neither a requirement nor an
     explicit decision that it imposes none."   -> linked to R-012, R-112

43 of the live bid's 47 open COVERAGE_GAP findings say this about clauses that
plainly did produce requirements. The cause is in `extract_requirements`: the
check compared against blocks THIS RUN processed, and a rerun skips every block
already decomposed under the current prompt, so on the second run of an
unchanged document almost nothing counts as covered.

That is fixed at the source. This exists because the fix only runs when
extraction runs, and an export taken after a full analysis showed all 47 still
open -- so the repair must not depend on reaching that path. A remediation that
can only be applied by the code path that failed is not a remediation.

The test is the same one extraction now applies, using the same helper, so the
two cannot disagree: a block with live requirements is covered. Nothing is
re-derived here.

    python scripts/close_false_coverage_gaps.py           # what would close
    python scripts/close_false_coverage_gaps.py --apply

Findings whose clause genuinely produced nothing stay open. Those are the ones
worth reading -- they are what the detector is for.
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

from sqlalchemy import delete, select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.models.enums import ClarificationStatus  # noqa: E402
from app.services import audit  # noqa: E402
from app.services.requirements import (  # noqa: E402
    _block_read_and_found_nothing,
    _requirements_from_block,
)

BID = "0e20677e-9cc3-434e-ad88-c953d6676028"
DETECTOR = "requirement_coverage_check@v1"


def main() -> int:
    import uuid

    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="close them")
    args = parser.parse_args()

    from app.models import (
        ClarificationIssue,
        ClarificationRequirement,
        DocumentBlock,
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
        bid_id = uuid.UUID(BID)

        issues = (
            session.execute(
                select(ClarificationIssue).where(
                    ClarificationIssue.org_id == org.id,
                    ClarificationIssue.bid_id == bid_id,
                    ClarificationIssue.detected_by == DETECTOR,
                    ClarificationIssue.status == ClarificationStatus.OPEN,
                )
            )
            .scalars()
            .all()
        )
        if not issues:
            print(f"No open findings from {DETECTOR}.")
            return 0

        blocks = {
            (b.document_version_id, b.char_start, b.char_end): b
            for b in session.execute(
                select(DocumentBlock).where(DocumentBlock.org_id == org.id)
            ).scalars()
        }

        false_positive: list = []
        genuine: list = []
        unresolvable: list = []
        for issue in issues:
            locator = issue.source_a if isinstance(issue.source_a, dict) else {}
            version_id = locator.get("document_version_id")
            if isinstance(version_id, str):
                try:
                    version_id = uuid.UUID(version_id)
                except ValueError:
                    version_id = None
            block = blocks.get(
                (version_id, locator.get("char_start"), locator.get("char_end"))
            )
            if block is None:
                unresolvable.append(issue)
                continue
            produced = _requirements_from_block(session, ctx, bid_id=bid_id, block=block)
            if produced:
                false_positive.append((issue, [r.code for r, _t in produced]))
            elif _block_read_and_found_nothing(session, ctx, bid_id=bid_id, block=block):
                false_positive.append((issue, []))
            else:
                genuine.append((issue, block))

        print(f"{len(issues)} open coverage finding(s).")
        print(f"  {len(false_positive)} contradict themselves and would close")
        print(f"  {len(genuine)} name a clause that really produced nothing")
        print(f"  {len(unresolvable)} cite a block that cannot be resolved (left alone)\n")

        for issue, codes in false_positive[:10]:
            text = textwrap.shorten(" ".join(str(issue.description).split()), 80)
            print(f"  CLOSE  {text}")
            print(f"         produced: {', '.join(codes) if codes else 'nothing, and that was recorded'}")

        if genuine:
            print("\nstaying open -- read these:")
            for issue, block in genuine:
                clause = block.clause_ref or f"block {block.ordinal}"
                head = " > ".join(block.heading_path or []) or "(no heading)"
                print(f"  {clause:12} {head}")
                print(f"      {textwrap.shorten(' '.join((block.text or '').split()), 92)}")

        if not args.apply:
            print(
                "\nNothing was changed.\n"
                "    python scripts/close_false_coverage_gaps.py --apply"
            )
            return 0

        ids = [issue.id for issue, _ in false_positive]
        if not ids:
            print("Nothing to close.")
            return 0
        session.execute(
            delete(ClarificationRequirement).where(
                ClarificationRequirement.org_id == org.id,
                ClarificationRequirement.clarification_id.in_(ids),
            )
        )
        for issue, _codes in false_positive:
            issue.status = ClarificationStatus.CLOSED
        session.flush()
        audit.record(
            session,
            ctx,
            action="clarification.coverage_gap_resolved",
            entity_type="bid",
            entity_id=bid_id,
            bid_id=bid_id,
            after={"detected_by": DETECTOR, "closed": len(ids), "still_open": len(genuine)},
            reason="The clause has requirements, or a recorded reading that found none.",
        )
        session.commit()
        print(f"\nClosed {len(ids)} finding(s) and removed their links.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
