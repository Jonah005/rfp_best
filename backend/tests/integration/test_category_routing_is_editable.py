"""An organization can send a requirement category to a different department."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.context import ExecutionContext
from app.core.errors import ValidationError
from app.db.session import apply_tenant_context
from app.models import Department
from app.models.enums import DepartmentCode, RequirementCategory
from app.services import category_routing

pytestmark = pytest.mark.integration


def _open(app_engine, org):
    factory = sessionmaker(bind=app_engine, autoflush=False, expire_on_commit=False, future=True)
    s = factory()
    s.begin()
    ctx = ExecutionContext(org_id=org)
    apply_tenant_context(s, ctx)
    return s, ctx


def _dept(s, org, code):
    d = s.execute(
        select(Department).where(Department.org_id == org, Department.code == code)
    ).scalar_one_or_none()
    if d is None:
        d = Department(org_id=org, code=code, name=code.value.title())
        s.add(d)
        s.flush()
    return d


def test_the_default_applies_until_it_is_changed_and_again_once_reset(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        finance = _dept(s, org, DepartmentCode.FINANCE)
        _dept(s, org, DepartmentCode.COMMERCIAL)
        assert (
            category_routing.routes(s, ctx)[RequirementCategory.COMMERCIAL]
            is DepartmentCode.COMMERCIAL
        )

        category_routing.set_route(s, ctx, category="COMMERCIAL", department_id=finance.id)
        assert (
            category_routing.routes(s, ctx)[RequirementCategory.COMMERCIAL]
            is DepartmentCode.FINANCE
        )
        row = next(
            r for r in category_routing.describe(s, ctx)["routes"] if r["category"] == "COMMERCIAL"
        )
        assert row["is_default"] is False and row["department_id"] == str(finance.id)

        category_routing.reset_route(s, ctx, category="COMMERCIAL")
        assert (
            category_routing.routes(s, ctx)[RequirementCategory.COMMERCIAL]
            is DepartmentCode.COMMERCIAL
        )
    finally:
        s.rollback()
        s.close()


def test_an_unknown_category_is_refused(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        d = _dept(s, org, DepartmentCode.FINANCE)
        with pytest.raises(ValidationError):
            category_routing.set_route(s, ctx, category="NOT_A_CATEGORY", department_id=d.id)
    finally:
        s.rollback()
        s.close()


def test_a_changed_route_reaches_the_clarification_department(app_engine, seeded_orgs):
    from app.gateway.embeddings import HashingEmbeddingProvider
    from app.ingestion.safety import EicarAwareScanner
    from app.services.storage import InMemoryStorage
    from tests.integration.test_assessment_readiness import (
        _company_version,
        _new_bid,
        _requirement,
    )
    from tests.integration.test_clarifications_have_owners import _issue, _staffed

    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        finance, head, _ = _staffed(s, ctx, org, DepartmentCode.FINANCE)
        _staffed(s, ctx, org, DepartmentCode.COMMERCIAL)
        category_routing.set_route(s, ctx, category="COMMERCIAL", department_id=finance.id)
        bid = _new_bid(s, ctx)
        env = (org, InMemoryStorage(), EicarAwareScanner(), HashingEmbeddingProvider())
        version = _company_version(s, ctx, env)
        req = _requirement(
            s,
            ctx,
            bid=bid,
            doc_version=version,
            code="R-9",
            text="Payment",
            category=RequirementCategory.COMMERCIAL,
        )
        issue = _issue(s, ctx, bid, "The payment schedule is unclear.", requirement_ids=[req.id])
        assert issue.department_id == finance.id and issue.owner_user_id == head.id
    finally:
        s.rollback()
        s.close()
