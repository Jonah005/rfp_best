"""Admit CUSTOMER_OBLIGATION as a bid_readiness label.

A clause that binds the customer ("Jadwa controls the negotiation", "the
Authority may waive minor non-conformity") was reported as
NEEDS_COMPANY_CONFIRMATION or NEEDS_CLIENT_CLARIFICATION and generated a company
task, though the bidder has nothing to confirm, evidence or ask. It is neither
work nor a scope exclusion (NOT_APPLICABLE means a condition does not apply), so
it gets its own label.

ADD VALUE only declares the label; this revision writes no rows with it.
"""

from alembic import op

revision = "0029_customer_obligation"
down_revision = "0028_clar_resolution_note"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE bid_readiness ADD VALUE IF NOT EXISTS 'CUSTOMER_OBLIGATION'")


def downgrade() -> None:
    # PostgreSQL cannot drop an enum label; an unused label is harmless.
    pass
