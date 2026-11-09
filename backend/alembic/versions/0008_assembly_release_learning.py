"""Phases M-P: assembly, validation, release, outcome and learning.

Revision ID: 0008_assembly_release_learning
"""

from __future__ import annotations

from alembic import op
from app.models import Base

revision = "0008_assembly_release_learning"
down_revision = "0007_response_artifacts"
branch_labels = None
depends_on = None

APP_ROLE = "bidready_app"

NEW_TENANT_TABLES = (
    "proposal_structure",
    "proposal_section",
    "section_artifact_map",
    "generated_document",
    "generated_document_version",
    "package",
    "package_item",
    "manifest",
    "manifest_entry",
    "validation_run",
    "validation_finding",
    "release_exception",
    "outcome",
    "knowledge_update_candidate",
    "amendment_impact",
)

NEW_ENUMS = (
    "structure_derivation_method",
    "inclusion_mode",
    "output_mode",
    "document_stream",
    "package_state",
    "validation_run_status",
    "finding_severity",
    "finding_waivability",
    "exception_state",
    "outcome_result",
    "candidate_state",
)


def upgrade() -> None:
    tables = [Base.metadata.tables[name] for name in NEW_TENANT_TABLES]
    Base.metadata.create_all(bind=op.get_bind(), tables=tables)

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

    # A NON_WAIVABLE finding has no exception path, including through SQL.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION release_exception_is_waivable()
        RETURNS trigger AS $$
        DECLARE finding_waivability finding_waivability;
        BEGIN
          SELECT waivability INTO finding_waivability
          FROM validation_finding
          WHERE id = NEW.finding_id AND org_id = NEW.org_id;
          IF finding_waivability IS DISTINCT FROM 'WAIVABLE'::finding_waivability THEN
            RAISE EXCEPTION 'release exception requires a WAIVABLE finding';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;
        CREATE TRIGGER release_exception_waivable_guard
        BEFORE INSERT OR UPDATE ON release_exception
        FOR EACH ROW EXECUTE FUNCTION release_exception_is_waivable();
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
        "DROP TRIGGER IF EXISTS release_exception_waivable_guard ON release_exception"
    )
    op.execute("DROP FUNCTION IF EXISTS release_exception_is_waivable()")
    for table in reversed(NEW_TENANT_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for enum_name in NEW_ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {enum_name} CASCADE")
