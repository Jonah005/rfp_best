"""The chat is told what the person is looking at, and never mixes one thing with another."""

from __future__ import annotations

import pytest

from app.core.context import ExecutionContext
from app.core.errors import ValidationError
from app.db.session import apply_tenant_context
from app.gateway.embeddings import HashingEmbeddingProvider
from app.gateway.gateway import ModelGateway
from app.services import copilot
from app.services import tasks as task_service
from tests.integration import test_copilot as _copilot_tests
from tests.integration.test_assessment_readiness import _company_version, _requirement
from tests.integration.test_copilot import ScriptedProvider, _scene, user_ctx

# The same organisation and storage fixtures the copilot tests use.
fresh_org = _copilot_tests.fresh_org
env = _copilot_tests.env

pytestmark = pytest.mark.integration


def _two_requirement_task(s, ctx, env, dept, user, bid):
    version = _company_version(s, ctx, env)
    r1 = _requirement(s, ctx, bid=bid, doc_version=version, code="R-1", text="Encrypt data at rest")
    r2 = _requirement(s, ctx, bid=bid, doc_version=version, code="R-2", text="Provide a rota")
    task = task_service.create_task(
        s,
        ctx,
        bid_id=bid.id,
        title="Mixed",
        department_id=dept.id,
        owner_user_id=user.id,
        requirement_ids=[r1.id, r2.id],
    )
    return task, r1, r2


def _answer_prompt(provider, question):
    """The prompt the answering call was given (not the diagram router's)."""
    hits = [p for p in provider.prompts if question in p and "AUTHORIZED CONTEXT" in p]
    assert hits, "the answering call was never made"
    return hits[0]


def _ask(s, ctx, conversation, provider, question, focus=None):
    gateway = ModelGateway(provider, HashingEmbeddingProvider())
    return copilot.ask(
        s,
        ctx,
        conversation_id=conversation.id,
        question=question,
        gateway=gateway,
        embedder=HashingEmbeddingProvider(),
        focus=focus,
    )


def test_each_thing_has_its_own_thread_and_nothing_leaks_between_them(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        dept, user, _o, bid, _t = _scene(s, ctx, env)
        task, r1, r2 = _two_requirement_task(s, ctx, env, dept, user, bid)
        me = user_ctx(org, user.id)
        conv = copilot.create_conversation(s, me, task_id=task.id)
        provider = ScriptedProvider("Answer text.")

        _ask(s, me, conv, provider, "GENERAL-QUESTION", None)
        _ask(
            s,
            me,
            conv,
            provider,
            "ABOUT-ONE",
            {"kind": "requirements", "requirement_ids": [str(r1.id)]},
        )
        _ask(
            s,
            me,
            conv,
            provider,
            "ABOUT-TWO",
            {"kind": "requirements", "requirement_ids": [str(r2.id)]},
        )
        prompt_two = _answer_prompt(provider, "ABOUT-TWO")
        assert "ABOUT-ONE" not in prompt_two and "GENERAL-QUESTION" not in prompt_two
        focus_part = prompt_two[prompt_two.index("FOCUS:") :]
        assert "Provide a rota" in focus_part and "Encrypt data at rest" not in focus_part

        _ask(
            s,
            me,
            conv,
            provider,
            "ABOUT-ONE-AGAIN",
            {"kind": "requirements", "requirement_ids": [str(r1.id)]},
        )
        prompt_again = _answer_prompt(provider, "ABOUT-ONE-AGAIN")
        # Back on the first requirement it remembers that thread, and only that thread.
        assert "ABOUT-ONE\n" in prompt_again or "user: ABOUT-ONE" in prompt_again
        assert "ABOUT-TWO" not in prompt_again and "GENERAL-QUESTION" not in prompt_again

        _ask(s, me, conv, provider, "BACK-TO-GENERAL", None)

        one = copilot.messages(s, me, conversation_id=conv.id, focus_key="req:" + str(r1.id))
        assert [m.content for m in one if m.role.value == "USER"] == [
            "ABOUT-ONE",
            "ABOUT-ONE-AGAIN",
        ]
        general = copilot.messages(s, me, conversation_id=conv.id, focus_key=None)
        assert {m.content for m in general if m.role.value == "USER"} == {
            "GENERAL-QUESTION",
            "BACK-TO-GENERAL",
        }
        s.commit()


def test_a_focus_that_is_not_on_the_task_is_refused_and_nothing_is_recorded(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        dept, user, _o, bid, other_task = _scene(s, ctx, env)
        task, r1, _r2 = _two_requirement_task(s, ctx, env, dept, user, bid)
        version = _company_version(s, ctx, env)
        stranger = _requirement(
            s, ctx, bid=bid, doc_version=version, code="R-9", text="Not on this task"
        )
        me = user_ctx(org, user.id)
        conv = copilot.create_conversation(s, me, task_id=task.id)
        provider = ScriptedProvider()

        with pytest.raises(ValidationError):
            _ask(
                s,
                me,
                conv,
                provider,
                "Q",
                {"kind": "requirements", "requirement_ids": [str(stranger.id)]},
            )
        with pytest.raises(ValidationError):
            _ask(s, me, conv, provider, "Q", {"kind": "section", "section": "Nope"})
        assert copilot.messages(s, me, conversation_id=conv.id) == []
        assert provider.prompts == []
        _ = other_task
        s.commit()


def test_missing_parts_chat_is_its_own_thread(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        dept, user, _o, bid, _t = _scene(s, ctx, env)
        task, r1, _r2 = _two_requirement_task(s, ctx, env, dept, user, bid)
        me = user_ctx(org, user.id)
        conv = copilot.create_conversation(s, me, task_id=task.id)
        provider = ScriptedProvider("Answer text.")

        _ask(s, me, conv, provider, "GENERAL-QUESTION", None)
        _ask(s, me, conv, provider, "ABOUT-MISSING", {"kind": "gaps"})
        prompt = _answer_prompt(provider, "ABOUT-MISSING")
        assert "GENERAL-QUESTION" not in prompt
        assert "RFP missing" in prompt[prompt.index("FOCUS:") :]

        mine = copilot.messages(s, me, conversation_id=conv.id, focus_key="gaps")
        assert [m.content for m in mine if m.role.value == "USER"] == ["ABOUT-MISSING"]
        general = copilot.messages(s, me, conversation_id=conv.id, focus_key=None)
        assert "ABOUT-MISSING" not in {m.content for m in general}
        s.commit()
