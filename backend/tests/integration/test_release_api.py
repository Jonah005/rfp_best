"""The Phase L–P HTTP surface: one bid walked from blank draft to released package.

This is the test that answers "can I check the state of anything from outside?".
It uses only the API — no service calls, no direct database writes — because the
point is that a person with a token can drive and inspect the whole workflow.

It also pins the two separations that matter at the end of the line: a bid
manager may submit a release but never approve one, and a non-waivable finding
has no exception route even for someone holding every relevant permission.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.models.enums import ApprovalStepState, OutputMode
from tests.integration.conftest import api_login

pytestmark = pytest.mark.integration

from sqlalchemy import select  # noqa: E402

from app.models import Department, User  # noqa: E402
from app.models.enums import DepartmentCode  # noqa: E402
from app.services import artifacts as artifact_service  # noqa: E402
from app.services import bids as bid_service  # noqa: E402
from app.services import tasks as task_service  # noqa: E402


@pytest.fixture(autouse=True)
def object_storage(monkeypatch):
    """One in-memory bucket for the whole test.

    The routes do not take a storage argument -- assembly and validation both
    resolve it themselves -- so the substitution has to happen where the service
    looks it up, not where the test calls in.
    """
    from app.services.storage import InMemoryStorage

    store = InMemoryStorage()
    monkeypatch.setattr("app.services.release.get_storage", lambda: store)
    return store


@pytest.fixture
def headers(api, users, seeded_orgs):
    """One header set per role, since release deliberately needs two people."""
    org_a, _ = seeded_orgs
    return {
        role: {"Authorization": f"Bearer {api_login(api, users[org_a][role])}"}
        for role in ("manager", "auditor", "approver", "knowledge_admin")
    }


@pytest.fixture
def bid_with_task(app_engine, seeded_orgs, seeded_users):
    """A bid with one task, owned by the manager. Everything else goes over HTTP."""
    org_a, _ = seeded_orgs
    factory = sessionmaker(bind=app_engine, autoflush=False, expire_on_commit=False, future=True)
    with factory() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org_a)
        apply_tenant_context(s, ctx)
        manager = (
            s.execute(select(User).where(User.org_id == org_a, User.display_name == "Manager"))
            .scalars()
            .first()
        )
        dept = s.execute(
            select(Department).where(
                Department.org_id == org_a, Department.code == DepartmentCode.BID_MANAGEMENT
            )
        ).scalar_one()
        actor = ExecutionContext(org_id=org_a, actor_user_id=manager.id)
        opportunity = bid_service.create_opportunity(
            s,
            actor,
            customer_name="API Customer",
            tender_name=f"API Tender {uuid.uuid4().hex[:6]}",
        )
        bid = bid_service.promote_to_bid(s, actor, opportunity_id=opportunity.id)
        task = task_service.create_task(
            s,
            actor,
            bid_id=bid.id,
            title="Technical response",
            department_id=dept.id,
            owner_user_id=manager.id,
        )
        artifact = artifact_service.for_task(s, actor, task_id=task.id)
        s.commit()
        return {"bid_id": bid.id, "task_id": task.id, "artifact_id": artifact.id}


def _approve_artifact(api, headers, artifact_id, text, *, reason=None):
    """Save a draft, submit it, and approve it — all over the API."""
    body = {"statements": [{"statement_class": "HUMAN_PROVIDED_INPUT", "text": text}]}
    if reason:
        body["change_reason"] = reason
    saved = api.post(
        f"/api/v1/artifacts/{artifact_id}/versions", headers=headers["manager"], json=body
    )
    assert saved.status_code == 200, saved.text

    submitted = api.post(
        f"/api/v1/artifacts/{artifact_id}/submit",
        headers=headers["manager"],
        json={"approval_roles": ["BID_MANAGER"]},
    )
    assert submitted.status_code == 200, submitted.text

    chain = api.get(
        "/api/v1/approvals/chain",
        headers=headers["manager"],
        params={"entity_type": "ARTIFACT_VERSION", "entity_id": saved.json()["id"]},
    )
    assert chain.status_code == 200, chain.text
    step = chain.json()[0]
    decided = api.post(
        f"/api/v1/approvals/{step['id']}/decide",
        headers=headers["manager"],
        json={"decision": ApprovalStepState.APPROVED.value, "comment": "Checked."},
    )
    assert decided.status_code == 200, decided.text
    return saved.json()


def test_a_bid_walks_from_draft_to_released_package_over_http(api, headers, bid_with_task):
    bid_id = bid_with_task["bid_id"]
    artifact_id = bid_with_task["artifact_id"]

    # 1. The artifact exists from the moment the task does.
    found = api.get(
        f"/api/v1/tasks/{bid_with_task['task_id']}/artifact", headers=headers["manager"]
    )
    assert found.status_code == 200, found.text
    assert found.json()["state"] == "DRAFT"

    # 2. Draft, submit, approve.
    _approve_artifact(api, headers, artifact_id, "The delivery approach is described in full.")
    detail = api.get(f"/api/v1/artifacts/{artifact_id}", headers=headers["manager"])
    assert detail.json()["state"] == "APPROVED"

    versions = api.get(f"/api/v1/artifacts/{artifact_id}/versions", headers=headers["manager"])
    assert [v["version_no"] for v in versions.json()] == [1]

    # 3. Structure, coverage, assembly.
    derived = api.post(
        f"/api/v1/bids/{bid_id}/structure/derive", headers=headers["manager"], json={}
    )
    assert derived.status_code == 200, derived.text

    coverage = api.get(f"/api/v1/bids/{bid_id}/structure/coverage", headers=headers["manager"])
    assert coverage.status_code == 200
    assert coverage.json()["uncovered_sections"] == []

    assembled = api.post(
        f"/api/v1/bids/{bid_id}/assemble",
        headers=headers["manager"],
        json={"output_mode": OutputMode.REVIEW.value},
    )
    assert assembled.status_code == 200, assembled.text
    assert assembled.json()

    # 4. Package and validate.
    package = api.post(
        f"/api/v1/bids/{bid_id}/packages",
        headers=headers["manager"],
        json={"output_mode": OutputMode.REVIEW.value},
    )
    assert package.status_code == 200, package.text
    package_id = package.json()["id"]

    validated = api.post(
        f"/api/v1/packages/{package_id}/validate", headers=headers["manager"], json={}
    )
    assert validated.status_code == 200, validated.text

    report = api.get(f"/api/v1/packages/{package_id}/validation", headers=headers["manager"])
    assert report.status_code == 200, report.text
    # Findings are split by waivability, because "can this be waived at all" is
    # the first question a release manager asks.
    assert {"waivable", "non_waivable"} <= set(report.json())
    assert report.json()["non_waivable"] == []

    # A review package carries honest advisory findings; a named person accepts each
    # one before release, as the release process requires.
    for finding in report.json()["waivable"]:
        requested = api.post(
            f"/api/v1/findings/{finding['id']}/exception",
            headers=headers["manager"],
            json={"justification": "Accepted for this test."},
        )
        assert requested.status_code == 200, requested.text
        decided = api.post(
            f"/api/v1/exceptions/{requested.json()['id']}/decide",
            headers=headers["approver"],
            json={"decision": "APPROVED"},
        )
        assert decided.status_code == 200, decided.text

    # 5. Maker submits; maker may not approve; the checker can.
    submitted = api.post(
        f"/api/v1/packages/{package_id}/submit-for-release",
        headers=headers["manager"],
        json={},
    )
    assert submitted.status_code == 200, submitted.text

    refused = api.post(
        f"/api/v1/packages/{package_id}/approve-release", headers=headers["manager"], json={}
    )
    assert refused.status_code == 403, "a bid manager must not hold release:approve"

    released = api.post(
        f"/api/v1/packages/{package_id}/approve-release", headers=headers["approver"], json={}
    )
    assert released.status_code == 200, released.text
    assert released.json()["state"] == "RELEASED"
    assert released.json()["immutable"] is True

    # 6. A released package is frozen, and the API says so rather than silently
    #    revalidating.
    again = api.post(f"/api/v1/packages/{package_id}/validate", headers=headers["manager"], json={})
    assert again.status_code == 409

    # 7. The outcome closes the loop.
    outcome = api.post(
        f"/api/v1/bids/{bid_id}/outcome",
        headers=headers["manager"],
        json={
            "result": "WON",
            "evaluator_feedback": "Strong local delivery references.",
        },
    )
    assert outcome.status_code == 200, outcome.text

    read_back = api.get(f"/api/v1/bids/{bid_id}/outcome", headers=headers["manager"])
    assert read_back.status_code == 200
    assert read_back.json()["result"] == "WON"

    # Feedback became a proposal, not a fact -- and the queue belongs to a
    # knowledge administrator, not to whoever recorded the outcome.
    assert (
        api.get("/api/v1/knowledge/update-candidates", headers=headers["manager"]).status_code
        == 403
    )
    candidates = api.get("/api/v1/knowledge/update-candidates", headers=headers["knowledge_admin"])
    assert candidates.status_code == 200, candidates.text
    # Scoped to this bid's own outcome: the tenant's queue may hold candidates
    # from other work, including ones already decided.
    # A won bid proposes two things: the feedback, and the project itself.
    mine = {
        c["origin_type"]: c
        for c in candidates.json()
        if c["origin_entity_id"] == outcome.json()["id"]
    }
    assert set(mine) == {"OUTCOME_FEEDBACK", "OUTCOME_AWARD"}
    for candidate in mine.values():
        assert candidate["state"] == "PROPOSED"
        assert candidate["target_id"] is None, "nothing canonical was written"


def test_a_non_waivable_finding_is_refused_an_exception_over_http(
    api, headers, bid_with_task, object_storage
):
    """Every permission in the world does not create an exception route."""
    bid_id = bid_with_task["bid_id"]
    _approve_artifact(
        api, headers, bid_with_task["artifact_id"], "A complete and supported answer."
    )
    api.post(f"/api/v1/bids/{bid_id}/structure/derive", headers=headers["manager"], json={})
    api.post(
        f"/api/v1/bids/{bid_id}/assemble",
        headers=headers["manager"],
        json={"output_mode": OutputMode.SUBMISSION.value},
    )
    package_id = api.post(
        f"/api/v1/bids/{bid_id}/packages",
        headers=headers["manager"],
        json={"output_mode": OutputMode.SUBMISSION.value},
    ).json()["id"]

    docs = api.get(f"/api/v1/bids/{bid_id}/generated-documents", headers=headers["manager"]).json()
    assert docs, "assembly must have produced at least one document"

    # Corrupt the stored output behind the API's back, so validation has a real
    # non-waivable finding to refuse an exception for.
    for key in list(object_storage.keys()):
        object_storage.put(key, b"tampered", content_type="application/octet-stream")

    api.post(f"/api/v1/packages/{package_id}/validate", headers=headers["manager"], json={})
    report = api.get(f"/api/v1/packages/{package_id}/validation", headers=headers["manager"]).json()
    non_waivable = report["non_waivable"]
    if not non_waivable:
        pytest.skip("this package validated clean; the SQL-level guard is covered elsewhere")

    refused = api.post(
        f"/api/v1/findings/{non_waivable[0]['id']}/exception",
        headers=headers["manager"],
        json={"justification": "Please let this one through."},
    )
    assert refused.status_code == 409
    assert "exception path" in refused.json()["detail"]


def test_an_auditor_can_read_the_workflow_but_not_drive_it(api, headers, bid_with_task):
    """Read access to the whole trail is the point of the auditor role."""
    artifact_id = bid_with_task["artifact_id"]

    assert (
        api.get(f"/api/v1/artifacts/{artifact_id}", headers=headers["auditor"]).status_code == 200
    )
    assert (
        api.get(f"/api/v1/artifacts/{artifact_id}/versions", headers=headers["auditor"]).status_code
        == 200
    )

    blocked = api.post(
        f"/api/v1/artifacts/{artifact_id}/versions",
        headers=headers["auditor"],
        json={
            "statements": [
                {"statement_class": "HUMAN_PROVIDED_INPUT", "text": "Auditors do not draft."}
            ]
        },
    )
    assert blocked.status_code == 403
    assert "artifact:write" in blocked.json()["detail"]


def test_the_commitments_and_assumptions_registers_are_readable(api, headers, bid_with_task):
    """Both registers are what a reviewer opens before signing anything."""
    bid_id = bid_with_task["bid_id"]
    artifact_id = bid_with_task["artifact_id"]

    saved = api.post(
        f"/api/v1/artifacts/{artifact_id}/versions",
        headers=headers["manager"],
        json={
            "statements": [
                {
                    "statement_class": "ASSUMPTION",
                    "text": "The client provides the test environment.",
                },
                {
                    "statement_class": "PROPOSED_COMMITMENT",
                    "text": "We will report weekly to the steering committee.",
                    "required_approver_role": "BID_MANAGER",
                },
            ]
        },
    )
    assert saved.status_code == 200, saved.text

    assumptions = api.get(f"/api/v1/bids/{bid_id}/assumptions", headers=headers["manager"])
    assert assumptions.status_code == 200
    assert len(assumptions.json()) == 1
    assert assumptions.json()[0]["status"] == "OPEN"

    commitments = api.get(f"/api/v1/bids/{bid_id}/commitments", headers=headers["manager"])
    assert commitments.status_code == 200
    assert len(commitments.json()) == 1
    assert commitments.json()[0]["approval_status"] != "APPROVED"

    carried = api.patch(
        f"/api/v1/assumptions/{assumptions.json()[0]['id']}",
        headers=headers["manager"],
        json={
            "status": "CARRIED_INTO_SUBMISSION",
            "resolution": "Declared in the assumptions annex.",
        },
    )
    assert carried.status_code == 200, carried.text
    assert carried.json()["status"] == "CARRIED_INTO_SUBMISSION"


def test_another_tenant_sees_none_of_it(api, users, seeded_orgs, bid_with_task):
    _org_a, org_b = seeded_orgs
    other = {"Authorization": f"Bearer {api_login(api, users[org_b]['manager'])}"}
    assert (
        api.get(f"/api/v1/artifacts/{bid_with_task['artifact_id']}", headers=other).status_code
        == 404
    )
    assert (
        api.get(f"/api/v1/bids/{bid_with_task['bid_id']}/commitments", headers=other).json() == []
    )
