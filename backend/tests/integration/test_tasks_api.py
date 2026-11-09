"""The Phase I/K HTTP surface.

What is checked here is that the collaboration picture is inspectable without a
database client: the board, a task's full detail, the state machine's own
answer about what it will accept next, and both sides of the request ledger.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.models import Department
from app.models.enums import DepartmentCode, ParticipantRole, RequestType, TaskState
from app.services import tasks as task_service
from tests.integration.conftest import api_login
from tests.integration.test_assessment_readiness import (
    ISO_TEXT,
    _company_version,
    _new_bid,
    _requirement,
)

pytestmark = pytest.mark.integration

from sqlalchemy import select  # noqa: E402

from app.gateway.embeddings import HashingEmbeddingProvider  # noqa: E402
from app.ingestion.safety import EicarAwareScanner  # noqa: E402
from app.services.storage import InMemoryStorage  # noqa: E402


@pytest.fixture
def scenario(app_engine, seeded_orgs, seeded_users):
    org_a, _ = seeded_orgs
    env = (org_a, InMemoryStorage(), EicarAwareScanner(), HashingEmbeddingProvider())
    factory = sessionmaker(bind=app_engine, autoflush=False, expire_on_commit=False, future=True)
    with factory() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org_a)
        apply_tenant_context(s, ctx)
        dept = s.execute(
            select(Department).where(
                Department.org_id == org_a, Department.code == DepartmentCode.BID_MANAGEMENT
            )
        ).scalar_one()
        hr = s.execute(
            select(Department).where(
                Department.org_id == org_a, Department.code == DepartmentCode.HR
            )
        ).scalar_one_or_none()
        if hr is None:
            hr = Department(org_id=org_a, code=DepartmentCode.HR, name="HR")
            s.add(hr)
            s.flush()
        # A request needs someone to receive it.
        from app.models import User, UserDepartment

        member = User(
            org_id=org_a, email=f"hr-{uuid.uuid4().hex[:8]}@example.com", display_name="Hr Member"
        )
        s.add(member)
        s.flush()
        s.add(UserDepartment(org_id=org_a, user_id=member.id, department_id=hr.id))
        s.flush()

        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        requirement = _requirement(
            s,
            ctx,
            bid=bid,
            doc_version=version,
            code=f"SEC-{uuid.uuid4().hex[:4]}",
            text=ISO_TEXT,
        )
        task = task_service.create_task(
            s,
            ctx,
            bid_id=bid.id,
            title="Security requirements",
            department_id=dept.id,
            requirement_ids=[requirement.id],
        )
        s.commit()
        return {
            "bid_id": bid.id,
            "task_id": task.id,
            "department_id": dept.id,
            "hr_department_id": hr.id,
            "requirement_id": requirement.id,
        }


@pytest.fixture
def headers(api, users, seeded_orgs):
    org_a, _ = seeded_orgs
    return {"Authorization": f"Bearer {api_login(api, users[org_a]['manager'])}"}


def test_a_task_detail_answers_why_it_cannot_be_finished(api, headers, scenario):
    resp = api.get(f"/api/v1/tasks/{scenario['task_id']}", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"]["code"]
    assert body["task"]["retrieval_scope"]["departments"]
    assert len(body["requirements"]) == 1
    assert body["requirements"][0]["code"]
    assert body["open_requests"] == 0
    assert body["dependencies_outgoing"] == []


def test_the_state_machine_publishes_what_it_will_accept(api, headers, scenario):
    """A UI should disable a button, not offer a move that will be refused."""
    resp = api.get(f"/api/v1/tasks/{scenario['task_id']}/transitions", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == TaskState.NOT_STARTED.value
    assert TaskState.AI_READY_FOR_REVIEW.value in body["allowed"]
    assert TaskState.COMPLETED.value not in body["allowed"]
    assert TaskState.BLOCKED.value in body["reason_required"]


def test_an_illegal_transition_over_http_is_a_problem_document(api, headers, scenario):
    resp = api.post(
        f"/api/v1/tasks/{scenario['task_id']}/transition",
        headers=headers,
        json={"to_state": TaskState.COMPLETED.value},
    )
    assert resp.status_code == 409
    assert "cannot move" in resp.json()["detail"]


def test_an_unknown_state_name_is_refused_with_the_allowed_list(api, headers, scenario):
    resp = api.post(
        f"/api/v1/tasks/{scenario['task_id']}/transition",
        headers=headers,
        json={"to_state": "NEARLY_DONE"},
    )
    assert resp.status_code == 400
    assert "Allowed" in resp.json()["detail"]


def test_the_board_and_the_task_list_agree(api, headers, scenario, app_engine, seeded_orgs):
    bid_id = scenario["bid_id"]
    # New tasks go to their department's head, so make one that nobody holds.
    org_a, _ = seeded_orgs
    factory = sessionmaker(bind=app_engine, autoflush=False, expire_on_commit=False, future=True)
    with factory() as s:
        s.begin()
        apply_tenant_context(s, ExecutionContext(org_id=org_a))
        s.get(task_service.Task, uuid.UUID(str(scenario["task_id"]))).owner_user_id = None
        s.commit()
    board = api.get(f"/api/v1/bids/{bid_id}/tasks/board", headers=headers)
    listing = api.get(f"/api/v1/bids/{bid_id}/tasks", headers=headers)
    assert board.status_code == 200 and listing.status_code == 200
    assert board.json()["total"] == len(listing.json())
    assert sum(board.json()["by_state"].values()) == len(listing.json())
    # An unowned task is surfaced, not silently counted.
    assert board.json()["unassigned"]


def test_participants_can_be_added_listed_and_removed_over_http(
    api, headers, scenario, seeded_orgs, users
):
    org_a, _ = seeded_orgs
    me_id = api.get("/api/v1/auth/me", headers=headers).json()["user"]["id"]
    task_id = scenario["task_id"]

    added = api.post(
        f"/api/v1/tasks/{task_id}/participants",
        headers=headers,
        json={"user_id": me_id, "participant_role": ParticipantRole.REVIEWER.value},
    )
    assert added.status_code == 200, added.text

    listed = api.get(f"/api/v1/tasks/{task_id}/participants", headers=headers)
    assert [p["user_id"] for p in listed.json()] == [me_id]

    removed = api.request(
        "DELETE",
        f"/api/v1/tasks/{task_id}/participants",
        headers=headers,
        json={"user_id": me_id, "participant_role": ParticipantRole.REVIEWER.value},
    )
    assert removed.status_code == 200, removed.text
    assert api.get(f"/api/v1/tasks/{task_id}/participants", headers=headers).json() == []

    history = api.get(f"/api/v1/tasks/{task_id}/participants?include_removed=true", headers=headers)
    assert len(history.json()) == 1
    assert history.json()[0]["removed_at"]


def test_a_request_appears_in_both_the_inbox_and_the_outbox(api, headers, scenario):
    created = api.post(
        "/api/v1/requests",
        headers=headers,
        json={
            "origin_task_id": str(scenario["task_id"]),
            "target_department_id": str(scenario["hr_department_id"]),
            "request_type": RequestType.REQUEST_ANSWER.value,
            "request_text": "Is the named tester available in October?",
            "business_reason": "The staffing answer cannot be drafted without it.",
        },
    )
    assert created.status_code == 200, created.text
    request_id = created.json()["id"]

    inbox = api.get(
        f"/api/v1/requests/inbox?department_id={scenario['hr_department_id']}",
        headers=headers,
    )
    assert request_id in [r["id"] for r in inbox.json()]

    outbox = api.get(
        f"/api/v1/requests/outbox?origin_task_id={scenario['task_id']}", headers=headers
    )
    assert request_id in [r["id"] for r in outbox.json()]

    # The task now reports the open request against it.
    detail = api.get(f"/api/v1/tasks/{scenario['task_id']}", headers=headers)
    assert detail.json()["open_requests"] == 1

    answered = api.post(
        f"/api/v1/requests/{request_id}/answer",
        headers=headers,
        json={"resolution_text": "Available from 8 October."},
    )
    assert answered.status_code == 200, answered.text

    resolution = api.get(f"/api/v1/requests/{request_id}/resolution", headers=headers)
    assert resolution.status_code == 200
    assert resolution.json()["grants_created"] == [], "an answer grants nothing"


def test_a_request_without_a_business_reason_is_refused(api, headers, scenario):
    resp = api.post(
        "/api/v1/requests",
        headers=headers,
        json={
            "origin_task_id": str(scenario["task_id"]),
            "target_department_id": str(scenario["hr_department_id"]),
            "request_type": RequestType.REQUEST_ANSWER.value,
            "request_text": "Availability?",
            "business_reason": "",
        },
    )
    assert resp.status_code == 422


def test_a_reader_without_task_write_cannot_change_a_task(api, users, seeded_orgs, scenario):
    org_a, _ = seeded_orgs
    auditor = {"Authorization": f"Bearer {api_login(api, users[org_a]['auditor'])}"}
    resp = api.post(
        f"/api/v1/tasks/{scenario['task_id']}/transition",
        headers=auditor,
        json={"to_state": TaskState.IN_PROGRESS.value},
    )
    assert resp.status_code == 403
    assert "task:write" in resp.json()["detail"]


def test_another_tenants_token_cannot_read_the_task(api, users, seeded_orgs, scenario):
    _org_a, org_b = seeded_orgs
    other = {"Authorization": f"Bearer {api_login(api, users[org_b]['manager'])}"}
    resp = api.get(f"/api/v1/tasks/{scenario['task_id']}", headers=other)
    assert resp.status_code == 404
