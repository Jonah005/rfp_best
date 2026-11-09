"""Retire requirements that restate one already recorded from the same block.

Three analysis runs on an unchanged document produced 50 requirement rows for
roughly 38 obligations. Identity for an unnumbered block used to be the model's
own wording, so a re-worded restatement never matched and each rerun minted a
fresh row beside the old one. One block -- document version d1b106ba, characters
8216-9413 -- yielded six requirements that are two obligations phrased three
ways each.

`_find_existing` now keys identity on the source block, so no new duplicates are
created. That does nothing for the ones already there: they carry evidence
links, assessments and tasks of their own, and they inflate every count in the
matrix.

Grouping rule, deliberately the same one extraction uses, so this script and the
pipeline cannot disagree:

    same document version, same character span, similarity >= _SAME_OBLIGATION

Anything from the same block scoring between REVIEW_FLOOR and that threshold is
REPORTED and left alone. The first version of this script withdrew everything
above 0.60 and proposed deleting two real Tradenet obligations -- "create
beneficiary accounts" and "auto approval of compliance" -- as duplicates of
"create electronic trading accounts", because all three share a long stem and
differ only at the end. An obligation withdrawn in error is unrecoverable: it
leaves the matrix and nothing says the bid stopped covering it. A duplicate left
in place is merely visible. So the ambiguous band goes to a person.

The LOWEST code in each group is kept -- it is the one drafts, tasks and any
external reference are most likely to cite, and the later rows are the accidents.
The rest are marked WITHDRAWN.

Nothing is deleted. A withdrawn requirement keeps its versions, its assessments
and its audit trail; `_find_existing` already excludes WITHDRAWN rows, so it
simply stops appearing in the matrix and stops being matched against.

    python scripts/merge_duplicate_requirements.py                     # show the groups
    python scripts/merge_duplicate_requirements.py --apply              # withdraw the extras
    python scripts/merge_duplicate_requirements.py --withdraw R-040,R-048

The third form acts on the review list: having read two texts and judged them
the same obligation, name the rows to retire. It exists because "merge by hand"
was advice with no tool behind it -- there is no requirement-withdraw endpoint
in the API, so without this the reported pairs could only be looked at.

A code is refused unless something else in its own block scores at least
REVIEW_FLOOR against it. That keeps this a way to confirm a judgement the script
already surfaced, not a way to delete an arbitrary requirement.

Read the dry run before applying. It prints every text in each group, because a
similarity score is a judgement about prose and the only way to check it is to
read the prose.
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

from sqlalchemy import select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.models.enums import RequirementLifecycle  # noqa: E402
from app.services import requirements as requirement_service  # noqa: E402
from app.services.requirements import _SAME_OBLIGATION, _similarity  # noqa: E402

REVIEW_FLOOR = 0.60

REASON = "Withdrawn: restates an obligation already recorded from the same source block."


def _requested_codes(argv: list[str]) -> set[str]:
    for index, arg in enumerate(argv):
        if arg == "--withdraw" and index + 1 < len(argv):
            return {c.strip().upper() for c in argv[index + 1].split(",") if c.strip()}
        if arg.startswith("--withdraw="):
            return {c.strip().upper() for c in arg.split("=", 1)[1].split(",") if c.strip()}
    return set()


def main() -> int:
    apply_changes = "--apply" in sys.argv
    requested = _requested_codes(sys.argv)

    from app.models import Bid, Organization, Requirement, RequirementVersion

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        bids = (
            session.execute(select(Bid).where(Bid.org_id == org.id).order_by(Bid.created_at))
            .scalars()
            .all()
        )

        group_withdrawn = 0
        requested_withdrawn = 0
        for bid in bids:
            rows = session.execute(
                select(
                    Requirement,
                    RequirementVersion.requirement_text,
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

            # Bucketed by source block first. Two requirements from different
            # places in the document are different obligations however alike
            # they read -- a tender may state the same duty twice on purpose,
            # and merging those would lose a citation.
            by_block: dict[tuple, list] = defaultdict(list)
            for requirement, text, version_id, locator in rows:
                locator = locator if isinstance(locator, dict) else {}
                key = (version_id, locator.get("char_start"), locator.get("char_end"))
                by_block[key].append((requirement, text or ""))

            groups: list[list] = []
            review: list[tuple] = []
            for entries in by_block.values():
                entries.sort(key=lambda pair: pair[0].code)
                remaining = list(entries)
                while remaining:
                    keeper, keeper_text = remaining.pop(0)
                    cluster = [(keeper, keeper_text)]
                    rest = []
                    for requirement, text in remaining:
                        if _similarity(keeper_text, text) >= _SAME_OBLIGATION:
                            cluster.append((requirement, text))
                        else:
                            rest.append((requirement, text))
                    remaining = rest
                    if len(cluster) > 1:
                        groups.append(cluster)
                    # Close enough to be worth a person's eye, not close enough
                    # for this script to act on.
                    for requirement, text in rest:
                        score = _similarity(keeper_text, text)
                        if score >= REVIEW_FLOOR:
                            review.append((keeper, keeper_text, requirement, text, score))

            if not groups and not review:
                continue

            print(f"\n=== bid {str(bid.id)[:8]}   {len(rows)} live requirements")
            for cluster in groups:
                keep = cluster[0][0]
                drop = [r for r, _ in cluster[1:]]
                print(f"\n  keep {keep.code}, withdraw {', '.join(r.code for r in drop)}")
                for requirement, text in cluster:
                    mark = "KEEP  " if requirement is keep else "drop  "
                    score = _similarity(cluster[0][1], text)
                    body = " ".join(text.split())[:96]
                    print(f"    {mark}{requirement.code}  ({score:.3f})  {body}")

                # Counted only when actually applied. Counting the PLAN as
                # though it were the outcome is how this script reported
                # "Withdrew 3" on a run that withdrew one, two lines under its
                # own "Not withdrawn: R-053".
                if apply_changes:
                    # This script set the lifecycle column and nothing else:
                    # no successor recorded, no clarification links moved, no
                    # assessment retired. Two of those were fixed in the two
                    # sibling scripts that do the same job and never here --
                    # the third was fixed nowhere, which is how 69 retired
                    # requirements came to carry assessments still marked
                    # current on the live bid.
                    for requirement in drop:
                        requirement_service.withdraw(
                            session,
                            ctx,
                            requirement=requirement,
                            successors=[keep.id],
                            reason=f"Duplicate of {keep.code}.",
                        )
                    group_withdrawn += len(drop)

            # Named rows from the review list, retired on a human's say-so.
            if requested:
                eligible = {}
                # Rows the script grouped automatically count as eligible too.
                # They are the clearest duplicates it found, and leaving them
                # out meant a code named on --withdraw could be refused for
                # being TOO obviously a duplicate -- which is how R-053 was
                # reported "not withdrawn" while R-054 went through.
                for cluster in groups:
                    keeper = cluster[0][0]
                    for requirement, text in cluster[1:]:
                        eligible[requirement.code] = (
                            requirement,
                            keeper.code,
                            _similarity(cluster[0][1], text),
                        )
                for keep, _keep_text, other, _other_text, score in review:
                    eligible[other.code] = (other, keep.code, score)
                    eligible.setdefault(keep.code, (keep, other.code, score))
                for code in sorted(requested):
                    entry = eligible.get(code)
                    if entry is None:
                        continue
                    requirement, against, score = entry
                    print(
                        f"\n  withdrawing {code} on request (scored {score:.3f} against {against})"
                    )
                    # `against` is the CODE of the row this was scored
                    # against, and `eligible` holds the object for it, so the
                    # successor is recoverable rather than guessed.
                    twin = eligible.get(against)
                    requirement_service.withdraw(
                        session,
                        ctx,
                        requirement=requirement,
                        successors=[twin[0].id] if twin else [],
                        reason=f"Retired on request; duplicate of {against}.",
                    )
                    requested_withdrawn += 1
                    requested.discard(code)

            if review:
                print(
                    f"\n  {len(review)} pair(s) from the same block are similar but below the "
                    f"{_SAME_OBLIGATION:.2f} bar.\n  NOT touched -- read them and merge by hand "
                    "if they are the same obligation:"
                )
                for keep, keep_text, other, other_text, score in review:
                    print(f"    ({score:.3f})  {keep.code}: {' '.join(keep_text.split())[:80]}")
                    print(f"             {other.code}: {' '.join(other_text.split())[:80]}")

        if requested:
            print(
                "\nNot withdrawn: " + ", ".join(sorted(requested)) + "\n"
                "  Either the code does not exist, or nothing in its block scores "
                f"at least {REVIEW_FLOOR:.2f} against it. This only confirms pairs the "
                "script itself reported."
            )

        total_withdrawn = group_withdrawn + requested_withdrawn
        if total_withdrawn == 0 and not apply_changes and not _requested_codes(sys.argv):
            print("\nNo duplicate requirements. Nothing to do.")
            return 0

        # --withdraw is an instruction, not a proposal, so it commits on its own.
        if not apply_changes and not _requested_codes(sys.argv):
            print(
                f"\n{total_withdrawn} requirement(s) would be withdrawn."
                "\nDry run. Nothing was changed. Re-run with --apply."
            )
            return 0

        if total_withdrawn == 0:
            print("\nNothing was withdrawn.")
            if not apply_changes:
                print("  Add --apply to act on the groups listed above.")
            return 0

        session.commit()
        parts = []
        if group_withdrawn:
            parts.append(f"{group_withdrawn} from the groups above")
        if requested_withdrawn:
            parts.append(f"{requested_withdrawn} named on --withdraw")
        print(
            f"\nWithdrew {total_withdrawn} requirement(s) ({', '.join(parts)}). Nothing was\n"
            "deleted -- each keeps its versions, assessments and audit trail, and simply\n"
            "stops appearing.\n\n"
            "Re-run the assessment so the matrix is recomputed over the remaining set."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
