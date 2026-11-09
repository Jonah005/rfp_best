"""Mark the evidence rows that say, in their own text, that they are not proof.

610 evidence rows are typed OTHER and every one reads as proof of capability.
`scripts/check_evidence_units.py` showed why that is worse than a missing label:
the company pack is largely a set of REGISTERS, and its rows declare their own
status in the text --

    Evidence status: NOT EVIDENCED          Value: [REPLACE FROM OFFICIAL RECORD]
    Status / owner: MISSING / Sales         Current status: [HR TO CONFIRM]
    SYNTHETIC / NOT AN OFFER                Notes: Demo value
    Status: TEMPLATE BELOW                  DRAFT FOR APPROVAL

-- and a drafter citing one of those to a customer asserts something the company
cannot back.

This applies `app/services/evidence_markers.py`, which is deterministic code
rather than a model call for three reasons given in full there: the downgrade
direction is the safe one, a regex cannot be argued out of a downgrade by the
document it is reading, and a free auditable pass over 610 rows is worth having
before any paid one.

## What it will not do

It never marks anything substantiated. The column has three states and this
script only ever writes one of them:

    NULL   nobody has judged this row        <- every row today
    false  written here, and by the classifier
    true   only ever written by the model classifier or a person

So a row this script leaves alone is not thereby proof -- it is a row the
markers had nothing to say about. Absence of a marker is not evidence of
substantiation, which is the same rule that cost 44 customer questions when a
capped model reader's silence was read as "nothing there".

It also leaves alone anything a person has already judged. A human filing
decision outranks an inferred one, the same way `ingestion.register_version`
records a stated document type as `human:<user_id>` and no classifier overrides
it.

    python scripts/mark_unsubstantiated_evidence.py           # what would change
    python scripts/mark_unsubstantiated_evidence.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from collections import Counter
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

from sqlalchemy import select, update  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.services import audit, evidence_markers  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write the verdicts")
    parser.add_argument("--show", type=int, default=2, help="examples to print per rule")
    args = parser.parse_args()

    from app.models import Document, DocumentVersion, Evidence, Organization

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        # Columns, not entities. Loading an Evidence object pulls
        # `excerpt_vector` and pgvector's decoder raises on the value psycopg
        # hands it; nothing here needs the embedding.
        rows = list(
            session.execute(
                select(
                    Evidence.id,
                    Evidence.excerpt,
                    Evidence.is_substantiated,
                    Evidence.classified_by,
                    Document.original_filename,
                    Document.side,
                    Document.withdrawn_at,
                )
                .join(
                    DocumentVersion,
                    (DocumentVersion.id == Evidence.source_document_version_id)
                    & (DocumentVersion.org_id == Evidence.org_id),
                )
                .join(
                    Document,
                    (Document.id == DocumentVersion.document_id)
                    & (Document.org_id == DocumentVersion.org_id),
                )
                .where(Evidence.org_id == org.id)
            ).all()
        )
        print(f"{len(rows)} evidence row(s) in the organization.\n")

        hits: list[tuple] = []
        by_rule: Counter = Counter()
        by_file: Counter = Counter()
        examples: dict[str, list[str]] = {}
        already_human = 0
        already_marked = 0

        # The first dry run reported 55 marked rows in a file holding 50, and 33
        # in a file holding 24. Not a bug in the matching -- this query has no
        # side or withdrawn filter, so it aggregates the duplicate document
        # records ("28 document records for 14 files") and tender-side evidence
        # under the same filenames. A count that cannot be reconciled against
        # the diagnostic beside it is the thing rule 5 is about, so the scope is
        # printed rather than left to be inferred.
        scope: Counter = Counter()
        for _id, _x, _j, _c, _f, side, withdrawn in rows:
            label = getattr(side, "value", side) or "?"
            if withdrawn is not None:
                label += " (withdrawn)"
            scope[label] += 1
        print("evidence by document scope:")
        for label, count in scope.most_common():
            print(f"  {count:>4}  {label}")
        print()

        for evidence_id, excerpt, judged, classified_by, filename, side, withdrawn in rows:
            if (classified_by or "").startswith("human:"):
                # A person's judgment is not re-decided by a rule.
                already_human += 1
                continue
            found = evidence_markers.find(excerpt)
            if not found:
                continue
            if judged is False:
                already_marked += 1
                continue
            reason = evidence_markers.reason_for(excerpt)
            hits.append((evidence_id, reason))
            suffix = "" if withdrawn is None else "  (withdrawn doc)"
            by_file[f"{getattr(side, 'value', side)}  {filename}{suffix}"] += 1
            for name, _text in found:
                by_rule[name] += 1
                examples.setdefault(name, [])
                if len(examples[name]) < args.show:
                    examples[name].append(" ".join((excerpt or "").split())[:220])

        if already_human:
            print(f"{already_human} row(s) already carry a human judgment and were skipped.")
        if already_marked:
            print(f"{already_marked} row(s) are already marked unsubstantiated.")

        print(f"\n{len(hits)} row(s) would be marked NOT substantiated.\n")
        for name, count in by_rule.most_common():
            print("=" * 96)
            print(f"  {name}   {count} row(s)")
            for sample in examples.get(name, []):
                print(
                    textwrap.fill(
                        sample, width=92, initial_indent="      ", subsequent_indent="      "
                    )
                )
        print("=" * 96)

        print("\nby document:")
        for filename, count in by_file.most_common():
            print(f"  {count:>4}  {filename}")

        untouched = len(rows) - len(hits) - already_human - already_marked
        print(
            f"\n{untouched} row(s) carry no marker. They stay NULL -- unjudged, not proof."
            "\nTyping them, and catching the placeholders that never say so, is the model"
            "\ncall's job; this pass is the free floor underneath it."
        )

        if not args.apply:
            print(
                "\nNothing was changed.\n"
                "    python scripts/mark_unsubstantiated_evidence.py --apply"
            )
            return 0
        if not hits:
            print("\nNothing to mark.")
            return 0

        for evidence_id, reason in hits:
            session.execute(
                update(Evidence)
                .where(Evidence.org_id == org.id, Evidence.id == evidence_id)
                .values(
                    is_substantiated=False,
                    substantiation_note=reason,
                    classified_by=evidence_markers.MARKER_VERSION,
                )
            )
        session.flush()

        audit.record(
            session,
            ctx,
            action="evidence.marked_unsubstantiated",
            entity_type="organization",
            entity_id=org.id,
            after={
                "marked": len(hits),
                "by_rule": dict(by_rule),
                "marker_version": evidence_markers.MARKER_VERSION,
            },
            reason=(
                "The excerpt states in its own text that the evidence is missing, "
                "unfilled, synthetic or awaiting approval."
            ),
        )
        session.commit()
        print(
            f"\nMarked {len(hits)} row(s) as not substantiated, stamped "
            f"{evidence_markers.MARKER_VERSION}.\nNothing was deleted and no row was marked "
            "substantiated.\n\nTwo consumers change behaviour from here: a shortlist holding "
            "one of these rows\nis now read by the model instead of trusted on score, and the "
            "drafter's context\nlabels the row with what is missing."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
