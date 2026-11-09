"""Phase I/J/K: tasks, conversations and cross-department requests.

Phase E created a deliberately minimal `task` and `task_participant` so that
`access_grant` and `task_participant` had a real parent to point at. Their table
names were already the final ones, so this migration extends them in place
rather than dropping and recreating -- an existing grant keeps its parent row
and its foreign key throughout.

`access_grant.task_id` could not carry a foreign key in Phase E because a task
was barely a table yet. It gets its composite tenant key here, which is what
makes "a grant scoped to a task in another organization" unrepresentable rather
than merely unlikely.

Revision ID: 0006_tasks_collaboration
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.models import Base
from app.models.enums import TaskPriority, TaskState

revision = "0006_tasks_collaboration"
down_revision = "0005_assessment_readiness"
branch_labels = None
depends_on = None

APP_ROLE = "bidready_app"

NEW_TENANT_TABLES = (
    "task_requirement",
    "task_dependency",
    "task_conversation",
    "chat_message",
    "conversation_summary",
    "cross_department_request",
    "requirement_request",
)

PHASE_I_ENUMS = (
    "task_state",
    "task_priority",
    "task_dependency_type",
    "chat_role",
    "request_type",
    "request_state",
)


def upgrade() -> None:
    bind = op.get_bind()

    task_state = postgresql.ENUM(
        *[m.value for m in TaskState], name="task_state", create_type=False
    )
    task_priority = postgresql.ENUM(
        *[m.value for m in TaskPriority], name="task_priority", create_type=False
    )
    task_state.create(bind, checkfirst=True)
    task_priority.create(bind, checkfirst=True)

    # --- grow the Phase E task stub into the real thing ---------------------
    op.add_column("task", sa.Column("bid_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("task", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("task", sa.Column("priority", task_priority, nullable=True))
    op.add_column("task", sa.Column("due_date", sa.Date(), nullable=True))
    op.add_column(
        "task", sa.Column("retrieval_scope", postgresql.JSONB(), nullable=True)
    )
    op.add_column("task", sa.Column("blocker_note", sa.Text(), nullable=True))
    op.add_column("task", sa.Column("grouping_rationale", sa.Text(), nullable=True))
    op.add_column(
        "task", sa.Column("state_changed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "task", sa.Column("state_changed_by", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.alter_column("task", "title", type_=sa.String(300), existing_nullable=False)

    # Backfill anything the stub allowed, so the NOT NULLs below can be applied
    # to a database that already carries rows.
    op.execute("UPDATE task SET retrieval_scope = '{}'::jsonb WHERE retrieval_scope IS NULL")
    op.execute("UPDATE task SET priority = 'NORMAL' WHERE priority IS NULL")
    # The stub's textual default cannot be cast implicitly; drop it, change the
    # type, then restore it as an enum literal.
    op.execute("ALTER TABLE task ALTER COLUMN state DROP DEFAULT")
    op.execute(
        "ALTER TABLE task ALTER COLUMN state TYPE task_state USING state::task_state"
    )
    op.execute("ALTER TABLE task ALTER COLUMN state SET DEFAULT 'NOT_STARTED'::task_state")
    op.alter_column("task", "retrieval_scope", nullable=False)
    op.alter_column("task", "priority", nullable=False)

    # A task without a bid or a department is not a task anyone can work, but
    # only rows that exist can be checked -- so these become NOT NULL only when
    # nothing would be orphaned by it.
    orphans = bind.execute(
        sa.text("SELECT count(*) FROM task WHERE bid_id IS NULL OR department_id IS NULL")
    ).scalar_one()
    if orphans == 0:
        op.alter_column("task", "bid_id", nullable=False)
        op.alter_column("task", "department_id", nullable=False)

    op.create_unique_constraint("uq_task_bid_code", "task", ["bid_id", "code"])
    op.create_foreign_key(
        "fk_bid_id_org", "task", "bid", ["bid_id", "org_id"], ["id", "org_id"], ondelete="CASCADE"
    )
    op.create_index("ix_task_owner", "task", ["org_id", "owner_user_id", "state"])
    op.create_index(
        "ix_task_department", "task", ["org_id", "bid_id", "department_id", "state"]
    )

    # The tenant key access_grant.task_id could not have in Phase E.
    op.create_foreign_key(
        "fk_access_grant_task_org",
        "access_grant",
        "task",
        ["task_id", "org_id"],
        ["id", "org_id"],
    )

    # --- the new tables -----------------------------------------------------
    tables = [Base.metadata.tables[name] for name in NEW_TENANT_TABLES]
    Base.metadata.create_all(bind=bind, tables=tables)

    for table in NEW_TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
              USING      (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
              WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
            """
        )

    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE};
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE};
            REVOKE UPDATE, DELETE ON audit_event FROM {APP_ROLE};
          END IF;
        END $$
        """
    )


def downgrade() -> None:
    for table in reversed(NEW_TENANT_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    op.execute("ALTER TABLE access_grant DROP CONSTRAINT IF EXISTS fk_access_grant_task_org")
    op.execute("DROP INDEX IF EXISTS ix_task_owner")
    op.execute("DROP INDEX IF EXISTS ix_task_department")
    op.execute("ALTER TABLE task DROP CONSTRAINT IF EXISTS uq_task_bid_code")
    op.execute("ALTER TABLE task DROP CONSTRAINT IF EXISTS fk_bid_id_org")
    op.execute("ALTER TABLE task ALTER COLUMN state DROP DEFAULT")
    op.execute("ALTER TABLE task ALTER COLUMN state TYPE varchar(32) USING state::text")
    op.execute("ALTER TABLE task ALTER COLUMN state SET DEFAULT 'NOT_STARTED'")
    op.execute("ALTER TABLE task ALTER COLUMN department_id DROP NOT NULL")
    for column in (
        "bid_id",
        "description",
        "priority",
        "due_date",
        "retrieval_scope",
        "blocker_note",
        "grouping_rationale",
        "state_changed_at",
        "state_changed_by",
    ):
        op.execute(f"ALTER TABLE task DROP COLUMN IF EXISTS {column}")

    for enum_name in PHASE_I_ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {enum_name} CASCADE")
