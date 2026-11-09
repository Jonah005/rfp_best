"""Record who a clause actually binds.

Nineteen rows of the first real bid reported BLOCKED — "a mandatory requirement
with no supporting evidence" — and only four of them were anything of the kind.
Eight were the customer reserving rights for itself:

    5.14  ADX may, at its discretion, waive any minor non-conformity.
    5.17  The ADX shall be under no obligation to accept the lowest offer.
    8.23  ADX will not be liable to pay any amounts other than the grand total.

No company document can evidence a clause like that, ever. Assessed as a bidder
obligation it sits at BLOCKED permanently, and the four requirements that
genuinely put the bid at risk — the performance bond, the service-interruption
liability, the System Document File, the list of other commitments — are one
fifth of a wall of red nobody can act on.

This revision adds the missing distinction. It changes no evidence, no
retrieval, and no evidence status: a requirement with nothing attached still
reports NONE. What changes is only what the empty rows are CALLED.

**BIDDER is the default, and the existing rows keep it.** The server default
means every requirement extracted before this column existed stays the bidder's
problem rather than being quietly excused, and the classifier only moves a
clause off BIDDER when the tender text plainly puts the obligation elsewhere.
The asymmetry is the point: labelling a customer right as the bidder's costs a
cluttered matrix, while labelling a bidder obligation as the customer's takes a
real no-bid risk off the list — so uncertainty resolves toward the bidder.

Revision ID: 0013_obligation_owner
"""

from __future__ import annotations

from alembic import op

revision = "0013_obligation_owner"
down_revision = "0012_model_result_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent for the same reason revision 0009 is: revision 0004 builds its
    # tables from live SQLAlchemy metadata, so a database created from empty
    # already has this column by the time this revision runs, while one migrated
    # before the model changed does not. Both must work.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'obligation_owner') THEN
            CREATE TYPE obligation_owner AS ENUM ('BIDDER', 'CUSTOMER', 'MUTUAL');
          END IF;
        END $$
        """
    )
    op.execute(
        """
        ALTER TABLE requirement
          ADD COLUMN IF NOT EXISTS obligation_owner obligation_owner
          NOT NULL DEFAULT 'BIDDER'
        """
    )
    # The matrices filter on it alongside the category they already filter on.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_requirement_obligation
          ON requirement (org_id, bid_id, obligation_owner)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_requirement_obligation")
    op.execute("ALTER TABLE requirement DROP COLUMN IF EXISTS obligation_owner")
    op.execute("DROP TYPE IF EXISTS obligation_owner")
