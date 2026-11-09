"""Tell people the thing that is waiting on them.

Every moment worth knowing about was already recorded -- sixty-odd audit
actions, a request lifecycle with six states, assignment, approval, release.
None of it reached a person. A bid manager learned her cross-department request
had been answered by happening to ask the copilot a second question; a
contributor learned work had been assigned to her by opening the task list on
a hunch. A governed request nobody is told about is only better than a chat
message if somebody is looking, and nobody was.

This adds the smallest table that fixes it: a recipient, what happened, where
to go, and whether they have seen it.

It is separate from `audit_event` on purpose. That table is append-only and the
application role holds no UPDATE or DELETE on it, which is exactly right for an
auditor's record and exactly wrong for something a person marks as read. Nor
does audit record who should care -- it records what occurred. Routing is a
product decision that changes as the product changes, and it does not belong in
the evidentiary record.

Revision ID: 0016_notification
"""

from __future__ import annotations

from alembic import op
from app.db.base import Base

revision = "0016_notification"
down_revision = "0015_department_head"
branch_labels = None
depends_on = None

APP_ROLE = "bidready_app"
TABLE = "notification"


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=[Base.metadata.tables[TABLE]])

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    # Tenant isolation only, matching every other table. Restricting a row to
    # its recipient is done in the service, where the actor is known: the
    # database session carries the organization, not the person, so a policy
    # here could not express "mine" without a second setting to keep in step.
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {TABLE}
          USING      (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
          WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
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
    op.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
