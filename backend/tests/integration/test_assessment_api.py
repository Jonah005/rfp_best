"""The Phase H HTTP surface, exercised the way a person would.

The services are tested elsewhere. What matters here is that the whole picture
is reachable over the API without a database client: run an assessment, open a
verdict, follow a dashboard number to the requirements behind it, and record a
decision. A number with no route to its evidence is a number nobody can defend
in a review.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.models.enums import BidNoBidResult, BidReadiness, EvidenceStatus
from tests.integration.conftest import api_login
from tests.integration.test_assessment_readiness import (
    ISO_TEXT,
    TODAY,
    _company_version,
    _evidence,
    _new_bid,
    _requirement,
)

pytestmark = pytest.mark.integration

from app.gateway.embeddings import HashingEmbeddingProvider  # noqa: E402
from app.ingestion.safety import EicarAwareScanner  # noqa: E402
from app.services.storage import InMemoryStorage  # noqa: E402


@pytest.fixture
def scenario(app_engine, seeded_orgs, seeded_users):
    """A bid in tenant A with one satisfied and one unevidenced requirement."""
    org_a, _ = seeded_orgs
    env = (org_a, InMemoryStorage(), EicarAwareScanner(), HashingEmbeddingProvider())
    factory = sessionmaker(bind=app_engine, autoflush=False, expire_on_commit=False, future=True)
    with factory() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org_a)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        _evidence(
            s, ctx, doc_version=version, excerpt=ISO_TEXT, expiry=TODAY + timedelta(days=365)
        )
        satisfied = _requirement(
            s, ctx, bid=bid, doc_version=version, code=f"SEC-{uuid.uuid4().hex[:4]}",
            text=ISO_TEXT,
        )
        unevidenced = _requirement(
            s,
            ctx,
            bid=bid,
            doc_version=version,
            code=f"ELG-{uuid.uuid4().hex[:4]}",
            text="Audited annual accounts covering three fiscal years accompany every offer.",
        )
        s.commit()
        return {
            "bid_id": bid.id,
            "satisfied_id": satisfied.id,
            "unevidenced_id": unevidenced.id,
        }


@pytest.fixture
def headers(api, users, seeded_orgs):
    org_a, _ = seeded_orgs
    return {"Authorization": f"Bearer {api_login(api, users[org_a]['manager'])}"}


def test_the_whole_assessment_picture_is_reachable_over_http(api, headers, scenario):
    bid_id = scenario["bid_id"]

    run = api.post(f"/api/v1/bids/{bid_id}/assess", headers=headers)
    assert run.status_code == 200, run.text
    report = run.json()
    assert report["assessed"] >= 2
    assert report["by_readiness"] and report["by_evidence_status"]

    detail = api.get(
        f"/api/v1/requirements/{scenario['satisfied_id']}/assessment", headers=headers
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["assessment"]["evidence_status"] == EvidenceStatus.CURRENT_VERIFIED.value
    assert body["assessment"]["rationale"]
    assert body["evidence"], "the verdict must arrive with the proof behind it"
    assert body["evidence"][0]["excerpt"]
    assert body["evidence"][0]["source_locator"], "evidence must be traceable to its document"

    summary = api.get(f"/api/v1/bids/{bid_id}/readiness", headers=headers)
    assert summary.status_code == 200, summary.text
    figures = summary.json()
    assert figures["applicable_requirements"] >= 2

    # Every figure opens.
    items = api.get(
        f"/api/v1/bids/{bid_id}/readiness/{BidReadiness.BLOCKED.value}/items", headers=headers
    )
    assert items.status_code == 200, items.text
    codes = [row["code"] for row in items.json()]
    assert codes, "the blocked count must open into the requirements behind it"
    assert all(row["rationale"] for row in items.json())


def test_an_unknown_readiness_metric_is_a_problem_document_not_a_crash(
    api, headers, scenario
):
    resp = api.get(
        f"/api/v1/bids/{scenario['bid_id']}/readiness/NEARLY_THERE/items", headers=headers
    )
    assert resp.status_code == 400
    assert "Unknown readiness metric" in resp.json()["detail"]


def test_an_unassessed_requirement_reads_as_not_found_rather_than_empty(
    api, headers, scenario
):
    """Silence and 'no evidence' must not look the same to a caller."""
    resp = api.get(f"/api/v1/requirements/{uuid.uuid4()}/assessment", headers=headers)
    assert resp.status_code == 404


def test_the_matrices_are_all_served(api, headers, scenario):
    api.post(f"/api/v1/bids/{scenario['bid_id']}/assess", headers=headers)
    for name in ("eligibility", "technical", "submission", "contract"):
        resp = api.get(
            f"/api/v1/bids/{scenario['bid_id']}/matrices/{name}", headers=headers
        )
        assert resp.status_code == 200, f"{name}: {resp.text}"
        assert isinstance(resp.json(), list)


def test_an_override_is_visible_in_the_history_it_created(api, headers, scenario):
    api.post(f"/api/v1/bids/{scenario['bid_id']}/assess", headers=headers)
    requirement_id = scenario["unevidenced_id"]

    refused = api.patch(
        f"/api/v1/requirements/{requirement_id}/assessment",
        headers=headers,
        json={"bid_readiness": BidReadiness.READY.value, "reason": "   "},
    )
    assert refused.status_code == 400

    ok = api.patch(
        f"/api/v1/requirements/{requirement_id}/assessment",
        headers=headers,
        json={
            "bid_readiness": BidReadiness.READY.value,
            "reason": "Accounts are held by Finance and will be attached at submission.",
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["bid_readiness"] == BidReadiness.READY.value
    assert ok.json()["assessment_state"] == "HUMAN_ADJUSTED"

    history = api.get(
        f"/api/v1/requirements/{requirement_id}/assessment/history", headers=headers
    )
    assert history.status_code == 200
    assert len(history.json()) >= 1


def test_the_recommendation_and_the_decision_are_separate_routes(api, headers, scenario):
    bid_id = scenario["bid_id"]
    api.post(f"/api/v1/bids/{bid_id}/assess", headers=headers)

    recommendation = api.get(f"/api/v1/bids/{bid_id}/bid-no-bid/recommendation", headers=headers)
    assert recommendation.status_code == 200, recommendation.text
    suggested = recommendation.json()
    assert suggested["result"] in {r.value for r in BidNoBidResult}
    assert "factors" in suggested

    assert api.get(f"/api/v1/bids/{bid_id}/bid-no-bid", headers=headers).status_code == 404

    recorded = api.post(
        f"/api/v1/bids/{bid_id}/bid-no-bid",
        headers=headers,
        json={"result": BidNoBidResult.GREEN.value, "rationale": "Proceeding; gaps are closable."},
    )
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["recommended_result"] == suggested["result"]

    current = api.get(f"/api/v1/bids/{bid_id}/bid-no-bid", headers=headers)
    assert current.status_code == 200
    assert current.json()["result"] == BidNoBidResult.GREEN.value


def test_a_reader_without_assessment_write_cannot_run_an_assessment(
    api, users, seeded_orgs, scenario
):
    org_a, _ = seeded_orgs
    auditor = {"Authorization": f"Bearer {api_login(api, users[org_a]['auditor'])}"}
    resp = api.post(f"/api/v1/bids/{scenario['bid_id']}/assess", headers=auditor)
    assert resp.status_code == 403
    assert "assessment:write" in resp.json()["detail"]


def test_a_token_from_another_tenant_sees_no_assessments(api, users, seeded_orgs, scenario):
    _org_a, org_b = seeded_orgs
    other = {"Authorization": f"Bearer {api_login(api, users[org_b]['manager'])}"}
    resp = api.get(
        f"/api/v1/requirements/{scenario['satisfied_id']}/assessment", headers=other
    )
    assert resp.status_code == 404
