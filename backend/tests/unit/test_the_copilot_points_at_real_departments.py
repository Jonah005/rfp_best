"""Who to ask: the model proposes, the code keeps only what exists.

Seen live: asked who holds third-party licence terms, the copilot named
"Procurement / Vendor Management" and "IT / Architecture" -- departments the
company does not have -- and offered no way to ask anyone.
"""

from __future__ import annotations

import uuid

from app.gateway.registry import get
from app.models import Department
from app.models.enums import DepartmentCode
from app.schemas.department_routing import DepartmentRoutingResult, RoutedDepartment
from app.services import routing


def _dept(code, name):
    return Department(id=uuid.uuid4(), org_id=uuid.uuid4(), code=code, name=name)


LEGAL = _dept(DepartmentCode.LEGAL, "Legal")
ARCH = _dept(DepartmentCode.SOLUTION_ARCHITECTURE, "Solution Architecture")
COMM = _dept(DepartmentCode.COMMERCIAL, "Commercial")
ROWS = [LEGAL, ARCH, COMM]


class _Task:
    code, title = "LEG-03", "Third party licensing rights"
    department_id = LEGAL.id


class _Gateway:
    def __init__(self, result=None, fail=False):
        self.result, self.fail, self.seen = result, fail, None

    def generate_structured(self, name, content, **_):
        assert name == "department_routing"
        self.seen = content
        if self.fail:
            raise RuntimeError("provider down")
        return self.result


def _route(monkeypatch, gateway, answer="The gap is the licence terms.", fallback=None):
    monkeypatch.setattr(routing, "departments", lambda session, ctx: ROWS)
    return routing.route(
        None,
        None,
        task=_Task(),
        question="who has the licence terms?",
        answer_text=answer,
        requirement_block="[R-061] third-party controls licensed to vendor and Jadwa",
        fallback_departments=fallback or [],
        gateway=gateway,
    )


def test_invented_and_own_departments_are_dropped():
    kept = routing.validate(
        [
            RoutedDepartment(code="PROCUREMENT", question="q"),
            RoutedDepartment(code="LEGAL", question="q"),  # the asker's own
            RoutedDepartment(code="solution_architecture", question="List the components."),
            RoutedDepartment(code="SOLUTION_ARCHITECTURE", question="dup"),
            RoutedDepartment(code="COMMERCIAL", question="q"),
            RoutedDepartment(code="COMMERCIAL", question="q"),
        ],
        rows=ROWS,
        own_department_id=LEGAL.id,
    )
    assert [k["code"] for k in kept] == ["SOLUTION_ARCHITECTURE", "COMMERCIAL"]
    assert kept[0]["question"] == "List the components." and kept[0]["id"] == str(ARCH.id)


def test_the_model_choice_prefills_the_request(monkeypatch):
    gateway = _Gateway(
        DepartmentRoutingResult(
            needs_other_department=True,
            departments=[
                RoutedDepartment(
                    code="SOLUTION_ARCHITECTURE",
                    question="Which third-party controls are in the proposed solution?",
                    reason="owns the solution's third-party components",
                )
            ],
        )
    )
    got = _route(monkeypatch, gateway)
    assert got["needed"] and got["source"] == "model"
    assert got["departments"][0]["name"] == "Solution Architecture"
    assert "responsible_for" in gateway.seen, "the model must be told what each department does"


def test_no_department_needed_means_no_buttons(monkeypatch):
    got = _route(monkeypatch, _Gateway(DepartmentRoutingResult(needs_other_department=False)))
    assert got == {"needed": False, "departments": [], "source": "model"}


def test_a_failed_call_falls_back_to_the_rules(monkeypatch):
    fallback = [{"code": "COMMERCIAL", "id": str(COMM.id), "name": "Commercial"}]
    got = _route(monkeypatch, _Gateway(fail=True), fallback=fallback)
    assert got["source"] == "rules" and got["needed"] and got["departments"] == fallback


def test_asking_for_someone_but_naming_nobody_real_falls_back(monkeypatch):
    bogus = DepartmentRoutingResult(
        needs_other_department=True, departments=[RoutedDepartment(code="PROCUREMENT")]
    )
    got = _route(monkeypatch, _Gateway(bogus), fallback=[])
    assert got["source"] == "rules_after_invalid_model_choice" and not got["needed"]


def test_every_department_code_has_a_default_responsibility():
    assert set(routing.DEFAULT_RESPONSIBILITIES) == set(DepartmentCode)


def test_the_prompts_forbid_invented_departments():
    assert "never invent a department" in get("task_copilot").system_prompt
    assert "Use ONLY codes" in get("department_routing").system_prompt


def test_each_suggestion_carries_its_own_question_and_need():
    kept = routing.validate(
        [
            RoutedDepartment(
                code="SOLUTION_ARCHITECTURE", question="Which components?", need="R-061"
            ),
            RoutedDepartment(
                code="COMMERCIAL", question="What do the licences cost?", need="R-061"
            ),
        ],
        rows=ROWS,
        own_department_id=LEGAL.id,
    )
    assert [(k["question"], k["need"]) for k in kept] == [
        ("Which components?", "R-061"),
        ("What do the licences cost?", "R-061"),
    ]


def test_the_copilot_is_told_not_to_send_the_reader_to_their_own_department():
    assert "department marked as this task's own" in get("task_copilot").system_prompt
    routing_prompt = get("department_routing").system_prompt
    assert "own department is where the contributor works" in routing_prompt
