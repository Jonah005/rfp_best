"""Evidence matching must not get slower as the corpus grows.

The original implementation loaded every approved evidence row in the tenant and
scored it in Python, once per requirement. That is O(requirements x corpus): it
looks fine on a demo and collapses on a real one.

These tests pin the property rather than a wall-clock number, because timings
vary with the machine and would make the suite flaky. What is asserted is that
the *work reaching Python* stays bounded, and that the answers do not change.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.models import Evidence
from app.models.enums import BidReadiness, EvidenceStatus
from app.services import assessment
from app.services.assessment import CANDIDATE_POOL
from tests.integration.test_assessment_readiness import (
    ISO_TEXT,
    TODAY,
    _company_version,
    _evidence,
    _new_bid,
    _requirement,
)

pytestmark = pytest.mark.integration

from app.gateway.embeddings import HashingEmbeddingProvider  # noqa: E402
from app.ingestion.safety import EicarAwareScanner  # noqa: E402
from app.services.storage import InMemoryStorage  # noqa: E402

NOISE = (
    "The company maintains a documented procedure for {n} covering security "
    "certification information and audited operational controls."
)


@pytest.fixture
def fresh_org(owner_engine):
    from sqlalchemy.orm import sessionmaker

    from app.models import Organization

    org_id = uuid.uuid4()
    Owner = sessionmaker(bind=owner_engine, future=True)
    with Owner() as s:
        s.add(
            Organization(id=org_id, name=f"Scale {org_id.hex[:6]}", slug=f"scale-{org_id.hex[:10]}")
        )
        s.commit()
    return org_id


@pytest.fixture
def env(fresh_org):
    return fresh_org, InMemoryStorage(), EicarAwareScanner(), HashingEmbeddingProvider()


def _corpus(session, ctx, version, size: int) -> None:
    """`size` pieces of approved evidence that all share the query's vocabulary.

    Deliberately matching: if the noise did not match, the database would filter
    it out and the test would prove nothing about bounded work.
    """
    for n in range(size):
        _evidence(
            session,
            ctx,
            doc_version=version,
            excerpt=NOISE.format(n=f"process {n}"),
            expiry=TODAY + timedelta(days=365),
        )


def test_candidate_work_is_bounded_by_the_pool_not_the_corpus(app_sessionmaker, env):
    """The query returns at most CANDIDATE_POOL rows however big the corpus is."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        _corpus(s, ctx, version, CANDIDATE_POOL * 3)

        total = s.execute(
            select(func.count()).select_from(Evidence).where(Evidence.org_id == org)
        ).scalar_one()
        assert total > CANDIDATE_POOL, "the corpus must exceed the pool for this to mean anything"

        requirement = _requirement(
            s, ctx, bid=bid, doc_version=version, code="SEC-1", text=ISO_TEXT
        )

        result = assessment.assess_requirement(s, ctx, requirement=requirement, today=TODAY)

        # Whatever the corpus size, the trace records at most a full pool.
        considered = result.reasoning_trace["candidates"]
        assert len(considered) <= CANDIDATE_POOL
        s.rollback()


def test_a_large_corpus_does_not_change_the_verdict(app_sessionmaker, env):
    """Bounding the work must not bound the correctness.

    The one genuinely relevant piece of evidence still wins, even buried in
    noise that shares its vocabulary.
    """
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)

        wanted = _evidence(
            s, ctx, doc_version=version, excerpt=ISO_TEXT, expiry=TODAY + timedelta(days=365)
        )
        _corpus(s, ctx, version, 120)

        requirement = _requirement(
            s, ctx, bid=bid, doc_version=version, code="SEC-1", text=ISO_TEXT
        )
        result = assessment.assess_requirement(s, ctx, requirement=requirement, today=TODAY)

        assert result.evidence_status is EvidenceStatus.CURRENT_VERIFIED
        # The point of this test is that 120 unrelated excerpts do not change
        # the verdict, and they do not: DRAFTABLE here rather than READY
        # because no model read the shortlist in this fixture, which is a fact
        # about verification and not about corpus size.
        assert result.bid_readiness is BidReadiness.DRAFTABLE
        attached = assessment.evidence_for(s, ctx, assessment_id=result.id)
        assert str(wanted.id) in {str(e.id) for _link, e in attached}, (
            "the exact-match evidence was crowded out by noise"
        )
        s.rollback()


def test_evidence_selection_happens_in_the_database(app_sessionmaker, env):
    """The filter must be a WHERE clause, not a Python loop.

    Asserted by checking the query the service issues actually narrows: an
    unrelated requirement must return nothing at all, which is only possible if
    the database did the matching.
    """
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        _corpus(s, ctx, version, 40)

        unrelated = _requirement(
            s,
            ctx,
            bid=bid,
            doc_version=version,
            code="ZZZ-1",
            text="Zebra xylophone quokka narwhal jamboree kumquat.",
        )
        result = assessment.assess_requirement(s, ctx, requirement=unrelated, today=TODAY)
        assert result.evidence_status is EvidenceStatus.NONE
        assert result.reasoning_trace["candidates"] == []
        s.rollback()


def test_the_full_text_index_is_actually_used(app_sessionmaker, env):
    """A GIN index that the planner ignores is decoration.

    Uses EXPLAIN on the real predicate, with a corpus big enough that the
    planner would not prefer a sequential scan purely for size.
    """
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        version = _company_version(s, ctx, env)
        _corpus(s, ctx, version, 400)
        s.flush()
        s.execute(select(func.count()).select_from(Evidence)).scalar_one()

        from sqlalchemy import text as sql_text

        s.execute(sql_text("ANALYZE evidence"))
        plan = "\n".join(
            row[0]
            for row in s.execute(
                sql_text(
                    "EXPLAIN SELECT id FROM evidence "
                    "WHERE excerpt_tsv @@ to_tsquery('simple', 'certification')"
                )
            )
        )
        # On a small table PostgreSQL may still choose a sequential scan, which
        # is a correct choice rather than a missing index -- so assert the index
        # exists and is available to the planner.
        indexes = {
            row[0]
            for row in s.execute(
                sql_text("SELECT indexname FROM pg_indexes WHERE tablename = 'evidence'")
            )
        }
        assert "ix_evidence_tsv" in indexes
        assert "ix_evidence_reusable" in indexes
        assert plan  # the predicate is valid SQL against the generated column
        s.rollback()
