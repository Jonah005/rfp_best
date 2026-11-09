"""Why a clarification finding was closed.

The live bid closed 47 coverage gaps and recorded nothing against any of them:
their text still said "must be reviewed manually before drafting" while their
status said closed. A closure with no reason is indistinguishable from a
question quietly dropped.

Revision ID: 0028_clar_resolution_note
"""

from __future__ import annotations

from alembic import op

revision = "0028_clar_resolution_note"
down_revision = "0027_priced_separately"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE clarification_issue ADD COLUMN IF NOT EXISTS resolution_note text")


def downgrade() -> None:
    op.execute("ALTER TABLE clarification_issue DROP COLUMN IF EXISTS resolution_note")
