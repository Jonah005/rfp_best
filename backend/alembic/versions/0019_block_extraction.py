"""Record that a prompt version read a block, whatever it found there.

Extraction inferred this from the requirements: a block counted as already
decomposed if some requirement pointed at it carrying the current
`extracted_by`. The inference is silently wrong for a block that correctly
yields nothing.

A live tender's executive summary demonstrated both consequences. Prompt v6
applied its new obligation test to that block and rightly raised no
requirements -- "Jadwa is looking forward for an online portal" is background,
not a duty. Because nothing was stored, nothing recorded that v6 had read it:

  * the rerun guard found no v6 requirement on the block and re-decomposed it on
    the next run, and on every run after that, spending a model call each time
    to reach the same correct answer;
  * the sweep that retires superseded extractions could not distinguish "v6 read
    this and found nothing" from "v6 never read this". It is deliberately
    conservative about the second, so it left four v5 rows of prose in the
    matrix that v6 had already judged not to be requirements.

`block_extraction` records the reading itself. `found` carries the yield, so
zero is a real answer rather than an absence.

The table is scoped by bid as well as document version: two bids may share a
document, and one bid's extraction state must not satisfy the other's guard.

Empty on creation, and that is correct rather than a gap to backfill. Existing
requirements already carry their own `extracted_by`, so the first run under any
prompt version re-reads its blocks once and records what it did -- which is what
a run under a version with no recorded history should do.

Revision ID: 0019_block_extraction
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.db.base import Base

revision = "0019_block_extraction"
down_revision = "0018_qualified_clause_ref"
branch_labels = None
depends_on = None

APP_ROLE = "bidready_app"
TABLE = "block_extraction"


def upgrade() -> None:
    # Idempotent, for the reason 0015 and 0017 give: databases in this project
    # have been repaired by hand, and a migration that fails against an
    # already-correct schema is one people learn to skip.
    bind = op.get_bind()
    exists = bind.execute(
        sa.text("SELECT to_regclass(:name)"), {"name": f"public.{TABLE}"}
    ).scalar()
    if exists is None:
        Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables[TABLE]])

    # RLS is FORCEd on every tenant table here, including for the table owner.
    # A new table without it would be readable across organizations by the
    # application role -- the one guarantee this product cannot lose.
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE tablename = '{TABLE}' AND policyname = 'tenant_isolation'
          ) THEN
            CREATE POLICY tenant_isolation ON {TABLE}
              USING      (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
              WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid);
          END IF;
        END $$
        """
    )
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE};
          END IF;
        END $$
        """
    )


def downgrade() -> None:
    # Dropping this loses only the record of which prompt read which block. The
    # guard then falls back to inferring it from requirements, which is where
    # this started.
    op.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
