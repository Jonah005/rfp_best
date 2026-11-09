"""Evidence added from inside a task.

The walkthrough has a contributor discover, mid-task, that the approved
methodology does not cover mobile testing -- and upload the one that does.
Before this, the only way to add company material was the knowledge screen,
with no record of which task needed it, so the task could not say what it was
waiting for and its draft could not prefer the document uploaded for it.

The upload itself is ordinary company knowledge and waits for a knowledge
administrator. This table only records where it came from.

Revision ID: 0025_task_evidence
"""

from __future__ import annotations

from alembic import op
from app.db.base import Base

revision = "0025_task_evidence"
down_revision = "0024_requirement_confirmation"
branch_labels = None
depends_on = None

APP_ROLE = "bidready_app"
TABLE = "task_evidence"


def upgrade() -> None:
    import app.models  # noqa: F401 - registers the table on the metadata

    Base.metadata.create_all(bind=op.get_bind(), tables=[Base.metadata.tables[TABLE]])
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
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE};
          END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
