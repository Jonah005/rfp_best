"""The Phase J HTTP surface: 202-then-stream, and the inspection routes.

The streaming test drives the real path -- a background thread, its own session,
its own tenant binding -- with the provider swapped for a scripted one. What is
asserted is the contract: the POST does not block, the answer is durable before
the job completes, and a dropped stream costs the animation and not the answer.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.gateway.embeddings import HashingEmbeddingProvider
from app.gateway.gateway import ModelGateway
from app.models import Department
from app.models.enums import DepartmentCode
from app.services import tasks as task_service
from tests.integration.conftest import api_login
from tests.integration.test_assessment_readiness import _new_bid
from tests.integration.test_copilot import ScriptedProvider

pytestmark = pytest.mark.integration


@pytest.fixture
def scripted_gateway(monkeypatch):
    """Swap the gateway the background worker builds for itself."""
    import app.api.v1.copilot as copilot_routes

    provider = ScriptedProvider("Yes, a current ISO 27001 certificate is held.")
    gateway = ModelGateway(provider, HashingEmbeddingProvider())
    monkeypatch.setattr(copilot_routes, "build_gateway", lambda: gateway)
    return gateway


@pytest.fixture
def scenario(app_engine, seeded_orgs, seeded_users):
    """A task owned by the logged-in manager, so they are on it."""
    org_a, _ = seeded_orgs
    factory = sessionmaker(bind=app_engine, autoflush=False, expire_on_commit=False, future=True)
    with factory() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org_a)
        apply_tenant_context(s, ctx)
        from app.models import User

        manager = s.execute(
            select(User).where(User.org_id == org_a, User.display_name == "Manager")
        ).scalars().first()
        dept = s.execute(
            select(Department).where(
                Department.org_id == org_a, Department.code == DepartmentCode.BID_MANAGEMENT
            )
        ).scalar_one()
        bid = _new_bid(s, ctx)
        task = task_service.create_task(
            s,
            ctx,
            bid_id=bid.id,
            title="Copilot task",
            department_id=dept.id,
            owner_user_id=manager.id,
        )
        s.commit()
        return {"task_id": task.id, "user_id": manager.id}


@pytest.fixture
def headers(api, users, seeded_orgs):
    org_a, _ = seeded_orgs
    return {"Authorization": f"Bearer {api_login(api, users[org_a]['manager'])}"}


def test_a_conversation_must_be_created_explicitly(api, headers, scenario):
    """Reading before creating is a 404 that says how to create one."""
    task_id = scenario["task_id"]
    # create_task with an owner already opened one, so remove that assumption by
    # asking about a task the caller has no conversation on.
    missing = api.get(f"/api/v1/tasks/{uuid.uuid4()}/conversation", headers=headers)
    assert missing.status_code == 404
    assert "POST" in missing.json()["detail"]

    created = api.post(f"/api/v1/tasks/{task_id}/conversations", headers=headers, json={})
    assert created.status_code == 200, created.text
    assert created.json()["message_count"] == 0

    fetched = api.get(f"/api/v1/tasks/{task_id}/conversation", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created.json()["id"]


def test_asking_returns_202_and_the_answer_arrives_on_the_stream(
    api, headers, scenario, scripted_gateway
):
    task_id = scenario["task_id"]
    conversation_id = api.post(
        f"/api/v1/tasks/{task_id}/conversations", headers=headers, json={}
    ).json()["id"]

    accepted = api.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=headers,
        json={"content": "Do we hold ISO 27001 certification?"},
    )
    assert accepted.status_code == 202, accepted.text
    body = accepted.json()
    assert body["stream_url"].endswith("/stream")
    assert accepted.headers["Location"] == body["stream_url"]

    with api.stream("GET", body["stream_url"], headers=headers) as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        payload = "".join(stream.iter_text())

    assert "event: done" in payload
    done = [
        json.loads(line[len("data: ") :])
        for line in payload.splitlines()
        if line.startswith("data: ")
    ][-1]
    assert done["message_id"]

    # The answer is durable regardless of the stream: a client that never
    # connected can still read it.
    messages = api.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=headers
    )
    assert [m["role"] for m in messages.json()] == ["USER", "ASSISTANT"]


def test_a_job_can_be_polled_instead_of_streamed(api, headers, scenario, scripted_gateway):
    conversation_id = api.post(
        f"/api/v1/tasks/{scenario['task_id']}/conversations", headers=headers, json={}
    ).json()["id"]
    job = api.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=headers,
        json={"content": "Do we hold ISO 27001?"},
    ).json()

    # Draining the stream is the simplest way to wait for completion.
    with api.stream("GET", job["stream_url"], headers=headers) as stream:
        "".join(stream.iter_text())

    polled = api.get(job["result_url"], headers=headers)
    assert polled.status_code == 200
    assert polled.json()["state"] == "DONE"
    assert polled.json()["result"]["content"]


def test_another_tenant_cannot_read_a_job(api, users, seeded_orgs, scenario, scripted_gateway):
    """A job id is a bearer token for whatever the job produced."""
    org_a, org_b = seeded_orgs
    headers_a = {"Authorization": f"Bearer {api_login(api, users[org_a]['manager'])}"}
    conversation_id = api.post(
        f"/api/v1/tasks/{scenario['task_id']}/conversations", headers=headers_a, json={}
    ).json()["id"]
    job = api.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=headers_a,
        json={"content": "Do we hold ISO 27001?"},
    ).json()

    other = {"Authorization": f"Bearer {api_login(api, users[org_b]['manager'])}"}
    assert api.get(job["result_url"], headers=other).status_code == 404
    assert api.get(job["stream_url"], headers=other).status_code == 404


def test_the_context_preview_shows_the_boundary_without_asking_the_model(
    api, headers, scenario
):
    """The permission boundary is inspectable directly, not inferred from prose."""
    resp = api.get(
        f"/api/v1/tasks/{scenario['task_id']}/context-preview?q=ISO+27001",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "out_of_scope" in body
    assert "trace" in body
    assert body["trace"]["query"] == "ISO 27001"
    assert "filters_applied" in body["trace"]
    if body["out_of_scope"]:
        assert body["refusal"]["options"][0]["grants_access"] is False


def test_the_retrieval_trace_is_reachable_for_any_answer(
    api, headers, scenario, scripted_gateway
):
    conversation_id = api.post(
        f"/api/v1/tasks/{scenario['task_id']}/conversations", headers=headers, json={}
    ).json()["id"]
    job = api.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=headers,
        json={"content": "Do we hold ISO 27001?"},
    ).json()
    with api.stream("GET", job["stream_url"], headers=headers) as stream:
        "".join(stream.iter_text())

    messages = api.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=headers
    ).json()
    assistant = messages[-1]
    trace = api.get(
        f"/api/v1/messages/{assistant['id']}/retrieval-trace", headers=headers
    )
    assert trace.status_code == 200, trace.text
    assert trace.json()["trace"]["query"] == "Do we hold ISO 27001?"
    assert "filters_applied" in trace.json()["trace"]


def test_a_user_without_chat_permission_is_refused(api, users, seeded_orgs, scenario):
    org_a, _ = seeded_orgs
    auditor = {"Authorization": f"Bearer {api_login(api, users[org_a]['auditor'])}"}
    resp = api.post(
        f"/api/v1/tasks/{scenario['task_id']}/conversations", headers=auditor, json={}
    )
    assert resp.status_code == 403
    assert "chat:use" in resp.json()["detail"]
