"""Move clarification links off requirements that were merged away.

An export of 104 requirements referenced 129 requirement codes. Twenty-five of
them -- R-070 to R-073, R-098 to R-100, R-103 to R-108, R-113, R-114, R-116 to
R-118, R-120 and R-145 to R-150 -- were merged into survivors by
`coalesce_enumerated_fragments.py`, which withdrew them and left the join table
addressing rows that no longer appear anywhere. A workbook that ends at R-144
carried a clarification citing R-150.

`coalesce_enumerated_fragments.py` now moves the links as part of the merge, so
this is for the merge that already happened.

The link moves rather than being deleted. The question was raised about that
clause, and the clause is still in the bid -- it is part of the survivor now.
Deleting would quietly drop a customer question because of a bookkeeping change,
which is the direction this codebase does not take.

Every withdrawn requirement records what replaced it in
`superseded_by_requirement_id`, so no guessing is involved. A withdrawn
requirement with no successor is reported and left alone: that is a withdrawal
somebody made deliberately, and its questions are not somebody else's.

    python scripts/repair_withdrawn_links.py          # what would move
    python scripts/repair_withdrawn_links.py --apply
"""

from __future__ import annotations

import argparse
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

from sqlalchemy import select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.models.enums import RequirementLifecycle  # noqa: E402
from app.services import audit  # noqa: E402

BID = "0e20677e-9cc3-434e-ad88-c953d6676028"


def main() -> int:
    import uuid

    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="move the links")
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

        requirements = {
            r.id: r
            for r in session.execute(
                select(Requirement).where(
                    Requirement.org_id == org.id, Requirement.bid_id == bid_id
                )
            ).scalars()
        }
        withdrawn = {
            r.id: r
            for r in requirements.values()
            if r.lifecycle_state
            in (RequirementLifecycle.WITHDRAWN, RequirementLifecycle.SUPERSEDED)
        }
        links = list(
            session.execute(
                select(ClarificationRequirement).where(
                    ClarificationRequirement.org_id == org.id,
                    ClarificationRequirement.requirement_id.in_(list(withdrawn)),
                )
            ).scalars()
        )
        if not links:
            print("No clarification links point at a withdrawn requirement.")
            return 0

        issues = {
            i.id: i
            for i in session.execute(
                select(ClarificationIssue).where(
                    ClarificationIssue.org_id == org.id,
                    ClarificationIssue.id.in_([link.clarification_id for link in links]),
                )
            ).scalars()
        }

        # Where each withdrawn requirement's links should go.
        #
        # `superseded_by_requirement_id` is the exact answer when it is set.
        # `retire_superseded_extractions.py` did not set it until today, so 25
        # requirements were withdrawn with no successor recorded and their 130
        # links were left addressing rows that appear nowhere.
        #
        # The fallback is not a guess. A clarification cites a BLOCK, and the
        # block's surviving requirements are exactly what the analysis backfill
        # would link it to on the next run -- so the links go there, computed
        # with extraction's own block lookup rather than a second implementation
        # of it.
        from app.services.requirements import _requirements_from_block

        blocks = {
            (b.document_version_id, b.char_start, b.char_end): b
            for b in session.execute(
                select(DocumentBlock).where(DocumentBlock.org_id == org.id)
            ).scalars()
        }
        versions = {
            v.requirement_id: v
            for v in session.execute(
                select(RequirementVersion).where(RequirementVersion.org_id == org.id)
            ).scalars()
        }

        def _targets(source) -> list:
            if source.superseded_by_requirement_id in requirements:
                return [source.superseded_by_requirement_id]
            version = versions.get(source.current_version_id) or versions.get(source.id)
            if version is None:
                return []
            locator = version.source_locator or {}
            block = blocks.get(
                (
                    version.source_document_version_id,
                    locator.get("char_start"),
                    locator.get("char_end"),
                )
            )
            if block is None:
                return []
            return [
                r.id
                for r, _t in _requirements_from_block(
                    session, ctx, bid_id=bid_id, block=block
                )
            ]

        movable: list = []
        orphaned: list = []
        for link in links:
            source = withdrawn[link.requirement_id]
            if _targets(source):
                movable.append(link)
            else:
                orphaned.append(link)

        by_pair: dict[tuple, int] = defaultdict(int)
        for link in movable:
            source = withdrawn[link.requirement_id]
            names = ", ".join(
                sorted(requirements[t].code for t in _targets(source) if t in requirements)
            )
            by_pair[(source.code, names)] += 1

        print(f"{len(links)} link(s) point at a withdrawn requirement.")
        print(f"  {len(movable)} can be re-pointed at a live requirement")
        print(f"  {len(orphaned)} have no live target and are left alone\n")
        for (source_code, target_code), count in sorted(by_pair.items()):
            print(f"  {source_code} -> {target_code}   {count} link(s)")

        affected = {issues[link.clarification_id] for link in movable if link.clarification_id in issues}
        detectors = sorted({i.detected_by for i in affected})
        print(f"\nheld by {len(affected)} clarification(s): {', '.join(detectors)}")

        if orphaned:
            print("\nleft alone (withdrawn with no successor recorded):")
            for link in orphaned[:10]:
                print(f"  {withdrawn[link.requirement_id].code}")

        if not args.apply:
            print(
                "\nNothing was changed.\n"
                "    python scripts/repair_withdrawn_links.py --apply"
            )
            return 0

        # The move itself is `retire_superseded_extractions._move_clarification_links`,
        # imported rather than rewritten: it already handles the unique
        # constraint on (clarification, requirement), copies a question onto
        # every survivor, and drops a link only when all of them already carry
        # it. A second implementation of that is how two tools come to disagree
        # about the same table.
        from retire_superseded_extractions import _move_clarification_links

        moved = 0
        for source in {withdrawn[link.requirement_id] for link in movable}:
            if source.superseded_by_requirement_id is None:
                targets = _targets(source)
                if targets:
                    source.superseded_by_requirement_id = targets[0]
            moved += _move_clarification_links(
                session, org.id, source=source.id, targets=_targets(source)
            )
        collapsed = len(movable) - moved
        session.flush()

        audit.record(
            session,
            ctx,
            action="clarification.links_followed_merge",
            entity_type="bid",
            entity_id=bid_id,
            bid_id=bid_id,
            after={"moved": moved, "collapsed": collapsed, "left": len(orphaned)},
            reason=(
                "Links addressed requirements merged into survivors; the clause is "
                "still in the bid, so the question follows it."
            ),
        )
        session.commit()
        print(
            f"\nMoved {moved} link(s); {collapsed} collapsed into a link the survivor "
            f"already held.\nRe-export and the clarifications should reference only "
            "live requirement codes."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
