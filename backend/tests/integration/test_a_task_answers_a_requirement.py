"""A task must answer a requirement of its bid; the service enforces it, not the fixtures."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.errors import ValidationError
from app.models import Department
from app.services import bids, tasks
from tests.integration.test_release_lifecycle import _ctx, _user

pytestmark = [pytest.mark.integration, pytest.mark.bare_tasks]


def test_a_task_with_no_requirement_is_refused(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        maker = _user(session, org_id, users[org_id]["manager"])
        ctx = _ctx(org_id, maker.id, "BID_MANAGER")
        opportunity = bids.create_opportunity(
            session, ctx, customer_name="Rule Customer", tender_name=f"Rule {uuid.uuid4().hex[:6]}"
        )
        bid = bids.promote_to_bid(session, ctx, opportunity_id=opportunity.id)
        department = (
            session.execute(select(Department).where(Department.org_id == org_id)).scalars().first()
        )
        assert department is not None
        with pytest.raises(ValidationError, match="at least one requirement"):
            tasks.create_task(
                session, ctx, bid_id=bid.id, title="Nothing to answer", department_id=department.id
            )
        session.rollback()
