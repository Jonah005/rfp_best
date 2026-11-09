"""The ingestion pipeline end to end, against a real database.

Upload -> validate -> scan -> store -> parse -> block -> chunk -> embed -> index,
with provenance surviving every step.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.core.errors import ValidationError
from app.db.session import apply_tenant_context
from app.gateway.deepseek import NullLLMProvider
from app.gateway.embeddings import HashingEmbeddingProvider
from app.gateway.gateway import ModelGateway
from app.ingestion.safety import EicarAwareScanner
from app.models import (
    AuditEvent,
    DocumentBlock,
    DocumentChunk,
    DocumentVersion,
    Embedding,
    Evidence,
    EvidenceLink,
    KnowledgeEntity,
    KnowledgeFact,
    KnowledgeRelation,
    KnowledgeReviewItem,
    ModelUsage,
)
from app.models.enums import (
    ApprovalStatus,
    BlockType,
    ConfidentialityClass,
    DocumentSide,
    DocumentVersionState,
    EvidenceLinkTarget,
    KnowledgeStatus,
    ReviewQueueKind,
)
from app.services import ingestion
from app.services.storage import InMemoryStorage

pytestmark = pytest.mark.integration

RFP_MARKDOWN = b"""# 6 Scope of Work

The bidder shall perform penetration testing of 35 applications.

## 6.1 Testing window

Testing must occur between 01:00 and 05:00 Gulf Standard Time.

## 6.2 Retesting

One retest per finding is included in the fixed price.
"""

REFERENCES_CSV = (
    b"Client,Sector,Country,Year,Scope\n"
    b"Ministry of Interior,Government,UAE,2024,External and internal VAPT\n"
    b"Acme Bank,Finance,KSA,2023,Web application testing\n"
    b"City Utilities,Utilities,UAE,2025,Government GRC assessment\n"
)


@pytest.fixture
def ingest_env(app_sessionmaker, seeded_orgs):
    org_a, _ = seeded_orgs
    return org_a, InMemoryStorage(), EicarAwareScanner(), HashingEmbeddingProvider()


def _ingest(session, ctx, env, data, filename, *, classification=None):
    _org, storage, scanner, embedder = env
    version = ingestion.register_upload(
        session,
        ctx,
        data=data,
        filename=filename,
        side=DocumentSide.COMPANY,
        confidentiality_class=classification or ConfidentialityClass.INTERNAL,
        scanner=scanner,
        storage=storage,
    )
    empty_extraction = ModelGateway(
        NullLLMProvider(canned='{"entities": [], "facts": [], "relations": []}')
    )
    ingestion.parse_and_index(
        session,
        ctx,
        version_id=version.id,
        storage=storage,
        embedder=embedder,
        knowledge_gateway=empty_extraction,
    )
    return version


def test_full_pipeline_produces_blocks_chunks_and_embeddings(app_sessionmaker, ingest_env):
    org, *_ = ingest_env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)

        version = _ingest(s, ctx, ingest_env, RFP_MARKDOWN, "scope.md")
        s.refresh(version)

        assert version.state is DocumentVersionState.INDEXED
        assert version.block_count and version.chunk_count

        blocks = (
            s.execute(
                select(DocumentBlock)
                .where(DocumentBlock.document_version_id == version.id)
                .order_by(DocumentBlock.ordinal)
            )
            .scalars()
            .all()
        )
        chunks = (
            s.execute(select(DocumentChunk).where(DocumentChunk.document_version_id == version.id))
            .scalars()
            .all()
        )
        embeddings = (
            s.execute(select(Embedding).where(Embedding.chunk_id.in_([c.id for c in chunks])))
            .scalars()
            .all()
        )
        assert len(blocks) == version.block_count
        assert len(chunks) == version.chunk_count
        assert len(embeddings) == len(chunks), "every chunk must be embedded"
        assert all(len(e.vector) == 768 for e in embeddings)
        s.commit()


def test_provenance_survives_from_file_to_chunk(app_sessionmaker, ingest_env):
    """A chunk that cannot cite its source is useless as evidence."""
    org, *_ = ingest_env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        version = _ingest(s, ctx, ingest_env, RFP_MARKDOWN, "scope.md")

        chunks = (
            s.execute(select(DocumentChunk).where(DocumentChunk.document_version_id == version.id))
            .scalars()
            .all()
        )
        assert any(c.clause_ref == "6.1" for c in chunks)
        assert all(c.heading_path for c in chunks)
        assert all(c.block_ids for c in chunks), "chunks must point back at their blocks"

        block_ids = {
            b.id
            for b in s.execute(
                select(DocumentBlock).where(DocumentBlock.document_version_id == version.id)
            )
            .scalars()
            .all()
        }
        for chunk in chunks:
            assert set(chunk.block_ids) <= block_ids
        s.commit()


def test_table_rows_keep_their_table_name_and_columns(app_sessionmaker, ingest_env):
    org, *_ = ingest_env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        version = _ingest(s, ctx, ingest_env, REFERENCES_CSV, "Referenceable Clients.csv")

        rows = (
            s.execute(
                select(DocumentBlock).where(
                    DocumentBlock.document_version_id == version.id,
                    DocumentBlock.block_type == BlockType.TABLE_ROW,
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 3
        assert all(r.table_name == "Referenceable Clients" for r in rows)
        assert any("Ministry of Interior" in r.text for r in rows)
        s.commit()


def test_chunk_carries_denormalized_class_and_side_for_filtering(app_sessionmaker, ingest_env):
    """Retrieval filters on these inside the vector query, so they must be present."""
    org, *_ = ingest_env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        version = _ingest(
            s,
            ctx,
            ingest_env,
            RFP_MARKDOWN,
            "restricted.md",
            classification=ConfidentialityClass.RESTRICTED,
        )
        chunks = (
            s.execute(select(DocumentChunk).where(DocumentChunk.document_version_id == version.id))
            .scalars()
            .all()
        )
        assert all(c.confidentiality_class is ConfidentialityClass.RESTRICTED for c in chunks)
        assert all(c.side is DocumentSide.COMPANY for c in chunks)
        s.commit()


def test_infected_upload_never_reaches_a_parser(app_sessionmaker, ingest_env):
    org, storage, scanner, _ = ingest_env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        payload = b"report " + EicarAwareScanner.EICAR
        with pytest.raises(ValidationError, match="malware"):
            ingestion.register_upload(
                s,
                ctx,
                data=payload,
                filename="report.txt",
                side=DocumentSide.COMPANY,
                confidentiality_class=ConfidentialityClass.INTERNAL,
                scanner=scanner,
                storage=storage,
            )
        # Nothing was stored and no version row exists.
        assert not storage._objects
        s.rollback()


def test_company_documents_cannot_be_attached_to_a_bid(app_sessionmaker, ingest_env):
    """Company knowledge is organization-wide; tender documents belong to a bid."""
    org, storage, scanner, _ = ingest_env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        with pytest.raises(ValidationError, match="organization-wide"):
            ingestion.register_upload(
                s,
                ctx,
                data=b"text",
                filename="a.txt",
                side=DocumentSide.COMPANY,
                confidentiality_class=ConfidentialityClass.INTERNAL,
                bid_id=uuid.uuid4(),
                scanner=scanner,
                storage=storage,
            )
        with pytest.raises(ValidationError, match="must belong to a bid"):
            ingestion.register_upload(
                s,
                ctx,
                data=b"text",
                filename="b.txt",
                side=DocumentSide.TENDER,
                confidentiality_class=ConfidentialityClass.INTERNAL,
                scanner=scanner,
                storage=storage,
            )
        s.rollback()


def test_second_version_increments_and_does_not_replace_the_first(app_sessionmaker, ingest_env):
    org, storage, scanner, embedder = ingest_env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        v1 = ingestion.register_upload(
            s,
            ctx,
            data=b"# Version one\n\nOriginal text.\n",
            filename="policy.md",
            side=DocumentSide.COMPANY,
            confidentiality_class=ConfidentialityClass.INTERNAL,
            scanner=scanner,
            storage=storage,
        )
        v2 = ingestion.register_upload(
            s,
            ctx,
            data=b"# Version two\n\nRevised text.\n",
            filename="policy.md",
            side=DocumentSide.COMPANY,
            confidentiality_class=ConfidentialityClass.INTERNAL,
            scanner=scanner,
            storage=storage,
            document_id=v1.document_id,
        )
        assert v1.version_no == 1 and v2.version_no == 2
        assert v1.storage_key != v2.storage_key
        versions = (
            s.execute(select(DocumentVersion).where(DocumentVersion.document_id == v1.document_id))
            .scalars()
            .all()
        )
        assert len(versions) == 2, "the earlier version must be retained"
        s.commit()


def test_ingestion_is_audited(app_sessionmaker, ingest_env):
    org, *_ = ingest_env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        version = _ingest(s, ctx, ingest_env, RFP_MARKDOWN, "audited.md")
        actions = {
            e.action
            for e in s.execute(select(AuditEvent).where(AuditEvent.entity_id == version.id))
            .scalars()
            .all()
        }
        assert "document.uploaded" in actions
        assert "document.indexed" in actions
        s.commit()


def test_reindexing_does_not_duplicate_embeddings(app_sessionmaker, ingest_env):
    org, storage, scanner, embedder = ingest_env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        version = _ingest(s, ctx, ingest_env, RFP_MARKDOWN, "idem.md")
        written = ingestion.embed_chunks(s, ctx, version_id=version.id, embedder=embedder)
        assert written == 0, "chunks already embedded for this model must be skipped"
        s.commit()


def test_company_document_automatically_extracts_governed_knowledge(app_sessionmaker, ingest_env):
    """Parsed company chunks must become proposed, evidence-linked graph rows."""
    org, storage, scanner, embedder = ingest_env
    suffix = uuid.uuid4().hex[:8]
    company_name = f"Acme Cyber {suffix} LLC"
    certification_name = f"CREST Penetration Testing {suffix}"
    response = json.dumps(
        {
            "entities": [
                {
                    "ref": "company",
                    "entity_type": "LEGAL_ENTITY",
                    "canonical_name": company_name,
                    "aliases": ["Acme Cyber"],
                    "attributes": {"country": "UAE"},
                    "source_chunk_ordinals": [0],
                    "confidence": 0.98,
                },
                {
                    "ref": "crest",
                    "entity_type": "CERTIFICATION",
                    "canonical_name": certification_name,
                    "source_chunk_ordinals": [0],
                    "confidence": 0.97,
                },
            ],
            "facts": [
                {
                    "entity_ref": "company",
                    "predicate": "legal_name",
                    "value_text": company_name,
                    "source_chunk_ordinals": [0],
                    "confidence": 0.99,
                }
            ],
            "relations": [
                {
                    "subject_ref": "company",
                    "predicate": "HAS_CERTIFICATION",
                    "object_ref": "crest",
                    "attributes": {"certificate_number": "CR-8871"},
                    "source_chunk_ordinals": [0],
                    "confidence": 0.96,
                }
            ],
        }
    )
    gateway = ModelGateway(NullLLMProvider(canned=response))
    content = (
        b"# Company credentials\n\nAcme Cyber LLC holds CREST Penetration Testing "
        b"certificate CR-8871.\n"
    )

    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        version = ingestion.register_upload(
            s,
            ctx,
            data=content,
            filename="company-profile.md",
            side=DocumentSide.COMPANY,
            confidentiality_class=ConfidentialityClass.INTERNAL,
            scanner=scanner,
            storage=storage,
        )
        ingestion.parse_and_index(
            s,
            ctx,
            version_id=version.id,
            storage=storage,
            embedder=embedder,
            knowledge_gateway=gateway,
        )

        entities = list(
            s.execute(
                select(KnowledgeEntity).where(
                    KnowledgeEntity.org_id == org,
                    KnowledgeEntity.canonical_name.in_([company_name, certification_name]),
                )
            ).scalars()
        )
        entity_ids = {entity.id for entity in entities}
        facts = list(
            s.execute(
                select(KnowledgeFact).where(KnowledgeFact.entity_id.in_(entity_ids))
            ).scalars()
        )
        relations = list(
            s.execute(
                select(KnowledgeRelation).where(
                    KnowledgeRelation.subject_entity_id.in_(entity_ids),
                    KnowledgeRelation.object_entity_id.in_(entity_ids),
                )
            ).scalars()
        )
        evidence = list(
            s.execute(
                select(Evidence).where(Evidence.source_document_version_id == version.id)
            ).scalars()
        )
        links = list(
            s.execute(
                select(EvidenceLink).where(EvidenceLink.evidence_id == evidence[0].id)
            ).scalars()
        )
        subject_ids = entity_ids | {facts[0].id, relations[0].id, evidence[0].id}
        reviews = list(
            s.execute(
                select(KnowledgeReviewItem).where(
                    KnowledgeReviewItem.org_id == org,
                    KnowledgeReviewItem.kind == ReviewQueueKind.PROPOSED,
                    KnowledgeReviewItem.subject_id.in_(subject_ids),
                )
            ).scalars()
        )

        assert len(entities) == 2
        assert len(facts) == 1
        assert len(relations) == 1
        assert len(evidence) == 1, "one cited chunk should create one reusable evidence row"
        assert {link.target_type for link in links} == {
            EvidenceLinkTarget.KNOWLEDGE_ENTITY,
            EvidenceLinkTarget.KNOWLEDGE_FACT,
            EvidenceLinkTarget.KNOWLEDGE_RELATION,
        }
        assert all(entity.status is KnowledgeStatus.PROPOSED for entity in entities)
        assert all(entity.approval_status is ApprovalStatus.PENDING_APPROVAL for entity in entities)
        assert facts[0].status is KnowledgeStatus.PROPOSED
        assert relations[0].status is KnowledgeStatus.PROPOSED
        assert evidence[0].status is KnowledgeStatus.PROPOSED
        assert evidence[0].approval_status is ApprovalStatus.PENDING_APPROVAL
        assert evidence[0].approved_for_reuse is False
        assert evidence[0].source_locator["document_version_id"] == str(version.id)
        assert evidence[0].source_locator["char_start"] == 0
        assert len(reviews) == 5, "entities, fact, relation, and evidence all require review"
        usage = s.execute(
            select(ModelUsage).where(
                ModelUsage.call_type == "knowledge_extraction",
                ModelUsage.request_id == ctx.request_id,
            )
        ).scalar_one()
        assert usage.prompt_version == "v1"

        # Completion is idempotent: re-entering the pipeline does not call the
        # model or duplicate graph/evidence rows.
        before_calls = len(gateway.llm.calls)
        ingestion.parse_and_index(
            s,
            ctx,
            version_id=version.id,
            storage=storage,
            embedder=embedder,
            knowledge_gateway=gateway,
        )
        assert len(gateway.llm.calls) == before_calls
        assert (
            s.execute(select(Evidence).where(Evidence.source_document_version_id == version.id))
            .scalars()
            .all()
            == evidence
        )
        s.commit()


def test_reparse_replaces_the_previous_parse_rather_than_duplicating_it(
    app_sessionmaker, ingest_env
):
    """A parser improvement has to reach documents that are already stored.

    Blocks and chunks are keyed by (version, ordinal), so a second parse that
    does not first discard the old output collides on the unique index — and
    without a re-parse path the only way to benefit from a parser fix is to
    upload the same file again and live with a duplicate in the bid.
    """
    org, storage, _scanner, embedder = ingest_env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)

        version = _ingest(s, ctx, ingest_env, RFP_MARKDOWN, "scope.md")
        s.refresh(version)
        first_blocks, first_chunks = version.block_count, version.chunk_count
        assert first_blocks and first_chunks

        again = ingestion.parse_and_index(
            s,
            ctx,
            version_id=version.id,
            storage=storage,
            embedder=embedder,
            knowledge_gateway=ModelGateway(
                NullLLMProvider(canned='{"entities": [], "facts": [], "relations": []}')
            ),
            force=True,
        )
        s.refresh(again)

        assert again.state is DocumentVersionState.INDEXED
        assert again.block_count == first_blocks, "re-parse must replace, not append"
        assert again.chunk_count == first_chunks

        rows = s.execute(
            select(DocumentBlock).where(DocumentBlock.document_version_id == version.id)
        ).scalars().all()
        assert len(rows) == first_blocks, "the previous parse's blocks must be gone"
        s.commit()


def test_parse_without_force_leaves_an_indexed_version_untouched(
    app_sessionmaker, ingest_env
):
    """The default path stays idempotent, so a retried job costs nothing."""
    org, *_ = ingest_env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)

        version = _ingest(s, ctx, ingest_env, RFP_MARKDOWN, "scope.md")
        s.refresh(version)
        block_ids = {
            row.id
            for row in s.execute(
                select(DocumentBlock).where(DocumentBlock.document_version_id == version.id)
            ).scalars()
        }

        ingestion.parse_and_index(s, ctx, version_id=version.id)
        after = {
            row.id
            for row in s.execute(
                select(DocumentBlock).where(DocumentBlock.document_version_id == version.id)
            ).scalars()
        }
        assert after == block_ids, "an indexed version must not be re-parsed by default"
        s.commit()
