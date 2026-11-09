"""A write-up kept as it was when someone finalized it.


Revision ID: 0032_writeup_finalization
"""

from __future__ import annotations

from alembic import op
from app.db.base import Base

revision = "0032_writeup_finalization"
down_revision = "0031_statement_sections"
branch_labels = None
depends_on = None

APP_ROLE = "bidready_app"
TABLES = ["writeup_finalization"]


def upgrade() -> None:
    import app.models  # noqa: F401 - registers the tables on the metadata

    Base.metadata.create_all(bind=op.get_bind(), tables=[Base.metadata.tables[t] for t in TABLES])
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM pg_policies
                 WHERE tablename = '{table}' AND policyname = 'tenant_isolation'
              ) THEN
                CREATE POLICY tenant_isolation ON {table}
                  USING      (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
                  WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid);
              END IF;
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE};
              END IF;
            END $$
            """
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
