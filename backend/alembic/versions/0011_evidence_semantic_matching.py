"""Let a requirement find evidence that means the same thing.

Requirement assessment matched evidence lexically: it built a `to_tsquery` from
the requirement's words and returned excerpts sharing one. That fails on the
cases the system exists to get right. A requirement reading

    "Firms must be Industry Recognized/Certified to bid"

shares no token with a certificate reading

    "is certified to ISO/IEC 27001:2022"

and the `simple` text-search configuration does not stem, so even `certified`
and `certificate` are different words. The certificate was not ranked low — it
was never retrieved, so the requirement reported no evidence while the proof sat
approved in the corpus.

This revision gives `evidence` an embedding of its excerpt, so matching can be
hybrid: lexical for exact terms and identifiers, semantic for meaning.

**Nullable, deliberately.** Evidence extracted before this column existed has no
vector until backfilled, and the lexical path must keep working for it in the
meantime. A NOT NULL column would have meant a migration that either fails on
existing rows or invents zero vectors, and a zero vector is not "no opinion" —
it is a specific point in the space that would match things it should not.

**Index only under pgvector.** The portable backend stores `real[]` and computes
cosine in the application, which has no index to offer; the column and the
ordering are otherwise identical, so a deployment can move between backends
without touching this revision.

Revision ID: 0011_evidence_semantic_matching
"""

from __future__ import annotations

from alembic import op
from app.core.config import EMBEDDING_DIM, get_settings

revision = "0011_evidence_semantic_matching"
down_revision = "0010_company_document_taxonomy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    portable = get_settings().vector_backend == "portable"
    column_type = "real[]" if portable else f"vector({EMBEDDING_DIM})"
    op.execute(
        f"ALTER TABLE evidence ADD COLUMN IF NOT EXISTS excerpt_vector {column_type}"
    )
    if not portable:
        # Same shape as the chunk embedding index: cosine, because the
        # embeddings are normalised and cosine is what the query orders by.
        op.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_evidence_excerpt_vector_hnsw
              ON evidence USING hnsw (excerpt_vector vector_cosine_ops)
              WITH (m = 16, ef_construction = 64)
            """
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_evidence_excerpt_vector_hnsw")
    op.execute("ALTER TABLE evidence DROP COLUMN IF EXISTS excerpt_vector")
