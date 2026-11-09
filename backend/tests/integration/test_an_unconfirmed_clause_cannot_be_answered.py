"""The last gate in the chain, and the one that was missing.

Every other approval in this product is a person's:

    evidence            a knowledge administrator, at approval time
    an artifact version a reviewer, before it becomes immutable
    a commitment        a named role, personally, because it binds the company

And the model's reading of the CLAUSE ITSELF was approved by nobody.
`RequirementLifecycle.CONFIRMED` was written by `confirm_requirements` and read
by nothing at all -- every query in the codebase filters on `!= WITHDRAWN`, so a
requirement sat in DRAFT_EXTRACTED forever and it changed nothing. All 89
requirements on the live bid are in that state, tasks were generated from them,
and a draft was written against one.

The consequence is not subtle. Each of those other approvals approves an ANSWER
to a question nobody checked, so if `requirement_decomposition` misreads a
clause, the entire governed chain runs faithfully on top of the mistake and
produces an immutable, approved, shippable answer to something the customer
never asked.

`requirement_scope.py` opens by noting that `lifecycle_state` once carried a
WITHDRAWN value nothing read, and that it was cleaned up. This was the leftover
of the same problem, and it is the fourth instance found today of a rule that
was declared and never enforced.

## Why the gate sits at approval

Not at task generation, not at drafting, not at submission. Those are all
reversible and all useful while somebody is still reading the tender -- blocking
them would stop work that has to happen before anyone can sensibly confirm
anything. What must not happen is an IMMUTABLE version becoming the company's
answer to a clause no person has agreed was read correctly.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.errors import StateConflict
from app.models import Department, Requirement
from app.models.enums import (
    ApprovalStepState,
    RequirementApplicability,
    RequirementCategory,
    RequirementLifecycle,
    RiskLevel,
)
from app.services import artifacts, bids
from app.services import requirements as requirement_service
from app.services import tasks as task_service
from tests.integration.test_release_lifecycle import _ctx, _user

pytestmark = pytest.mark.integration


def _scene(session, org_id, users, *, lifecycle=RequirementLifecycle.DRAFT_EXTRACTED):
    """A bid, a requirement in the given lifecycle, and an artifact answering it."""
    actor = _user(session, org_id, users[org_id]["manager"])
    ctx = _ctx(org_id, actor.id, "BID_MANAGER")
    opportunity = bids.create_opportunity(
        session,
        ctx,
        customer_name="Confirmation Customer",
        tender_name=f"Confirmation Tender {uuid.uuid4().hex[:6]}",
    )
    bid = bids.promote_to_bid(session, ctx, opportunity_id=opportunity.id)
    requirement = Requirement(
        org_id=org_id,
        bid_id=bid.id,
        code="R-501",
        category=RequirementCategory.TECHNICAL,
        applicability=RequirementApplicability.ACTIVE,
        lifecycle_state=lifecycle,
        risk=RiskLevel.HIGH,
        is_mandatory=True,
    )
    session.add(requirement)
    session.flush()

    department = (
        session.execute(select(Department).where(Department.org_id == org_id)).scalars().first()
    )
    task = task_service.create_task(
        session,
        ctx,
        bid_id=bid.id,
        title="Answer the clause",
        department_id=department.id,
        owner_user_id=actor.id,
        requirement_ids=[requirement.id],
    )
    artifact = artifacts.for_task(session, ctx, task_id=task.id)
    version = artifacts.save_draft(
        session,
        ctx,
        artifact_id=artifact.id,
        statements=[
            {
                "statement_class": "HUMAN_PROVIDED_INPUT",
                "text": "We comply with the stated requirement.",
            }
        ],
    )
    artifacts.submit(session, ctx, artifact_id=artifact.id, approval_roles=["BID_MANAGER"])
    approval = artifacts.approval_chain(
        session, ctx, entity_type="ARTIFACT_VERSION", entity_id=version.id
    )[0]
    return ctx, bid, requirement, artifact, approval


# --- the gate ---------------------------------------------------------------


def test_an_answer_to_an_unconfirmed_clause_cannot_be_approved(
    app_sessionmaker, seeded_orgs, users
):
    """The whole point. Everything else about this answer is in order.

    The draft is typed, submitted and has a pending reviewer step. What has
    never happened is a person agreeing that the requirement is what the
    customer actually asked for.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, _bid, _requirement, _artifact, approval = _scene(session, org_id, users)

        with pytest.raises(StateConflict) as raised:
            artifacts.decide_approval(
                session, ctx, approval_id=approval.id, decision=ApprovalStepState.APPROVED
            )
        message = str(raised.value)
        assert "R-501" in message, "the refusal must name what has to be confirmed"
        assert "confirm" in message.lower()
        session.rollback()


def test_confirming_the_requirement_unblocks_it(app_sessionmaker, seeded_orgs, users):
    """The gate opens, and only a person can open it.

    Without this the test above would pass against a product that simply never
    approves anything.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, requirement, artifact, approval = _scene(session, org_id, users)

        confirmed = requirement_service.confirm_requirements(
            session, ctx, bid_id=bid.id, requirement_ids=[requirement.id]
        )
        assert confirmed == 1

        artifacts.decide_approval(
            session, ctx, approval_id=approval.id, decision=ApprovalStepState.APPROVED
        )
        fresh = artifacts.get_artifact(session, ctx, artifact_id=artifact.id)
        assert str(getattr(fresh.state, "value", fresh.state)) == "APPROVED"
        session.rollback()


def test_a_rejection_is_never_blocked_by_confirmation(app_sessionmaker, seeded_orgs, users):
    """Sending work back must always be possible.

    The check is on APPROVAL only. A reviewer who spots that the answer is wrong
    -- perhaps BECAUSE the requirement was misread -- has to be able to say so,
    and a gate that also blocked rejection would trap the draft in review with
    no way out but confirming the very clause under doubt.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, _bid, _requirement, _artifact, approval = _scene(session, org_id, users)

        artifacts.decide_approval(
            session,
            ctx,
            approval_id=approval.id,
            decision=ApprovalStepState.REJECTED,
            comment="The clause has been misread; re-extract before answering.",
        )
        session.rollback()


def test_a_legacy_confirmed_row_with_no_attribution_still_passes(
    app_sessionmaker, seeded_orgs, users
):
    """The lifecycle is the fact; who and when are attribution beside it.

    The first version of this gate checked `confirmed_at IS NULL` and
    immediately disagreed with itself: every requirement confirmed before those
    columns existed reads CONFIRMED with a NULL timestamp, and was refused as
    unconfirmed. Two sources of truth for one fact is the same error as
    collapsing two facts into one column, pointed the other way.

    Somebody did accept these. All that is missing is the record of who.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, _bid, requirement, artifact, approval = _scene(
            session, org_id, users, lifecycle=RequirementLifecycle.CONFIRMED
        )
        assert requirement.confirmed_at is None
        assert requirement.confirmed_by is None

        artifacts.decide_approval(
            session, ctx, approval_id=approval.id, decision=ApprovalStepState.APPROVED
        )
        fresh = artifacts.get_artifact(session, ctx, artifact_id=artifact.id)
        assert str(getattr(fresh.state, "value", fresh.state)) == "APPROVED"
        session.rollback()


# --- confirmation is an approval, not a formality ---------------------------


def test_confirmation_records_who_and_when(app_sessionmaker, seeded_orgs, users):
    """ "Who accepted R-042, and when" is the only question an approval settles.

    The old `confirm_requirements` promoted every row on the bid and wrote an
    audit entry saying how many. That answered "were they confirmed" and nothing
    else.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, requirement, _artifact, _approval = _scene(session, org_id, users)

        requirement_service.confirm_requirements(
            session, ctx, bid_id=bid.id, requirement_ids=[requirement.id]
        )
        assert requirement.lifecycle_state is RequirementLifecycle.CONFIRMED
        assert requirement.confirmed_by == ctx.actor_user_id
        assert requirement.confirmed_at is not None
        session.rollback()


def test_confirming_a_subset_leaves_the_rest_alone(app_sessionmaker, seeded_orgs, users):
    """A reviewer accepts the rows they have actually read.

    Confirming 89 requirements in one call is a rubber stamp however carefully
    it is recorded, and a gate cleared by a rubber stamp is not a gate. The
    bid-wide form stays available for when somebody genuinely means all of them.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, requirement, _artifact, _approval = _scene(session, org_id, users)
        other = Requirement(
            org_id=org_id,
            bid_id=bid.id,
            code="R-502",
            category=RequirementCategory.TECHNICAL,
            applicability=RequirementApplicability.ACTIVE,
            lifecycle_state=RequirementLifecycle.DRAFT_EXTRACTED,
            risk=RiskLevel.LOW,
            is_mandatory=False,
        )
        session.add(other)
        session.flush()

        confirmed = requirement_service.confirm_requirements(
            session, ctx, bid_id=bid.id, requirement_ids=[requirement.id]
        )
        assert confirmed == 1
        assert requirement.lifecycle_state is RequirementLifecycle.CONFIRMED
        assert other.lifecycle_state is RequirementLifecycle.DRAFT_EXTRACTED
        session.rollback()


def test_confirming_an_empty_list_confirms_nothing(app_sessionmaker, seeded_orgs, users):
    """An empty selection is not "all of them".

    `requirement_ids=[]` and `requirement_ids=None` mean opposite things, and a
    screen that sends an empty selection must not silently confirm the bid.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, requirement, _artifact, _approval = _scene(session, org_id, users)

        assert (
            requirement_service.confirm_requirements(
                session, ctx, bid_id=bid.id, requirement_ids=[]
            )
            == 0
        )
        assert requirement.lifecycle_state is RequirementLifecycle.DRAFT_EXTRACTED
        session.rollback()


def test_confirming_the_whole_bid_still_works(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, requirement, _artifact, _approval = _scene(session, org_id, users)

        assert requirement_service.confirm_requirements(session, ctx, bid_id=bid.id) == 1
        assert requirement.lifecycle_state is RequirementLifecycle.CONFIRMED
        session.rollback()


def test_a_withdrawn_requirement_does_not_block_an_answer(app_sessionmaker, seeded_orgs, users):
    """A retired clause is not an unconfirmed one.

    Without this, withdrawing a requirement an artifact once answered would
    block that artifact forever: the row is unconfirmed and will never now be
    confirmed, because nobody confirms something they have just retired.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, _bid, requirement, artifact, approval = _scene(session, org_id, users)
        requirement_service.withdraw(session, ctx, requirement=requirement)

        assert artifacts.unconfirmed_requirements(session, ctx, artifact_id=artifact.id) == []
        artifacts.decide_approval(
            session, ctx, approval_id=approval.id, decision=ApprovalStepState.APPROVED
        )
        session.rollback()
