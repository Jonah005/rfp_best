"""A scorecard a person can compare between tenders.

"Was this document read as well as the last one" has been answered so far by
reading exports and arguing about them. These are the numbers that settle it,
counted from what the pipeline actually wrote: what parsed, what was read,
what produced requirements, what nothing accounts for, and how many open
findings can actually be put to the customer.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.models.enums import DocumentType
from app.services import precedence, requirements
from tests.integration.test_tender_intelligence import (
    RFP_TEXT,
    _decomposition,
    _new_bid,
    _upload,
    env,  # noqa: F401 - pytest fixture
    gateway_for,
)

# ruff: noqa: F811 - `env` is a fixture, imported and then requested by name

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "score_extraction.py"


def _score_module():
    spec = importlib.util.spec_from_file_location("score_extraction", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_scorecard_reports_the_run_it_measured(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)
        report = requirements.extract_requirements(
            s,
            ctx,
            bid_id=bid.id,
            gateway=gateway_for({"requirement_decomposition": _decomposition}),
        )
        assert report.requirements_created > 0

        card = _score_module().score(s, ctx, bid)

        assert card["parsing"]["tender_documents"] == 1
        assert card["parsing"]["blocks"] > 0
        assert card["extraction"]["requirements"] == report.requirements_created
        assert card["extraction"]["blocks_read"] > 0
        assert card["extraction"]["blocks_never_read"] == 0, "this run read every eligible block"
        assert card["sections"]["found"] >= 1
        assert 0 <= card["sections"]["largest_share_pct"] <= 100
        # The comparable part has to be numbers, not None.
        assert isinstance(card["questions"]["askable_pct"], float)
        assert card["questions"]["closed_without_a_reason"] == 0
        s.rollback()


def test_a_block_nothing_accounted_for_is_counted(app_sessionmaker, env):
    """The number that matters most: an obligation nobody will ever answer."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)
        # Nothing extracted at all: every eligible block is unaccounted for.
        card = _score_module().score(s, ctx, bid)
        assert card["extraction"]["blocks_read"] == 0
        assert card["extraction"]["blocks_never_read"] == card["extraction"]["eligible_blocks"] > 0
        assert card["extraction"]["requirements"] == 0
        s.rollback()
