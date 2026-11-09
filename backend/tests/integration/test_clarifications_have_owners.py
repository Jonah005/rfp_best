"""A clarification belongs to a department and is owned by a person in it."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.context import ExecutionContext
from app.core.errors import PermissionDenied
from app.core.permissions import Perm
from app.db.session import apply_tenant_context
from app.gateway.embeddings import HashingEmbeddingProvider
from app.ingestion.safety import EicarAwareScanner
from app.models import Department
from app.models.enums import (
    ClarificationIssueType,
    ClarificationSeverity,
    DepartmentCode,
)
from app.services import clarifications, identity
from app.services.storage import InMemoryStorage
from tests.integration.test_assessment_readiness import _company_version, _new_bid, _requirement

pytestmark = pytest.mark.integration


def _open(app_engine, org):
    factory = sessionmaker(bind=app_engine, autoflush=False, expire_on_commit=False, future=True)
    s = factory()
    s.begin()
    ctx = ExecutionContext(
        org_id=org, permissions=frozenset({Perm.TASK_ASSIGN.value, Perm.BID_READ.value})
    )
    apply_tenant_context(s, ctx)
    return s, ctx


def _staffed(s, ctx, org, code):
    """The department with this code, with a head and one team member."""
    dept = s.execute(
        select(Department).where(Department.org_id == org, Department.code == code)
    ).scalar_one_or_none()
    if dept is None:
        dept = Department(org_id=org, code=code, name=code.value.title())
        s.add(dept)
        s.flush()
    dept.head_user_id = None
    s.flush()
    head = identity.create_user(
        s,
        ctx,
        email=f"h-{uuid.uuid4().hex[:8]}@example.com",
        display_name="Head",
        department_ids_to_assign=[dept.id],
    )
    mate = identity.create_user(
        s,
        ctx,
        email=f"m-{uuid.uuid4().hex[:8]}@example.com",
        display_name="Mate",
        department_ids_to_assign=[dept.id],
    )
    return dept, head, mate


def _issue(s, ctx, bid, text, **extra):
    return clarifications.raise_issue(
        s,
        ctx,
        bid_id=bid.id,
        issue_type=ClarificationIssueType.AMBIGUITY
        if hasattr(ClarificationIssueType, "AMBIGUITY")
        else list(ClarificationIssueType)[0],
        description=text,
        severity=ClarificationSeverity.MAJOR,
        fingerprint=uuid.uuid4().hex,
        detected_by="test",
        **extra,
    )


def test_a_clarification_goes_to_the_department_that_answers_for_it(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        legal, legal_head, _ = _staffed(s, ctx, org, DepartmentCode.LEGAL)
        bidm, bid_head, _ = _staffed(s, ctx, org, DepartmentCode.BID_MANAGEMENT)
        bid = _new_bid(s, ctx)

        nda = _issue(
            s, ctx, bid, "The liability and indemnity terms in the contract conflict with the NDA."
        )
        assert nda.department_id == legal.id
        assert nda.owner_user_id == legal_head.id

        deadline = _issue(
            s,
            ctx,
            bid,
            "No submission deadline date or time zone is given for receiving the proposals.",
        )
        assert deadline.department_id == bidm.id
        assert deadline.owner_user_id == bid_head.id

        vague = _issue(s, ctx, bid, "Something is unclear here.")
        assert vague.department_id == bidm.id
    finally:
        s.rollback()
        s.close()


def test_the_linked_requirement_decides_before_the_wording(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        legal, _, _ = _staffed(s, ctx, org, DepartmentCode.LEGAL)
        finance, finance_head, _ = _staffed(s, ctx, org, DepartmentCode.FINANCE)
        bid = _new_bid(s, ctx)
        env = (org, InMemoryStorage(), EicarAwareScanner(), HashingEmbeddingProvider())
        version = _company_version(s, ctx, env)
        req = _requirement(s, ctx, bid=bid, doc_version=version, code="R-1", text="Guarantee")
        req.recommended_department_id = finance.id
        s.flush()

        # Reads like a legal question, but its requirement is routed to Finance.
        issue = _issue(
            s,
            ctx,
            bid,
            "The contract liability wording for the guarantee is unclear.",
            requirement_ids=[req.id],
        )
        assert issue.department_id == finance.id
        assert issue.owner_user_id == finance_head.id
        assert legal.id != issue.department_id
    finally:
        s.rollback()
        s.close()


def test_the_owner_is_chosen_from_the_departments_people(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        legal, _, mate = _staffed(s, ctx, org, DepartmentCode.LEGAL)
        _, _, stranger = _staffed(s, ctx, org, DepartmentCode.PMO)
        bid = _new_bid(s, ctx)
        issue = _issue(s, ctx, bid, "The liability terms in the contract are unclear.")
        assert issue.department_id == legal.id

        offer = clarifications.assignable(s, ctx, issue_id=issue.id)
        assert {c["id"] for c in offer["candidates"]} >= {str(mate.id)}
        assert str(stranger.id) not in {c["id"] for c in offer["candidates"]}

        clarifications.assign_owner(s, ctx, issue_id=issue.id, owner_user_id=mate.id)
        assert issue.owner_user_id == mate.id
        with pytest.raises(PermissionDenied):
            clarifications.assign_owner(s, ctx, issue_id=issue.id, owner_user_id=stranger.id)

        shown = clarifications.describe(s, ctx, [issue])[0]
        assert shown.department == legal.name
        assert shown.owner_position == "Team member"
    finally:
        s.rollback()
        s.close()


def test_a_misfiled_clarification_can_be_moved_and_its_owner_follows(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        _staffed(s, ctx, org, DepartmentCode.LEGAL)
        commercial, c_head, _ = _staffed(s, ctx, org, DepartmentCode.COMMERCIAL)
        bid = _new_bid(s, ctx)
        issue = _issue(s, ctx, bid, "The liability terms in the contract are unclear.")
        clarifications.move_to_department(s, ctx, issue_id=issue.id, department_id=commercial.id)
        assert issue.department_id == commercial.id
        assert issue.owner_user_id == c_head.id
    finally:
        s.rollback()
        s.close()


def test_settling_routes_old_clarifications(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        legal, head, _ = _staffed(s, ctx, org, DepartmentCode.LEGAL)
        bid = _new_bid(s, ctx)
        issue = _issue(s, ctx, bid, "The liability terms in the contract are unclear.")
        issue.department_id = None
        issue.owner_user_id = None
        s.flush()
        assert clarifications.settle_clarification_owners(s, ctx) >= 1
        assert issue.department_id == legal.id and issue.owner_user_id == head.id
    finally:
        s.rollback()
        s.close()


def test_a_requirement_with_no_recommendation_routes_by_its_category(app_engine, seeded_orgs):
    from app.models.enums import RequirementCategory

    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        legal, head, _ = _staffed(s, ctx, org, DepartmentCode.LEGAL)
        bid = _new_bid(s, ctx)
        env = (org, InMemoryStorage(), EicarAwareScanner(), HashingEmbeddingProvider())
        version = _company_version(s, ctx, env)
        req = _requirement(
            s,
            ctx,
            bid=bid,
            doc_version=version,
            code="R-2",
            text="Terms",
            category=RequirementCategory.LEGAL,
        )
        assert req.recommended_department_id is None
        # The wording says "deadline" (Bid Management), the clause says legal.
        issue = _issue(s, ctx, bid, "The deadline is unclear.", requirement_ids=[req.id])
        assert issue.department_id == legal.id and issue.owner_user_id == head.id
    finally:
        s.rollback()
        s.close()


class _Model:
    """A stand-in gateway that answers with a fixed department per item number."""

    def __init__(self, answers=None, fail=False):
        self.answers = answers or {}
        self.fail = fail
        self.calls = 0

    def generate_structured(self, name, user_content, **_):
        from app.schemas.clarification_routing import ClarificationRoute, ClarificationRoutingResult

        self.calls += 1
        assert name == "clarification_routing"
        if self.fail:
            raise RuntimeError("model down")
        return ClarificationRoutingResult(
            items=[
                ClarificationRoute(n=n, department=code, reason="because")
                for n, code in self.answers.items()
            ]
        )


def test_the_model_chooses_where_there_is_no_requirement_link(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        legal, legal_head, _ = _staffed(s, ctx, org, DepartmentCode.LEGAL)
        bidm, _, _ = _staffed(s, ctx, org, DepartmentCode.BID_MANAGEMENT)
        bid = _new_bid(s, ctx)
        issue = _issue(s, ctx, bid, "Something about the deadline is unclear.")
        assert issue.department_id == bidm.id  # the word list's answer

        model = _Model({1: "LEGAL"})
        counts = clarifications.route_open_issues(s, ctx, bid_id=bid.id, gateway=model)
        assert counts["model"] == 1 and model.calls == 1
        assert issue.department_id == legal.id
        assert issue.owner_user_id == legal_head.id
        assert issue.routing_source == "model" and issue.routing_reason == "because"

        # Asked once: the model's answer is not asked for again.
        clarifications.route_open_issues(s, ctx, bid_id=bid.id, gateway=model)
        assert model.calls == 1
    finally:
        s.rollback()
        s.close()


def test_an_invented_department_or_a_failed_call_falls_back_to_the_word_list(
    app_engine, seeded_orgs
):
    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        _staffed(s, ctx, org, DepartmentCode.LEGAL)
        bidm, _, _ = _staffed(s, ctx, org, DepartmentCode.BID_MANAGEMENT)
        bid = _new_bid(s, ctx)
        a = _issue(s, ctx, bid, "No submission deadline date is given.")
        clarifications.route_open_issues(s, ctx, bid_id=bid.id, gateway=_Model({1: "PROCUREMENT"}))
        assert a.department_id == bidm.id and a.routing_source == "rules"
        clarifications.route_open_issues(s, ctx, bid_id=bid.id, gateway=_Model(fail=True))
        assert a.department_id == bidm.id and a.routing_source == "rules"
    finally:
        s.rollback()
        s.close()


def test_the_model_decides_linked_clarifications_too_and_a_persons_choice_is_kept(
    app_engine, seeded_orgs
):
    from app.models.enums import RequirementCategory

    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        legal, _, _ = _staffed(s, ctx, org, DepartmentCode.LEGAL)
        bidm, _, _ = _staffed(s, ctx, org, DepartmentCode.BID_MANAGEMENT)
        commercial, _, _ = _staffed(s, ctx, org, DepartmentCode.COMMERCIAL)
        bid = _new_bid(s, ctx)
        env = (org, InMemoryStorage(), EicarAwareScanner(), HashingEmbeddingProvider())
        version = _company_version(s, ctx, env)
        req = _requirement(
            s,
            ctx,
            bid=bid,
            doc_version=version,
            code="R-3",
            text="Terms",
            category=RequirementCategory.LEGAL,
        )
        linked = _issue(s, ctx, bid, "Unclear.", requirement_ids=[req.id])
        assert linked.department_id == legal.id  # the category's answer, before the model
        moved = _issue(s, ctx, bid, "Also unclear.")
        clarifications.move_to_department(s, ctx, issue_id=moved.id, department_id=commercial.id)

        # The model is asked about the linked one only; the person's choice is not
        # offered to it, and survives.
        model = _Model({1: "BID_MANAGEMENT"})
        clarifications.route_open_issues(s, ctx, bid_id=bid.id, gateway=model)
        assert linked.department_id == bidm.id and linked.routing_source == "model"
        assert moved.department_id == commercial.id and moved.routing_source == "person"

        # If the model fails, the category answer is what stands for the rest.
        again = _issue(s, ctx, bid, "Unclear again.", requirement_ids=[req.id])
        clarifications.route_open_issues(s, ctx, bid_id=bid.id, gateway=_Model(fail=True))
        assert again.department_id == legal.id and again.routing_source == "category"
    finally:
        s.rollback()
        s.close()


def test_the_comparison_shows_both_answers_and_changes_nothing(app_engine, seeded_orgs):
    org, _ = seeded_orgs
    s, ctx = _open(app_engine, org)
    try:
        legal, _, _ = _staffed(s, ctx, org, DepartmentCode.LEGAL)
        bidm, _, _ = _staffed(s, ctx, org, DepartmentCode.BID_MANAGEMENT)
        bid = _new_bid(s, ctx)
        a = _issue(s, ctx, bid, "No submission deadline date is given.")
        b = _issue(s, ctx, bid, "The liability terms in the contract are unclear.")
        before = (a.department_id, b.department_id)
        report = clarifications.compare_routing(
            s, ctx, bid_id=bid.id, gateway=_Model({1: "BID_MANAGEMENT", 2: "BID_MANAGEMENT"})
        )
        assert report["total"] == 2 and report["ai_answered"] == 2 and report["agree"] == 1
        by_id = {r["id"]: r for r in report["rows"]}  # same-transaction rows tie on time
        assert by_id[str(a.id)]["ai"] == bidm.name and by_id[str(b.id)]["ai"] == bidm.name
        assert by_id[str(b.id)]["fixed"] == legal.name and by_id[str(b.id)]["agree"] is False
        assert (a.department_id, b.department_id) == before
    finally:
        s.rollback()
        s.close()
