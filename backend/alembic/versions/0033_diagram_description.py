"""A statement class for sentences that describe a diagram.

ADD VALUE only declares the label; this revision writes no rows with it. Idempotent.
"""

from alembic import op

revision = "0033_diagram_description"
down_revision = "0032_writeup_finalization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE statement_class ADD VALUE IF NOT EXISTS 'DIAGRAM_DESCRIPTION'")


def downgrade() -> None:
    # PostgreSQL cannot drop an enum label; an unused label is harmless.
    pass
