"""Paragraphs in a response draft.

A write-up reads as paragraphs, but every sentence in it is still typed and stays its own
row. The paragraph number says which sentences belong to the same paragraph, so the
screen can set them as prose while the type of each sentence survives underneath.
Idempotent, like the revision before it.
"""

from alembic import op

revision = "0034_statement_paragraphs"
down_revision = "0033_diagram_description"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE artifact_statement ADD COLUMN IF NOT EXISTS paragraph_no integer")


def downgrade() -> None:
    op.execute("ALTER TABLE artifact_statement DROP COLUMN IF EXISTS paragraph_no")
