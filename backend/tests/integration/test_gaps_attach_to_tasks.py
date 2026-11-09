"""Gaps in the tender are attached to the task they concern, by the model, and a
person has the last word."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.core.permissions import Perm
from app.db.session import apply_tenant_context
from app.models import Department
from app.models.enums import (
    ClarificationIssueType,
    ClarificationSeverity,
    ClarificationStatus,
    DepartmentCode,
)
from app.services import clarifications, identity, task_gaps
from app.services import tasks as task_service
from tests.integration.test_assessment_readiness import _new_bid

pytestmark = pytest.mark.integration


def _open(app_engine, org):
    factory = sessionmaker(bind=app_engine, autoflush=False, expire_on_commit=False, future=True)
    s = factory()
    s.begin()
    ctx = ExecutionContext(
        org_id=org, permissions=frozenset({Perm.TASK_ASSIGN.value, Perm.BID_READ.value})
    )
    apply_tenant_context(s, ctx)
    return s, ctx


def _department_with_head(s, ctx, org):
    dept = s.execute(
        select(Department).where(Department.org_id == org, Department.code == DepartmentCode.LEGAL)
    ).scalar_one_or_none()
    if dept is None:
        dept = Department(org_id=org, code=DepartmentCode.LEGAL, name="Legal")
        s.add(dept)
        s.flush()
    dept.head_user_id = None
    s.flush()
    head = identity.create_user(
        s,
        ctx,
        email=f"h-{uuid.uuid4().hex[:8]}@example.com",
        display_name="Head",
        department_ids_to_assign=[dept.id],
    )
    return dept, head


def _gap(s, ctx, bid, text):
    return clarifications.raise_issue(
        s,
        ctx,
        bid_id=bid.id,
        issue_type=list(ClarificationIssueType)[0],
        description=text,
        severity=ClarificationSeverity.MAJOR,
        fingerprint=uuid.uuid4().hex,
        detected_by="test",
    )


class _Model:
    """Marks the gaps whose text contains a keyword as relevant."""

    def __init__(self, keyword="liability", fail=False):
        self.keyword = keyword
        self.fail = fail
        self.calls = 0

    def generate_structured(self, name, user_content, **_):
        import json
        import re

        from app.schemas.task_gaps import GapJudgement, GapRelevanceResult

        self.calls += 1
        assert name == "task_gap_relevance"
        if self.fail:
            raise RuntimeError("model down")
        gaps = json.loads(re.search(r"\{.*\}", user_content, re.S).group(0))["GAPS"]
        return GapRelevanceResult(
            items=[
                GapJudgement(
                    n=g["n"],
                    relevant=self.keyword in g["problem"],
                    missing="The RFP omits the point." if self.keyword in g["problem"] else "",
                    why_needed="The task depends on it." if self.keyword in g["problem"] else "",
                )
                for g in gaps
            ]
        )


def _setup(s, ctx, org):
    dept, head = _department_with_head(s, ctx, org)
    bid = _new_bid(s, ctx)
    task = task_service.create_task(
        s, ctx, bid_id=bid.id, title="Contract review", department_id=dept.id
    )
    near = _gap(s, ctx, bid, "The liability cap is not stated anywhere.")
    far = _gap(s, ctx, bid, "The parking arrangements are unclear.")
    for g in (near, far):
        g.department_id = dept.id
    s.flush()
    return task, near, far


def test_the_model_attaches_only_the_gaps_that_concern_the_task(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        task, near, far = _setup(s, ctx, org)
        model = _Model()
        result = task_gaps.check(s, ctx, task_id=task.id, gateway=model)
        assert result["attached"] == 1
        shown = task_gaps.list_for_task(s, ctx, task_id=task.id)
        assert shown["checked"] is True
        assert [i["id"] for i in shown["items"]] == [str(near.id)]
        assert shown["items"][0]["missing"] == "The RFP omits the point."
        assert shown["items"][0]["why_needed"] == "The task depends on it."

        # Cached: opening the task again does not ask again.
        task_gaps.check(s, ctx, task_id=task.id, gateway=model)
        assert model.calls == 1
    finally:
        s.rollback()
        s.close()


def test_a_failed_call_attaches_nothing_and_asks_again_next_time(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        task, _, _ = _setup(s, ctx, org)
        task_gaps.check(s, ctx, task_id=task.id, gateway=_Model(fail=True))
        assert task_gaps.list_for_task(s, ctx, task_id=task.id) == {"checked": False, "items": []}
        task_gaps.check(s, ctx, task_id=task.id, gateway=_Model())
        assert len(task_gaps.list_for_task(s, ctx, task_id=task.id)["items"]) == 1
    finally:
        s.rollback()
        s.close()


def test_a_persons_choice_is_never_overridden(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        task, near, far = _setup(s, ctx, org)
        task_gaps.check(s, ctx, task_id=task.id, gateway=_Model())
        task_gaps.detach(s, ctx, task_id=task.id, clarification_id=near.id)
        task_gaps.attach(s, ctx, task_id=task.id, clarification_id=far.id)
        # Asking again, even forced, leaves both decisions alone.
        task_gaps.check(s, ctx, task_id=task.id, gateway=_Model(), force=True)
        ids = {i["id"] for i in task_gaps.list_for_task(s, ctx, task_id=task.id)["items"]}
        assert ids == {str(far.id)}
    finally:
        s.rollback()
        s.close()


def test_a_gap_from_another_bid_cannot_be_attached(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        task, _, _ = _setup(s, ctx, org)
        other = _gap(s, ctx, _new_bid(s, ctx), "Elsewhere.")
        with pytest.raises(NotFound):
            task_gaps.attach(s, ctx, task_id=task.id, clarification_id=other.id)
    finally:
        s.rollback()
        s.close()


def test_recording_an_assumption_settles_the_gap_and_needs_its_wording(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        task, near, _ = _setup(s, ctx, org)
        task_gaps.check(s, ctx, task_id=task.id, gateway=_Model())
        with pytest.raises(ValidationError):
            task_gaps.settle(
                s,
                ctx,
                task_id=task.id,
                clarification_id=near.id,
                status=ClarificationStatus.ANSWERED,
                assumption="  ",
            )
        task_gaps.settle(
            s,
            ctx,
            task_id=task.id,
            clarification_id=near.id,
            status=ClarificationStatus.ANSWERED,
            assumption="We assume liability is capped at contract value.",
        )
        item = task_gaps.list_for_task(s, ctx, task_id=task.id)["items"][0]
        assert item["status"] == "ANSWERED"
        assert item["assumption"].startswith("We assume")
        with pytest.raises(ValidationError):
            task_gaps.settle(
                s,
                ctx,
                task_id=task.id,
                clarification_id=near.id,
                status=ClarificationStatus.OPEN,
                assumption=None,
            )
    finally:
        s.rollback()
        s.close()


class _Drafter:
    def __init__(self, refs):
        self.refs = refs
        self.prompt = ""

    def generate_structured(self, name, user_content, **_):
        from app.schemas.task_gaps import GapAssumptionProposal

        assert name == "gap_assumption"
        self.prompt = user_content
        return GapAssumptionProposal(
            assumption="We assume liability is capped at the contract value.",
            based_on=self.refs,
            caution="Check with Legal.",
        )


def test_a_proposal_records_nothing_and_drops_invented_sources(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        task, near, _ = _setup(s, ctx, org)
        ctx = ExecutionContext(
            org_id=org,
            actor_user_id=task.owner_user_id,
            permissions=frozenset({Perm.TASK_ASSIGN.value, Perm.BID_READ.value}),
        )
        apply_tenant_context(s, ctx)
        drafter = _Drafter(["E99"])
        out = task_gaps.propose_assumption(
            s, ctx, task_id=task.id, clarification_id=near.id, gateway=drafter
        )
        assert out["assumption"].startswith("We assume")
        assert out["based_on"] == []  # E99 is not in the task's context
        assert "liability cap" in drafter.prompt
        assert near.status is ClarificationStatus.OPEN and near.client_response is None
    finally:
        s.rollback()
        s.close()


def test_a_gap_closed_as_no_impact_leaves_the_tasks_list(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        task, near, _ = _setup(s, ctx, org)
        task_gaps.check(s, ctx, task_id=task.id, gateway=_Model())
        task_gaps.settle(
            s,
            ctx,
            task_id=task.id,
            clarification_id=near.id,
            status=ClarificationStatus.CLOSED,
            assumption=None,
        )
        assert task_gaps.list_for_task(s, ctx, task_id=task.id)["items"] == []
    finally:
        s.rollback()
        s.close()


def test_the_recorded_answer_says_how_it_came_about_and_the_person_decides_if_it_is_used(
    app_engine, seeded_orgs
):
    from app.services import writeup

    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        task, near, _ = _setup(s, ctx, org)
        actor = task.owner_user_id
        ctx = ExecutionContext(org_id=org, actor_user_id=actor, permissions=ctx.permissions)
        apply_tenant_context(s, ctx)
        task_gaps.check(s, ctx, task_id=task.id, gateway=_Model())
        task_gaps.settle(
            s,
            ctx,
            task_id=task.id,
            clarification_id=near.id,
            status=ClarificationStatus.ANSWERED,
            assumption="We assume the cap is the contract value.",
            answer_source="department",
        )
        item = task_gaps.list_for_task(s, ctx, task_id=task.id)["items"][0]
        assert item["answer_source"] == "department"
        assert item["answered_by"] == "Head"
        assert item["include_in_response"] is True
        assert len(writeup.bid_context(s, ctx, task.id)["assumptions"]) == 1

        task_gaps.set_include(s, ctx, task_id=task.id, clarification_id=near.id, include=False)
        assert writeup.bid_context(s, ctx, task.id)["assumptions"] == []
        assert (
            task_gaps.list_for_task(s, ctx, task_id=task.id)["items"][0]["include_in_response"]
            is False
        )

        with pytest.raises(ValidationError):
            task_gaps.settle(
                s,
                ctx,
                task_id=task.id,
                clarification_id=near.id,
                status=ClarificationStatus.ANSWERED,
                assumption="x",
                answer_source="stranger",
            )
    finally:
        s.rollback()
        s.close()


def test_a_request_raised_from_a_gap_comes_back_to_it(app_engine, seeded_orgs):
    from app.models.enums import RequestType
    from app.services import requests as request_service

    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        task, near, _ = _setup(s, ctx, org)
        other = Department(org_id=org, code=DepartmentCode.FINANCE, name="Fin-x")
        existing = s.execute(
            select(Department).where(
                Department.org_id == org, Department.code == DepartmentCode.FINANCE
            )
        ).scalar_one_or_none()
        other = existing or other
        if existing is None:
            s.add(other)
            s.flush()
        identity.create_user(
            s,
            ctx,
            email=f"f-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Fin",
            department_ids_to_assign=[other.id],
        )
        actor = task.owner_user_id
        ctx = ExecutionContext(org_id=org, actor_user_id=actor, permissions=ctx.permissions)
        apply_tenant_context(s, ctx)
        task_gaps.check(s, ctx, task_id=task.id, gateway=_Model())
        request_service.create_request(
            s,
            ctx,
            origin_task_id=task.id,
            target_department_id=other.id,
            request_type=RequestType.REQUEST_ANSWER,
            request_text="What is the liability cap?",
            business_reason="The contract task depends on it.",
            clarification_id=near.id,
        )
        item = task_gaps.list_for_task(s, ctx, task_id=task.id)["items"][0]
        assert [r["state"] for r in item["requests"]] == ["PENDING"]
        assert item["requests"][0]["question"] == "What is the liability cap?"
    finally:
        s.rollback()
        s.close()


class _Reviser:
    def __init__(self):
        self.prompt = ""

    def generate_structured(self, name, user_content, **_):
        from app.schemas.task_gaps import GapRevision

        assert name == "gap_assumption_revise"
        self.prompt = user_content
        return GapRevision(
            assumption="We assume we will translate the Arabic content ourselves.",
            based_on=["E99"],
            caution="Nothing approved covers translation capacity.",
            what_changed="Now says we translate the Arabic content.",
        )


def test_the_person_can_change_the_draft_by_chatting_and_it_stays_in_the_thread(
    app_engine, seeded_orgs
):
    from app.services import copilot

    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        task, near, _ = _setup(s, ctx, org)
        ctx = ExecutionContext(
            org_id=org,
            actor_user_id=task.owner_user_id,
            permissions=frozenset({Perm.TASK_ASSIGN.value, Perm.BID_READ.value}),
        )
        apply_tenant_context(s, ctx)
        conversation = copilot.create_conversation(s, ctx, task_id=task.id)
        reviser = _Reviser()
        out = task_gaps.revise_assumption(
            s,
            ctx,
            task_id=task.id,
            clarification_id=near.id,
            current="We assume the customer translates.",
            instruction="say we translate the Arabic ourselves",
            gateway=reviser,
            conversation_id=conversation.id,
        )
        assert out["assumption"].startswith("We assume we will translate")
        assert out["based_on"] == [] and out["what_changed"]
        # The model saw the wording being changed and what was asked.
        assert "customer translates" in reviser.prompt and "translate the Arabic" in reviser.prompt
        # Nothing was recorded; the exchange is in the missing-parts thread only.
        assert near.status is ClarificationStatus.OPEN and near.client_response is None
        thread = copilot.messages(s, ctx, conversation_id=conversation.id, focus_key="gaps")
        assert [m.role.value for m in thread] == ["USER", "ASSISTANT"]
        assert not copilot.messages(s, ctx, conversation_id=conversation.id, focus_key=None)
    finally:
        s.rollback()
        s.close()


class _Talker:
    def __init__(self):
        self.prompts: list[str] = []

    def generate_structured(self, name, user_content, **_):
        from app.schemas.task_gaps import GapChatTurn

        assert name == "gap_chat"
        self.prompts.append(user_content)
        if "draft it" in user_content:
            return GapChatTurn(
                reply="Here is a draft.",
                assumption="We assume liability is capped at the contract value.",
                caution="Check with Legal.",
            )
        return GapChatTurn(reply="It depends on the contract.")


def test_each_missing_part_has_its_own_chat_and_only_a_draft_turn_proposes_wording(
    app_engine, seeded_orgs
):
    from app.services import copilot

    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        task, near, far = _setup(s, ctx, org)
        ctx = ExecutionContext(
            org_id=org,
            actor_user_id=task.owner_user_id,
            permissions=frozenset({Perm.TASK_ASSIGN.value, Perm.BID_READ.value}),
        )
        apply_tenant_context(s, ctx)
        task_gaps.attach(s, ctx, task_id=task.id, clarification_id=near.id)
        task_gaps.attach(s, ctx, task_id=task.id, clarification_id=far.id)
        conversation = copilot.create_conversation(s, ctx, task_id=task.id)
        talker = _Talker()

        def say(gap, text, current=""):
            return task_gaps.gap_chat(
                s,
                ctx,
                task_id=task.id,
                clarification_id=gap.id,
                message=text,
                current=current,
                conversation_id=conversation.id,
                gateway=talker,
            )

        first = say(near, "what are our options?")
        assert first["assumption"] is None
        say(far, "FAR-QUESTION")
        drafted = say(near, "please draft it")
        assert drafted["assumption"].startswith("We assume liability")
        # The second turn on `near` saw its own earlier turn, and nothing said about `far`.
        near_prompt = talker.prompts[-1]
        assert "what are our options?" in near_prompt and "FAR-QUESTION" not in near_prompt
        # Nothing was recorded.
        assert near.status is ClarificationStatus.OPEN and near.client_response is None
        mine = copilot.messages(s, ctx, conversation_id=conversation.id, focus_key=f"gap:{near.id}")
        assert [m.role.value for m in mine] == ["USER", "ASSISTANT", "USER", "ASSISTANT"]
        theirs = copilot.messages(
            s, ctx, conversation_id=conversation.id, focus_key=f"gap:{far.id}"
        )
        assert len(theirs) == 2
    finally:
        s.rollback()
        s.close()
