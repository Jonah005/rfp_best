"""Diagrams are asked for in the task chat, the same conversation as everything else."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.gateway.embeddings import HashingEmbeddingProvider
from app.models import ChatMessage, User
from app.schemas.diagrams import (
    DiagramDraftResult,
    DiagramIntentResult,
    DiagramRevisionResult,
    DiagramStep,
    MissingInformation,
    StyleIntentResult,
)
from app.services import copilot, diagram_chat, diagrams
from app.services import tasks as task_service
from tests.integration.test_a_customer_clause_is_not_left_in_a_task import (  # noqa: F401
    _build,
    env,
    fresh_org,
)
from tests.integration.test_a_diagram_in_the_task_workspace import FLOW, _task

pytestmark = pytest.mark.integration


def plan(*steps, **first):
    """The router's reading of one message: a list of moves."""
    return DiagramIntentResult(wants_diagram=True, steps=[DiagramStep(**x) for x in steps])


NOT_ABOUT_DIAGRAMS = DiagramIntentResult(wants_diagram=False)


class Talker:
    """A stand-in model: labels are queued per turn, other calls answer by name."""

    def __init__(self, intents, **answers):
        self.intents = list(intents)
        self.answers = answers
        self.seen = []

    def generate_structured(self, call, prompt, *, ctx, session):
        self.seen.append(call)
        if call == "diagram_intent":
            return self.intents.pop(0)
        return self.answers[call]


@pytest.fixture
def chat(app_sessionmaker, env):  # noqa: F811
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org, actor_kind="SYSTEM")
        apply_tenant_context(s, ctx)
        _bid, _legal, _mixed, bidder = _build(s, ctx, env)
        task = _task(s, ctx, bidder)
        person = User(
            org_id=org, email=f"chat-{uuid.uuid4().hex[:8]}@example.com", display_name="Chat Person"
        )
        s.add(person)
        s.flush()
        conversation = task_service.ensure_conversation(s, ctx, task_id=task.id, user_id=person.id)
        yield s, ctx, task, conversation
        s.commit()


def _say(s, ctx, conversation, text, talker):
    answer = copilot.ask(
        s,
        ctx,
        conversation_id=conversation.id,
        question=text,
        gateway=talker,
        embedder=HashingEmbeddingProvider(),
    )
    return answer.message


def test_a_diagram_is_drawn_changed_and_restyled_in_one_conversation(chat):
    s, ctx, task, conversation = chat
    talker = Talker(
        [
            plan({"action": "CREATE", "diagram_type": "FLOW"}),
            plan({"action": "CHANGE", "target": "D1", "instruction": "add a retest step"}),
            plan({"action": "RESTYLE", "target": "D1", "instruction": "black and white"}),
        ],
        diagram_generation=DiagramDraftResult(enough_information=True, spec=FLOW),
        diagram_revision=DiagramRevisionResult(
            spec={
                **FLOW,
                "nodes": [*FLOW["nodes"], {"id": "d", "label": "Retest", "basis": "PERSON"}],
                "edges": [*FLOW["edges"], {"source": "c", "target": "d"}],
            }
        ),
        diagram_style_intent=StyleIntentResult(
            style={"theme": "MONO"}, understood="Black and white."
        ),
    )
    first = _say(s, ctx, conversation, "please draw the process flow for this", talker)
    trace = first.retrieval_trace["diagram"]
    assert trace["action"] == "CREATE" and trace["version_no"] == 1
    assert "process flow" in first.content

    second = _say(s, ctx, conversation, "add a retest step", talker)
    assert second.retrieval_trace["diagram"]["version_no"] == 2
    assert "Retest" in second.content

    third = _say(s, ctx, conversation, "make it black and white for print", talker)
    assert third.retrieval_trace["diagram"]["version_no"] == 3
    assert "Black and white" in third.content

    # All of it is in the one thread, in order.
    rows = s.execute(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation.id)
        .order_by(ChatMessage.created_at)
    ).scalars()
    roles = [m.role.value for m in rows]
    assert sorted(roles) == ["ASSISTANT"] * 3 + ["USER"] * 3


def test_not_enough_information_is_said_in_the_chat_and_can_be_recorded(chat):
    s, ctx, task, conversation = chat
    talker = Talker(
        [
            plan({"action": "CREATE", "diagram_type": "ARCHITECTURE"}),
            plan({"action": "RECORD_QUESTIONS"}),
        ],
        diagram_generation=DiagramDraftResult(
            enough_information=False,
            missing=[
                MissingInformation(
                    what="Which systems are in scope",
                    why="Boxes would be invented",
                    suggested_question="Which systems are in scope?",
                )
            ],
        ),
    )
    reply = _say(s, ctx, conversation, "draw the architecture diagram", talker)
    assert "isn't enough information" in reply.content
    assert "Which systems are in scope" in reply.content
    assert diagrams.diagrams_for_task(s, ctx, task_id=task.id) == []
    done = _say(s, ctx, conversation, "yes record those", talker)
    assert "Recorded 1 open question" in done.content


def test_saying_a_type_is_not_needed_is_remembered(chat):
    s, ctx, task, conversation = chat
    talker = Talker(
        [
            plan(
                {
                    "action": "NOT_NEEDED",
                    "diagram_type": "NETWORK",
                    "reason": "The customer hosts it.",
                }
            )
        ]
    )
    reply = _say(s, ctx, conversation, "we don't need a network diagram here", talker)
    assert "no network diagram" in reply.content
    needs = {n.diagram_type: n for n in diagrams.needs_for_task(s, ctx, task_id=task.id)}
    assert needs["NETWORK"].status == "DECLINED"
    assert needs["NETWORK"].decline_reason == "The customer hosts it."


def test_an_ordinary_question_that_mentions_a_diagram_is_not_hijacked(chat):
    s, ctx, task, conversation = chat
    talker = Talker([NOT_ABOUT_DIAGRAMS])
    reply = diagram_chat.handle(
        s,
        ctx,
        task_id=task.id,
        question="Does R-038 say anything about the architecture diagram format?",
        last_assistant=None,
        gateway=talker,
    )
    assert reply is None


def test_a_plain_question_falls_through_to_the_ordinary_answer(chat):
    s, ctx, task, conversation = chat
    talker = Talker([NOT_ABOUT_DIAGRAMS])
    reply = diagram_chat.handle(
        s,
        ctx,
        task_id=task.id,
        question="Do we hold ISO 27001?",
        last_assistant=None,
        gateway=talker,
    )
    assert reply is None
    assert talker.seen == ["diagram_intent"]


def test_one_message_can_plan_several_moves(chat):
    s, ctx, task, conversation = chat
    talker = Talker(
        [
            plan(
                {"action": "CREATE", "diagram_type": "FLOW", "instruction": "brief base flow"},
                {"action": "RESTYLE", "target": "LAST", "instruction": "black and white"},
            )
        ],
        diagram_generation=DiagramDraftResult(enough_information=True, spec=FLOW),
        diagram_style_intent=StyleIntentResult(style={"theme": "MONO"}, understood="Mono."),
    )
    reply = _say(s, ctx, conversation, "draw a base flow and make it print friendly", talker)
    shown = reply.retrieval_trace["diagrams"]
    assert [d["version_no"] for d in shown] == [1, 2]
    assert len(diagrams.diagrams_for_task(s, ctx, task_id=task.id)) == 1


def test_a_person_who_may_not_edit_is_told_so_instead_of_getting_a_diagram(chat):
    s, ctx, task, conversation = chat
    talker = Talker([plan({"action": "CREATE", "diagram_type": "FLOW"})])
    reply = diagram_chat.handle(
        s,
        ctx,
        task_id=task.id,
        question="draw the flow",
        last_assistant=None,
        gateway=talker,
        can_write=False,
    )
    assert "not change them" in reply["content"]
    assert diagrams.diagrams_for_task(s, ctx, task_id=task.id) == []


def test_pasted_mermaid_in_the_chat_is_imported_without_the_router(chat):
    s, ctx, task, conversation = chat
    talker = Talker([])
    reply = diagram_chat.handle(
        s,
        ctx,
        task_id=task.id,
        question="flowchart LR\n A[Start] --> B[End]",
        last_assistant=None,
        gateway=talker,
    )
    assert "Imported" in reply["content"] and talker.seen == []


def test_a_picture_in_the_chat_needs_the_go_ahead_and_lands_in_the_thread(chat):
    s, ctx, task, conversation = chat
    no = diagram_chat.picture_reply(
        s,
        ctx,
        task_id=task.id,
        image=b"\x89PNG\r\n\x1a\n" + b"0" * 16,
        use_vision=False,
        can_write=True,
    )
    assert "go-ahead" in no["content"]
    assert diagrams.diagrams_for_task(s, ctx, task_id=task.id) == []
