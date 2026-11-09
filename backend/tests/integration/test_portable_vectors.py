"""Semantic retrieval without pgvector.

The portable backend exists so the product runs on a stock PostgreSQL. It must
earn that by keeping the two things that matter:

* **Semantic ranking really works.** Falling back to keyword search would mean
  a question about "security certification" never finds a document that says
  "ISO 27001 accreditation" -- which is the entire reason embeddings are here.
* **The permission boundary does not move.** The admissibility predicate is
  still applied in the query, so an inadmissible chunk is never fetched, let
  alone ranked. Doing the arithmetic in Python must not turn a database-enforced
  boundary into an application-code one.
"""

from __future__ import annotations

import math
import uuid

import pytest
from sqlalchemy import select

from app.core.config import EMBEDDING_DIM, get_settings
from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.models import DocumentChunk, Embedding
from app.models.enums import ConfidentialityClass, DocumentSide
from app.services.retrieval import RetrievalScope, RetrievalService

pytestmark = pytest.mark.integration


class StubEmbedder:
    """A tiny, deterministic embedder with meaning we control exactly.

    Real embeddings cannot be asserted on; these can. Each text maps to a fixed
    vector, so "which is nearest" has one correct answer the test can state.
    """

    model_id = "stub-embedder"
    dimension = EMBEDDING_DIM

    # Meaning lives in the first four components; the rest are padding so the
    # rows fit the real schema width either backend was built with.
    MEANING = {
        "security certification": [1.0, 0.0, 0.0, 0.0],
        "iso 27001 accreditation": [0.96, 0.28, 0.0, 0.0],   # near neighbour
        "disaster recovery runbook": [0.0, 0.0, 1.0, 0.0],   # unrelated
        "office parking policy": [0.0, 0.0, 0.0, 1.0],       # unrelated
    }

    def embed_one(self, text: str) -> list[float]:
        meaning = self.MEANING[text.strip().lower()]
        return meaning + [0.0] * (EMBEDDING_DIM - len(meaning))

    def embed(self, texts):
        return [self.embed_one(t) for t in texts]


def _chunk(session, org_id, *, text, confidentiality=ConfidentialityClass.INTERNAL):
    """A chunk plus its embedding, written the way ingestion writes them."""
    from datetime import datetime, timezone

    from app.models import Document, DocumentVersion
    from app.models.enums import DocumentVersionState

    document = Document(
        org_id=org_id,
        side=DocumentSide.COMPANY,
        title=text[:80],
        original_filename=f"{uuid.uuid4().hex[:6]}.md",
        confidentiality_class=confidentiality,
    )
    session.add(document)
    session.flush()
    version = DocumentVersion(
        org_id=org_id,
        document_id=document.id,
        version_no=1,
        state=DocumentVersionState.INDEXED,
        storage_key=f"k/{uuid.uuid4().hex}",
        sha256="0" * 64,
        byte_size=len(text),
        mime_type="text/markdown",
        received_at=datetime.now(timezone.utc),
    )
    session.add(version)
    session.flush()
    document.current_version_id = version.id

    chunk = DocumentChunk(
        org_id=org_id,
        document_version_id=version.id,
        ordinal=1,
        text=text,
        side=DocumentSide.COMPANY,
        confidentiality_class=confidentiality,
        token_count=len(text.split()),
        block_ids=[],
        char_start=0,
        char_end=len(text),
    )
    session.add(chunk)
    session.flush()

    session.add(
        Embedding(
            org_id=org_id,
            chunk_id=chunk.id,
            model_id=StubEmbedder.model_id,
            dim=StubEmbedder.dimension,
            vector=StubEmbedder().embed_one(text),
            created_at=datetime.now(timezone.utc),
        )
    )
    session.flush()
    return chunk


@pytest.fixture
def portable(monkeypatch):
    """Force the portable path regardless of what the schema was built with.

    The ranking arithmetic is what is under test; the column type is asserted
    separately below.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "vector_backend", "portable", raising=False)
    return settings


@pytest.fixture
def org(seeded_orgs):
    return seeded_orgs[0]


def _ctx(org_id, **perms):
    return ExecutionContext(
        org_id=org_id,
        actor_user_id=uuid.uuid4(),
        permissions=frozenset(perms.get("permissions", {"document:read", "knowledge:read"})),
    )


def test_the_nearest_meaning_wins_without_pgvector(app_sessionmaker, org, portable):
    """The case keyword search cannot do: different words, same meaning."""
    with app_sessionmaker() as s:
        s.begin()
        ctx = _ctx(org)
        apply_tenant_context(s, ctx)
        wanted = _chunk(s, org, text="ISO 27001 accreditation")
        _chunk(s, org, text="Disaster recovery runbook")
        _chunk(s, org, text="Office parking policy")

        service = RetrievalService(s, ctx)
        result = service.search(
            "security certification",
            scope=RetrievalScope(sides=(DocumentSide.COMPANY,)),
            limit=3,
            embedder=StubEmbedder(),
        )
        assert result.candidates, "portable retrieval returned nothing"
        # The winning chunk shares no search term with the query.
        assert result.candidates[0].chunk_id == wanted.id
        assert "security" not in wanted.text.lower()
        s.rollback()


def test_portable_ranking_matches_the_arithmetic_it_claims(app_sessionmaker, org, portable):
    """Ranked by cosine similarity, in the order cosine actually gives."""
    with app_sessionmaker() as s:
        s.begin()
        ctx = _ctx(org)
        apply_tenant_context(s, ctx)
        for text in (
            "ISO 27001 accreditation",
            "Disaster recovery runbook",
            "Office parking policy",
        ):
            _chunk(s, org, text=text)

        service = RetrievalService(s, ctx)
        query_vec = StubEmbedder().embed_one("security certification")
        result = service.search(
            "security certification",
            scope=RetrievalScope(sides=(DocumentSide.COMPANY,)),
            limit=3,
            embedder=StubEmbedder(),
        )

        def cosine(a, b):
            dot = sum(x * y for x, y in zip(a, b, strict=False))
            return dot / (math.dist(a, [0] * len(a)) * math.dist(b, [0] * len(b)))

        got = []
        for candidate in result.candidates:
            chunk = s.execute(
                select(DocumentChunk).where(DocumentChunk.id == candidate.chunk_id)
            ).scalar_one()
            got.append(cosine(query_vec, StubEmbedder().embed_one(chunk.text)))
        assert got == sorted(got, reverse=True), "results are not in cosine order"
        s.rollback()


def test_the_permission_boundary_still_holds_in_portable_mode(app_sessionmaker, org, portable):
    """The important one.

    Ranking moved into Python; admissibility must not. A caller without the
    restricted-data permission must never see the restricted chunk, even though
    it is the closest match by meaning.
    """
    with app_sessionmaker() as s:
        s.begin()
        ctx = _ctx(org)
        apply_tenant_context(s, ctx)
        secret = _chunk(
            s,
            org,
            text="ISO 27001 accreditation",
            confidentiality=ConfidentialityClass.RESTRICTED,
        )
        allowed = _chunk(s, org, text="Disaster recovery runbook")

        service = RetrievalService(s, ctx)
        result = service.search(
            "security certification",
            scope=RetrievalScope(sides=(DocumentSide.COMPANY,)),
            limit=5,
            embedder=StubEmbedder(),
        )
        returned = {c.chunk_id for c in result.candidates}
        assert secret.id not in returned, "portable mode leaked a restricted chunk"
        assert allowed.id in returned or not returned
        s.rollback()


def test_a_foreign_tenants_vectors_are_never_scanned(app_sessionmaker, seeded_orgs, portable):
    """RLS still applies: the scan only ever sees this tenant's rows."""
    org_a, org_b = seeded_orgs
    with app_sessionmaker() as s:
        s.begin()
        ctx_a = _ctx(org_a)
        apply_tenant_context(s, ctx_a)
        mine = _chunk(s, org_a, text="ISO 27001 accreditation")
        s.commit()

    with app_sessionmaker() as s:
        s.begin()
        ctx_b = _ctx(org_b)
        apply_tenant_context(s, ctx_b)
        result = RetrievalService(s, ctx_b).search(
            "security certification",
            scope=RetrievalScope(sides=(DocumentSide.COMPANY,)),
            limit=5,
            embedder=StubEmbedder(),
        )
        assert mine.id not in {c.chunk_id for c in result.candidates}
        assert (
            s.execute(select(Embedding).where(Embedding.org_id == org_a)).scalars().all()
            == []
        )
        s.rollback()


def test_the_configured_backend_decides_the_column_type():
    """The schema and the ORM must agree about how a vector is stored."""
    from app.models.documents import _vector_column

    settings = get_settings()
    original = settings.vector_backend
    try:
        settings.vector_backend = "portable"
        assert "ARRAY" in repr(_vector_column()).upper()
        settings.vector_backend = "pgvector"
        assert "VECTOR" in repr(_vector_column()).upper()
    finally:
        settings.vector_backend = original
