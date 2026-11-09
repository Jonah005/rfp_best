"""A promise waiting on a role nobody holds must say so.

The draft model routes every PROPOSED_COMMITMENT to an approver role, chosen
from `COMMITMENT_APPROVER_ROLES` -- the roles that can own a promise at all.
That set is narrowed in code, which is a decision about what a commitment
MEANS. What is deliberately NOT narrowed is the customer's own org chart: the
model is never told which of those roles this organization has staffed, because
a model trimming its answer to who happens to be employed is doing a permission
calculation, and it would hide the gap these tests are about.

The consequence is that a commitment can be routed to a role the organization
does not staff. `decide_commitment` then refuses everybody -- correctly, since
nobody holds the role -- the commitment stays PENDING_APPROVAL, and the artifact
can never be approved. Nothing is wrong with any of those rules. What was wrong
is that the refusal said only "Every proposed commitment must be approved before
the artifact can pass", which names neither the commitments nor the roles, so
the two very different situations behind it looked identical:

  * a QUEUE -- somebody holds the role and has not decided yet, and the answer
    is to go and ask them;
  * an ORPHAN -- nobody holds it, no approver was ever notified, and the
    artifact is stopped for good until a person staffs the role or re-routes
    the promise.

This was found by the fixture walk twice. The first run routed a delivery
schedule to PROJECT_MANAGER and a technical undertaking to SOLUTION_ARCHITECT;
the cast was extended to cover those two, and the next run routed an NDA clause
to LEGAL and stalled identically. The cast now staffs every role in the set and
asserts it at setup, so reproducing the orphan there would cost a model call
and a deliberately broken fixture. It is cheaper and more honest here.

Note what these tests do NOT assert: that anything re-routes itself. Code may
report that a commitment has no possible approver. Changing who may bind the
company is a person's decision and the audit log's record, and a system that
quietly reassigns the approver to somebody who happens to be available has
removed the gate rather than reported it.
"""

from __future__ import annotations

import pytest

from app.core.errors import StateConflict
from app.models.enums import ApprovalStepState
from app.services import artifacts
from tests.integration.test_artifact_lifecycle import _actor, _context, _make_task

pytestmark = pytest.mark.integration

# A real role, and one the seeded organizations do not staff. If a future seed
# starts issuing it, `test_the_unstaffed_role_is_actually_unstaffed` fails
# first and names the problem, rather than this file quietly testing nothing.
UNSTAFFED = "SOLUTION_ARCHITECT"


# The seeded organizations staff BID_MANAGER, AUDITOR, EXECUTIVE_APPROVER and
# KNOWLEDGE_ADMIN, so the artifact is reviewed by the executive approver here.
# Which role reviews is not what these tests are about; that somebody real does
# is.
REVIEWING_ROLE = "EXECUTIVE_APPROVER"


def _scene(session, org_id, users):
    actor = _actor(session, org_id, users[org_id]["manager"])
    ctx, task, artifact = _make_task(session, org_id, actor.id)
    return actor, ctx, task, artifact


def _commit(session, ctx, artifact, actor, role: str):
    return artifacts.save_draft(
        session,
        ctx,
        artifact_id=artifact.id,
        statements=[
            {
                "statement_class": "PROPOSED_COMMITMENT",
                "text": "We will deliver the assessment within eight weeks of award.",
                "required_approver_role": role,
                "commitment_owner_user_id": actor.id,
            }
        ],
    )


def test_the_unstaffed_role_is_actually_unstaffed(app_sessionmaker, seeded_orgs, users):
    """The premise of every test below, asserted rather than assumed.

    A fixture that silently starts staffing SOLUTION_ARCHITECT would turn the
    orphan cases into queue cases, and they would still pass while covering the
    opposite of what they claim.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor = _actor(session, org_id, users[org_id]["manager"])
        ctx = _context(org_id, actor.id, "BID_MANAGER")
        staffed = artifacts.staffed_approver_roles(session, ctx)
        assert staffed, "the organization staffs no roles at all; the seed is broken"
        assert UNSTAFFED not in staffed
        session.rollback()


def test_a_commitment_routed_to_nobody_says_so(app_sessionmaker, seeded_orgs, users):
    """The orphan case names the role, and says what to do about it."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor, ctx, _task, artifact = _scene(session, org_id, users)
        version = _commit(session, ctx, artifact, actor, UNSTAFFED)

        message = artifacts.commitment_blockage(session, ctx, version_id=version.id)
        assert UNSTAFFED in message
        assert "Nobody in this organization holds" in message
        # The reader is told the two ways out, because neither is obvious from
        # the screen they are looking at.
        assert "re-route" in message
        session.rollback()


def test_a_commitment_waiting_on_a_staffed_role_is_not_called_an_orphan(
    app_sessionmaker, seeded_orgs, users
):
    """The distinction is the point, so the other side is asserted too.

    Without this, a message that said "nobody holds this" about every pending
    commitment would pass the test above and be wrong about most of them.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor, ctx, _task, artifact = _scene(session, org_id, users)
        # Staffed AND able to own a promise -- two different questions. These
        # organizations staff AUDITOR, which is now refused outright, so
        # "the first staffed role" would exercise the wrong refusal.
        from app.core.permissions import COMMITMENT_APPROVER_ROLES

        staffed = sorted(artifacts.staffed_approver_roles(session, ctx) & COMMITMENT_APPROVER_ROLES)
        assert staffed, "the seed staffs nobody who can own a commitment"
        version = _commit(session, ctx, artifact, actor, staffed[0])

        message = artifacts.commitment_blockage(session, ctx, version_id=version.id)
        assert "unapproved" in message
        assert "Nobody in this organization holds" not in message
        session.rollback()


def test_nothing_blocking_means_nothing_said(app_sessionmaker, seeded_orgs, users):
    """A blocker report that always reports is not a blocker report.

    `decide_approval` calls this only when it is already refusing, but the
    function is public and a caller checking "is anything holding this" must be
    able to trust an empty answer.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor, ctx, _task, artifact = _scene(session, org_id, users)
        version = artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[
                {
                    "statement_class": "ASSUMPTION",
                    "text": "The client provides VPN access for the test window.",
                }
            ],
        )
        assert artifacts.commitment_blockage(session, ctx, version_id=version.id) == ""
        session.rollback()


def test_the_refusal_a_reviewer_actually_sees_carries_the_diagnosis(
    app_sessionmaker, seeded_orgs, users
):
    """End to end: the reviewer's own error message names the stuck role.

    This is the assertion that would have saved the walk a round trip. Every
    other test here checks the reporting function directly; this one checks
    that the function is reached from the path a person is actually on, which
    is the part that silently rots when somebody edits the refusal.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor, ctx, _task, artifact = _scene(session, org_id, users)
        _commit(session, ctx, artifact, actor, UNSTAFFED)
        artifacts.submit(
            session,
            ctx,
            artifact_id=artifact.id,
            approval_roles=[REVIEWING_ROLE],
            note="Ready for review.",
        )
        session.flush()

        reviewer = _actor(session, org_id, users[org_id]["approver"])
        reviewer_ctx = _context(org_id, reviewer.id, REVIEWING_ROLE)
        version_id = artifact.current_version_id
        steps = artifacts.approval_chain(
            session,
            reviewer_ctx,
            entity_type=artifacts.ENTITY_TYPE,
            entity_id=version_id,
        )
        pending = [s for s in steps if s.state is ApprovalStepState.PENDING]
        assert pending, "submit wrote no approval step for the reviewer to decide"

        with pytest.raises(StateConflict) as raised:
            artifacts.decide_approval(
                session,
                reviewer_ctx,
                approval_id=pending[0].id,
                decision=ApprovalStepState.APPROVED,
                comment="Looks right.",
            )
        assert UNSTAFFED in str(raised.value)
        session.rollback()


def test_a_role_that_cannot_own_a_promise_is_refused(app_sessionmaker, seeded_orgs, users):
    """The narrowing that stopped this being whack-a-mole.

    `required_approver_role` used to be validated against the whole `RoleCode`
    enum, and the prompt offered the whole enum too -- including four roles that
    are wrong by construction. An AUDITOR reviews what happened and undertakes
    nothing; a commitment routed there is not a slow approval, it is a category
    error, and it should be refused at the door rather than surfacing later as
    an unapprovable row.

    This is a modelling decision (what a commitment can MEAN), not a permission
    calculation (who may approve one), which is why it lives in code and in the
    prompt rather than being inferred from the customer's org chart.
    """
    from app.core.errors import ValidationError as AppValidationError
    from app.core.permissions import COMMITMENT_APPROVER_ROLES, RoleCode

    assert "AUDITOR" not in COMMITMENT_APPROVER_ROLES
    assert COMMITMENT_APPROVER_ROLES < {r.value for r in RoleCode}

    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor, ctx, _task, artifact = _scene(session, org_id, users)
        with pytest.raises(AppValidationError) as raised:
            _commit(session, ctx, artifact, actor, "AUDITOR")
        # The refusal lists what IS valid -- the old one named neither the
        # offending value nor the alternatives.
        assert "AUDITOR" in str(raised.value)
        assert "BID_MANAGER" in str(raised.value)
        session.rollback()
