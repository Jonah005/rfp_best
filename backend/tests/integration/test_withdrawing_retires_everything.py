"""Withdrawing a requirement is three facts, not one.

The live bid carried **158 current assessments against 89 live requirements**.
Nothing was corrupt -- exactly one `is_current` row per requirement throughout --
and that is what made it dangerous. 69 of them assessed requirements that had
since been WITHDRAWN and went on claiming to be current:

    state                          all   live   diff
    BLOCKED                         81     59     22
    NEEDS_CLIENT_CLARIFICATION      43     21     22
    NEEDS_COMPANY_CONFIRMATION      33      9     24
    DRAFTABLE                        1      0      1

Every number a person had actually seen was right, because the export joins to
the requirement and filters. The trap was set for whatever queried `is_current`
next without knowing it had to -- and the very first thing that did, a
read-only diagnostic written to answer a different question, reported BLOCKED as
81 instead of 59. A 37% overstatement of the work outstanding on a bid.

Note the last row too: the only DRAFTABLE requirement in the entire matrix
belongs to a retired row. Unfiltered, the bid looks like it has one answerable
requirement. It has none.

## Why one function

Withdrawing touches three things, and this codebase has paid for splitting them
up twice already:

  1. the requirement's lifecycle and its successor;
  2. its clarification links, which must follow the clause onto the survivors;
  3. its assessment, which must stop claiming to be current.

Fact 2 was fixed in `coalesce_enumerated_fragments.py` and not in
`retire_superseded_extractions.py`, which does the same thing -- the identical
failure reappeared a day later with 130 occurrences. Fact 3 was never fixed
anywhere, and five separate call sites set `lifecycle_state = WITHDRAWN` by hand.

So these tests are against `requirements.withdraw`, and a caller that sets the
column directly is the defect rather than the exception.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models import (
    ClarificationIssue,
    ClarificationRequirement,
    Requirement,
    RequirementAssessment,
)
from app.models.enums import (
    AssessmentState,
    BidReadiness,
    ClarificationIssueType,
    ClarificationSeverity,
    ClarificationStatus,
    EvidenceStatus,
    RequirementApplicability,
    RequirementCategory,
    RequirementLifecycle,
    RiskLevel,
)
from app.services import requirements as requirement_service
from tests.integration.test_release_lifecycle import _ctx, _user

pytestmark = pytest.mark.integration


def _bid(session, ctx):
    from app.services import bids

    opportunity = bids.create_opportunity(
        session,
        ctx,
        customer_name="Withdrawal Customer",
        tender_name=f"Withdrawal Tender {uuid.uuid4().hex[:6]}",
    )
    return bids.promote_to_bid(session, ctx, opportunity_id=opportunity.id)


def _requirement(session, ctx, bid, code: str) -> Requirement:
    row = Requirement(
        org_id=ctx.org_id,
        bid_id=bid.id,
        code=code,
        category=RequirementCategory.TECHNICAL,
        applicability=RequirementApplicability.ACTIVE,
        lifecycle_state=RequirementLifecycle.DRAFT_EXTRACTED,
        risk=RiskLevel.MEDIUM,
        is_mandatory=True,
    )
    session.add(row)
    session.flush()
    return row


def _assessment(session, ctx, requirement, readiness=BidReadiness.BLOCKED):
    row = RequirementAssessment(
        org_id=ctx.org_id,
        requirement_id=requirement.id,
        bid_id=requirement.bid_id,
        evidence_status=EvidenceStatus.NONE,
        bid_readiness=readiness,
        rationale="Nothing supports this yet.",
        assessment_state=AssessmentState.COMPUTED,
        version=1,
        is_current=True,
        computed_at=datetime.now(timezone.utc),
        computed_by="test",
    )
    session.add(row)
    session.flush()
    return row


def _question(session, ctx, bid, requirement, text: str) -> ClarificationIssue:
    issue = ClarificationIssue(
        org_id=ctx.org_id,
        bid_id=bid.id,
        issue_type=ClarificationIssueType.UNDEFINED_FIELD,
        description=text,
        severity=ClarificationSeverity.MAJOR,
        blocks_generation=False,
        recommended_question=text,
        status=ClarificationStatus.OPEN,
        detected_by="test",
        fingerprint=f"test:{uuid.uuid4().hex[:8]}",
    )
    session.add(issue)
    session.flush()
    session.add(
        ClarificationRequirement(
            org_id=ctx.org_id, clarification_id=issue.id, requirement_id=requirement.id
        )
    )
    session.flush()
    return issue


def _current_for(session, ctx, requirement_id):
    return list(
        session.execute(
            select(RequirementAssessment).where(
                RequirementAssessment.org_id == ctx.org_id,
                RequirementAssessment.requirement_id == requirement_id,
                RequirementAssessment.is_current.is_(True),
            )
        ).scalars()
    )


# --- the defect that was found in the wild ----------------------------------


def test_a_withdrawn_requirement_stops_carrying_a_current_assessment(
    app_sessionmaker, seeded_orgs, users
):
    """The 69 rows, in one case.

    This is the whole finding: the verdict said `is_current = True` about a
    requirement nobody should be reading, so a count over assessments alone
    included retired work.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor = _user(session, org_id, users[org_id]["manager"])
        ctx = _ctx(org_id, actor.id, "BID_MANAGER")
        bid = _bid(session, ctx)
        retired = _requirement(session, ctx, bid, "R-901")
        _assessment(session, ctx, retired)
        assert len(_current_for(session, ctx, retired.id)) == 1

        requirement_service.withdraw(session, ctx, requirement=retired, reason="Duplicate.")

        assert retired.lifecycle_state is RequirementLifecycle.WITHDRAWN
        assert _current_for(session, ctx, retired.id) == []
        session.rollback()


def test_the_prior_verdict_is_kept_not_deleted(app_sessionmaker, seeded_orgs, users):
    """Retiring is not erasing.

    A withdrawn requirement's last verdict is part of how the bid reached its
    current state, and deleting it would make the audit trail lie by omission.
    `is_current = False` is exactly how every superseded assessment is already
    kept.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor = _user(session, org_id, users[org_id]["manager"])
        ctx = _ctx(org_id, actor.id, "BID_MANAGER")
        bid = _bid(session, ctx)
        retired = _requirement(session, ctx, bid, "R-902")
        assessment = _assessment(session, ctx, retired)
        assessment_id = assessment.id

        requirement_service.withdraw(session, ctx, requirement=retired)

        kept = session.get(RequirementAssessment, assessment_id)
        assert kept is not None, "the verdict was deleted rather than retired"
        assert kept.is_current is False
        assert kept.bid_readiness is BidReadiness.BLOCKED
        session.rollback()


def test_a_live_requirement_keeps_its_assessment(app_sessionmaker, seeded_orgs, users):
    """The half that stops the fix from over-reaching.

    Retiring assessments belonging to OTHER requirements would empty the matrix
    silently, which is a far worse failure than the one being fixed.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor = _user(session, org_id, users[org_id]["manager"])
        ctx = _ctx(org_id, actor.id, "BID_MANAGER")
        bid = _bid(session, ctx)
        retired = _requirement(session, ctx, bid, "R-903")
        survivor = _requirement(session, ctx, bid, "R-904")
        _assessment(session, ctx, retired)
        _assessment(session, ctx, survivor, readiness=BidReadiness.DRAFTABLE)

        requirement_service.withdraw(session, ctx, requirement=retired, successors=[survivor.id])

        assert _current_for(session, ctx, retired.id) == []
        still = _current_for(session, ctx, survivor.id)
        assert len(still) == 1
        assert still[0].bid_readiness is BidReadiness.DRAFTABLE
        session.rollback()


# --- the two facts that were already known, now in the same place -----------


def test_a_customer_question_follows_the_clause_onto_the_survivor(
    app_sessionmaker, seeded_orgs, users
):
    """Fact 2, the one that reappeared a day after being fixed elsewhere.

    The question was raised about a BLOCK, and the block still has a
    requirement. Losing the link would drop a real customer question over a
    bookkeeping change.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor = _user(session, org_id, users[org_id]["manager"])
        ctx = _ctx(org_id, actor.id, "BID_MANAGER")
        bid = _bid(session, ctx)
        retired = _requirement(session, ctx, bid, "R-905")
        survivor = _requirement(session, ctx, bid, "R-906")
        issue = _question(session, ctx, bid, retired, "Which standard applies here?")

        result = requirement_service.withdraw(
            session, ctx, requirement=retired, successors=[survivor.id]
        )

        assert result["clarification_links_moved"] == 1
        landed = list(
            session.execute(
                select(ClarificationRequirement.requirement_id).where(
                    ClarificationRequirement.org_id == org_id,
                    ClarificationRequirement.clarification_id == issue.id,
                )
            ).scalars()
        )
        assert landed == [survivor.id], "the question now addresses a code that does not exist"
        session.rollback()


def test_the_successor_is_recorded(app_sessionmaker, seeded_orgs, users):
    """So a reader who finds the retired code learns what to read instead."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor = _user(session, org_id, users[org_id]["manager"])
        ctx = _ctx(org_id, actor.id, "BID_MANAGER")
        bid = _bid(session, ctx)
        retired = _requirement(session, ctx, bid, "R-907")
        survivor = _requirement(session, ctx, bid, "R-908")

        requirement_service.withdraw(session, ctx, requirement=retired, successors=[survivor.id])
        assert retired.superseded_by_requirement_id == survivor.id
        session.rollback()


def test_withdrawing_with_no_successor_is_allowed_and_still_retires_the_verdict(
    app_sessionmaker, seeded_orgs, users
):
    """Not every withdrawal has a replacement.

    A requirement extracted from a block that turned out to state no obligation
    is retired outright. There is nothing to re-point links at -- and the
    assessment must still stop claiming to be current, which is the case the
    five hand-written call sites would most easily have missed.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor = _user(session, org_id, users[org_id]["manager"])
        ctx = _ctx(org_id, actor.id, "BID_MANAGER")
        bid = _bid(session, ctx)
        retired = _requirement(session, ctx, bid, "R-909")
        _assessment(session, ctx, retired)

        result = requirement_service.withdraw(session, ctx, requirement=retired)

        assert result == {
            "clarification_links_moved": 0,
            "assessments_retired": 1,
            "task_links_moved": 0,
        }
        assert retired.superseded_by_requirement_id is None
        assert _current_for(session, ctx, retired.id) == []
        session.rollback()


def test_withdrawing_is_audited_with_what_it_moved(app_sessionmaker, seeded_orgs, users):
    """A retirement that leaves no record is indistinguishable from data loss.

    The audit row carries the counts, so "where did R-905's question go" has an
    answer that does not require re-deriving it from the join table.
    """
    from app.models import AuditEvent

    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor = _user(session, org_id, users[org_id]["manager"])
        ctx = _ctx(org_id, actor.id, "BID_MANAGER")
        bid = _bid(session, ctx)
        retired = _requirement(session, ctx, bid, "R-910")
        survivor = _requirement(session, ctx, bid, "R-911")
        _assessment(session, ctx, retired)
        _question(session, ctx, bid, retired, "Does this apply to phase two?")

        requirement_service.withdraw(
            session,
            ctx,
            requirement=retired,
            successors=[survivor.id],
            reason="Fragment of R-911.",
        )
        session.flush()

        event = (
            session.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "requirement.withdrawn",
                    AuditEvent.entity_id == retired.id,
                )
                .order_by(AuditEvent.occurred_at.desc())
            )
            .scalars()
            .first()
        )
        assert event is not None, "a withdrawal left no audit record"
        assert event.after_state["assessments_retired"] == 1
        assert event.after_state["clarification_links_moved"] == 1
        assert event.after_state["successors"] == [str(survivor.id)]
        session.rollback()


def test_withdrawing_moves_the_tasks_that_covered_the_clause(app_sessionmaker, seeded_orgs, users):
    """Fact 4: a task must not keep answering a clause that no longer exists.

    BID-03 on the live bid covered three withdrawn requirements. Its draft still
    answered them while the diagram chat ignored them ("covers no requirements"),
    so one screen contradicted itself.
    """
    from app.models import Department, TaskRequirement
    from app.services import tasks as task_service

    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor = _user(session, org_id, users[org_id]["manager"])
        ctx = _ctx(org_id, actor.id, "BID_MANAGER")
        bid = _bid(session, ctx)
        retired = _requirement(session, ctx, bid, "R-911")
        survivor = _requirement(session, ctx, bid, "R-912")
        orphaned = _requirement(session, ctx, bid, "R-913")
        department = (
            session.execute(select(Department).where(Department.org_id == org_id)).scalars().first()
        )
        task = task_service.create_task(
            session,
            ctx,
            bid_id=bid.id,
            title="Cover it",
            department_id=department.id,
            requirement_ids=[retired.id, orphaned.id],
        )

        requirement_service.withdraw(session, ctx, requirement=retired, successors=[survivor.id])
        requirement_service.withdraw(session, ctx, requirement=orphaned)

        linked = {
            link.requirement_id
            for link in session.execute(
                select(TaskRequirement).where(TaskRequirement.task_id == task.id)
            ).scalars()
        }
        assert linked == {survivor.id}
        assert [r.code for r in task_service.task_requirements(session, ctx, task_id=task.id)] == [
            "R-912"
        ]
        session.rollback()
