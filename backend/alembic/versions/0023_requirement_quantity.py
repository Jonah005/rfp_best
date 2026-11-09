"""How many the tender asks for, recorded next to the wording that asks.

R-006 on the fixture bid reads:

    The supplier must provide three comparable projects delivered in the UAE or
    wider GCC region within the last three years.

The company pack holds one. The row read DRAFTABLE, with the rationale "Some
current evidence exists; the gaps must be filled during drafting." Every part of
that is individually correct and the conclusion is false: two of the three
projects do not exist, and no amount of drafting will produce them. A bid would
have gone out claiming three references and shown one.

## Why this is a column and not a prompt fix

The obvious repair was to tell `evidence_relevance` that one project is not
partial proof of three. That was tried, and it made the verdicts flap: the
instruction fights the meaning of its own label, because the one reference IS
partial proof, exactly as labelled. The model was being asked to encode an
arithmetic result in a word that means something else, and v3 now says so
explicitly -- "Quantity is NOT your question... Counting is done elsewhere."

This is elsewhere. Counting is arithmetic over rows, which is code's job for the
same reason permissions and approvals are: a model asked to count will guess, and
the guess is invisible.

## Three columns, not one

`required_count` alone would be a number with no way to check it. So:

  * `required_count` -- how many the clause demands.
  * `counted_noun` -- what is being counted ("comparable projects"), which is
    what a shortfall message has to name to be actionable, and what makes a
    wrong reading visible to a reviewer rather than silently arithmetic.
  * `quantity_by` -- `llm:<name>@<version>`, the same provenance stamp every
    other model verdict in this schema carries, so a bad reading can be found
    and re-run by prompt version.

They live on `requirement_version`, not on `requirement`, because the number is
a property of the WORDING. An amendment changing "three" to "two" is a new
version of the same obligation, and the old count stays with the old text --
which is precisely the lifecycle `version_no` and `superseded_at` exist for.

Nullable throughout, and null means "not judged", never "none required". The
distinction matters: a requirement nobody has read for quantity must not be
treated as one the tender asks for zero of.

Revision ID: 0023_requirement_quantity
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0023_requirement_quantity"
down_revision = "0022_deliverable_equivalence"
branch_labels = None
depends_on = None

TABLE = "requirement_version"


def _columns(bind) -> set[str]:
    return {
        row[0]
        for row in bind.execute(
            sa.text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
            {"t": TABLE},
        )
    }


def upgrade() -> None:
    # Idempotent, for the reason 0015, 0017, 0019, 0020, 0021 and 0022 give.
    bind = op.get_bind()
    existing = _columns(bind)

    if "required_count" not in existing:
        op.add_column(TABLE, sa.Column("required_count", sa.Integer()))
    if "counted_noun" not in existing:
        op.add_column(TABLE, sa.Column("counted_noun", sa.String(200)))
    if "quantity_by" not in existing:
        op.add_column(TABLE, sa.Column("quantity_by", sa.String(128)))

    # A count of one is not a quantity requirement, it is the ordinary case, and
    # storing it would make every row look like a counting obligation. Two or
    # more, or nothing at all.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_requirement_version_count'
          ) THEN
            ALTER TABLE requirement_version
              ADD CONSTRAINT ck_requirement_version_count
              CHECK (required_count IS NULL OR required_count >= 2);
          END IF;
        END $$
        """
    )

    # The assessment asks "which current versions state a count" once per bid.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_requirement_version_required_count "
        "ON requirement_version (org_id, requirement_id) "
        "WHERE required_count IS NOT NULL"
    )


def downgrade() -> None:
    # Dropping these loses the readings, not the requirements. Every row keeps
    # its text, its version history and its assessments; re-running the
    # quantity review restores the counts.
    op.execute("DROP INDEX IF EXISTS ix_requirement_version_required_count")
    op.execute(
        "ALTER TABLE requirement_version DROP CONSTRAINT IF EXISTS ck_requirement_version_count"
    )
    bind = op.get_bind()
    existing = _columns(bind)
    for column in ("quantity_by", "counted_noun", "required_count"):
        if column in existing:
            op.drop_column(TABLE, column)
