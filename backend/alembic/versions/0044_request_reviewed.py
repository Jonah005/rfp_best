"""When the person who asked has dealt with a department's reply.

Revision ID: 0044_request_reviewed
"""

from __future__ import annotations

from alembic import op

revision = "0044_request_reviewed"
down_revision = "0043_requirement_position"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE cross_department_request "
        "ADD COLUMN IF NOT EXISTS reviewed_at timestamp with time zone"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE cross_department_request DROP COLUMN IF EXISTS reviewed_at")
