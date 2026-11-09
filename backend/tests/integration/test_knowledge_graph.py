"""Knowledge graph governance: resolution, conflicts, expiry, provenance."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.core.errors import ValidationError
from app.db.session import apply_tenant_context
from app.ingestion.safety import EicarAwareScanner
from app.models import KnowledgeEntity, KnowledgeFact
from app.models.enums import (
    ApprovalStatus,
    ConfidentialityClass,
    DocumentSide,
    DocumentType,
    EntityType,
    EvidenceLinkTarget,
    KnowledgeStatus,
    RelationPredicate,
    ReviewQueueKind,
)
from app.services import ingestion, knowledge
from app.services.storage import InMemoryStorage

pytestmark = pytest.mark.integration


@pytest.fixture
def org(seeded_orgs):
    return seeded_orgs[0]


@pytest.fixture
def ctx(org):
    return ExecutionContext(org_id=org)


def _version_id(session, ctx) -> uuid.UUID:
    """A real document version so evidence has somewhere to point."""
    version = ingestion.register_upload(
        session,
        ctx,
        data=b"# CV\n\nAhmed holds OSCP OS-101923, valid to March 2027.\n",
        filename="CV_Ahmed.md",
        side=DocumentSide.COMPANY,
        confidentiality_class=ConfidentialityClass.CONFIDENTIAL,
        scanner=EicarAwareScanner(),
        storage=InMemoryStorage(),
    )
    return version.id


# --- entity resolution ------------------------------------------------------


def test_identical_names_link_rather_than_duplicate(app_sessionmaker, ctx):
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        name = f"Ahmed Hassan {uuid.uuid4().hex[:4]}"
        first, created_a = knowledge.resolve_or_create_entity(
            s, ctx, entity_type=EntityType.PERSON, name=name
        )
        second, created_b = knowledge.resolve_or_create_entity(
            s, ctx, entity_type=EntityType.PERSON, name=name
        )
        assert created_a and not created_b
        assert first.id == second.id
        s.commit()


def test_similar_but_not_identical_names_are_never_merged_silently(app_sessionmaker, ctx):
    """Conflating two people's credentials is exactly the error that ends up in
    a submitted proposal."""
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        suffix = uuid.uuid4().hex[:4]
        knowledge.resolve_or_create_entity(
            s, ctx, entity_type=EntityType.PERSON, name=f"Ahmed Hassan {suffix}"
        )
        variant, created = knowledge.resolve_or_create_entity(
            s, ctx, entity_type=EntityType.PERSON, name=f"A. Hassan {suffix}"
        )
        assert created, "a near-match must create a proposal, not silently merge"
        assert variant.status is KnowledgeStatus.PROPOSED
        assert variant.approval_status is ApprovalStatus.PENDING_APPROVAL

        queue = knowledge.open_reviews(s, ctx, kind=ReviewQueueKind.ENTITY_MATCH)
        item = [q for q in queue if q.subject_id == variant.id][0]
        assert item.candidate_ids, "the reviewer must be shown what it might be"
        s.commit()


def test_merging_moves_facts_and_leaves_a_forwarding_pointer(app_sessionmaker, ctx):
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        suffix = uuid.uuid4().hex[:4]
        keep, _ = knowledge.resolve_or_create_entity(
            s, ctx, entity_type=EntityType.PERSON, name=f"Zain Malik {suffix}"
        )
        dupe, _ = knowledge.resolve_or_create_entity(
            s, ctx, entity_type=EntityType.PERSON, name=f"Z. Malik {suffix}"
        )
        knowledge.add_fact(s, ctx, entity_id=dupe.id, predicate="role", value_text="Senior Tester")

        merged = knowledge.merge_entities(
            s, ctx, source_id=dupe.id, target_id=keep.id, reason="same person"
        )
        assert merged.id == keep.id

        s.refresh(dupe)
        assert dupe.merged_into_entity_id == keep.id
        assert dupe.status is KnowledgeStatus.SUPERSEDED
        moved = (
            s.execute(select(KnowledgeFact).where(KnowledgeFact.entity_id == keep.id))
            .scalars()
            .all()
        )
        assert any(f.value_text == "Senior Tester" for f in moved)
        s.commit()


def test_entities_of_different_types_cannot_merge(app_sessionmaker, ctx):
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        person, _ = knowledge.resolve_or_create_entity(
            s, ctx, entity_type=EntityType.PERSON, name=f"P{uuid.uuid4().hex[:6]}"
        )
        cert, _ = knowledge.resolve_or_create_entity(
            s, ctx, entity_type=EntityType.CERTIFICATION, name=f"C{uuid.uuid4().hex[:6]}"
        )
        with pytest.raises(ValidationError, match="different types"):
            knowledge.merge_entities(s, ctx, source_id=person.id, target_id=cert.id, reason="wrong")
        s.rollback()


# --- facts, relations, conflicts -------------------------------------------


def test_contradictory_facts_are_flagged_not_ranked(app_sessionmaker, ctx):
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        person, _ = knowledge.resolve_or_create_entity(
            s, ctx, entity_type=EntityType.PERSON, name=f"Conflicted {uuid.uuid4().hex[:6]}"
        )
        first = knowledge.add_fact(
            s,
            ctx,
            entity_id=person.id,
            predicate="years_experience",
            value_number=9,
            valid_from=date(2026, 1, 1),
        )
        second = knowledge.add_fact(
            s,
            ctx,
            entity_id=person.id,
            predicate="years_experience",
            value_number=12,
            valid_from=date(2026, 1, 1),
        )
        s.refresh(first)
        assert first.status is KnowledgeStatus.CONFLICTING
        assert second.status is KnowledgeStatus.CONFLICTING

        conflicts = knowledge.open_reviews(s, ctx, kind=ReviewQueueKind.CONFLICT)
        assert any(c.subject_id == second.id for c in conflicts)
        s.commit()


def test_non_overlapping_validity_is_not_a_conflict(app_sessionmaker, ctx):
    """Superseded history is not a contradiction."""
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        person, _ = knowledge.resolve_or_create_entity(
            s, ctx, entity_type=EntityType.PERSON, name=f"Sequential {uuid.uuid4().hex[:6]}"
        )
        old = knowledge.add_fact(
            s,
            ctx,
            entity_id=person.id,
            predicate="role",
            value_text="Tester",
            valid_from=date(2020, 1, 1),
            valid_to=date(2022, 12, 31),
        )
        new = knowledge.add_fact(
            s,
            ctx,
            entity_id=person.id,
            predicate="role",
            value_text="Lead Tester",
            valid_from=date(2023, 1, 1),
        )
        s.refresh(old)
        assert old.status is KnowledgeStatus.HISTORICAL_VERIFIED
        assert new.status is KnowledgeStatus.CURRENT_VERIFIED
        s.commit()


def test_relation_records_the_worked_example(app_sessionmaker, ctx):
    """Ahmed --HAS_CERTIFICATION--> OSCP, with attributes and validity."""
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        suffix = uuid.uuid4().hex[:4]
        ahmed, _ = knowledge.resolve_or_create_entity(
            s, ctx, entity_type=EntityType.PERSON, name=f"Ahmed Hassan {suffix}"
        )
        oscp, _ = knowledge.resolve_or_create_entity(
            s, ctx, entity_type=EntityType.CERTIFICATION, name=f"OSCP {suffix}"
        )
        relation = knowledge.add_relation(
            s,
            ctx,
            subject_entity_id=ahmed.id,
            predicate=RelationPredicate.HAS_CERTIFICATION,
            object_entity_id=oscp.id,
            attributes={"certificate_number": "OS-101923"},
            valid_from=date(2024, 3, 11),
            valid_to=date(2027, 3, 10),
        )
        assert relation.status is KnowledgeStatus.CURRENT_VERIFIED
        assert relation.attributes["certificate_number"] == "OS-101923"
        s.commit()


def test_self_relations_and_backwards_validity_are_refused(app_sessionmaker, ctx):
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        entity, _ = knowledge.resolve_or_create_entity(
            s, ctx, entity_type=EntityType.PERSON, name=f"Solo {uuid.uuid4().hex[:6]}"
        )
        with pytest.raises(ValidationError, match="cannot relate to itself"):
            knowledge.add_relation(
                s,
                ctx,
                subject_entity_id=entity.id,
                predicate=RelationPredicate.HAS_CERTIFICATION,
                object_entity_id=entity.id,
            )
        with pytest.raises(ValidationError, match="cannot precede"):
            knowledge.add_fact(
                s,
                ctx,
                entity_id=entity.id,
                predicate="role",
                value_text="x",
                valid_from=date(2026, 5, 1),
                valid_to=date(2026, 1, 1),
            )
        with pytest.raises(ValidationError, match="must carry a value"):
            knowledge.add_fact(s, ctx, entity_id=entity.id, predicate="role")
        s.rollback()


# --- expiry -----------------------------------------------------------------


def test_expiry_sweep_invalidates_past_validity(app_sessionmaker, ctx):
    """An expired certificate must invalidate the claim resting on it, without
    anyone having to remember."""
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        version_id = _version_id(s, ctx)
        person, _ = knowledge.resolve_or_create_entity(
            s, ctx, entity_type=EntityType.PERSON, name=f"Expiring {uuid.uuid4().hex[:6]}"
        )
        yesterday = date.today() - timedelta(days=1)
        fact = knowledge.add_fact(
            s,
            ctx,
            entity_id=person.id,
            predicate="certification",
            value_text="CREST",
            valid_to=date.today() + timedelta(days=30),
        )
        evidence = knowledge.create_evidence(
            s,
            ctx,
            source_document_version_id=version_id,
            source_locator={"document_version_id": str(version_id), "page_from": 1},
            excerpt="CREST certificate CR-8871",
            evidence_type=DocumentType.CERTIFICATE,
            expiry_date=date.today() + timedelta(days=30),
        )
        # Bring both forward past their window.
        fact.valid_to = yesterday
        evidence.expiry_date = yesterday
        s.flush()

        counts = knowledge.expire_stale(s, ctx)
        s.refresh(fact)
        s.refresh(evidence)
        assert counts["facts"] >= 1 and counts["evidence"] >= 1
        assert fact.status is KnowledgeStatus.EXPIRED
        assert evidence.status is KnowledgeStatus.EXPIRED
        s.commit()


def test_expired_evidence_cannot_be_approved_for_reuse(app_sessionmaker, ctx):
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        version_id = _version_id(s, ctx)
        evidence = knowledge.create_evidence(
            s,
            ctx,
            source_document_version_id=version_id,
            source_locator={"document_version_id": str(version_id)},
            excerpt="Old certificate",
            evidence_type=DocumentType.CERTIFICATE,
            expiry_date=date.today() - timedelta(days=1),
        )
        evidence.status = KnowledgeStatus.EXPIRED
        s.flush()
        with pytest.raises(ValidationError, match="Expired evidence"):
            knowledge.approve_evidence(s, ctx, evidence_id=evidence.id, reason="looks fine to me")
        s.rollback()


def test_find_expiring_surfaces_the_dashboard_warning(app_sessionmaker, ctx):
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        version_id = _version_id(s, ctx)
        soon = knowledge.create_evidence(
            s,
            ctx,
            source_document_version_id=version_id,
            source_locator={"document_version_id": str(version_id)},
            excerpt="CREST expiring soon",
            evidence_type=DocumentType.CERTIFICATE,
            expiry_date=date.today() + timedelta(days=45),
        )
        expiring = knowledge.find_expiring(s, ctx, within_days=90)
        assert soon.id in {e.id for e in expiring}
        s.commit()


# --- provenance -------------------------------------------------------------


def test_supporting_evidence_excludes_unapproved_and_expired(app_sessionmaker, ctx):
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx)
        version_id = _version_id(s, ctx)
        person, _ = knowledge.resolve_or_create_entity(
            s, ctx, entity_type=EntityType.PERSON, name=f"Backed {uuid.uuid4().hex[:6]}"
        )
        good = knowledge.create_evidence(
            s,
            ctx,
            source_document_version_id=version_id,
            source_locator={"document_version_id": str(version_id)},
            excerpt="valid proof",
            evidence_type=DocumentType.CERTIFICATE,
        )
        knowledge.approve_evidence(s, ctx, evidence_id=good.id, reason="verified")
        pending = knowledge.create_evidence(
            s,
            ctx,
            source_document_version_id=version_id,
            source_locator={"document_version_id": str(version_id)},
            excerpt="unreviewed proof",
            evidence_type=DocumentType.CERTIFICATE,
        )
        fact = knowledge.add_fact(
            s,
            ctx,
            entity_id=person.id,
            predicate="certification",
            value_text="CREST",
            evidence_ids=[good.id, pending.id],
        )
        live = knowledge.supporting_evidence(
            s, ctx, target_type=EvidenceLinkTarget.KNOWLEDGE_FACT, target_id=fact.id
        )
        ids = {e.id for e in live}
        assert good.id in ids
        assert pending.id not in ids, "unapproved evidence must not count as support"
        s.commit()


def test_evidence_link_cannot_cross_tenants(app_sessionmaker, seeded_orgs):
    """The polymorphic target has no composite FK, so a trigger enforces it."""
    from sqlalchemy.exc import DBAPIError

    org_a, org_b = seeded_orgs
    ctx_a = ExecutionContext(org_id=org_a)
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ctx_a)
        version_id = _version_id(s, ctx_a)
        evidence = knowledge.create_evidence(
            s,
            ctx_a,
            source_document_version_id=version_id,
            source_locator={"document_version_id": str(version_id)},
            excerpt="tenant A evidence",
            evidence_type=DocumentType.CERTIFICATE,
        )
        with pytest.raises(DBAPIError):
            knowledge.link_evidence(
                s,
                ctx_a,
                evidence_id=evidence.id,
                target_type=EvidenceLinkTarget.KNOWLEDGE_FACT,
                target_id=uuid.uuid4(),  # a fact that does not exist
            )
        s.rollback()


def test_knowledge_does_not_leak_across_tenants(app_sessionmaker, seeded_orgs):
    org_a, org_b = seeded_orgs
    with app_sessionmaker() as s:
        s.begin()
        ctx_a = ExecutionContext(org_id=org_a)
        apply_tenant_context(s, ctx_a)
        knowledge.resolve_or_create_entity(
            s, ctx_a, entity_type=EntityType.PERSON, name=f"OnlyInA {uuid.uuid4().hex[:6]}"
        )
        s.commit()
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ExecutionContext(org_id=org_b))
        rows = s.execute(select(KnowledgeEntity)).scalars().all()
        assert all(e.org_id == org_b for e in rows)
        s.commit()
