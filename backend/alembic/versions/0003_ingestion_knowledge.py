"""Phase F: document ingestion and the company knowledge graph.

Adds the ingestion chain (document -> version -> block -> chunk -> embedding)
and the knowledge graph (entity / fact / relation / evidence / evidence_link),
with the same tenant-safety guarantees the foundation established:

  * UNIQUE (id, org_id) on every new tenant table,
  * composite tenant-aware foreign keys wherever a target is a single table,
  * a trigger for evidence_link, whose target is polymorphic and therefore
    cannot be protected by a composite key, and
  * RLS enabled and FORCEd on all thirteen new tables.

This is also the revision that introduces pgvector, so the embedding dimension
is fixed here (see docs/05 section 14.4).

Revision ID: 0003_ingestion_knowledge
"""

from __future__ import annotations

from alembic import op
from app.core.config import EMBEDDING_DIM
from app.models import Base

revision = "0003_ingestion_knowledge"
down_revision = "0002_global_unique_email"
branch_labels = None
depends_on = None

APP_ROLE = "bidready_app"

# Enum types this revision owns. confidentiality_class, document_type and
# knowledge_status are used only by Phase F tables, so they belong here.
PHASE_F_ENUMS = (
    "document_side",
    "document_type",
    "document_version_state",
    "block_type",
    "document_relation_type",
    "entity_type",
    "relation_predicate",
    "knowledge_status",
    "approval_status",
    "evidence_link_target",
    "evidence_link_role",
    "review_queue_kind",
    "confidentiality_class",
)

NEW_TENANT_TABLES = (
    "document",
    "document_version",
    "document_classification",
    "document_relation",
    "document_block",
    "document_chunk",
    "embedding",
    "knowledge_entity",
    "knowledge_fact",
    "knowledge_relation",
    "evidence",
    "evidence_link",
    "knowledge_review_item",
)

# evidence_link.target_id may point at a fact, relation, entity, assessment or
# artifact statement, so no composite foreign key can express the constraint.
# A trigger enforces the same guarantee instead: the target row must exist and
# must belong to the same tenant.
EVIDENCE_LINK_GUARD = """
CREATE OR REPLACE FUNCTION evidence_link_target_same_tenant()
RETURNS trigger AS $$
DECLARE
  target_org uuid;
  target_table text;
BEGIN
  target_table := CASE NEW.target_type
    WHEN 'KNOWLEDGE_FACT'     THEN 'knowledge_fact'
    WHEN 'KNOWLEDGE_RELATION' THEN 'knowledge_relation'
    WHEN 'KNOWLEDGE_ENTITY'   THEN 'knowledge_entity'
    ELSE NULL
  END;

  -- REQUIREMENT_ASSESSMENT and ARTIFACT_STATEMENT arrive in later phases.
  -- Until their tables exist, links of those types are rejected rather than
  -- silently accepted unchecked.
  IF target_table IS NULL THEN
    RAISE EXCEPTION 'evidence_link target_type % is not yet supported', NEW.target_type;
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
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    bind = op.get_bind()

    # pgvector arrives with the first vector column, not before. The portable
    # backend stores the same embeddings in a real[] column and needs no
    # extension, so this revision must not demand one it will not use.
    from app.core.config import get_settings

    portable = get_settings().vector_backend == "portable"
    if not portable:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

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

    op.execute(EVIDENCE_LINK_GUARD)
    op.execute(
        """
        CREATE TRIGGER evidence_link_tenant_guard
        BEFORE INSERT OR UPDATE ON evidence_link
        FOR EACH ROW EXECUTE FUNCTION evidence_link_target_same_tenant()
        """
    )

    # Assert the schema constant matches what the column was actually created
    # with, so a mismatched EMBEDDING_DIM fails the migration rather than the
    # first insert. Only pgvector carries the width in the column type; the
    # portable backend enforces the same constant in the application, where
    # every write goes through `assert_dimension`.
    if not portable:
        op.execute(
            f"""
            DO $$
            DECLARE actual_dim integer;
            BEGIN
              SELECT a.atttypmod INTO actual_dim
              FROM pg_attribute a
              JOIN pg_class c ON c.oid = a.attrelid
              WHERE c.relname = 'embedding' AND a.attname = 'vector';
              IF actual_dim IS DISTINCT FROM {EMBEDDING_DIM} THEN
                RAISE EXCEPTION 'embedding.vector is vector(%), expected vector(%)',
                  actual_dim, {EMBEDDING_DIM};
              END IF;
            END $$
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
    op.execute("DROP TRIGGER IF EXISTS evidence_link_tenant_guard ON evidence_link")
    op.execute("DROP FUNCTION IF EXISTS evidence_link_target_same_tenant()")
    for table in NEW_TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    for table in reversed(NEW_TENANT_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for enum_name in PHASE_F_ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {enum_name} CASCADE")
