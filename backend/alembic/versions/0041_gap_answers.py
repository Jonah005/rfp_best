"""How a gap's assumption came about, whether the response rests on it, and
the request raised from a gap.

Revision ID: 0041_gap_answers
"""

from __future__ import annotations

from alembic import op

revision = "0041_gap_answers"
down_revision = "0040_task_gap"
branch_labels = None
depends_on = None


def _fk(table: str, column: str, ref: str, name: str, on_delete: str = "") -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN
            ALTER TABLE {table}
              ADD CONSTRAINT {name}
              FOREIGN KEY ({column}, org_id) REFERENCES {ref} (id, org_id) {on_delete};
          END IF;
        END $$
        """
    )


def upgrade() -> None:
    op.execute("ALTER TABLE clarification_issue ADD COLUMN IF NOT EXISTS answer_source varchar(16)")
    op.execute("ALTER TABLE clarification_issue ADD COLUMN IF NOT EXISTS answered_by_user_id uuid")
    op.execute(
        "ALTER TABLE clarification_issue "
        "ADD COLUMN IF NOT EXISTS include_in_response boolean NOT NULL DEFAULT true"
    )
    _fk(
        "clarification_issue",
        "answered_by_user_id",
        "app_user",
        "fk_clarification_answered_by_org",
    )
    op.execute(
        "ALTER TABLE cross_department_request ADD COLUMN IF NOT EXISTS clarification_id uuid"
    )
    _fk(
        "cross_department_request",
        "clarification_id",
        "clarification_issue",
        "fk_request_clarification_org",
    )


def downgrade() -> None:
    op.execute("ALTER TABLE cross_department_request DROP COLUMN IF EXISTS clarification_id")
    op.execute("ALTER TABLE clarification_issue DROP COLUMN IF EXISTS include_in_response")
    op.execute("ALTER TABLE clarification_issue DROP COLUMN IF EXISTS answered_by_user_id")
    op.execute("ALTER TABLE clarification_issue DROP COLUMN IF EXISTS answer_source")
