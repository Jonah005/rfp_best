"""The to-do list: what a person must answer, review and finish, each with a way back in."""

from __future__ import annotations

import pytest

from app.models import Department, User, UserDepartment
from app.models.enums import DepartmentCode, RequestType
from app.services import requests as request_service
from app.services import todo
from tests.integration import test_copilot as _copilot_tests
from tests.integration.test_requirement_positions import _setup

fresh_org = _copilot_tests.fresh_org
env = _copilot_tests.env

pytestmark = pytest.mark.integration


def _items(todo_out, key):
    return next(g for g in todo_out["groups"] if g["key"] == key)["items"]


def test_a_reply_waits_to_be_reviewed_then_leaves_the_list(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        me, task, r1, r2 = _setup(s, env, org)
        other = Department(org_id=org, code=DepartmentCode.LEGAL, name="Legal")
        s.add(other)
        s.flush()
        lena = User(org_id=org, email="legal@example.com", display_name="Lena")
        s.add(lena)
        s.flush()
        s.add(UserDepartment(org_id=org, user_id=lena.id, department_id=other.id))
        s.flush()
        req = request_service.create_request(
            s,
            me,
            origin_task_id=task.id,
            target_department_id=other.id,
            request_type=RequestType.REQUEST_ANSWER,
            request_text="Do we hold ISO 9001?",
            business_reason="R-1 needs it",
            requirement_ids=[r1.id],
        )

        # Still open: the asker is only waiting.
        out = todo.for_user(s, me)
        assert [i["id"] for i in _items(out, "waiting")] == [f"waiting:{req.id}"]
        assert not _items(out, "review")

        request_service.answer(s, me, request_id=req.id, resolution_text="Yes, certificate 42.")
        out = todo.for_user(s, me)
        review = _items(out, "review")
        assert len(review) == 1 and "certificate 42" in review[0]["title"]
        # The link opens the task on that very requirement.
        assert f"open=req:{r1.id}" in review[0]["href"]
        assert not _items(out, "waiting")
        # And the requirement row carries the reply.
        replies = todo.replies_by_requirement(s, me, task.id)
        assert replies[r1.id][0]["text"] == "Yes, certificate 42."

        assert todo.mark_reviewed(s, me, request_ids=[req.id]) == 1
        assert not _items(todo.for_user(s, me), "review")
        assert not todo.replies_by_requirement(s, me, task.id)
        s.commit()


def test_unanswered_requirements_are_listed_as_left_to_do(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        me, task, r1, r2 = _setup(s, env, org)
        out = todo.for_user(s, me)
        do = _items(out, "do")
        mine = [i for i in do if i["task_id"] == str(task.id)]
        assert mine and mine[0]["count"] == 2
        assert "open=req:" in mine[0]["href"]
        s.commit()
