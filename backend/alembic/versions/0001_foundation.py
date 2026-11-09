"""Phase E foundation: identity, RBAC, access grants, audit, model usage.

Creates the schema from the SQLAlchemy metadata, then applies the two database
level tenant-safety mechanisms that application code cannot be trusted to
provide on its own:

  * UNIQUE (id, org_id) on every tenant table, which the composite foreign keys
    declared in the models reference; and
  * ROW LEVEL SECURITY, ENABLEd and FORCEd, on every tenant table including the
    join tables, keyed on a transaction-local GUC.

FORCE matters: without it the table owner bypasses the policy, and migrations
normally run as the owner. The application connects as a separate non-superuser
role without BYPASSRLS so the policies actually bind it.

Revision ID: 0001_foundation
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.models import Base

# The tables this revision owns. TENANT_TABLES grows with each phase; this
# list is frozen at what Phase E created.
FOUNDATION_TABLES = (
    "organization",
    "permission",
    "department",
    "app_user",
    "role",
    "role_permission",
    "user_role",
    "user_department",
    "task",
    "task_participant",
    "access_grant",
    "audit_event",
    "model_usage",
)
# `task` is created by hand below rather than from the metadata: by Phase I the
# model carries a bid, a department, an enum state and a retrieval scope, none
# of which existed here. A metadata-driven CREATE would make this revision
# depend on tables three phases away, which is how a migration chain stops
# being a history and becomes a snapshot.
TENANT_TABLES = tuple(
    t for t in FOUNDATION_TABLES if t not in ("organization", "permission")
)

# Everything up to and including user_department; `task` is created by hand
# between the two halves because its Phase I model references tables three
# phases away.
BEFORE_TASK = FOUNDATION_TABLES[: FOUNDATION_TABLES.index("task")]
AFTER_TASK = FOUNDATION_TABLES[FOUNDATION_TABLES.index("task") + 1 :]

# Enum types this revision owns.
FOUNDATION_ENUMS = (
    "department_code",
    "grant_resource_type",
    "grant_permission",
    "grant_state",
    "actor_kind",
    "participant_role",
)

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = "bidready_app"


def upgrade() -> None:
    bind = op.get_bind()

    # No extensions are created here. The foundation schema has no vector or
    # citext columns, and a migration should not install an extension it does
    # not use -- pgvector arrives with the embedding table in Phase F. The
    # Compose stack provisions both at container init (scripts/init_db.sql).

    # Only this revision's tables. Later revisions add their own; creating
    # the whole metadata here would make 0001 silently depend on models
    # that belong to a later phase.
    Base.metadata.create_all(
        bind=bind,
        tables=[Base.metadata.tables[name] for name in BEFORE_TASK],
    )

    # The Phase E task: the minimum that lets access_grant and task_participant
    # point at a real parent row. Written out rather than taken from the
    # metadata because by Phase I the model carries a bid, a department, an
    # enum state and a retrieval scope, none of which exist yet here. Phase I
    # grows this table in place.
    op.create_table(
        "task",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("department_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("state", sa.String(32), server_default="NOT_STARTED", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("id", "org_id", name="uq_task_id_org"),
        sa.ForeignKeyConstraint(["org_id"], ["organization.id"], name="fk_task_org_id"),
        sa.ForeignKeyConstraint(
            ["department_id", "org_id"],
            ["department.id", "department.org_id"],
            name="fk_department_id_org",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id", "org_id"],
            ["app_user.id", "app_user.org_id"],
            name="fk_task_owner_org",
        ),
    )

    Base.metadata.create_all(
        bind=bind,
        tables=[Base.metadata.tables[name] for name in AFTER_TASK],
    )

    # --- row level security ------------------------------------------------
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
              USING      (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
              WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
            """
        )

    # --- effective grant state (correction 2) ------------------------------
    # A generated column cannot be used here: generated columns must be
    # immutable and cannot reference now(). State is therefore derived.
    op.execute(
        """
        CREATE VIEW access_grant_effective AS
        SELECT g.*,
               CASE WHEN g.revoked_at IS NOT NULL THEN 'REVOKED'
                    WHEN g.expires_at <= now()    THEN 'EXPIRED'
                    ELSE 'ACTIVE' END AS effective_state
        FROM access_grant g
        """
    )

    # --- audit log is append-only -----------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_event_is_append_only()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'audit_event is append-only (attempted %)', TG_OP;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_event_no_mutation
        BEFORE UPDATE OR DELETE ON audit_event
        FOR EACH ROW EXECUTE FUNCTION audit_event_is_append_only()
        """
    )

    # --- pre-tenant login lookup -------------------------------------------
    sql_path = Path(__file__).resolve().parents[2] / "app" / "db" / "auth_lookup.sql"
    op.execute(sql_path.read_text(encoding="utf-8"))
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bidready_auth') THEN
            ALTER FUNCTION auth_lookup_user(text) OWNER TO bidready_auth;
            -- BYPASSRLS lifts the policy, not the table privilege. This role
            -- gets SELECT on exactly one table and owns exactly one function.
            GRANT SELECT ON app_user TO bidready_auth;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            GRANT EXECUTE ON FUNCTION auth_lookup_user(text) TO {APP_ROLE};
          END IF;
        END $$
        """
    )

    # --- privileges for the application role -------------------------------
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            GRANT USAGE ON SCHEMA public TO {APP_ROLE};
            GRANT SELECT, INSERT, UPDATE, DELETE
              ON ALL TABLES IN SCHEMA public TO {APP_ROLE};
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE};
            -- audit_event: insert and read only, enforced by privilege as well
            -- as by trigger.
            REVOKE UPDATE, DELETE ON audit_event FROM {APP_ROLE};
          END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_event_no_mutation ON audit_event")
    op.execute("DROP FUNCTION IF EXISTS audit_event_is_append_only()")
    op.execute("DROP FUNCTION IF EXISTS auth_lookup_user(text)")
    op.execute("DROP VIEW IF EXISTS access_grant_effective")
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    # Explicit DROP ... CASCADE rather than metadata.drop_all: drop_all orders
    # by the CURRENT metadata, which later phases extend, and it cannot know
    # which enum types are still in use by tables another revision owns. A
    # destructive path should be unambiguous.
    for table in reversed(FOUNDATION_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for enum_name in FOUNDATION_ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {enum_name} CASCADE")
