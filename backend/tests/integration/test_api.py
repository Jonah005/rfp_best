"""End-to-end API tests: login, RBAC enforcement, error shape, tenant scoping."""

from __future__ import annotations

import pytest

from app.core.permissions import RoleCode
from tests.integration.conftest import PASSWORD, api_login

pytestmark = pytest.mark.integration


def test_login_and_me(api, users, seeded_orgs):
    client = api
    org_a, _ = seeded_orgs
    token = api_login(client, users[org_a]["manager"])
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["org_id"] == str(org_a)
    assert RoleCode.BID_MANAGER.value in body["roles"]
    assert "bid:read" in body["permissions"]


def test_bad_password_is_rejected_with_a_problem_document(api, users, seeded_orgs):
    client = api
    org_a, _ = seeded_orgs
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": users[org_a]["manager"], "password": "wrong-password-here"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["type"].endswith("authentication_required")
    assert body["status"] == 401
    assert "request_id" in body


def test_unauthenticated_requests_are_refused(api):
    client = api
    assert client.get("/api/v1/admin/users").status_code == 401


def test_rbac_blocks_a_role_without_the_permission(api, users, seeded_orgs):
    """An auditor may read audit events but must not create users."""
    client = api
    org_a, _ = seeded_orgs
    token = api_login(client, users[org_a]["auditor"])
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/v1/audit", headers=headers).status_code == 200

    resp = client.post(
        "/api/v1/admin/users",
        headers=headers,
        json={"email": "nope@a.example.com", "display_name": "Nope", "password": "LongEnough-123"},
    )
    assert resp.status_code == 403
    assert resp.json()["type"].endswith("permission_denied")
    assert "user:manage" in resp.json()["detail"]


def test_listing_users_returns_only_the_callers_tenant(api, users, seeded_orgs):
    client = api
    org_a, org_b = seeded_orgs
    # The auditor holds user:read but not user:manage, which is exactly the
    # separation this test relies on.
    token = api_login(client, users[org_a]["auditor"])
    resp = client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    emails = {u["email"] for u in resp.json()}
    assert users[org_a]["manager"] in emails
    assert users[org_b]["manager"] not in emails, "cross-tenant user leaked into the list"


def test_a_token_from_tenant_b_cannot_read_tenant_a(api, users, seeded_orgs):
    client = api
    org_a, org_b = seeded_orgs
    token_b = api_login(client, users[org_b]["auditor"])
    resp = client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {token_b}"})
    assert resp.status_code == 200
    emails = {u["email"] for u in resp.json()}
    assert users[org_a]["auditor"] not in emails


def test_refresh_token_cannot_be_used_as_an_access_token(api, users, seeded_orgs):
    client = api
    org_a, _ = seeded_orgs
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": users[org_a]["manager"], "password": PASSWORD},
    )
    refresh = resp.json()["refresh_token"]
    denied = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    assert denied.status_code == 401


def test_health_live_needs_no_auth(api):
    client = api
    resp = client.get("/api/v1/health/live")
    assert resp.status_code == 200 and resp.json()["status"] == "ok"


def test_request_id_is_returned_on_every_response(api):
    client = api
    resp = client.get("/api/v1/health/live")
    assert "X-Request-ID" in resp.headers


def test_an_admin_can_write_a_department_responsibility_repeatedly(api, users, seeded_orgs):
    """Several saves in a row, each one landing -- the screen seemed to stop saving."""
    client = api
    org_a, _ = seeded_orgs
    token = api_login(client, users[org_a]["admin"])
    headers = {"Authorization": f"Bearer {token}"}
    departments = client.get("/api/v1/admin/departments", headers=headers).json()
    assert departments and all(d["responsibilities"] for d in departments), "defaults shown"
    for text in ("licence terms and usage rights", "contracts and NDAs", ""):
        for department in departments[:3]:
            resp = client.patch(
                f"/api/v1/admin/departments/{department['id']}/responsibilities",
                json={"responsibilities": text},
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["responsibilities"], "blank restores the default"
    names = [d["name"] for d in client.get("/api/v1/admin/departments", headers=headers).json()]
    assert names == sorted(names), "a save must not reshuffle the list"


def test_a_non_admin_cannot_write_a_responsibility(api, users, seeded_orgs):
    client = api
    org_a, _ = seeded_orgs
    token = api_login(client, users[org_a]["manager"])
    headers = {"Authorization": f"Bearer {token}"}
    admin = api_login(client, users[org_a]["admin"])
    department = client.get(
        "/api/v1/admin/departments", headers={"Authorization": f"Bearer {admin}"}
    ).json()[0]
    resp = client.patch(
        f"/api/v1/admin/departments/{department['id']}/responsibilities",
        json={"responsibilities": "anything"},
        headers=headers,
    )
    assert resp.status_code == 403
