"""Phase L: response artifacts, typed statements, reviews and approvals.

The Phase L model was added after the task and collaboration schema.  This
revision makes those declared tables part of the real deployment path and
applies the same forced tenant isolation used by every other tenant table.

Revision ID: 0007_response_artifacts
"""

from __future__ import annotations

from alembic import op
from app.models import Base

revision = "0007_response_artifacts"
down_revision = "0006_tasks_collaboration"
branch_labels = None
depends_on = None

APP_ROLE = "bidready_app"

NEW_TENANT_TABLES = (
    "response_artifact",
    "artifact_requirement",
    "artifact_version",
    "commitment",
    "assumption",
    "artifact_statement",
    "review",
    "approval",
)

PHASE_L_ENUMS = (
    "statement_class",
    "statement_approval",
    "artifact_state",
    "artifact_version_state",
    "review_decision",
    "approval_step_state",
    "assumption_status",
)


def upgrade() -> None:
    tables = [Base.metadata.tables[name] for name in NEW_TENANT_TABLES]
    Base.metadata.create_all(bind=op.get_bind(), tables=tables)

    # Existing Phase I tasks also need the one continuous draft/artifact
    # lifecycle.  Backfill an empty v1 without pretending it has been reviewed.
    op.execute(
        """
        INSERT INTO response_artifact
          (id, org_id, bid_id, task_id, title, state, created_at)
        SELECT gen_random_uuid(), t.org_id, t.bid_id, t.id, t.title,
               'DRAFT'::artifact_state, now()
        FROM task t
        WHERE t.bid_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM response_artifact a WHERE a.task_id = t.id
          )
        """
    )
    op.execute(
        """
        INSERT INTO artifact_version
          (id, org_id, artifact_id, version_no, state, created_at)
        SELECT gen_random_uuid(), a.org_id, a.id, 1,
               'DRAFT'::artifact_version_state, now()
        FROM response_artifact a
        WHERE NOT EXISTS (
          SELECT 1 FROM artifact_version v WHERE v.artifact_id = a.id
        )
        """
    )
    op.execute(
        """
        UPDATE response_artifact a
        SET current_version_id = v.id
        FROM artifact_version v
        WHERE v.artifact_id = a.id AND v.version_no = 1
          AND a.current_version_id IS NULL
        """
    )
    op.execute(
        """
        INSERT INTO artifact_requirement
          (id, org_id, artifact_id, requirement_id, created_at)
        SELECT gen_random_uuid(), a.org_id, a.id, tr.requirement_id, now()
        FROM response_artifact a
        JOIN task_requirement tr
          ON tr.task_id = a.task_id AND tr.org_id = a.org_id
        ON CONFLICT (artifact_id, requirement_id) DO NOTHING
        """
    )

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

    # Phase F deliberately rejected ARTIFACT_STATEMENT evidence targets until
    # the table existed.  It exists now, so extend the polymorphic tenant guard
    # rather than weakening it or accepting unchecked ids.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION evidence_link_target_same_tenant()
        RETURNS trigger AS $$
        DECLARE target_org uuid; target_table text;
        BEGIN
          target_table := CASE NEW.target_type
            WHEN 'KNOWLEDGE_FACT'     THEN 'knowledge_fact'
            WHEN 'KNOWLEDGE_RELATION' THEN 'knowledge_relation'
            WHEN 'KNOWLEDGE_ENTITY'   THEN 'knowledge_entity'
            WHEN 'ARTIFACT_STATEMENT' THEN 'artifact_statement'
            ELSE NULL END;
          IF target_table IS NULL THEN
            RAISE EXCEPTION 'evidence_link target_type % is not yet supported',
              NEW.target_type;
          END IF;
          EXECUTE format('SELECT org_id FROM %I WHERE id = $1', target_table)
            INTO target_org USING NEW.target_id;
          IF target_org IS NULL THEN
            RAISE EXCEPTION 'evidence_link target %/% does not exist',
              NEW.target_type, NEW.target_id;
          END IF;
          IF target_org <> NEW.org_id THEN
            RAISE EXCEPTION 'evidence_link would cross tenants: link org %, target org %',
              NEW.org_id, target_org;
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;
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
    op.execute(
        """
        CREATE OR REPLACE FUNCTION evidence_link_target_same_tenant()
        RETURNS trigger AS $$
        DECLARE target_org uuid; target_table text;
        BEGIN
          target_table := CASE NEW.target_type
            WHEN 'KNOWLEDGE_FACT'     THEN 'knowledge_fact'
            WHEN 'KNOWLEDGE_RELATION' THEN 'knowledge_relation'
            WHEN 'KNOWLEDGE_ENTITY'   THEN 'knowledge_entity'
            ELSE NULL END;
          IF target_table IS NULL THEN
            RAISE EXCEPTION 'evidence_link target_type % is not yet supported',
              NEW.target_type;
          END IF;
          EXECUTE format('SELECT org_id FROM %I WHERE id = $1', target_table)
            INTO target_org USING NEW.target_id;
          IF target_org IS NULL OR target_org <> NEW.org_id THEN
            RAISE EXCEPTION 'evidence_link target missing or cross-tenant';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;
        """
    )
    for table in reversed(NEW_TENANT_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for enum_name in PHASE_L_ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {enum_name} CASCADE")
