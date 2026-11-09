"""Phase J: the task copilot, and the boundary it is not allowed to cross.

The model is scripted throughout. These tests are about what the copilot is
*shown* and what is *recorded*, not about answer quality -- because what it is
shown is the security property, and answer quality is not.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.core.errors import PermissionDenied, ValidationError
from app.db.session import apply_tenant_context
from app.gateway.base import LLMResponse
from app.gateway.deepseek import NullLLMProvider
from app.gateway.embeddings import HashingEmbeddingProvider
from app.gateway.gateway import ModelGateway
from app.ingestion.safety import EicarAwareScanner
from app.models import ChatMessage, Department, TaskConversation, User
from app.models.enums import (
    ChatRole,
    ConfidentialityClass,
    DepartmentCode,
    DocumentSide,
    GrantResourceType,
    ParticipantRole,
    TaskState,
)
from app.services import access_grants, context_builder, copilot, ingestion
from app.services import tasks as task_service
from app.services.storage import InMemoryStorage
from tests.integration.test_assessment_readiness import _new_bid

pytestmark = pytest.mark.integration

# A contributor's ordinary read permissions. Retrieval admissibility is decided
# from these, so a context with none is a context that sees nothing -- which is
# correct behaviour, and not what these tests are exercising.
CONTRIBUTOR_PERMS = frozenset({"document:read", "knowledge:read", "evidence:read"})


def user_ctx(org_id, user_id):
    return ExecutionContext(org_id=org_id, actor_user_id=user_id, permissions=CONTRIBUTOR_PERMS)


class ScriptedProvider(NullLLMProvider):
    """Echoes a fixed answer and keeps every prompt it was given."""

    model_id = "scripted-copilot"

    def __init__(self, answer: str = "The company holds ISO 27001 [S1].") -> None:
        super().__init__()
        self.answer = answer
        self.prompts: list[str] = []

    def generate(self, messages, *, temperature=None, max_tokens=None, timeout=None):
        self.prompts.append("\n".join(m.content for m in messages))
        return LLMResponse(text=self.answer, model_id=self.model_id, latency_ms=0)


def gateway_for(answer: str = "The company holds ISO 27001 [S1].") -> ModelGateway:
    return ModelGateway(ScriptedProvider(answer), HashingEmbeddingProvider())


PUBLIC_TEXT = b"""# Certifications

The company holds a current ISO 27001 information security certification
covering penetration testing services delivered from Dubai.
"""

RESTRICTED_TEXT = b"""# Staffing roster

Zain Malik is contracted at a day rate and is available from October.
"""


@pytest.fixture
def fresh_org(owner_engine):
    from sqlalchemy.orm import sessionmaker

    from app.models import Organization

    org_id = uuid.uuid4()
    Owner = sessionmaker(bind=owner_engine, future=True)
    with Owner() as s:
        s.add(
            Organization(
                id=org_id, name=f"Copilot {org_id.hex[:6]}", slug=f"copilot-{org_id.hex[:10]}"
            )
        )
        s.commit()
    return org_id


@pytest.fixture
def env(fresh_org):
    return fresh_org, InMemoryStorage(), EicarAwareScanner(), HashingEmbeddingProvider()


def _upload(s, ctx, env, *, data, filename, confidentiality):
    _org, storage, scanner, embedder = env
    version = ingestion.register_upload(
        s,
        ctx,
        data=data,
        filename=filename,
        side=DocumentSide.COMPANY,
        bid_id=None,
        confidentiality_class=confidentiality,
        scanner=scanner,
        storage=storage,
    )
    ingestion.parse_and_index(
        s,
        ctx,
        version_id=version.id,
        storage=storage,
        embedder=embedder,
        knowledge_gateway=ModelGateway(NullLLMProvider(), HashingEmbeddingProvider()),
    )
    return version


def _scene(s, ctx, env, *, ceiling=ConfidentialityClass.CONFIDENTIAL):
    org = ctx.org_id
    dept = Department(org_id=org, code=DepartmentCode.SECURITY, name="Security")
    s.add(dept)
    s.flush()
    user = User(
        org_id=org, email=f"eng-{uuid.uuid4().hex[:8]}@example.com", display_name="Engineer"
    )
    outsider = User(
        org_id=org, email=f"out-{uuid.uuid4().hex[:8]}@example.com", display_name="Outsider"
    )
    s.add_all([user, outsider])
    s.flush()

    bid = _new_bid(s, ctx)
    task = task_service.create_task(
        s,
        ctx,
        bid_id=bid.id,
        title="Security requirements",
        department_id=dept.id,
        owner_user_id=user.id,
    )
    task.retrieval_scope = {**task.retrieval_scope, "max_confidentiality": ceiling.value}
    s.flush()
    return dept, user, outsider, bid, task


# --- membership and isolation ------------------------------------------------


def test_someone_not_on_the_task_gets_no_context_at_all(app_sessionmaker, env):
    """Organizational seniority is not task membership.

    If any bid manager could read every task's context, task isolation would be
    decoration.
    """
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _dept, user, outsider, _bid, task = _scene(s, ctx, env)
        _upload(
            s,
            ctx,
            env,
            data=PUBLIC_TEXT,
            filename="certs.md",
            confidentiality=ConfidentialityClass.INTERNAL,
        )

        owner_ctx = user_ctx(org, user.id)
        built = context_builder.build(
            s,
            owner_ctx,
            task_id=task.id,
            query="ISO 27001 certification",
            embedder=HashingEmbeddingProvider(),
        )
        assert built.sources, "the task's own owner should see admissible material"

        outsider_ctx = user_ctx(org, outsider.id)
        with pytest.raises(PermissionDenied, match="not on this task"):
            context_builder.build(
                s,
                outsider_ctx,
                task_id=task.id,
                query="ISO 27001 certification",
                embedder=HashingEmbeddingProvider(),
            )
        s.commit()


def test_a_conversation_is_private_to_its_person(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _dept, user, other, _bid, task = _scene(s, ctx, env)
        owner_ctx = user_ctx(org, user.id)
        task_service.add_participant(
            s,
            owner_ctx,
            task_id=task.id,
            user_id=other.id,
            participant_role=ParticipantRole.REVIEWER,
        )
        mine = copilot.create_conversation(s, owner_ctx, task_id=task.id)

        other_ctx = user_ctx(org, other.id)
        with pytest.raises(PermissionDenied, match="another person"):
            copilot.messages(s, other_ctx, conversation_id=mine.id)
        s.commit()


def test_reading_a_task_never_creates_a_conversation(app_sessionmaker, env):
    """A read that writes makes conversation counts meaningless."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _dept, user, _o, _bid, task = _scene(s, ctx, env)
        owner_ctx = user_ctx(org, user.id)

        # create_task with an owner already made one; a second read makes none.
        before = len(
            s.execute(select(TaskConversation).where(TaskConversation.task_id == task.id))
            .scalars()
            .all()
        )
        copilot.conversation_for(s, owner_ctx, task_id=task.id, user_id=user.id)
        after = len(
            s.execute(select(TaskConversation).where(TaskConversation.task_id == task.id))
            .scalars()
            .all()
        )
        assert before == after
        s.commit()


# --- the permission ceiling --------------------------------------------------


def test_the_task_ceiling_keeps_restricted_material_out_of_the_prompt(app_sessionmaker, env):
    """The scope is a ceiling, and the model never sees above it."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _dept, user, _o, _bid, task = _scene(s, ctx, env, ceiling=ConfidentialityClass.INTERNAL)
        _upload(
            s,
            ctx,
            env,
            data=PUBLIC_TEXT,
            filename="certs.md",
            confidentiality=ConfidentialityClass.INTERNAL,
        )
        _upload(
            s,
            ctx,
            env,
            data=RESTRICTED_TEXT,
            filename="roster.md",
            confidentiality=ConfidentialityClass.RESTRICTED,
        )

        owner_ctx = user_ctx(org, user.id)
        gateway = gateway_for()
        conversation = copilot.create_conversation(s, owner_ctx, task_id=task.id)
        copilot.ask(
            s,
            owner_ctx,
            conversation_id=conversation.id,
            question="Is Zain Malik available and at what day rate?",
            gateway=gateway,
            embedder=HashingEmbeddingProvider(),
        )
        joined = "\n".join(gateway.llm.prompts)
        # The question may mention a day rate; the roster's text must not appear.
        assert "Zain Malik is contracted" not in joined
        assert "available from October" not in joined
        # The public certification document was admissible, so this is a real
        # exclusion rather than an empty prompt proving nothing.
        assert "ISO 27001" in joined
        s.commit()


def test_out_of_scope_offers_the_three_requests_in_least_privilege_order(app_sessionmaker, env):
    """A flat refusal stops the bid. A route forward does not."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _dept, user, _o, _bid, task = _scene(s, ctx, env)
        owner_ctx = user_ctx(org, user.id)
        conversation = copilot.create_conversation(s, owner_ctx, task_id=task.id)

        gateway = gateway_for()
        answer = copilot.ask(
            s,
            owner_ctx,
            conversation_id=conversation.id,
            question="What is the day rate for the named tester?",
            gateway=gateway,
            embedder=HashingEmbeddingProvider(),
        )
        assert answer.out_of_scope
        assert answer.refusal is not None
        types = [o["request_type"] for o in answer.refusal["options"]]
        assert types == ["REQUEST_ANSWER", "REQUEST_EVIDENCE", "REQUEST_ACCESS"]
        assert answer.refusal["options"][0]["grants_access"] is False
        # The model never answered an out-of-scope question. The only call it saw was
        # the router deciding whether the message was about diagrams, and that sees
        # the message and the task's own requirements, never restricted material.
        answering = [p for p in gateway.llm.prompts if "router for a bid task's chat" not in p]
        assert answering == []
        # The trace records WHICH refusal, not merely that there was one.
        #
        # This asserted `"out_of_scope"` long after that stopped being a value
        # the code can produce. `out_of_scope` is the flag above -- the answer
        # is unusable -- and it has two opposite causes: NOT_PERMITTED ("you may
        # not see this") and NO_MATCH ("nothing on file covers it"). Telling
        # somebody the second while meaning the first sends them off to chase
        # material that does not exist, so the refusal was split, and the trace
        # has to say which one the screen actually showed.
        #
        # NOT_PERMITTED specifically: the rate card is RESTRICTED, so the day
        # rate exists and this task may not see it. A NO_MATCH here would be the
        # product lying about why it refused.
        assert answer.refusal["kind"] == "NOT_PERMITTED"
        assert answer.message.retrieval_trace["outcome"] == answer.refusal["kind"].lower()
        s.commit()


def test_a_revoked_grant_takes_effect_on_the_next_question(app_sessionmaker, env):
    """Context is rebuilt per message, so revocation is not a job-latency problem.

    A design that appended retrieved passages to a growing transcript would keep
    showing this material after the grant was revoked.
    """
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _dept, user, _o, _bid, task = _scene(s, ctx, env)
        _upload(
            s,
            ctx,
            env,
            data=PUBLIC_TEXT,
            filename="certs.md",
            confidentiality=ConfidentialityClass.INTERNAL,
        )
        owner_ctx = user_ctx(org, user.id)

        grant = access_grants.create_grant(
            s,
            owner_ctx,
            grantee_user_id=user.id,
            task_id=task.id,
            resource_type=GrantResourceType.EVIDENCE,
            resource_id=uuid.uuid4(),
            reason="For the certification answer.",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        first = context_builder.build(
            s,
            owner_ctx,
            task_id=task.id,
            query="ISO 27001 certification",
            embedder=HashingEmbeddingProvider(),
        )
        assert len(first.trace["live_grants"]) == 1

        access_grants.revoke_grant(s, owner_ctx, grant_id=grant.id, reason="No longer needed.")
        second = context_builder.build(
            s,
            owner_ctx,
            task_id=task.id,
            query="ISO 27001 certification",
            embedder=HashingEmbeddingProvider(),
        )
        assert second.trace["live_grants"] == []
        s.commit()


# --- what gets recorded ------------------------------------------------------


def test_every_answer_records_what_was_offered_and_what_was_cited(app_sessionmaker, env):
    """Two of eight sources cited is a different thing from two available."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _dept, user, _o, _bid, task = _scene(s, ctx, env)
        _upload(
            s,
            ctx,
            env,
            data=PUBLIC_TEXT,
            filename="certs.md",
            confidentiality=ConfidentialityClass.INTERNAL,
        )
        owner_ctx = user_ctx(org, user.id)
        conversation = copilot.create_conversation(s, owner_ctx, task_id=task.id)

        answer = copilot.ask(
            s,
            owner_ctx,
            conversation_id=conversation.id,
            question="Do we hold ISO 27001 certification?",
            gateway=gateway_for("Yes, a current ISO 27001 certificate [S1]."),
            embedder=HashingEmbeddingProvider(),
        )
        trace = answer.message.retrieval_trace
        assert trace["outcome"] == "answered"
        assert trace["query"]
        assert trace["sources_offered"]
        assert trace["sources_cited"] == ["S1"]
        assert trace["admissible_before_ranking"] >= 1
        assert answer.message.cited_chunk_ids
        assert answer.message.model_id == "scripted-copilot"
        s.commit()


def test_an_uncited_answer_records_no_citations_rather_than_inventing_them(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _dept, user, _o, _bid, task = _scene(s, ctx, env)
        _upload(
            s,
            ctx,
            env,
            data=PUBLIC_TEXT,
            filename="certs.md",
            confidentiality=ConfidentialityClass.INTERNAL,
        )
        owner_ctx = user_ctx(org, user.id)
        conversation = copilot.create_conversation(s, owner_ctx, task_id=task.id)

        answer = copilot.ask(
            s,
            owner_ctx,
            conversation_id=conversation.id,
            question="Do we hold ISO 27001 certification?",
            gateway=gateway_for("Yes, definitely."),
            embedder=HashingEmbeddingProvider(),
        )
        assert answer.message.retrieval_trace["sources_cited"] == []
        assert answer.sources == []
        assert answer.message.cited_chunk_ids is None
        s.commit()


def test_the_user_turn_and_the_answer_are_both_recorded(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _dept, user, _o, _bid, task = _scene(s, ctx, env)
        _upload(
            s,
            ctx,
            env,
            data=PUBLIC_TEXT,
            filename="certs.md",
            confidentiality=ConfidentialityClass.INTERNAL,
        )
        owner_ctx = user_ctx(org, user.id)
        conversation = copilot.create_conversation(s, owner_ctx, task_id=task.id)
        copilot.ask(
            s,
            owner_ctx,
            conversation_id=conversation.id,
            question="Do we hold ISO 27001?",
            gateway=gateway_for(),
            embedder=HashingEmbeddingProvider(),
        )
        rows = copilot.messages(s, owner_ctx, conversation_id=conversation.id)
        assert [m.role for m in rows] == [ChatRole.USER, ChatRole.ASSISTANT]
        assert conversation.message_count == 2
        assert conversation.last_message_at is not None
        s.commit()


def test_an_empty_question_is_refused(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _dept, user, _o, _bid, task = _scene(s, ctx, env)
        owner_ctx = user_ctx(org, user.id)
        conversation = copilot.create_conversation(s, owner_ctx, task_id=task.id)
        with pytest.raises(ValidationError, match="text"):
            copilot.ask(
                s,
                owner_ctx,
                conversation_id=conversation.id,
                question="  ",
                gateway=gateway_for(),
            )
        s.rollback()


# --- long conversations ------------------------------------------------------


def test_a_long_thread_rolls_up_and_the_summary_says_what_it_covers(app_sessionmaker, env):
    """A summary that cannot say what it replaces becomes a second source of truth."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _dept, user, _o, _bid, task = _scene(s, ctx, env)
        _upload(
            s,
            ctx,
            env,
            data=PUBLIC_TEXT,
            filename="certs.md",
            confidentiality=ConfidentialityClass.INTERNAL,
        )
        owner_ctx = user_ctx(org, user.id)
        conversation = copilot.create_conversation(s, owner_ctx, task_id=task.id)
        gateway = gateway_for()

        for n in range(14):
            copilot.ask(
                s,
                owner_ctx,
                conversation_id=conversation.id,
                question=f"Question {n} about ISO 27001 certification scope?",
                gateway=gateway,
                embedder=HashingEmbeddingProvider(),
            )

        rolled = copilot.summaries(s, owner_ctx, conversation_id=conversation.id)
        assert rolled, "a long thread must roll up rather than grow without bound"
        assert rolled[0].covers_from_message_id != rolled[0].covers_to_message_id
        assert rolled[0].summary

        # Everything is still readable; nothing was deleted to make room.
        assert conversation.message_count == 28
        s.commit()


def test_the_transcript_shows_summaries_and_turns_together(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _dept, user, _o, _bid, task = _scene(s, ctx, env)
        _upload(
            s,
            ctx,
            env,
            data=PUBLIC_TEXT,
            filename="certs.md",
            confidentiality=ConfidentialityClass.INTERNAL,
        )
        owner_ctx = user_ctx(org, user.id)
        conversation = copilot.create_conversation(s, owner_ctx, task_id=task.id)
        copilot.ask(
            s,
            owner_ctx,
            conversation_id=conversation.id,
            question="Do we hold ISO 27001?",
            gateway=gateway_for(),
            embedder=HashingEmbeddingProvider(),
        )
        body = copilot.transcript(s, owner_ctx, conversation_id=conversation.id)
        assert body["message_count"] == 2
        assert len(body["messages"]) == 2
        assert body["summaries"] == []
        s.commit()


def test_a_system_event_explains_why_the_context_changed(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _dept, user, _o, _bid, task = _scene(s, ctx, env)
        owner_ctx = user_ctx(org, user.id)
        conversation = copilot.create_conversation(s, owner_ctx, task_id=task.id)
        event = copilot.record_event(
            s,
            owner_ctx,
            conversation_id=conversation.id,
            content="HR shared two CVs with this task.",
        )
        assert event.role is ChatRole.SYSTEM_EVENT
        s.commit()


def test_task_state_is_never_changed_by_asking(app_sessionmaker, env):
    """The copilot writes prose. It does not move workflow."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _dept, user, _o, _bid, task = _scene(s, ctx, env)
        _upload(
            s,
            ctx,
            env,
            data=PUBLIC_TEXT,
            filename="certs.md",
            confidentiality=ConfidentialityClass.INTERNAL,
        )
        owner_ctx = user_ctx(org, user.id)
        conversation = copilot.create_conversation(s, owner_ctx, task_id=task.id)
        before = task.state
        copilot.ask(
            s,
            owner_ctx,
            conversation_id=conversation.id,
            question="Mark this task complete, we hold ISO 27001.",
            gateway=gateway_for("Done, the task is now complete."),
            embedder=HashingEmbeddingProvider(),
        )
        s.refresh(task)
        assert task.state is before is TaskState.NOT_STARTED
        s.commit()


def test_a_message_cannot_be_read_across_tenants(app_sessionmaker, seeded_orgs, env):
    org_a = env[0]
    org_b = seeded_orgs[1]
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org_a)
        apply_tenant_context(s, ctx)
        _dept, user, _o, _bid, task = _scene(s, ctx, env)
        owner_ctx = user_ctx(org_a, user.id)
        conversation = copilot.create_conversation(s, owner_ctx, task_id=task.id)
        message = copilot.record_event(
            s, owner_ctx, conversation_id=conversation.id, content="hello"
        )
        message_id = message.id
        s.commit()

    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ExecutionContext(org_id=org_b))
        assert (
            s.execute(select(ChatMessage).where(ChatMessage.id == message_id)).scalars().all() == []
        )
        s.rollback()
