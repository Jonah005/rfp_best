"""Which piece of a requirement a gap is about, and gaps that repeat another.

Revision ID: 0046_gap_part_and_duplicates
"""

from __future__ import annotations

from alembic import op

revision = "0046_gap_part_and_duplicates"
down_revision = "0045_draft_hold"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE task_gap ADD COLUMN IF NOT EXISTS part varchar(120)")
    op.execute("ALTER TABLE task_gap ADD COLUMN IF NOT EXISTS duplicate_of uuid")
    op.execute(
        "ALTER TABLE task_gap ADD COLUMN IF NOT EXISTS keep_separate boolean NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE task_gap DROP COLUMN IF EXISTS keep_separate")
    op.execute("ALTER TABLE task_gap DROP COLUMN IF EXISTS duplicate_of")
    op.execute("ALTER TABLE task_gap DROP COLUMN IF EXISTS part")
