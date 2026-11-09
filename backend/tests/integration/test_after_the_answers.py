"""What happens once the answers exist.

Two approved answers that contradict each other become a resolution task; a
claim can be traced back to its approvals and forward to the proposal it went
out in; a task's history reads in order; the bid dashboard shows each measure
separately; the customer letter lists the open questions; and a won bid is
proposed into company knowledge.
"""

from __future__ import annotations

import io
import json
import re
import uuid

import pytest
from docx import Document as WordDocument
from sqlalchemy import select

from app.gateway.base import LLMResponse
from app.gateway.deepseek import NullLLMProvider
from app.gateway.embeddings import HashingEmbeddingProvider
from app.gateway.gateway import ModelGateway
from app.models import (
    ArtifactStatement,
    ClarificationIssue,
    KnowledgeEntity,
    KnowledgeFact,
    KnowledgeUpdateCandidate,
    ResponseArtifact,
    Task,
)
from app.models.enums import (
    ApprovalStatus,
    CandidateState,
    ClarificationIssueType,
    ClarificationSeverity,
    ClarificationStatus,
    DepartmentCode,
    OutcomeResult,
    OutputMode,
)
from app.services import consistency, release, traceability
from app.services.storage import InMemoryStorage
from tests.integration.test_release_lifecycle import _ctx, _user
from tests.integration.test_the_final_output import _approved, _department

pytestmark = pytest.mark.integration


class ConflictReader(NullLLMProvider):
    """Reports the six-weeks / weeks-2-to-5 contradiction when it sees both."""

    model_id = "scripted-consistency"

    def generate(self, messages, *, temperature=None, max_tokens=None, timeout=None):
        blob = "\n".join(m.content for m in messages)
        refs = dict(
            re.findall(r'"ref":\s*"(S\d+)",\s*"section":\s*"[^"]*",\s*"text":\s*"([^"]*)"', blob)
        )
        six = [r for r, t in refs.items() if "six weeks" in t]
        weeks = [r for r, t in refs.items() if "weeks 2 to 5" in t]
        conflicts = []
        if six and weeks:
            conflicts.append(
                {
                    "statement_refs": [six[0], weeks[0]],
                    "kind": "TIMELINE",
                    "explanation": "Methodology says six weeks of testing; the plan allows four.",
                }
            )
        return LLMResponse(
            text=json.dumps({"conflicts": conflicts}), model_id=self.model_id, latency_ms=0
        )


def _scene(session, org_id, users):
    from app.services import bids

    manager = _user(session, org_id, users[org_id]["manager"])
    ctx = _ctx(org_id, manager.id, "BID_MANAGER")
    opportunity = bids.create_opportunity(
        session,
        ctx,
        customer_name="Dubai Government Entity",
        tender_name=f"VAPT {uuid.uuid4().hex[:6]}",
    )
    bid = bids.promote_to_bid(session, ctx, opportunity_id=opportunity.id)
    security = _department(session, org_id, DepartmentCode.SECURITY)
    pmo = _department(session, org_id, DepartmentCode.PMO)
    method_task, _ = _approved(
        session,
        ctx,
        bid=bid,
        department=security,
        title="VAPT methodology",
        text="Testing will take six weeks.",
    )
    plan_task, _ = _approved(
        session,
        ctx,
        bid=bid,
        department=pmo,
        title="Project plan",
        text="Testing runs weeks 2 to 5.",
    )
    return ctx, bid, method_task, plan_task, opportunity


def test_a_contradiction_between_approved_answers_becomes_a_resolution_task(
    app_sessionmaker, seeded_orgs, users
):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, method_task, plan_task, _ = _scene(session, org_id, users)
        gateway = ModelGateway(ConflictReader(), HashingEmbeddingProvider())

        result = consistency.check_bid(session, ctx, bid_id=bid.id, gateway=gateway)
        assert result["conflicts"] == 1 and result["tasks_opened"] == 1
        resolution = session.execute(
            select(Task).where(Task.bid_id == bid.id, Task.title.like("Resolve%"))
        ).scalar_one()
        assert method_task.code in resolution.title and plan_task.code in resolution.title
        assert "six weeks" in resolution.description and "weeks 2 to 5" in resolution.description

        again = consistency.check_bid(session, ctx, bid_id=bid.id, gateway=gateway)
        assert again["tasks_opened"] == 0, "the same conflict must not open a second task"
        session.rollback()


def test_a_claim_can_be_traced_both_ways(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    storage = InMemoryStorage()
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, method_task, _plan, _ = _scene(session, org_id, users)
        release.derive_structure(
            session,
            ctx,
            bid_id=bid.id,
            gateway=ModelGateway(NullLLMProvider("{}"), HashingEmbeddingProvider()),
        )
        release.assemble(
            session, ctx, bid_id=bid.id, output_mode=OutputMode.SUBMISSION, storage=storage
        )
        release.create_package(session, ctx, bid_id=bid.id, output_mode=OutputMode.SUBMISSION)

        artifact = session.execute(
            select(ResponseArtifact).where(ResponseArtifact.task_id == method_task.id)
        ).scalar_one()
        statement = session.execute(
            select(ArtifactStatement).where(
                ArtifactStatement.artifact_version_id == artifact.approved_version_id
            )
        ).scalar_one()
        trace = traceability.trace_statement(session, ctx, statement_id=statement.id)
        assert trace["statement"]["text"] == "Testing will take six weeks."
        assert trace["answer"]["task"] == method_task.code
        assert trace["approvals"] and trace["approvals"][0]["state"] == "APPROVED"
        assert trace["released_in"] and trace["released_in"][0]["packages"]

        history = traceability.timeline(session, ctx, task_id=method_task.id)
        actions = [e["action"] for e in history]
        assert "task.created" in actions and "artifact.created" in actions
        assert history == sorted(history, key=lambda e: e["at"])
        session.rollback()


def test_the_bid_dashboard_keeps_its_measures_apart(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, *_ = _scene(session, org_id, users)
        board = traceability.bid_dashboard(session, ctx, bid_id=bid.id)
        for key in (
            "requirement_coverage_pct",
            "evidence_readiness_pct",
            "task_completion_pct",
            "approvals_pct",
            "package_readiness_pct",
        ):
            assert key in board
        assert board["answers_total"] == 2 and board["answers_approved"] == 2
        session.rollback()


def test_the_customer_letter_lists_open_questions_and_can_mark_them_sent(
    app_sessionmaker, seeded_orgs, users
):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, *_ = _scene(session, org_id, users)
        for severity, question in (
            (ClarificationSeverity.MINOR, "Which browser versions are in scope?"),
            (ClarificationSeverity.MAJOR, "How long is the retesting window?"),
        ):
            session.add(
                ClarificationIssue(
                    org_id=org_id,
                    bid_id=bid.id,
                    issue_type=ClarificationIssueType.UNDEFINED_FIELD,
                    description=question,
                    severity=severity,
                    status=ClarificationStatus.OPEN,
                    recommended_question=question,
                    detected_by="test",
                    fingerprint=uuid.uuid4().hex,
                )
            )
        session.flush()

        content, count = traceability.customer_letter(session, ctx, bid_id=bid.id, mark_sent=True)
        text = "\n".join(p.text for p in WordDocument(io.BytesIO(content)).paragraphs)
        assert count == 2
        assert text.index("retesting window") < text.index("browser versions"), "major first"
        statuses = set(
            session.execute(
                select(ClarificationIssue.status).where(ClarificationIssue.bid_id == bid.id)
            ).scalars()
        )
        assert statuses == {ClarificationStatus.SENT_TO_CLIENT}
        session.rollback()


def test_a_won_bid_is_proposed_into_company_knowledge(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, _m, _p, opportunity = _scene(session, org_id, users)
        outcome = release.record_outcome(
            session, ctx, bid_id=bid.id, result=OutcomeResult.WON, award_value=180000
        )
        candidate = session.execute(
            select(KnowledgeUpdateCandidate).where(
                KnowledgeUpdateCandidate.origin_entity_id == outcome.id,
                KnowledgeUpdateCandidate.origin_type == "OUTCOME_AWARD",
            )
        ).scalar_one()
        assert candidate.state is CandidateState.PROPOSED, "proposed, not written"

        release.decide_candidate(
            session, ctx, candidate_id=candidate.id, decision=CandidateState.APPROVED
        )
        project = session.get(KnowledgeEntity, candidate.target_id)
        assert project.canonical_name == opportunity.tender_name
        facts = {
            f.predicate: f
            for f in session.execute(
                select(KnowledgeFact).where(KnowledgeFact.entity_id == project.id)
            ).scalars()
        }
        assert facts["customer"].value_text == "Dubai Government Entity"
        assert facts["award_value"].value_number == 180000
        assert all(f.approval_status is ApprovalStatus.APPROVED for f in facts.values())
        session.rollback()


def test_a_person_can_settle_a_conflict_with_a_reason(app_sessionmaker, seeded_orgs, users):
    from app.core.errors import ValidationError
    from app.models.enums import TaskState

    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, method_task, _plan, _ = _scene(session, org_id, users)
        gateway = ModelGateway(ConflictReader(), HashingEmbeddingProvider())
        consistency.check_bid(session, ctx, bid_id=bid.id, gateway=gateway)
        resolution = session.execute(
            select(Task).where(Task.bid_id == bid.id, Task.title.like("Resolve%"))
        ).scalar_one()

        with pytest.raises(ValidationError):
            consistency.resolve(session, ctx, task_id=resolution.id, outcome="FIXED", note=" ")
        with pytest.raises(ValidationError):
            consistency.resolve(
                session, ctx, task_id=method_task.id, outcome="FIXED", note="not a conflict task"
            )
        done = consistency.resolve(
            session,
            ctx,
            task_id=resolution.id,
            outcome="NOT_A_CONFLICT",
            note="Six weeks is the whole engagement; weeks 2 to 5 is the test window.",
        )
        assert done.state is TaskState.COMPLETED
        session.rollback()


def test_a_conflict_about_wording_that_was_replaced_closes_itself(
    app_sessionmaker, seeded_orgs, users
):
    from app.models.enums import TaskState

    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, method_task, _plan, _ = _scene(session, org_id, users)
        gateway = ModelGateway(ConflictReader(), HashingEmbeddingProvider())
        consistency.check_bid(session, ctx, bid_id=bid.id, gateway=gateway)
        resolution = session.execute(
            select(Task).where(Task.bid_id == bid.id, Task.title.like("Resolve%"))
        ).scalar_one()
        assert resolution.state is not TaskState.COMPLETED

        artifact = session.execute(
            select(ResponseArtifact).where(ResponseArtifact.task_id == method_task.id)
        ).scalar_one()
        statement = session.execute(
            select(ArtifactStatement).where(
                ArtifactStatement.artifact_version_id == artifact.approved_version_id
            )
        ).scalar_one()
        statement.text = "Testing will take four weeks."
        session.flush()

        consistency.check_bid(session, ctx, bid_id=bid.id, gateway=gateway)
        session.refresh(resolution)
        assert resolution.state is TaskState.COMPLETED
        session.rollback()


def test_two_open_tasks_for_one_matter_are_reduced_to_the_earlier_one(
    app_sessionmaker, seeded_orgs, users
):
    from app.models.enums import TaskState

    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, _method, _plan, _ = _scene(session, org_id, users)
        gateway = ModelGateway(ConflictReader(), HashingEmbeddingProvider())
        consistency.check_bid(session, ctx, bid_id=bid.id, gateway=gateway)
        first = session.execute(
            select(Task).where(Task.bid_id == bid.id, Task.title.like("Resolve%"))
        ).scalar_one()
        twin = Task(
            **{
                c.name: getattr(first, c.name)
                for c in Task.__table__.columns
                if c.name not in {"id", "code", "created_at", "updated_at"}
            },
            code="TWIN-1",
        )
        session.add(twin)
        session.flush()

        assert consistency.retire_twins(session, ctx, bid_id=bid.id) == 1
        session.refresh(first)
        session.refresh(twin)
        assert first.state is not TaskState.COMPLETED
        assert twin.state is TaskState.COMPLETED
        assert (twin.blocker_note or "").startswith(consistency.SUPERSEDED)
        session.rollback()


def test_a_conflict_whose_wording_is_not_in_the_package_is_retired_not_opened(
    app_sessionmaker, seeded_orgs, users
):
    from app.models.enums import TaskState

    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, _method, _plan, _ = _scene(session, org_id, users)
        gateway = ModelGateway(ConflictReader(), HashingEmbeddingProvider())
        elsewhere = "An unrelated document that mentions neither statement at all."
        result = consistency.check_bid(
            session, ctx, bid_id=bid.id, gateway=gateway, package_text=elsewhere
        )
        assert result["tasks_opened"] == 0 and result["outside_package"] == 1

        consistency.check_bid(session, ctx, bid_id=bid.id, gateway=gateway)
        task = session.execute(
            select(Task).where(Task.bid_id == bid.id, Task.title.like("Resolve%"))
        ).scalar_one()
        retired = consistency.retire_outside_package(
            session, ctx, bid_id=bid.id, package_text=elsewhere
        )
        session.refresh(task)
        assert retired == 1 and task.state is TaskState.COMPLETED
        assert (task.blocker_note or "").startswith(consistency.SUPERSEDED)
        assert "check run" in task.description
        session.rollback()


def test_statement_in_text_needs_the_wording_not_just_the_topic():
    text = "Testing runs weeks 2 to 5 of the plan and then hand over follows."
    assert consistency.statement_in_text("Testing runs weeks 2 to 5.", text)
    assert not consistency.statement_in_text("Testing will take six weeks of effort.", text)


def test_an_unstarted_conflict_task_outside_bid_management_reopens_there(
    app_sessionmaker, seeded_orgs, users
):
    from app.models import Department
    from app.models.enums import DepartmentCode, TaskState

    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, _method, _plan, _ = _scene(session, org_id, users)
        gateway = ModelGateway(ConflictReader(), HashingEmbeddingProvider())
        consistency.check_bid(session, ctx, bid_id=bid.id, gateway=gateway)
        home = session.execute(
            select(Department).where(
                Department.org_id == org_id, Department.code == DepartmentCode.BID_MANAGEMENT
            )
        ).scalar_one()
        first = session.execute(
            select(Task).where(Task.bid_id == bid.id, Task.title.like("Resolve%"))
        ).scalar_one()
        assert first.department_id == home.id, "conflicts between answers go to the bid manager"

        # A task from before that rule sits with an author's department.
        departments = session.execute(
            select(Department).where(Department.org_id == org_id)
        ).scalars()
        other = next(d for d in departments if d.id != home.id)
        first.department_id = other.id
        session.flush()

        consistency.check_bid(session, ctx, bid_id=bid.id, gateway=gateway)
        session.refresh(first)
        assert first.state is TaskState.COMPLETED
        assert (first.blocker_note or "").startswith(consistency.SUPERSEDED)
        live = session.execute(
            select(Task).where(
                Task.bid_id == bid.id,
                Task.title.like("Resolve%"),
                Task.state != TaskState.COMPLETED,
            )
        ).scalar_one()
        assert live.department_id == home.id
        session.rollback()
