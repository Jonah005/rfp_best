"""Sections in a response draft.

A write-up that explains a diagram is a run of typed statements under one
heading, and it has to remember which diagram (and which version of it) it was
written from, so the screen can say when the picture has moved on. Plain
nullable columns: every existing statement simply has no section.

Idempotent, because earlier revisions build their tables from the current models
and a fresh database may already have the columns.
"""

from alembic import op

revision = "0031_statement_sections"
down_revision = "0030_diagrams"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE artifact_statement ADD COLUMN IF NOT EXISTS section varchar(200)")
    op.execute("ALTER TABLE artifact_statement ADD COLUMN IF NOT EXISTS diagram_id uuid")
    op.execute("ALTER TABLE artifact_statement ADD COLUMN IF NOT EXISTS diagram_version_no integer")


def downgrade() -> None:
    op.execute("ALTER TABLE artifact_statement DROP COLUMN IF EXISTS diagram_version_no")
    op.execute("ALTER TABLE artifact_statement DROP COLUMN IF EXISTS diagram_id")
    op.execute("ALTER TABLE artifact_statement DROP COLUMN IF EXISTS section")
