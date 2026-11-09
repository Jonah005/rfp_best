"""Hybrid retrieval: ranking quality and, above all, permission filtering.

Retrieval is the one path from a question to the corpus and serves both the UI
and the Copilot, so the negative tests here matter more than the positive ones:
an inadmissible chunk must never be a CANDIDATE, not merely absent from the
final list.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.context import ExecutionContext
from app.core.permissions import Perm
from app.db.session import apply_tenant_context
from app.gateway.deepseek import NullLLMProvider
from app.gateway.embeddings import HashingEmbeddingProvider
from app.gateway.gateway import ModelGateway
from app.ingestion.safety import EicarAwareScanner
from app.models.enums import (
    ConfidentialityClass,
    DocumentSide,
    GrantPermission,
    GrantResourceType,
)
from app.services import access_grants, ingestion
from app.services.retrieval import RetrievalScope, RetrievalService
from app.services.storage import InMemoryStorage

pytestmark = pytest.mark.integration

REFERENCES = (
    b"Client,Sector,Country,Year,Scope\n"
    b"Ministry of Interior,Government,UAE,2024,External and internal VAPT\n"
    b"City Utilities,Government,UAE,2025,Government GRC assessment\n"
    b"Acme Bank,Finance,KSA,2023,Web application testing\n"
)

CV_TEXT = b"""# Curriculum Vitae

Ahmed Hassan is a lead penetration tester with nine years of experience
delivering security assessments for clients across the region.
"""

SALARY_TEXT = b"""# Compensation Schedule

Lead Tester band: confidential salary information for the delivery team.
"""

READER_PERMS = frozenset({Perm.KNOWLEDGE_READ.value, Perm.DOCUMENT_READ.value})


@pytest.fixture
def embedder():
    return HashingEmbeddingProvider()


@pytest.fixture
def corpus(app_sessionmaker, seeded_orgs, embedder):
    """An indexed corpus in tenant A: two open documents and one restricted."""
    org_a, _ = seeded_orgs
    storage, scanner = InMemoryStorage(), EicarAwareScanner()
    ctx = ExecutionContext(org_id=org_a, permissions=READER_PERMS)
    extraction_gateway = ModelGateway(
        NullLLMProvider(canned='{"entities": [], "facts": [], "relations": []}')
    )
    ids: dict[str, uuid.UUID] = {}
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        for key, data, filename, klass in (
            ("refs", REFERENCES, "Referenceable Clients.csv", ConfidentialityClass.INTERNAL),
            ("cv", CV_TEXT, "CV_Ahmed.md", ConfidentialityClass.INTERNAL),
            ("salary", SALARY_TEXT, "Salary_Bands.md", ConfidentialityClass.RESTRICTED),
        ):
            version = ingestion.register_upload(
                s,
                ctx,
                data=data,
                filename=filename,
                side=DocumentSide.COMPANY,
                confidentiality_class=klass,
                scanner=scanner,
                storage=storage,
            )
            ingestion.parse_and_index(
                s,
                ctx,
                version_id=version.id,
                storage=storage,
                embedder=embedder,
                knowledge_gateway=extraction_gateway,
            )
            ids[key] = version.id
        s.commit()
    return org_a, ids, storage


def _reader_ctx(org, *, permissions=None, **kw) -> ExecutionContext:
    return ExecutionContext(org_id=org, permissions=permissions or READER_PERMS, **kw)


def _scope(ids, **kw) -> RetrievalScope:
    """Bound retrieval to THIS test's corpus.

    The database is shared across the module and each fixture commits, so
    without this a later test ranks against earlier tests' documents.
    """
    kw.setdefault("document_version_ids", tuple(ids.values()))
    return RetrievalScope(**kw)


# --- ranking ----------------------------------------------------------------


def test_reference_table_outranks_a_cv_for_a_references_query(app_sessionmaker, corpus, embedder):
    """The whole point of preserving table names at parse time."""
    org, ids, _ = corpus
    ctx = _reader_ctx(org)
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        result = RetrievalService(s, ctx).search(
            "government VAPT client references",
            scope=_scope(ids),
            limit=5,
            embedder=embedder,
        )
        assert result.candidates
        top = result.candidates[0]
        assert top.document_version_id == ids["refs"], (
            f"expected the references table to rank first, got {top.text[:80]!r}"
        )
        assert any("Ministry of Interior" in c.text for c in result.candidates)
        s.commit()


def test_every_hit_carries_a_resolvable_citation(app_sessionmaker, corpus, embedder):
    org, _ids, _ = corpus
    ctx = _reader_ctx(org)
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        result = RetrievalService(s, ctx).search(
            "penetration testing experience",
            scope=_scope(_ids),
            limit=5,
            embedder=embedder,
        )
        for candidate in result.candidates:
            locator = candidate.source_locator()
            assert locator["document_version_id"]
            assert candidate.why_matched, "a hit must explain why it matched"
        s.commit()


def test_keyword_and_semantic_paths_both_contribute(app_sessionmaker, corpus, embedder):
    org, _ids, _ = corpus
    ctx = _reader_ctx(org)
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        result = RetrievalService(s, ctx).search(
            "Ministry of Interior", scope=_scope(_ids), limit=10, embedder=embedder
        )
        reasons = {r for c in result.candidates for r in c.why_matched}
        assert "keyword match" in reasons
        assert any("semantic" in r for r in reasons)
        s.commit()


# --- permission filtering ---------------------------------------------------


def test_restricted_content_is_not_a_candidate_without_permission(
    app_sessionmaker, corpus, embedder
):
    """Not merely absent from the ranked list -- never admissible at all."""
    org, ids, _ = corpus
    ctx = _reader_ctx(org)
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        service = RetrievalService(s, ctx)
        result = service.search(
            "confidential salary band", scope=_scope(ids), limit=20, embedder=embedder
        )
        assert all(c.document_version_id != ids["salary"] for c in result.candidates)

        # And the admissible pool itself excludes it, before any ranking.
        admissible = service._admissibility_clause(_scope(ids))
        from sqlalchemy import func, select

        from app.models import DocumentChunk

        restricted_visible = s.execute(
            select(func.count())
            .select_from(DocumentChunk)
            .where(admissible, DocumentChunk.document_version_id == ids["salary"])
        ).scalar_one()
        assert restricted_visible == 0
        s.commit()


def test_the_matching_role_permission_opens_restricted_content(app_sessionmaker, corpus, embedder):
    org, ids, _ = corpus
    ctx = _reader_ctx(org, permissions=READER_PERMS | {Perm.PRICING_READ.value})
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        result = RetrievalService(s, ctx).search(
            "confidential salary band", scope=_scope(ids), limit=20, embedder=embedder
        )
        assert any(c.document_version_id == ids["salary"] for c in result.candidates)
        s.commit()


def test_a_live_task_scoped_grant_opens_exactly_one_document(
    app_sessionmaker, corpus, embedder, seeded_orgs
):
    org, ids, _ = corpus
    task_id = uuid.uuid4()
    with app_sessionmaker() as s:
        s.begin()
        # A real task and users, so the grant's composite FKs resolve.
        from app.models import User
        from tests.integration.conftest import make_task

        granter = User(
            org_id=org, email=f"g-{uuid.uuid4().hex[:8]}@example.com", display_name="Granter"
        )
        grantee = User(
            org_id=org, email=f"r-{uuid.uuid4().hex[:8]}@example.com", display_name="Grantee"
        )
        apply_tenant_context(s, ExecutionContext(org_id=org))
        s.add_all([granter, grantee])
        s.flush()
        make_task(s, org, task_id=task_id, title="Security task")

        granter_ctx = ExecutionContext(org_id=org, actor_user_id=granter.id)
        access_grants.create_grant(
            s,
            granter_ctx,
            grantee_user_id=grantee.id,
            task_id=task_id,
            resource_type=GrantResourceType.DOCUMENT_VERSION,
            resource_id=ids["salary"],
            permission=GrantPermission.READ,
            reason="needed for R-042",
        )

        grantee_ctx = ExecutionContext(
            org_id=org, actor_user_id=grantee.id, permissions=READER_PERMS
        )
        service = RetrievalService(s, grantee_ctx)

        in_task = service.search(
            "confidential salary band",
            scope=RetrievalScope(
                task_id=task_id, max_confidentiality=ConfidentialityClass.RESTRICTED
            ),
            limit=20,
            embedder=embedder,
        )
        assert any(c.document_version_id == ids["salary"] for c in in_task.candidates)

        # The same user, a different task: the grant does not follow them.
        other = service.search(
            "confidential salary band",
            scope=RetrievalScope(task_id=uuid.uuid4()),
            limit=20,
            embedder=embedder,
        )
        assert all(c.document_version_id != ids["salary"] for c in other.candidates)
        s.commit()


def test_a_revoked_grant_stops_returning_results_immediately(app_sessionmaker, corpus, embedder):
    org, ids, _ = corpus
    task_id = uuid.uuid4()
    with app_sessionmaker() as s:
        s.begin()
        from app.models import User
        from tests.integration.conftest import make_task

        apply_tenant_context(s, ExecutionContext(org_id=org))
        granter = User(org_id=org, email=f"g2-{uuid.uuid4().hex[:8]}@example.com", display_name="G")
        grantee = User(org_id=org, email=f"r2-{uuid.uuid4().hex[:8]}@example.com", display_name="R")
        s.add_all([granter, grantee])
        s.flush()
        make_task(s, org, task_id=task_id, title="T")

        granter_ctx = ExecutionContext(org_id=org, actor_user_id=granter.id)
        grant = access_grants.create_grant(
            s,
            granter_ctx,
            grantee_user_id=grantee.id,
            task_id=task_id,
            resource_type=GrantResourceType.DOCUMENT_VERSION,
            resource_id=ids["salary"],
            reason="temporary",
        )
        grantee_ctx = ExecutionContext(
            org_id=org, actor_user_id=grantee.id, permissions=READER_PERMS
        )
        scope = RetrievalScope(task_id=task_id, max_confidentiality=ConfidentialityClass.RESTRICTED)
        service = RetrievalService(s, grantee_ctx)
        assert any(
            c.document_version_id == ids["salary"]
            for c in service.search(
                "salary band", scope=scope, limit=20, embedder=embedder
            ).candidates
        )

        access_grants.revoke_grant(s, granter_ctx, grant_id=grant.id, reason="no longer needed")
        after = service.search("salary band", scope=scope, limit=20, embedder=embedder)
        assert all(c.document_version_id != ids["salary"] for c in after.candidates)
        s.commit()


def test_an_expired_grant_does_not_admit_content(app_sessionmaker, corpus, embedder):
    org, ids, _ = corpus
    task_id = uuid.uuid4()
    with app_sessionmaker() as s:
        s.begin()
        from app.models import User
        from tests.integration.conftest import make_task

        apply_tenant_context(s, ExecutionContext(org_id=org))
        granter = User(org_id=org, email=f"g3-{uuid.uuid4().hex[:8]}@example.com", display_name="G")
        grantee = User(org_id=org, email=f"r3-{uuid.uuid4().hex[:8]}@example.com", display_name="R")
        s.add_all([granter, grantee])
        s.flush()
        make_task(s, org, task_id=task_id, title="T")

        granter_ctx = ExecutionContext(org_id=org, actor_user_id=granter.id)
        grant = access_grants.create_grant(
            s,
            granter_ctx,
            grantee_user_id=grantee.id,
            task_id=task_id,
            resource_type=GrantResourceType.DOCUMENT_VERSION,
            resource_id=ids["salary"],
            reason="short-lived",
        )
        grant.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.flush()

        grantee_ctx = ExecutionContext(
            org_id=org, actor_user_id=grantee.id, permissions=READER_PERMS
        )
        result = RetrievalService(s, grantee_ctx).search(
            "salary band",
            scope=RetrievalScope(
                task_id=task_id, max_confidentiality=ConfidentialityClass.RESTRICTED
            ),
            limit=20,
            embedder=embedder,
        )
        assert all(c.document_version_id != ids["salary"] for c in result.candidates)
        s.commit()


def test_retrieval_never_crosses_tenants(app_sessionmaker, corpus, seeded_orgs, embedder):
    org_a, _ids, _ = corpus
    _, org_b = seeded_orgs
    ctx_b = _reader_ctx(org_b)
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx_b)
        result = RetrievalService(s, ctx_b).search(
            "Ministry of Interior VAPT references", limit=20, embedder=embedder
        )
        assert result.candidates == []
        assert result.out_of_scope is True
        s.commit()


def test_no_admissible_candidates_reports_the_owning_department(
    app_sessionmaker, seeded_orgs, embedder
):
    """A dead end must name who to ask, so the caller can offer the request flow."""
    _, org_b = seeded_orgs
    ctx = _reader_ctx(org_b)
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        result = RetrievalService(s, ctx).search(
            "is engineer Zain available for eight weeks", limit=10, embedder=embedder
        )
        assert result.out_of_scope
        assert "HR" in result.owning_departments
        s.commit()


def test_a_caller_with_no_read_permission_sees_nothing(app_sessionmaker, corpus, embedder):
    org, _ids, _ = corpus
    ctx = ExecutionContext(org_id=org, permissions=frozenset())
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        result = RetrievalService(s, ctx).search(
            "client references", scope=_scope(_ids), limit=20, embedder=embedder
        )
        assert result.candidates == []
        s.commit()


def test_scope_narrows_by_side(app_sessionmaker, corpus, embedder):
    """Company knowledge must not surface when a task scopes to tender documents."""
    org, _ids, _ = corpus
    ctx = _reader_ctx(org)
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        result = RetrievalService(s, ctx).search(
            "client references",
            scope=_scope(_ids, sides=(DocumentSide.TENDER,)),
            limit=20,
            embedder=embedder,
        )
        assert result.candidates == []
        s.commit()


def test_only_the_active_embedding_model_is_queried(app_sessionmaker, corpus):
    """A partially backfilled corpus degrades to fewer candidates, never to
    nonsense distances."""
    org, _ids, _ = corpus
    ctx = _reader_ctx(org)
    other_model = HashingEmbeddingProvider()
    other_model.model_id = "some-other-model-v9"
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        result = RetrievalService(s, ctx).search(
            "client references", scope=_scope(_ids), limit=20, embedder=other_model
        )
        # No vectors exist for that model, so nothing matches semantically;
        # keyword hits may still appear, but none may carry a vector rank.
        assert all(c.vector_rank is None for c in result.candidates)
        s.commit()


def test_a_task_scope_can_narrow_below_the_callers_entitlement(app_sessionmaker, corpus, embedder):
    """The ceiling is a task's tool for deliberately restricting itself.

    Entitlement decides what a user MAY see; scope decides what this particular
    task SHOULD see. A security task drafting a methodology has no business
    pulling salary bands into its context even if its owner could open them.
    """
    org, ids, _ = corpus
    ctx = _reader_ctx(org, permissions=READER_PERMS | {Perm.PRICING_READ.value})
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        service = RetrievalService(s, ctx)

        entitled = service.search(
            "confidential salary band", scope=_scope(ids), limit=20, embedder=embedder
        )
        assert any(c.document_version_id == ids["salary"] for c in entitled.candidates)

        narrowed = service.search(
            "confidential salary band",
            scope=_scope(ids, max_confidentiality=ConfidentialityClass.CONFIDENTIAL),
            limit=20,
            embedder=embedder,
        )
        assert all(c.document_version_id != ids["salary"] for c in narrowed.candidates)
        s.commit()
