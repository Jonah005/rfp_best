"""Make evidence matching an indexed query instead of a scan.

Finding candidate evidence for one requirement used to load every approved
evidence row in the tenant into application memory and score it there. That is
O(requirements x corpus) per bid: fine on a demo, hopeless on a real corpus.

This revision gives `evidence` a generated tsvector over its excerpt and the
indexes the candidate query actually filters on, so selection happens in the
database against a bounded, ranked result set.

**Why these statements are idempotent.** Revision 0003 builds its tables from
live SQLAlchemy metadata, so the moment the model gained `excerpt_tsv` and its
indexes, 0003 started creating them too. A database built from empty therefore
already has them by the time this revision runs, while one migrated before the
model changed does not. Both must work. The general rule for this repository:
any model change to a table an earlier metadata-driven revision created must be
applied idempotently, or that revision must be frozen to explicit DDL first.

Revision ID: 0009_scale_evidence_search
"""

from __future__ import annotations

from alembic import op

revision = "0009_scale_evidence_search"
down_revision = "0008_assembly_release_learning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE evidence
          ADD COLUMN IF NOT EXISTS excerpt_tsv tsvector
          GENERATED ALWAYS AS (to_tsvector('simple', excerpt)) STORED
        """
    )
    # CONCURRENTLY would avoid the write lock, but it cannot run inside the
    # transaction Alembic wraps a revision in. At this table's size the plain
    # build is short; revisit if evidence grows before this ships.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_evidence_tsv ON evidence USING gin (excerpt_tsv)"
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_evidence_reusable
          ON evidence (org_id, approval_status, status)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_evidence_reusable")
    op.execute("DROP INDEX IF EXISTS ix_evidence_tsv")
    op.execute("ALTER TABLE evidence DROP COLUMN IF EXISTS excerpt_tsv")
