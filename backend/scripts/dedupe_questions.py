"""Group the clarification questions that one customer reply would answer.

234 open ambiguity findings on the live tender carry 79 distinct question texts,
and several of those 79 are still one question:

    What is the required scope, format and duration of the source code and
        development knowledge transfer?
    What are the required IP/source ownership terms and the expected format and
        duration of the knowledge transfer?
    What is the required scope, format and acceptance criteria for the source
        code and development knowledge transfer?

Each lands on R-027 and R-124 to R-127, so a reviewer opening R-124 sees eleven
questions where there are four.

The judgement is the model's -- character similarity fails from both directions
here, and two deterministic rules were tried against this data and reverted. The
service records the verdict with a version stamp; nothing is deleted, and a
restatement keeps its text and simply stops holding requirement links.

This runs that pass on its own, so a full re-analysis is not needed to see what
it does. The dry run makes NO model calls: it shows what would be compared, and
what it will cost, before spending anything.

    python scripts/dedupe_questions.py             # what would be compared
    python scripts/dedupe_questions.py --apply     # judge and record
    python scripts/dedupe_questions.py --show      # read the verdicts afterwards
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
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

from sqlalchemy import select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.gateway.registry import get as get_call_type  # noqa: E402
from app.models.enums import ClarificationStatus  # noqa: E402
from app.services.clarification_dedupe import (  # noqa: E402
    MIN_GROUP,
    _locator_key,
    dedupe_open_questions,
)

BID = "0e20677e-9cc3-434e-ad88-c953d6676028"


def _short(text: str | None, width: int = 96) -> str:
    return textwrap.shorten(" ".join((text or "").split()), width)


def main() -> int:
    import uuid

    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="make the model calls and record")
    parser.add_argument("--show", action="store_true", help="print verdicts already recorded")
    args = parser.parse_args()

    from app.models import ClarificationIssue, Organization

    call_type = get_call_type("question_equivalence")
    stamp = f"llm:{call_type.name}@{call_type.prompt_version}"

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)
        bid_id = uuid.UUID(BID)

        if args.show:
            rows = (
                session.execute(
                    select(ClarificationIssue).where(
                        ClarificationIssue.org_id == org.id,
                        ClarificationIssue.bid_id == bid_id,
                        ClarificationIssue.restates_clarification_id.isnot(None),
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                print("No restatements recorded yet.")
                return 0
            canonical_ids = {r.restates_clarification_id for r in rows}
            canonicals = {
                issue.id: issue
                for issue in session.execute(
                    select(ClarificationIssue).where(
                        ClarificationIssue.org_id == org.id,
                        ClarificationIssue.id.in_(canonical_ids),
                    )
                ).scalars()
            }
            grouped: dict[uuid.UUID, list] = defaultdict(list)
            for row in rows:
                grouped[row.restates_clarification_id].append(row)
            print(f"{len(grouped)} question(s) absorbed {len(rows)} restatement(s).\n")
            for canonical_id, members in sorted(
                grouped.items(), key=lambda kv: -len(kv[1])
            ):
                canonical = canonicals.get(canonical_id)
                print("=" * 100)
                print("KEPT: " + _short(canonical.recommended_question if canonical else "?"))
                if members and members[0].equivalence_reason:
                    print("  one answer settles all of these because:")
                    print("    " + _short(members[0].equivalence_reason, 92))
                for member in members:
                    print("  - " + _short(member.recommended_question, 92))
            return 0

        issues = [
            issue
            for issue in session.execute(
                select(ClarificationIssue).where(
                    ClarificationIssue.org_id == org.id,
                    ClarificationIssue.bid_id == bid_id,
                    ClarificationIssue.status == ClarificationStatus.OPEN,
                    ClarificationIssue.restates_clarification_id.is_(None),
                )
            ).scalars()
            if (issue.recommended_question or "").strip()
        ]
        by_block: dict[tuple, list] = defaultdict(list)
        for issue in issues:
            key = _locator_key(issue.source_a)
            if key is not None:
                by_block[key].append(issue)

        comparable = {k: v for k, v in by_block.items() if len(v) >= MIN_GROUP}
        pending = {
            k: v for k, v in comparable.items()
            if any(i.equivalence_by != stamp for i in v)
        }
        print(f"{len(issues)} open question(s) across {len(by_block)} block(s).")
        print(f"{len(comparable)} block(s) hold two or more and can be compared.")
        print(f"{len(pending)} of those have something not yet judged by {stamp}.")
        print(f"\nThat is {len(pending)} model call(s) if you apply.\n")

        for key, group in sorted(pending.items(), key=lambda kv: -len(kv[1]))[:8]:
            print("=" * 100)
            print(f"block {key[1]}-{key[2]}   {len(group)} question(s)")
            for issue in group:
                print("  - " + _short(issue.recommended_question, 92))

        if not args.apply:
            print(
                "\nNo model calls were made and nothing was changed.\n"
                "    python scripts/dedupe_questions.py --apply"
            )
            return 0

        marked = dedupe_open_questions(session, ctx, bid_id=bid_id)
        session.commit()
        print(f"\nMarked {marked} restatement(s). Their requirement links were removed;")
        print("the question each one restates keeps the row.\n")
        print("    python scripts/dedupe_questions.py --show     # read the verdicts")
        print("    python scripts/check_clarification_links.py   # the effect on rows")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
