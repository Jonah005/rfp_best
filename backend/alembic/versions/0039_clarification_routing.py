"""How each clarification's department was chosen, and why.

Revision ID: 0039_clarification_routing
"""

from __future__ import annotations

from alembic import op

revision = "0039_clarification_routing"
down_revision = "0038_category_route"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE clarification_issue ADD COLUMN IF NOT EXISTS routing_source varchar(16)"
    )
    op.execute("ALTER TABLE clarification_issue ADD COLUMN IF NOT EXISTS routing_reason text")


def downgrade() -> None:
    op.execute("ALTER TABLE clarification_issue DROP COLUMN IF EXISTS routing_reason")
    op.execute("ALTER TABLE clarification_issue DROP COLUMN IF EXISTS routing_source")
