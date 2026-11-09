"""An identical question is asked once, and only when it is identical.

Assessment is re-run constantly — approve three more documents, run it again —
and every run re-pays for every requirement whose inputs did not change. The
call types this cache serves are deterministic reads at temperature zero, so the
second answer is the first answer, bought twice.

That reuse is only safe if the key is exact. A cache that answers a question
slightly different from the one asked is worse than no cache at all, because the
wrongness is invisible: the matrix looks the same, the reasoning trace looks the
same, and the excerpt it was actually judging is gone. Most of these cases are
therefore about what must MISS.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.gateway.deepseek import NullLLMProvider
from app.gateway.gateway import ModelGateway
from app.gateway.registry import CallType, register
from app.models import ModelResultCache
from app.schemas.evidence_relevance import EvidenceRelevanceResult

pytestmark = pytest.mark.integration

CANNED = '{"verdicts": [{"candidate": 1, "verdict": "SUPPORTS", "reason": "ok"}]}'


@pytest.fixture(scope="module")
def cached_call_type():
    """A call type of this module's own, so registry order cannot affect it."""
    return register(
        CallType(
            name="test_cached_read",
            prompt_version="v1",
            system_prompt="Read the shortlist.",
            response_model=EvidenceRelevanceResult,
            temperature=0.0,
            max_output_tokens=256,
        )
    )


@pytest.fixture
def org(owner_engine):
    from sqlalchemy.orm import sessionmaker

    from app.models import Organization

    org_id = uuid.uuid4()
    Owner = sessionmaker(bind=owner_engine, future=True)
    with Owner() as s:
        s.add(
            Organization(
                id=org_id, name=f"Cache tenant {org_id.hex[:6]}", slug=f"cache-{org_id.hex[:10]}"
            )
        )
        s.commit()
    return org_id


def _gateway():
    return ModelGateway(NullLLMProvider(CANNED))


def _ask(gateway, session, ctx, content: str, *, call_type="test_cached_read", cache=True):
    return gateway.generate_structured(call_type, content, ctx=ctx, session=session, cache=cache)


# --- the saving --------------------------------------------------------------


def test_the_same_question_is_only_paid_for_once(app_sessionmaker, org, cached_call_type):
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)

        first = _gateway()
        _ask(first, s, ctx, "requirement A / shortlist A")
        assert len(first.llm.calls) == 1
        assert first.last_call_cached is False

        second = _gateway()
        result = _ask(second, s, ctx, "requirement A / shortlist A")

        assert second.llm.calls == [], "the provider must not be called again"
        assert second.last_call_cached is True
        # And the answer is the answer, not an empty stand-in.
        assert result.verdicts[0].verdict.value == "SUPPORTS"
        s.rollback()


def test_opting_out_writes_nothing(app_sessionmaker, org, cached_call_type):
    """A call type that is not a deterministic read must never be cached."""
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)

        gateway = _gateway()
        _ask(gateway, s, ctx, "uncached question", cache=False)

        rows = s.query(ModelResultCache).filter(ModelResultCache.org_id == org).count()
        assert rows == 0
        s.rollback()


# --- what must miss ----------------------------------------------------------


def test_a_changed_question_is_asked_again(app_sessionmaker, org, cached_call_type):
    """The case that makes the cache safe: a different shortlist is a miss."""
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)

        first = _gateway()
        _ask(first, s, ctx, "requirement A / shortlist A")

        second = _gateway()
        _ask(second, s, ctx, "requirement A / shortlist B")

        assert len(second.llm.calls) == 1
        assert second.last_call_cached is False
        s.rollback()


def test_a_narrowed_context_is_a_different_question(app_sessionmaker, org, cached_call_type):
    """`extra_system` is how a caller restricts what the model may use.

    If it were left out of the key, a narrowed call would be answered from a
    wider call's cached reply — the exact shape of a context-isolation bug, and
    invisible from the output.
    """
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)

        wide = _gateway()
        wide.generate_structured("test_cached_read", "question", ctx=ctx, session=s, cache=True)

        narrow = _gateway()
        narrow.generate_structured(
            "test_cached_read",
            "question",
            ctx=ctx,
            session=s,
            cache=True,
            extra_system="Use only evidence E1.",
        )

        assert len(narrow.llm.calls) == 1, "a narrowed call must not reuse a wider answer"
        s.rollback()


def test_a_prompt_version_change_invalidates_the_entry(app_sessionmaker, org, cached_call_type):
    """Editing a prompt must not serve answers to a question no longer asked."""
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)

        gateway = _gateway()
        _ask(gateway, s, ctx, "requirement A / shortlist A")

        row = (
            s.query(ModelResultCache)
            .filter(ModelResultCache.org_id == org)
            .order_by(ModelResultCache.created_at.desc())
            .first()
        )
        assert row is not None
        # Simulate the prompt being edited: the stored entry now belongs to a
        # version nobody asks for.
        row.prompt_version = "v0"
        s.flush()

        again = _gateway()
        _ask(again, s, ctx, "requirement A / shortlist A")
        assert len(again.llm.calls) == 1
        s.rollback()


def test_a_corrupt_entry_is_a_miss_not_a_failure(app_sessionmaker, org, cached_call_type):
    """A schema change can strand entries. Assessment must not break for them."""
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)

        gateway = _gateway()
        _ask(gateway, s, ctx, "requirement A / shortlist A")

        row = s.query(ModelResultCache).filter(ModelResultCache.org_id == org).first()
        row.response = {"verdicts": [{"candidate": "not-an-integer"}]}
        s.flush()

        again = _gateway()
        result = _ask(again, s, ctx, "requirement A / shortlist A")
        assert len(again.llm.calls) == 1
        assert result.verdicts[0].verdict.value == "SUPPORTS"
        s.rollback()


# --- isolation ---------------------------------------------------------------


def test_one_tenants_answer_is_never_served_to_another(
    app_sessionmaker, owner_engine, org, cached_call_type
):
    """The key is a hash of that tenant's own tender and evidence text.

    Two organizations can hold identical boilerplate — the same standard clause
    in the same template — so an identical key across tenants is not a freak
    case, and row-level security is what stops it becoming a leak.
    """
    from sqlalchemy.orm import sessionmaker

    from app.models import Organization

    other_id = uuid.uuid4()
    Owner = sessionmaker(bind=owner_engine, future=True)
    with Owner() as s:
        s.add(
            Organization(
                id=other_id, name=f"Other {other_id.hex[:6]}", slug=f"other-{other_id.hex[:10]}"
            )
        )
        s.commit()

    shared_question = "a standard clause both companies received"

    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _ask(_gateway(), s, ctx, shared_question)
        s.commit()

    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=other_id)
        apply_tenant_context(s, ctx)
        gateway = _gateway()
        _ask(gateway, s, ctx, shared_question)
        assert len(gateway.llm.calls) == 1, "the other tenant's entry must be invisible"
        s.rollback()
