"""Record that one deliverable restates another, instead of retiring it.

`coalesce_deliverables.py` folded 18 of 32 rows by setting them
NOT_APPLICABLE. A review of the resulting export called that out, correctly, on
three counts:

  * 15 of the folded rows are still marked mandatory, so the sheet says
    "mandatory" and "not applicable" about the same row.
  * A duplicate is not "not applicable". The tender DOES require that
    submission; this row is simply a less complete way of naming it. Filing it
    under the status that means "we do not have to do this" is false about the
    obligation.
  * Nothing records WHICH deliverable a folded row was folded into, so the
    merge cannot be reviewed, reversed, or explained to the person assembling
    the pack.

The pattern to follow already exists in this schema, twice.
`ClarificationIssue.restates_clarification_id` (0020) and
`Requirement.superseded_by_requirement_id` do exactly this: the row keeps its
status and its history, points at its canonical twin, and carries the verdict's
provenance. Deliverables were given a status flag instead, which is the same
inconsistency as the clarification links that had to be repaired twice -- a
pattern applied in some places and not the one in front of you.

So: the same three columns, the same self-referential tenant-scoped FK, the same
`equivalence_by` stamp carrying `llm:<name>@<version>`.

Nothing is deleted and a fold is reversed by clearing three columns. Status
returns to what it was; `coalesce_deliverables.py --apply` now restores any row
it previously retired without a link.

Revision ID: 0022_deliverable_equivalence
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0022_deliverable_equivalence"
down_revision = "0021_evidence_substantiation"
branch_labels = None
depends_on = None

TABLE = "deliverable"


def _columns(bind) -> set[str]:
    return {
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
            ),
            {"t": TABLE},
        )
    }


def upgrade() -> None:
    # Idempotent, for the reason 0015, 0017, 0019, 0020 and 0021 give.
    bind = op.get_bind()
    existing = _columns(bind)

    if "duplicate_of_deliverable_id" not in existing:
        # `sa.dialects.postgresql` does not resolve from `import sqlalchemy as
        # sa` -- the submodule is not imported by the package -- so the dialect
        # type is imported by name above. 0020 learned this by crashing.
        op.add_column(
            TABLE,
            sa.Column("duplicate_of_deliverable_id", postgresql.UUID(as_uuid=True)),
        )
    if "equivalence_by" not in existing:
        op.add_column(TABLE, sa.Column("equivalence_by", sa.String(128)))
    if "equivalence_reason" not in existing:
        op.add_column(TABLE, sa.Column("equivalence_reason", sa.Text()))

    # Tenant-scoped self-reference: the composite target is (id, org_id), which
    # uq_deliverable_id_org already provides, so a duplicate can never point at
    # another organization's deliverable.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'fk_deliverable_duplicate_of_org'
          ) THEN
            ALTER TABLE deliverable
              ADD CONSTRAINT fk_deliverable_duplicate_of_org
              FOREIGN KEY (duplicate_of_deliverable_id, org_id)
              REFERENCES deliverable (id, org_id);
          END IF;
        END $$
        """
    )
    # The submission pack asks "what are the canonical deliverables" (the link
    # is null) and "what was folded into this one" on every presentation.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_deliverable_duplicate_of "
        "ON deliverable (org_id, bid_id, duplicate_of_deliverable_id)"
    )


def downgrade() -> None:
    # Dropping these loses the verdicts, not the deliverables: every folded row
    # keeps its name, its status and its requirement links, and re-running the
    # coalesce script restores the grouping.
    op.execute("DROP INDEX IF EXISTS ix_deliverable_duplicate_of")
    op.execute(
        "ALTER TABLE deliverable DROP CONSTRAINT IF EXISTS fk_deliverable_duplicate_of_org"
    )
    bind = op.get_bind()
    existing = _columns(bind)
    for column in ("equivalence_reason", "equivalence_by", "duplicate_of_deliverable_id"):
        if column in existing:
            op.drop_column(TABLE, column)
