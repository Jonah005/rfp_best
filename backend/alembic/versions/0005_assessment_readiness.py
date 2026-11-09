"""Phase H: requirement assessment, evidence links, and the bid/no-bid record.

The first schema in which tender requirements and company evidence are joined.
The join table points at both sides, so both composite tenant keys apply and a
requirement in one organization can never be assessed against another's
evidence.

Revision ID: 0005_assessment_readiness
"""

from __future__ import annotations

from alembic import op
from app.models import Base

revision = "0005_assessment_readiness"
down_revision = "0004_tender_intelligence"
branch_labels = None
depends_on = None

APP_ROLE = "bidready_app"

NEW_TENANT_TABLES = (
    "requirement_assessment",
    "assessment_evidence",
    "bid_no_bid_decision",
)

PHASE_H_ENUMS = (
    "evidence_status",
    "bid_readiness",
    "assessment_state",
    "assessment_evidence_role",
    "bid_no_bid_result",
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
    for enum_name in PHASE_H_ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {enum_name} CASCADE")
