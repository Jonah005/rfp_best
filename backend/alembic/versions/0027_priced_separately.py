"""Requirements the tender asks to be quoted separately.

The live tender heads two clauses "Signature System (Qoute Separate)" and
"Document Management System (Qoute Separate)". Both reached the matrix with the
qualifier gone, which is how a separately-quoted system ends up inside a fixed
price. The marker is now read from the clause's own words at extraction.

Revision ID: 0027_priced_separately
"""

from __future__ import annotations

from alembic import op

revision = "0027_priced_separately"
down_revision = "0026_department_responsibilities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE requirement ADD COLUMN IF NOT EXISTS priced_separately "
        "boolean NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE requirement DROP COLUMN IF EXISTS priced_separately")
