"""Which version of the gap check produced a task's saved answer.

Revision ID: 0047_gap_check_version
"""

from __future__ import annotations

from alembic import op

revision = "0047_gap_check_version"
down_revision = "0046_gap_part_and_duplicates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE task ADD COLUMN IF NOT EXISTS gaps_check_version varchar(16)")


def downgrade() -> None:
    op.execute("ALTER TABLE task DROP COLUMN IF EXISTS gaps_check_version")
