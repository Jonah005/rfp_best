"""Clarifications belong to a department and have an owner.

Revision ID: 0037_clarification_owner
"""

from __future__ import annotations

from alembic import op

revision = "0037_clarification_owner"
down_revision = "0036_draft_images"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE clarification_issue ADD COLUMN IF NOT EXISTS department_id uuid")
    op.execute("ALTER TABLE clarification_issue ADD COLUMN IF NOT EXISTS owner_user_id uuid")
    for column, table, name in (
        ("department_id", "department", "fk_clarification_department_org"),
        ("owner_user_id", "app_user", "fk_clarification_owner_org"),
    ):
        op.execute(
            f"""
            DO $$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN
                ALTER TABLE clarification_issue
                  ADD CONSTRAINT {name}
                  FOREIGN KEY ({column}, org_id) REFERENCES {table} (id, org_id);
              END IF;
            END $$
            """
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_clarification_owner "
        "ON clarification_issue (org_id, owner_user_id, status)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_clarification_owner")
    for name in ("fk_clarification_department_org", "fk_clarification_owner_org"):
        op.execute(f"ALTER TABLE clarification_issue DROP CONSTRAINT IF EXISTS {name}")
    op.execute("ALTER TABLE clarification_issue DROP COLUMN IF EXISTS owner_user_id")
    op.execute("ALTER TABLE clarification_issue DROP COLUMN IF EXISTS department_id")
