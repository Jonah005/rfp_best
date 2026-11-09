"""What is actually inside a block that holds nine requirements?

Linking is exact: a finding reaches precisely the requirements of the block it
cites, no more. The reach is wide because the block is crowded -- 17727-18464
holds nine, 8216-9413 holds nine, 11420-12182 holds eight -- and a requirement
records the span of the block it came from, not its own sentence, so "this
block" is the finest granularity the data has.

That is only worth fixing if the nine are nine different obligations. If they
are nine facets of one subject, a question about that subject genuinely touches
all nine and the matrix is telling the truth.

So this prints the block's text beside the requirements drawn from it, and the
open questions currently landing on them, and the decision is made by reading
rather than by assuming.

Nothing is written. Read-only.

    python scripts/check_crowded_blocks.py            # blocks with 4+
    python scripts/check_crowded_blocks.py --min 6
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
from app.models.enums import ClarificationStatus, RequirementLifecycle  # noqa: E402

BID = "0e20677e-9cc3-434e-ad88-c953d6676028"


def _wrap(text: str, indent: str = "      ", width: int = 96) -> str:
    body = " ".join((text or "").split())
    return textwrap.fill(body, width=width, initial_indent=indent, subsequent_indent=indent)


def main() -> int:
    import uuid

    parser = argparse.ArgumentParser()
    parser.add_argument("--min", type=int, default=4, help="only blocks with this many requirements")
    args = parser.parse_args()

    from app.models import (
        ClarificationIssue,
        ClarificationRequirement,
        DocumentBlock,
        Organization,
        Requirement,
        RequirementVersion,
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

        rows = session.execute(
            select(Requirement, RequirementVersion)
            .join(RequirementVersion, RequirementVersion.id == Requirement.current_version_id)
            .where(
                Requirement.org_id == org.id,
                Requirement.bid_id == bid_id,
                Requirement.lifecycle_state.notin_(
                    [RequirementLifecycle.WITHDRAWN, RequirementLifecycle.SUPERSEDED]
                ),
            )
            .order_by(Requirement.code)
        ).all()

        per_block: dict[tuple, list] = defaultdict(list)
        for req, version in rows:
            locator = version.source_locator or {}
            per_block[
                (
                    version.source_document_version_id,
                    locator.get("char_start"),
                    locator.get("char_end"),
                )
            ].append((req, version))

        blocks = {
            (b.document_version_id, b.char_start, b.char_end): b
            for b in session.execute(
                select(DocumentBlock).where(DocumentBlock.org_id == org.id)
            ).scalars()
        }

        # Open questions per requirement, so each row shows what is landing on it.
        issues = {
            issue.id: issue
            for issue in session.execute(
                select(ClarificationIssue).where(
                    ClarificationIssue.org_id == org.id,
                    ClarificationIssue.bid_id == bid_id,
                    ClarificationIssue.status.in_(
                        [ClarificationStatus.OPEN, ClarificationStatus.SENT_TO_CLIENT]
                    ),
                )
            ).scalars()
        }
        questions: dict[uuid.UUID, list[str]] = defaultdict(list)
        for link in session.execute(
            select(ClarificationRequirement).where(ClarificationRequirement.org_id == org.id)
        ).scalars():
            issue = issues.get(link.clarification_id)
            if issue is None:
                continue
            question = (issue.recommended_question or "").strip()
            if question:
                questions[link.requirement_id].append(question)

        crowded = sorted(
            (kv for kv in per_block.items() if len(kv[1]) >= args.min),
            key=lambda kv: -len(kv[1]),
        )
        print(f"{len(crowded)} blocks hold {args.min}+ requirements\n")

        for key, members in crowded:
            block = blocks.get(key)
            codes = ", ".join(req.code for req, _v in members)
            print("=" * 100)
            print(f"block {key[1]}-{key[2]}   {len(members)} requirements: {codes}")
            if block is not None:
                if block.heading_path:
                    print(f"heading: {' > '.join(block.heading_path)}")
                if block.clause_ref:
                    print(f"clause_ref: {block.clause_ref}   ordinal: {block.ordinal}")
                print("\n--- block text ---")
                print(_wrap(block.text or "", indent="  "))
            print("\n--- requirements drawn from it ---")
            for req, version in members:
                print(f"  {req.code}  [{req.category.value}/{req.obligation_owner.value}]")
                print(_wrap(version.requirement_text or ""))
                asked = questions.get(req.id, [])
                if asked:
                    print(f"      ({len(asked)} open questions landing here)")
            print()
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
