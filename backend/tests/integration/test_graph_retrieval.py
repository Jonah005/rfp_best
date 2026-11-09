"""Retrieval through the knowledge graph, against a real database.

Lexical and semantic retrieval both read the requirement's words. The graph
reads what extraction already resolved the corpus to BE, which is the only path
that can connect a requirement to evidence sharing no vocabulary with it and
still say why. These cases are the two halves of that promise: it must find the
proof a word-matcher cannot, and it must refuse to attach to everything.

The governance rule is the same one evidence obeys. An unapproved entity or
relation is unreviewed extraction output; letting it route real evidence to a
requirement would put the graph outside the approval boundary the rest of the
system enforces.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.gateway.embeddings import HashingEmbeddingProvider
from app.ingestion.safety import EicarAwareScanner
from app.models import Evidence, KnowledgeEntity, KnowledgeRelation
from app.models.enums import (
    ApprovalStatus,
    ConfidentialityClass,
    DocumentSide,
    DocumentType,
    EntityType,
    EvidenceLinkRole,
    EvidenceLinkTarget,
    KnowledgeStatus,
    RelationPredicate,
)
from app.services import assessment, ingestion, knowledge
from app.services.storage import InMemoryStorage

pytestmark = pytest.mark.integration

COMPANY_TEXT = b"""# Accreditation record

Our penetration testing practice operates under an accreditation held since 2021.
"""


@pytest.fixture
def fresh_org(owner_engine):
    """A tenant of this test's own, so no other test's rows can be retrieved."""
    from sqlalchemy.orm import sessionmaker

    from app.models import Organization

    org_id = uuid.uuid4()
    Owner = sessionmaker(bind=owner_engine, future=True)
    with Owner() as s:
        s.add(
            Organization(
                id=org_id,
                name=f"Graph tenant {org_id.hex[:6]}",
                slug=f"graph-{org_id.hex[:10]}",
            )
        )
        s.commit()
    return org_id


@pytest.fixture
def env(fresh_org):
    return fresh_org, InMemoryStorage(), EicarAwareScanner(), HashingEmbeddingProvider()


def _version(session, ctx, env):
    _org, storage, scanner, _ = env
    return ingestion.register_upload(
        session,
        ctx,
        data=COMPANY_TEXT,
        filename=f"acc-{uuid.uuid4().hex[:6]}.md",
        side=DocumentSide.COMPANY,
        bid_id=None,
        confidentiality_class=ConfidentialityClass.INTERNAL,
        scanner=scanner,
        storage=storage,
    )


def _entity(
    session,
    ctx,
    *,
    name,
    entity_type=EntityType.ACCREDITATION_BODY,
    approval=ApprovalStatus.APPROVED,
    aliases=None,
):
    entity = KnowledgeEntity(
        org_id=ctx.org_id,
        entity_type=entity_type,
        canonical_name=name,
        aliases=aliases,
        classification=ConfidentialityClass.INTERNAL,
        status=KnowledgeStatus.CURRENT_VERIFIED,
        approval_status=approval,
    )
    session.add(entity)
    session.flush()
    return entity


def _evidence(session, ctx, *, version, excerpt):
    item = Evidence(
        org_id=ctx.org_id,
        source_document_version_id=version.id,
        source_locator={"block_ordinal": 1},
        excerpt=excerpt,
        evidence_type=DocumentType.CERTIFICATE,
        approval_status=ApprovalStatus.APPROVED,
        confidentiality_class=ConfidentialityClass.INTERNAL,
        status=KnowledgeStatus.CURRENT_VERIFIED,
        approved_for_reuse=True,
    )
    session.add(item)
    session.flush()
    return item


def _link(session, ctx, *, evidence, entity):
    return knowledge.link_evidence(
        session,
        ctx,
        evidence_id=evidence.id,
        target_type=EvidenceLinkTarget.KNOWLEDGE_ENTITY,
        target_id=entity.id,
        link_role=EvidenceLinkRole.SUPPORTS,
    )


def _graph(session, ctx, requirement_text):
    terms = [t for t in assessment._tokens(requirement_text) if len(t) > 3]
    return assessment._graph_candidates(session, ctx, terms=terms, text_query=requirement_text)


# --- what the graph is for --------------------------------------------------


def test_evidence_is_found_through_the_entity_the_requirement_names(app_sessionmaker, env):
    """The case no word-matcher can reach.

    The requirement says CREST. The proof says "accreditation held since 2021"
    and never says CREST at all. Only the stored link between them connects the
    two, and only the graph path follows it.
    """
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        version = _version(s, ctx, env)
        entity = _entity(s, ctx, name="CREST")
        item = _evidence(
            s, ctx, version=version, excerpt="Accreditation held since 2021 for pen testing."
        )
        _link(s, ctx, evidence=item, entity=entity)

        found = _graph(s, ctx, "Bidders shall hold CREST accreditation.")

        assert [evidence.id for evidence, _ in found] == [item.id]
        # The reason is the graph's real advantage over a score: it names the
        # entity that connected the two, and a reviewer can check that claim.
        assert "CREST" in found[0][1]
        s.rollback()


def test_a_neighbour_one_hop_away_is_reached(app_sessionmaker, env):
    """A requirement naming the accreditation reaches the certificate holder."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        version = _version(s, ctx, env)
        crest = _entity(s, ctx, name="CREST")
        company = _entity(s, ctx, name="Acme Cyber", entity_type=EntityType.LEGAL_ENTITY)
        s.add(
            KnowledgeRelation(
                org_id=ctx.org_id,
                subject_entity_id=company.id,
                object_entity_id=crest.id,
                predicate=RelationPredicate.HAS_CERTIFICATION,
                status=KnowledgeStatus.CURRENT_VERIFIED,
                approval_status=ApprovalStatus.APPROVED,
                extracted_by="test",
            )
        )
        s.flush()
        item = _evidence(s, ctx, version=version, excerpt="Acme Cyber has operated since 2014.")
        _link(s, ctx, evidence=item, entity=company)

        found = _graph(s, ctx, "Bidders shall hold CREST accreditation.")

        assert [evidence.id for evidence, _ in found] == [item.id]
        s.rollback()


# --- and what it must refuse ------------------------------------------------


def test_a_generic_entity_is_not_reached_by_a_shared_word(app_sessionmaker, env):
    """The failure that would make the graph confidently, structurally wrong."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        version = _version(s, ctx, env)
        policy = _entity(s, ctx, name="Information Security Policy", entity_type=EntityType.POLICY)
        item = _evidence(
            s, ctx, version=version, excerpt="All staff complete annual awareness training."
        )
        _link(s, ctx, evidence=item, entity=policy)

        found = _graph(s, ctx, "The supplier shall maintain security of the data centre.")

        assert found == []
        s.rollback()


def test_an_unapproved_entity_routes_nothing(app_sessionmaker, env):
    """Unreviewed extraction output must not reach into approved evidence."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        version = _version(s, ctx, env)
        entity = _entity(s, ctx, name="CREST", approval=ApprovalStatus.PENDING_APPROVAL)
        item = _evidence(s, ctx, version=version, excerpt="Accreditation held since 2021.")
        _link(s, ctx, evidence=item, entity=entity)

        assert _graph(s, ctx, "Bidders shall hold CREST accreditation.") == []
        s.rollback()


def test_unapproved_evidence_is_still_unreachable_through_the_graph(app_sessionmaker, env):
    """Graph expansion decides what to look for, never what may be seen."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        version = _version(s, ctx, env)
        entity = _entity(s, ctx, name="CREST")
        item = _evidence(s, ctx, version=version, excerpt="Accreditation held since 2021.")
        item.approval_status = ApprovalStatus.PENDING_APPROVAL
        item.approved_for_reuse = False
        s.flush()
        _link(s, ctx, evidence=item, entity=entity)

        assert _graph(s, ctx, "Bidders shall hold CREST accreditation.") == []
        s.rollback()


def test_an_alias_the_requirement_uses_still_matches(app_sessionmaker, env):
    """Tenders and company records rarely spell a body the same way."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        version = _version(s, ctx, env)
        entity = _entity(
            s,
            ctx,
            name="Council of Registered Ethical Security Testers",
            aliases=["CREST"],
        )
        item = _evidence(s, ctx, version=version, excerpt="Accreditation held since 2021.")
        _link(s, ctx, evidence=item, entity=entity)

        # The canonical name shares "registered" and "security" with the
        # requirement but nowhere near enough of itself; the alias is what
        # carries the match.
        found = _graph(s, ctx, "Bidders shall be CREST registered for security testing.")

        assert [evidence.id for evidence, _ in found] == [item.id]
        s.rollback()
