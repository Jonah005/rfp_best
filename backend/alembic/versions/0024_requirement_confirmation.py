"""Who accepted that this is what the customer asked for.

`RequirementLifecycle.CONFIRMED` was written by one function and read by
nothing. Every query in the codebase filters on `!= WITHDRAWN`, so a requirement
sat in DRAFT_EXTRACTED forever and it changed nothing: tasks were generated from
it, a draft was written against it, and an artifact was approved citing it.

That is the one hole in the governance chain. Evidence needs a knowledge
administrator. An artifact version needs a reviewer. A proposed commitment needs
a named role. But the MODEL'S READING OF WHAT THE CUSTOMER ASKED FOR needed
nobody -- and every one of those other approvals is an approval of an answer to
a question that was never itself checked. If `requirement_decomposition`
misreads a clause, the whole governed chain runs faithfully on top of the
mistake.

`requirement_scope.py` opens by noting that `lifecycle_state` once had a
WITHDRAWN value nothing read, and that it was cleaned up. CONFIRMED is the
leftover of the same problem.

## Two columns, because a count is not an approval

`confirm_requirements` promoted every DRAFT_EXTRACTED row on a bid in one call
and recorded an audit row saying how many. Nobody could answer "who accepted
R-042, and when" -- which is the only question an approval exists to answer.

  * `confirmed_by` -- the person. NULL while unconfirmed.
  * `confirmed_at` -- when.

Both nullable, and NULL means "not confirmed", which is the state every existing
requirement is in. Nothing is backfilled: pretending 89 rows were reviewed
because a migration ran is exactly the rubber stamp this is meant to remove.

A partial index because the question asked on every artifact approval is "are
any of THESE requirements unconfirmed", which reads only the not-yet-confirmed
side.

The index is on `lifecycle_state`, not on `confirmed_at`, and the difference
matters. The LIFECYCLE is the fact; the two columns are attribution beside it.
An earlier draft of the gate checked `confirmed_at IS NULL` and immediately
disagreed with itself -- rows confirmed before these columns existed read
CONFIRMED with a NULL timestamp and were refused as unconfirmed. Two sources of
truth for one fact, which is the same error as collapsing two facts into one
column. The index was written for that query and left behind when the query
changed; an index nothing uses is the same kind of dead declaration as a rule
nothing fires, which is what this migration exists to remove.

Revision ID: 0024_requirement_confirmation
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0024_requirement_confirmation"
down_revision = "0023_requirement_quantity"
branch_labels = None
depends_on = None

TABLE = "requirement"


def _columns(bind) -> set[str]:
    return {
        row[0]
        for row in bind.execute(
            sa.text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
            {"t": TABLE},
        )
    }


def upgrade() -> None:
    # Idempotent, for the reason 0015 onwards give.
    bind = op.get_bind()
    existing = _columns(bind)

    if "confirmed_by" not in existing:
        # `sa.dialects.postgresql` does not resolve from `import sqlalchemy as
        # sa`; 0020 learned this by crashing.
        op.add_column(TABLE, sa.Column("confirmed_by", postgresql.UUID(as_uuid=True)))
    if "confirmed_at" not in existing:
        op.add_column(TABLE, sa.Column("confirmed_at", sa.DateTime(timezone=True)))

    # Tenant-scoped: a confirmation can never point at another organization's
    # user, which is the same shape every other actor reference in this schema
    # uses.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'fk_requirement_confirmed_by_org'
          ) THEN
            ALTER TABLE requirement
              ADD CONSTRAINT fk_requirement_confirmed_by_org
              FOREIGN KEY (confirmed_by, org_id)
              REFERENCES app_user (id, org_id);
          END IF;
        END $$
        """
    )

    # "Which of this bid's requirements has nobody accepted yet" -- asked on
    # every artifact approval, and only ever over the unconfirmed side.
    op.execute("DROP INDEX IF EXISTS ix_requirement_unconfirmed")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_requirement_unconfirmed "
        "ON requirement (org_id, bid_id) WHERE lifecycle_state <> 'CONFIRMED'"
    )


def downgrade() -> None:
    # Dropping these loses the attribution, not the requirements. Every row
    # keeps its lifecycle_state, so a bid that was confirmed still reads
    # CONFIRMED; only "who and when" goes.
    op.execute("DROP INDEX IF EXISTS ix_requirement_unconfirmed")
    op.execute("ALTER TABLE requirement DROP CONSTRAINT IF EXISTS fk_requirement_confirmed_by_org")
    bind = op.get_bind()
    existing = _columns(bind)
    for column in ("confirmed_at", "confirmed_by"):
        if column in existing:
            op.drop_column(TABLE, column)
