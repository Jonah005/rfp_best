"""Record that one clarification question restates another.

A finding is fingerprinted on its own description, so re-reading a clause in a
different batch window produces a fresh row saying the same thing in other
words. On the live tender that left 234 open ambiguity findings carrying 79
distinct question texts, and several of those 79 were still one question:

    What is the required scope, format and duration of the source code and
        development knowledge transfer?
    What are the required IP/source ownership terms and the expected format and
        duration of the knowledge transfer?
    What is the required scope, format and acceptance criteria for the source
        code and development knowledge transfer?

One reply from the customer answers all three. They arrive on five matrix rows
each, and a reviewer reading R-124 sees eleven questions where there are four.

Two cheaper fixes were tried against the data first and both fail:

  * Fingerprint on (issue_type, source block) instead of prose. Stable, free,
    and wrong: one paragraph of this tender carries two genuine UNDEFINED_FIELD
    questions -- what "final release" means, and how proposals are weighted --
    which that key cannot tell apart.
  * Close whatever the newest run did not repeat. Tried, measured, reverted: the
    prompt caps the model at twelve issues per batch, so no single run can
    enumerate a document of this size, and the rule closed 44 real questions
    including the exact submission deadline.

What is left is a judgement about meaning, which is a model's job here and a
recorded verdict in the schema, like every other detector: `equivalence_by`
carries llm:<name>@<version>, so a verdict is traceable to the prompt that made
it and re-judgeable when that prompt changes.

Columns rather than a join table: a restatement points at exactly one canonical
question, and the FK is self-referential and tenant-scoped so a row can never
point outside its own organization.

Nothing is deleted and no status changes. A restatement keeps its text and its
history and simply holds no requirement links, so it neither crowds a matrix row
nor gates one. Reversing a merge is clearing two columns.

Revision ID: 0020_clarification_equivalence
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0020_clarification_equivalence"
down_revision = "0019_block_extraction"
branch_labels = None
depends_on = None

TABLE = "clarification_issue"


def _columns(bind) -> set[str]:
    return {
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t"
            ),
            {"t": TABLE},
        )
    }


def upgrade() -> None:
    # Idempotent, for the reason 0015, 0017 and 0019 give: databases in this
    # project have been repaired by hand, and a migration that fails against an
    # already-correct schema is one people learn to skip.
    bind = op.get_bind()
    existing = _columns(bind)

    if "restates_clarification_id" not in existing:
        # `sa.dialects.postgresql` is not resolvable from `import sqlalchemy as
        # sa` alone -- the submodule is not imported by the package -- so the
        # dialect type is imported by name above.
        op.add_column(
            TABLE,
            sa.Column("restates_clarification_id", postgresql.UUID(as_uuid=True)),
        )
    if "equivalence_by" not in existing:
        op.add_column(TABLE, sa.Column("equivalence_by", sa.String(128)))
    if "equivalence_reason" not in existing:
        op.add_column(TABLE, sa.Column("equivalence_reason", sa.Text()))

    # Tenant-scoped self-reference: the composite target is (id, org_id), which
    # uq_clarification_issue_id_org already provides, so a restatement cannot
    # point at another organization's question.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'fk_clarification_restates_org'
          ) THEN
            ALTER TABLE clarification_issue
              ADD CONSTRAINT fk_clarification_restates_org
              FOREIGN KEY (restates_clarification_id, org_id)
              REFERENCES clarification_issue (id, org_id);
          END IF;
        END $$
        """
    )
    # The service asks "what restates this one" when presenting a group, and
    # "which open findings are restatements" on every link pass.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_clarification_restates "
        "ON clarification_issue (org_id, bid_id, restates_clarification_id)"
    )


def downgrade() -> None:
    # Dropping these loses the verdicts, not the questions: every restatement is
    # a full row and simply becomes visible again as its own finding.
    op.execute("DROP INDEX IF EXISTS ix_clarification_restates")
    op.execute(
        "ALTER TABLE clarification_issue "
        "DROP CONSTRAINT IF EXISTS fk_clarification_restates_org"
    )
    for column in ("equivalence_reason", "equivalence_by", "restates_clarification_id"):
        op.execute(f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS {column}")
