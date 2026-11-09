"""Phase G: opportunity, bid, and tender intelligence.

Adds the tender-side model — opportunity, bid, per-clause precedence,
requirements and their versions, clarification issues, deliverables and the
scope-confidence gate — under the same tenant-safety guarantees as every
earlier phase.

It also tightens ``document.bid_id``. Phase F could not declare a foreign key
there because ``bid`` did not exist yet, which left the one place a tender
document could reference another tenant's bid. Now that the target table exists,
the composite ``(bid_id, org_id)`` key closes it, and a CHECK enforces the
authority boundary in the schema itself: tender documents must belong to a bid,
company documents must not.

Revision ID: 0004_tender_intelligence
"""

from __future__ import annotations

from alembic import op
from app.models import Base

revision = "0004_tender_intelligence"
down_revision = "0003_ingestion_knowledge"
branch_labels = None
depends_on = None

APP_ROLE = "bidready_app"

NEW_TENANT_TABLES = (
    "opportunity",
    "bid",
    "clause_resolution",
    "requirement",
    "requirement_version",
    "clarification_issue",
    "clarification_requirement",
    "deliverable",
    "deliverable_requirement",
    "scope_confidence",
)

PHASE_G_ENUMS = (
    "bid_state",
    "precedence_method",
    "requirement_category",
    "requirement_lifecycle",
    "requirement_applicability",
    "risk_level",
    "scope_confidence_level",
    "clarification_issue_type",
    "clarification_severity",
    "clarification_status",
    "deliverable_status",
    "generated_format",
)


def upgrade() -> None:
    bind = op.get_bind()

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

    # --- close the document -> bid gap left open by Phase F -----------------
    op.execute(
        """
        DO $$
        DECLARE orphaned integer;
        BEGIN
          SELECT count(*) INTO orphaned
          FROM document d
          WHERE d.bid_id IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM bid b WHERE b.id = d.bid_id AND b.org_id = d.org_id);
          IF orphaned > 0 THEN
            RAISE EXCEPTION
              'Cannot add the document->bid foreign key: % document row(s) reference a '
              'bid that does not exist in the same organization.', orphaned;
          END IF;
        END $$
        """
    )
    op.execute(
        """
        ALTER TABLE document
          ADD CONSTRAINT fk_document_bid_org
          FOREIGN KEY (bid_id, org_id) REFERENCES bid (id, org_id)
        """
    )
    # The tender/company authority boundary, enforced by the schema rather than
    # only by the ingestion service.
    op.execute(
        """
        ALTER TABLE document
          ADD CONSTRAINT ck_document_side_bid
          CHECK (
            (side = 'TENDER' AND bid_id IS NOT NULL)
            OR (side = 'COMPANY' AND bid_id IS NULL)
          )
        """
    )

    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE
              ON ALL TABLES IN SCHEMA public TO {APP_ROLE};
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE};
            REVOKE UPDATE, DELETE ON audit_event FROM {APP_ROLE};
          END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE document DROP CONSTRAINT IF EXISTS ck_document_side_bid")
    op.execute("ALTER TABLE document DROP CONSTRAINT IF EXISTS fk_document_bid_org")
    for table in reversed(NEW_TENANT_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for enum_name in PHASE_G_ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {enum_name} CASCADE")
