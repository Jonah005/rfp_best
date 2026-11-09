"""Stop paying twice for the same question.

Requirement assessment asks the model to read one shortlist per requirement, so
a bid of sixty requirements is sixty calls. That number is the honest price of
having a reader instead of a threshold — but it is charged again on every re-run,
and re-running is the normal way the product is used: approve three more
documents, assess again, watch the matrix change.

For most requirements nothing changed between those runs. Same requirement text,
same shortlist, same excerpts, temperature zero — the same answer, bought twice.
This revision adds a small tenant-scoped table so it is bought once.

**The key is the whole prompt.** `input_sha256` covers the rendered request, so
any change to the requirement, the shortlist or an excerpt is a different key
and gets a fresh call. There is no partial or fuzzy matching here on purpose: a
cache that answers a question slightly different from the one asked is worse
than no cache, because the wrongness is invisible.

**Prompt version is a separate column, not folded into the hash.** It could have
been part of the hashed input, but keeping it explicit means a prompt edit can
be seen — and cleaned up — rather than merely producing a silent flood of
misses.

Revision ID: 0012_model_result_cache
"""

from __future__ import annotations

from alembic import op
from app.db.base import Base

revision = "0012_model_result_cache"
down_revision = "0011_evidence_semantic_matching"
branch_labels = None
depends_on = None

APP_ROLE = "bidready_app"
TABLE = "model_result_cache"


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=[Base.metadata.tables[TABLE]])

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {TABLE}
          USING      (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
          WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
        """
    )
    # A cached reply is derived from one tenant's tender and evidence text, so
    # it is exactly as sensitive as the evidence itself and gets the same
    # isolation as every other tenant table.
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
    op.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
