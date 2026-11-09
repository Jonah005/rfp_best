"""A clause that binds the customer does not sit inside company work.

Reproduces the exported state: tasks were generated while a clause still read
as the bidder's, the assessment later labelled it CUSTOMER_OBLIGATION, and the
matrix showed a task ID and a NEEDS_INPUT state against it. Four tasks held
nothing else.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.gateway.embeddings import HashingEmbeddingProvider
from app.ingestion.safety import EicarAwareScanner
from app.models import Requirement, RequirementAssessment, Task, TaskRequirement
from app.models.enums import (
    BidReadiness,
    DepartmentCode,
    ObligationOwner,
    RequirementCategory,
)
from app.services import assessment as assessment_service
from app.services import tasks as task_service
from app.services.storage import InMemoryStorage
from tests.integration.test_assessment_readiness import (
    ISO_TEXT,
    TODAY,
    _company_version,
    _new_bid,
    _requirement,
)
from tests.integration.test_tasks_and_requests import _departments, _user

pytestmark = pytest.mark.integration


@pytest.fixture
def fresh_org(owner_engine):
    from sqlalchemy.orm import sessionmaker

    from app.models import Organization

    org_id = uuid.uuid4()
    with sessionmaker(bind=owner_engine, future=True)() as s:
        s.add(
            Organization(id=org_id, name=f"Cust {org_id.hex[:6]}", slug=f"cust-{org_id.hex[:10]}")
        )
        s.commit()
    return org_id


@pytest.fixture
def env(fresh_org):
    return fresh_org, InMemoryStorage(), EicarAwareScanner(), HashingEmbeddingProvider()


CLAUSE = "Jadwa may cancel the project or negotiate terms at its sole discretion."


def _build(s, ctx, env_):
    _departments(s, ctx.org_id, [DepartmentCode.LEGAL, DepartmentCode.SECURITY])
    bid = _new_bid(s, ctx)
    version = _company_version(s, ctx, env_)
    legal = _requirement(
        s,
        ctx,
        bid=bid,
        doc_version=version,
        code="R-011",
        text=CLAUSE,
        category=RequirementCategory.LEGAL,
    )
    mixed_customer = _requirement(
        s,
        ctx,
        bid=bid,
        doc_version=version,
        code="R-037",
        text=CLAUSE,
        category=RequirementCategory.SECURITY,
    )
    bidder = _requirement(
        s,
        ctx,
        bid=bid,
        doc_version=version,
        code="R-038",
        text=ISO_TEXT,
        category=RequirementCategory.SECURITY,
    )
    task_service.generate_tasks(s, ctx, bid_id=bid.id)
    # The label arrives after the tasks exist, exactly as on the live bid.
    for requirement in (legal, mixed_customer):
        requirement.obligation_owner = ObligationOwner.CUSTOMER
        result = assessment_service.assess_requirement(s, ctx, requirement=requirement, today=TODAY)
        assert result.bid_readiness is BidReadiness.CUSTOMER_OBLIGATION
    s.flush()
    return bid, legal, mixed_customer, bidder


def _links(s, ctx, bid):
    return {
        (t.code, link.requirement_id)
        for t, link in s.execute(
            select(Task, TaskRequirement)
            .join(TaskRequirement, TaskRequirement.task_id == Task.id)
            .where(Task.bid_id == bid.id)
        ).all()
    }


def test_a_customer_only_task_is_removed_and_a_mixed_one_keeps_its_bidder_work(
    app_sessionmaker, env
):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid, legal, mixed_customer, bidder = _build(s, ctx, env)

        assert any(rid == legal.id for _c, rid in _links(s, ctx, bid))

        done = task_service.release_customer_clauses(s, ctx, bid_id=bid.id)
        assert done["unlinked"] == 2
        assert done["tasks_removed"] == 1

        after = _links(s, ctx, bid)
        assert not any(rid in (legal.id, mixed_customer.id) for _c, rid in after)
        assert any(rid == bidder.id for _c, rid in after)
        s.commit()


def test_a_task_somebody_owns_keeps_every_link(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid, legal, _mc, _b = _build(s, ctx, env)
        task = s.execute(
            select(Task)
            .join(TaskRequirement, TaskRequirement.task_id == Task.id)
            .where(TaskRequirement.requirement_id == legal.id)
        ).scalar_one()
        task.owner_user_id = _user(s, org, "sara").id
        s.flush()

        done = task_service.release_customer_clauses(s, ctx, bid_id=bid.id)
        assert done["tasks_removed"] == 0
        assert any(rid == legal.id for _c, rid in _links(s, ctx, bid))
        s.commit()


def test_it_runs_with_the_state_pass_and_twice_changes_nothing(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid, legal, mixed_customer, bidder = _build(s, ctx, env)
        task_service.seed_initial_states(s, ctx, bid_id=bid.id)
        first = _links(s, ctx, bid)
        task_service.seed_initial_states(s, ctx, bid_id=bid.id)
        assert _links(s, ctx, bid) == first
        assert not any(rid in (legal.id, mixed_customer.id) for _c, rid in first)
        s.commit()


# --- a clause that hands something over is not excused -----------------------

HANDOVER = (
    "Jadwa will provide access to Core OMS system for the following purpose: check "
    "client already exists; create client account by adding all collected information."
)


def test_a_customer_clause_that_hands_something_over_keeps_its_row_and_its_task(
    app_sessionmaker, env
):
    """R-033: the customer provides OMS access, the bidder builds the integration.

    The extractor labelled it CUSTOMER and did not split it. Excusing it removed
    the largest integration on the tender from every list, so it stays with a
    person, with evidence searched and a task generated.
    """
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _departments(s, org, [DepartmentCode.PMO, DepartmentCode.LEGAL])
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        handover = _requirement(
            s,
            ctx,
            bid=bid,
            doc_version=version,
            code="R-033",
            text=HANDOVER,
            category=RequirementCategory.IMPLEMENTATION,
        )
        rights = _requirement(
            s,
            ctx,
            bid=bid,
            doc_version=version,
            code="R-011",
            text=CLAUSE,
            category=RequirementCategory.LEGAL,
        )
        for requirement in (handover, rights):
            requirement.obligation_owner = ObligationOwner.CUSTOMER
            assessment_service.assess_requirement(s, ctx, requirement=requirement, today=TODAY)
        s.flush()

        current = {
            r.code: a.bid_readiness
            for r, a in s.execute(
                select(Requirement, RequirementAssessment)
                .join(RequirementAssessment, RequirementAssessment.requirement_id == Requirement.id)
                .where(RequirementAssessment.is_current.is_(True), Requirement.bid_id == bid.id)
            ).all()
        }
        assert current["R-033"] is BidReadiness.NEEDS_COMPANY_CONFIRMATION
        assert current["R-011"] is BidReadiness.CUSTOMER_OBLIGATION

        task_service.generate_tasks(s, ctx, bid_id=bid.id)
        covered = {rid for _c, rid in _links(s, ctx, bid)}
        assert handover.id in covered
        assert rights.id not in covered
        s.commit()


# --- a handover whose bidder half was extracted -------------------------------

SPLIT_CLAUSE = "Integration Requirements / 2"


def _in_clause(session, requirement, ref):
    from app.models import RequirementVersion

    version = session.get(RequirementVersion, requirement.current_version_id)
    version.clause_ref = ref
    session.flush()


def test_a_handover_whose_bidder_work_was_extracted_is_only_context(app_sessionmaker, env):
    """R-092 after decomposition v12: the customer row stayed flagged as hiding
    bidder work that R-093 to R-097 now state, so the same work sat in Gaps twice."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        handover = _requirement(
            s,
            ctx,
            bid=bid,
            doc_version=version,
            code="R-092",
            text=HANDOVER,
            category=RequirementCategory.IMPLEMENTATION,
        )
        child = _requirement(
            s,
            ctx,
            bid=bid,
            doc_version=version,
            code="R-093",
            text="Using the Core OMS system access Jadwa provides, the solution shall check "
            "whether the client already exists.",
            category=RequirementCategory.FUNCTIONAL,
        )
        handover.obligation_owner = ObligationOwner.CUSTOMER
        _in_clause(s, handover, SPLIT_CLAUSE)
        _in_clause(s, child, SPLIT_CLAUSE)

        result = assessment_service.assess_requirement(s, ctx, requirement=handover, today=TODAY)
        assert result.bid_readiness is BidReadiness.CUSTOMER_OBLIGATION
        assert "own requirement" in result.rationale
        s.commit()


def test_a_neighbour_that_says_nothing_of_the_handover_does_not_switch_the_guard_off(
    app_sessionmaker, env
):
    """SMS: the engine and Arabic-support rows share the clause with the handover
    and are not its use. If only they exist the handover was NOT split."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        handover = _requirement(
            s,
            ctx,
            bid=bid,
            doc_version=version,
            code="R-037",
            text="Jadwa will provide SMS service connection details to send validation SMS "
            "to applicant.",
            category=RequirementCategory.IMPLEMENTATION,
        )
        neighbour = _requirement(
            s,
            ctx,
            bid=bid,
            doc_version=version,
            code="R-039",
            text="Develop engine to promptly send SMS",
            category=RequirementCategory.TECHNICAL,
        )
        handover.obligation_owner = ObligationOwner.CUSTOMER
        _in_clause(s, handover, "Integration Requirements / 4")
        _in_clause(s, neighbour, "Integration Requirements / 4")

        result = assessment_service.assess_requirement(s, ctx, requirement=handover, today=TODAY)
        assert result.bid_readiness is BidReadiness.NEEDS_COMPANY_CONFIRMATION
        s.commit()


def test_a_requirement_from_a_continuation_block_shows_the_clause_it_continues(
    app_sessionmaker, env
):
    """R-095 to R-097 were the last three bullets of the Core OMS list, cut off by
    a page break, and their Clause ref cell was blank."""
    import sys
    from pathlib import Path

    from app.models import DocumentBlock, RequirementVersion
    from app.models.enums import BlockType

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from _export_queries import continued_clause_ref

    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        head = ["Integration Requirements"]
        s.add_all(
            [
                DocumentBlock(
                    org_id=org,
                    document_version_id=version.id,
                    block_type=BlockType.PARAGRAPH,
                    ordinal=901,
                    text="2. Tradenet ... Create client account",
                    clause_ref="2",
                    heading_path=head,
                    char_start=1000,
                    char_end=1099,
                ),
                DocumentBlock(
                    org_id=org,
                    document_version_id=version.id,
                    block_type=BlockType.LIST_ITEM,
                    ordinal=902,
                    text="Create Beneficary accounts",
                    clause_ref=None,
                    heading_path=head,
                    char_start=1100,
                    char_end=1150,
                ),
                DocumentBlock(
                    org_id=org,
                    document_version_id=version.id,
                    block_type=BlockType.PARAGRAPH,
                    ordinal=903,
                    text="An unrelated paragraph",
                    clause_ref=None,
                    heading_path=head,
                    char_start=1300,
                    char_end=1350,
                ),
            ]
        )
        s.flush()
        requirement = _requirement(
            s, ctx, bid=bid, doc_version=version, code="R-095", text="Create beneficiary accounts."
        )
        requirement_version = s.get(RequirementVersion, requirement.current_version_id)
        requirement_version.source_document_version_id = version.id
        requirement_version.source_locator = {"char_start": 1100, "char_end": 1150}
        s.flush()
        assert continued_clause_ref(s, org, requirement_version) == (
            "Integration Requirements / 2 (continued)"
        )
        # Not contiguous with anything that has a reference: stays blank.
        requirement_version.source_locator = {"char_start": 1300, "char_end": 1350}
        s.flush()
        assert continued_clause_ref(s, org, requirement_version) == ""
        s.commit()


def test_a_stale_grouping_explanation_is_replaced_with_the_current_membership(
    app_sessionmaker, env
):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid, _legal, _mc, bidder = _build(s, ctx, env)
        task = s.execute(
            select(Task)
            .join(TaskRequirement, TaskRequirement.task_id == Task.id)
            .where(TaskRequirement.requirement_id == bidder.id)
        ).scalar_one()
        # Written before the set was recorded, describing clauses since released.
        task.grouping_rationale = "Covers Al-Elm and Core OMS access, both requirements."
        s.flush()

        assert task_service.refresh_stale_rationales(s, ctx, bid_id=bid.id) >= 1
        assert "Al-Elm" not in task.grouping_rationale
        assert bidder.code in task.grouping_rationale
        # Recorded membership matches, so a second pass leaves it alone.
        again = task.grouping_rationale
        assert task_service.refresh_stale_rationales(s, ctx, bid_id=bid.id) == 0
        assert task.grouping_rationale == again

        # A person's task is never rewritten.
        task.owner_user_id = _user(s, org, "sara").id
        task.grouping_rationale = "Sara's own note."
        s.flush()
        task_service.refresh_stale_rationales(s, ctx, bid_id=bid.id)
        assert task.grouping_rationale == "Sara's own note."
        s.commit()


def test_a_withdrawn_requirement_is_not_counted_in_the_explanation(app_sessionmaker, env):
    from app.models.enums import RequirementLifecycle

    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid, _legal, mixed, bidder = _build(s, ctx, env)
        task = s.execute(
            select(Task)
            .join(TaskRequirement, TaskRequirement.task_id == Task.id)
            .where(TaskRequirement.requirement_id == bidder.id)
        ).scalar_one()
        # A retired requirement still linked to the task, as after re-extraction.
        other = mixed
        other.lifecycle_state = RequirementLifecycle.WITHDRAWN
        task.grouping_rationale = "old prose"
        s.flush()

        task_service.refresh_stale_rationales(s, ctx, bid_id=bid.id)
        assert bidder.code in task.grouping_rationale
        assert other.code not in task.grouping_rationale
        assert "1 requirement(s)" in task.grouping_rationale
        s.commit()
