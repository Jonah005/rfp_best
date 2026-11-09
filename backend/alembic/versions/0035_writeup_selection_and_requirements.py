"""Which diagrams the write-up covers, and which requirements a sentence answers.

A diagram can be left out of the write-up (it stays on the task, it just gets no section),
and a sentence in the response can say which tender requirements it answers, so the draft
can be checked requirement by requirement. Idempotent, like the revisions before it.
"""

from alembic import op

revision = "0035_writeup_selection"
down_revision = "0034_statement_paragraphs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE diagram ADD COLUMN IF NOT EXISTS left_out boolean NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE artifact_statement ADD COLUMN IF NOT EXISTS requirement_codes jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE artifact_statement DROP COLUMN IF EXISTS requirement_codes")
    op.execute("ALTER TABLE diagram DROP COLUMN IF EXISTS left_out")
