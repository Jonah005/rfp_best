"""One submission, one row: fold restatements of the same deliverable together.

The tender's Deliverables sheet holds 32 rows for about thirteen things a bidder
actually hands over. Six of them are the financial proposal:

    Financial proposal
    Financial proposal (not more than two pages)
    Financial proposal (not more than two pages) indicating the overall fixed price
    Financial proposal (not more than two pages) indicating the overall fixed price
    Financial proposal (not more than two pages) indicating the overall fixed price, payment terms
    Financial proposal (not more than two pages, indicating overall fixed price, payment terms and ...)

Four are the terms and conditions, three are the CVs, three the signed proposal.
They accumulate because each is fingerprinted on its own wording, and several
clauses of the tender mention the same submission with a bit more detail each
time. Nobody assembling a submission pack can work from that list.

## Why this is a model call

Containment was implemented first and measured against the live list, and it
fails from both sides at once:

  * "Proposal (technical and financials)" CONTAINS both "Financial proposal" and
    "Technical proposal". A containment rule folds two separate submissions into
    one row, and a bidder hands over one document where the tender demanded two.
  * "... indicating the overall fixed price for the project" and "... indicating
    overall fixed price, payment terms and conditions" are siblings -- neither
    inside the other -- and are plainly one document. No containment rule reaches
    them.

Connected components over containment is worse still: it treats a directional
relation as an equivalence, and on this list it chained the financial proposal,
the technical proposal and the CVs into a single twelve-row blob.

So the model answers one question -- would a bidder hand over ONE document to
satisfy both of these -- and this script decides what happens to the rows. Same
division as `question_equivalence`, for the same reason: meaning is the model's
job, consequences are the code's.

## What travels with a merge

Deliverables carry requirement links of their own, and withdrawing a row without
moving them is the exact failure that put 25 dangling requirement codes in an
export, twice, in two different scripts. So the links move here as part of the
merge rather than being left to a repair script afterwards.

## Why a link and not a status

The first version marked a folded row NOT_APPLICABLE. A review of that export
was right to reject it: the tender DOES require that submission, so filing the
row under the status meaning "we do not have to do this" is false about the
obligation -- and it left 15 rows reading "mandatory" and "not applicable" at
once, with nothing recording which deliverable each was folded into.

`ClarificationIssue.restates_clarification_id` and
`Requirement.superseded_by_requirement_id` already do this properly. A folded
deliverable now keeps its status and points at its canonical twin, with the
verdict's provenance beside it. Reversing a fold is clearing three columns.

Rows retired by the earlier version are restored on the next --apply: they are
recognisable as NOT_APPLICABLE with no duplicate link.

    python scripts/coalesce_deliverables.py             # show the groups
    python scripts/coalesce_deliverables.py --apply
"""

from __future__ import annotations

import argparse
import json
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

from sqlalchemy import select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.models.enums import DeliverableStatus  # noqa: E402
from app.services import audit  # noqa: E402

BID = "0e20677e-9cc3-434e-ad88-c953d6676028"

def _move_requirement_links(session, org_id, *, source, target) -> int:
    """Carry a folded deliverable's requirement links onto the survivor.

    Withdrawing a row without this is how 25 dangling requirement codes reached
    an export -- twice, in two different scripts, because the first fix was
    applied to one instance of the pattern and not to its sibling. Doing it here
    as part of the merge is cheaper than another repair script.
    """
    from app.models import DeliverableRequirement

    existing = {
        row.requirement_id
        for row in session.execute(
            select(DeliverableRequirement).where(
                DeliverableRequirement.org_id == org_id,
                DeliverableRequirement.deliverable_id == target,
            )
        ).scalars()
    }
    moved = 0
    for link in session.execute(
        select(DeliverableRequirement).where(
            DeliverableRequirement.org_id == org_id,
            DeliverableRequirement.deliverable_id == source,
        )
    ).scalars():
        if link.requirement_id in existing:
            # The join is unique on (deliverable, requirement); the survivor
            # already carries this one.
            session.delete(link)
            continue
        link.deliverable_id = target
        existing.add(link.requirement_id)
        moved += 1
    session.flush()
    return moved


def _group(session, ctx, rows: list) -> list[list]:
    """Ask the model which entries are one document, then group by its answer.

    Containment was implemented first and measured against the live list. It
    fails from both sides at once, which is why this is a model call and not a
    rule:

      * "Proposal (technical and financials)" CONTAINS both "Financial proposal"
        and "Technical proposal", so a containment rule merges two separate
        submissions into one row -- and a bidder then hands over one document
        where the tender demanded two.
      * "... indicating the overall fixed price for the project" and
        "... indicating overall fixed price, payment terms and conditions" are
        siblings, neither inside the other, and are obviously one document. No
        containment rule reaches them.

    The model answers one question -- would a bidder hand over ONE document to
    satisfy both -- and this function does the grouping. Every index is checked
    before anything is grouped: an out-of-range index means the model miscounted,
    and grouping the in-range part of a miscounted answer would fold an
    unrelated submission.
    """
    from app.gateway.gateway import build_gateway
    from app.schemas.clarification_dedupe import QuestionDedupeResult

    if len(rows) < 2:
        return []
    payload = json.dumps(
        {
            "deliverables": [
                {"index": index, "name": " ".join((row.name or "").split())}
                for index, row in enumerate(rows)
            ]
        },
        ensure_ascii=False,
    )
    result = build_gateway().generate_structured(
        "deliverable_equivalence", payload, ctx=ctx, session=session
    )
    if not isinstance(result, QuestionDedupeResult):
        raise SystemExit("deliverable_equivalence returned an unexpected schema.")

    groups: list[list] = []
    for group in result.groups:
        indexes = [group.canonical_index, *group.restatement_indexes]
        if any(i < 0 or i >= len(rows) for i in indexes):
            print(f"  (ignored a group citing indexes outside the list: {indexes})")
            continue
        groups.append([rows[i] for i in indexes])
    return groups


def main() -> int:
    import uuid

    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="fold them")
    args = parser.parse_args()

    from app.models import Deliverable, Organization

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)
        bid_id = uuid.UUID(BID)

        rows = list(
            session.execute(
                select(Deliverable)
                .where(
                    Deliverable.org_id == org.id,
                    Deliverable.bid_id == bid_id,
                    # Canonical rows only. The selector is the LINK now, not the
                    # status -- a row retired by the earlier version is picked
                    # up here precisely so this run can restore it and file it
                    # properly.
                    Deliverable.duplicate_of_deliverable_id.is_(None),
                )
                .order_by(Deliverable.name)
            ).scalars()
        )
        groups = _group(session, ctx, rows)
        folded = sum(len(g) - 1 for g in groups)
        print(f"{len(rows)} live deliverable(s); {len(groups)} group(s) of restatements.")
        print(f"folding would leave {len(rows) - folded}.\n")

        moved_total = 0
        restored = 0
        equivalence_by = "llm:deliverable_equivalence@v1"
        for group in sorted(groups, key=lambda g: -len(g)):
            # The model's canonical_index is first in each group.
            survivor = group[0]
            others = [d for d in group if d.id != survivor.id]
            print("=" * 96)
            print(textwrap.fill(survivor.name or "", width=94,
                                initial_indent="  KEEP   ", subsequent_indent="         "))
            for other in sorted(others, key=lambda d: len(d.name or "")):
                print(textwrap.fill(other.name or "", width=94,
                                    initial_indent="  fold   ", subsequent_indent="         "))
            if not args.apply:
                continue
            for other in others:
                moved_total += _move_requirement_links(
                    session, org.id, source=other.id, target=survivor.id
                )
                # The link, not the status. A duplicate is still a mandatory
                # submission; it is simply named more fully by the survivor.
                other.duplicate_of_deliverable_id = survivor.id
                other.equivalence_by = equivalence_by
                other.equivalence_reason = (
                    f"Names the same submission as {survivor.name[:120]!r}, less completely."
                )
                if other.status == DeliverableStatus.NOT_APPLICABLE:
                    # Retired by the earlier version of this script, which used
                    # the status as the marker. Put it back: the obligation
                    # never went away.
                    other.status = DeliverableStatus.IDENTIFIED
                    restored += 1
        print("=" * 96)

        if not args.apply:
            print(
                "\nNothing was changed. Read each group: the KEEP line should be the same\n"
                "submission as every fold line, said more fully.\n"
                "    python scripts/coalesce_deliverables.py --apply"
            )
            return 0

        if not folded:
            print("Nothing to fold.")
            return 0

        audit.record(
            session,
            ctx,
            action="deliverable.restatements_folded",
            entity_type="bid",
            entity_id=bid_id,
            bid_id=bid_id,
            after={
                "folded": folded,
                "remaining": len(rows) - folded,
                "links_moved": moved_total,
                "status_restored": restored,
                "equivalence_by": equivalence_by,
            },
            reason=(
                "Rows naming the same submission less completely than the one kept. "
                "Recorded as a link to the canonical deliverable; status untouched, "
                "because the obligation is still the bidder's."
            ),
        )
        session.commit()
        print(
            f"\nFolded {folded} deliverable(s); {moved_total} requirement link(s) moved onto\n"
            f"the survivors; {restored} row(s) retired by the earlier version had their\n"
            "status restored. Nothing was deleted and no status was retired -- each folded\n"
            "row points at its canonical twin and keeps its history.\n\n"
            "Owner, deadline, format and filename are still blank on every row. That is\n"
            "the next piece of work, and it is data capture rather than extraction."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
