"""What each department answers for.

Deciding who to ask for a missing piece of information needs more than a
department's name: "Legal" says nothing about whether licence terms live there.
An administrator writes one line per department; the routing call reads it.

Revision ID: 0026_department_responsibilities
"""

from __future__ import annotations

from alembic import op

revision = "0026_department_responsibilities"
down_revision = "0025_task_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent like 0015: revision 0004 builds tables from live metadata, so a
    # database created from empty already has this column.
    op.execute("ALTER TABLE department ADD COLUMN IF NOT EXISTS responsibilities text")


def downgrade() -> None:
    op.execute("ALTER TABLE department DROP COLUMN IF EXISTS responsibilities")
