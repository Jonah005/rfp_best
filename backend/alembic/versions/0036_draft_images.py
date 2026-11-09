"""Pictures in a response draft, and the layout of its diagrams.

Revision ID: 0036_draft_images
"""

from __future__ import annotations

from alembic import op
from app.db.base import Base

revision = "0036_draft_images"
down_revision = "0035_writeup_selection"
branch_labels = None
depends_on = None

APP_ROLE = "bidready_app"
TABLES = ["draft_image"]


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
