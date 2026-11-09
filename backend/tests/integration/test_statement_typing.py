"""Phase L: the statement typing rules, and where they are enforced.

The distinction the drafting model rests on is that "we hold ISO 27001", "we
will staff two OSCP testers" and "we assume the client provides VPN access" are
three different kinds of sentence. Only the first is checkable, only the second
binds the company, and only the third has to survive into the assumptions
register. A response that cannot tell them apart cannot be reviewed.

These tests push on the boundary from both sides: the service refusal a
contributor sees, and the CHECK constraint that still holds when the service is
wrong. A rule enforced only in Python is a rule that lasts until the next code
path.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from app.core.errors import StateConflict
from app.core.errors import ValidationError as AppValidationError
from app.models import ArtifactStatement, ArtifactVersion, Assumption, Commitment
from app.models.enums import (
    ApprovalStatus,
    ApprovalStepState,
    ArtifactVersionState,
    AssumptionStatus,
    StatementApproval,
    StatementClass,
)
from app.services import artifacts
from tests.integration.test_artifact_lifecycle import _actor, _context, _make_task

pytestmark = pytest.mark.integration


def _scene(session, org_id, users):
    actor = _actor(session, org_id, users[org_id]["manager"])
    ctx, task, artifact = _make_task(session, org_id, actor.id)
    return actor, ctx, task, artifact


# --- what the service refuses ------------------------------------------------


def test_a_tender_derived_statement_must_say_where_it_came_from(
    app_sessionmaker, seeded_orgs, users
):
    """The whole point of the class is traceability back to the customer's text."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        _actor_row, ctx, _task, artifact = _scene(session, org_id, users)
        with pytest.raises(AppValidationError):
            artifacts.save_draft(
                session,
                ctx,
                artifact_id=artifact.id,
                statements=[
                    {
                        "statement_class": "TENDER_DERIVED",
                        "text": "The response covers the requested scope.",
                    }
                ],
            )
        session.rollback()


def test_a_blank_statement_is_refused(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        _a, ctx, _t, artifact = _scene(session, org_id, users)
        with pytest.raises(AppValidationError):
            artifacts.save_draft(
                session,
                ctx,
                artifact_id=artifact.id,
                statements=[{"statement_class": "HUMAN_PROVIDED_INPUT", "text": "   "}],
            )
        session.rollback()


def test_an_unknown_statement_class_is_refused(app_sessionmaker, seeded_orgs, users):
    """There is no untyped statement. A sentence with no class is a sentence
    nobody can review."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        _a, ctx, _t, artifact = _scene(session, org_id, users)
        with pytest.raises(AppValidationError, match="statement_class"):
            artifacts.save_draft(
                session,
                ctx,
                artifact_id=artifact.id,
                statements=[{"statement_class": "MARKETING_FLOURISH", "text": "We are best."}],
            )
        session.rollback()


def test_an_empty_draft_is_refused(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        _a, ctx, _t, artifact = _scene(session, org_id, users)
        with pytest.raises(AppValidationError):
            artifacts.save_draft(session, ctx, artifact_id=artifact.id, statements=[])
        session.rollback()


# --- what the database refuses ----------------------------------------------


@pytest.mark.parametrize(
    ("statement_class", "constraint"),
    [
        ("TENDER_DERIVED", "chk_tender_derived_has_locator"),
        ("PROPOSED_COMMITMENT", "chk_commitment_linked"),
        ("ASSUMPTION", "chk_assumption_linked"),
        ("HUMAN_PROVIDED_INPUT", "chk_human_input_attributed"),
    ],
)
def test_the_typing_rules_are_enforced_below_the_service(
    app_sessionmaker, seeded_orgs, users, statement_class, constraint
):
    """Raw SQL cannot write an untraceable statement either.

    This is the test that matters when someone later adds a second write path
    and forgets the validation in it.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        _a, ctx, _t, artifact = _scene(session, org_id, users)
        version = artifacts.versions(session, ctx, artifact_id=artifact.id)[0]

        with pytest.raises(Exception, match=constraint):
            session.execute(
                text(
                    "INSERT INTO artifact_statement "
                    "(id, org_id, artifact_version_id, order_index, statement_class, "
                    " text, approval_state, created_at) "
                    "VALUES (:id, :org, :ver, 0, :cls, 'bypass', 'DRAFT', now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "org": org_id,
                    "ver": version.id,
                    "cls": statement_class,
                },
            )
        session.rollback()


# --- commitments and assumptions become their own objects --------------------


def test_a_proposed_commitment_creates_a_commitment_needing_approval(
    app_sessionmaker, seeded_orgs, users
):
    """A promise is not prose. It gets a row, an owner and a named approver."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor, ctx, _t, artifact = _scene(session, org_id, users)
        artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[
                {
                    "statement_class": "PROPOSED_COMMITMENT",
                    "text": "We will provide 24x7 support for the contract term.",
                    "required_approver_role": "BID_MANAGER",
                    "commitment_owner_user_id": actor.id,
                }
            ],
        )
        commitments = artifacts.list_commitments(session, ctx, bid_id=artifact.bid_id)
        assert len(commitments) == 1
        assert commitments[0].approval_status is not ApprovalStatus.APPROVED
        assert commitments[0].required_approver_role == "BID_MANAGER"
        session.commit()


def test_an_assumption_creates_a_register_entry_that_can_be_carried(
    app_sessionmaker, seeded_orgs, users
):
    """An assumption that survives to submission must be declared to the
    customer; one silently resolved must not be."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        _a, ctx, _t, artifact = _scene(session, org_id, users)
        artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[
                {
                    "statement_class": "ASSUMPTION",
                    "text": "The client provides VPN access to the test environment.",
                }
            ],
        )
        register = artifacts.list_assumptions(session, ctx, bid_id=artifact.bid_id)
        assert len(register) == 1
        assert register[0].status is AssumptionStatus.OPEN

        carried = artifacts.update_assumption(
            session,
            ctx,
            assumption_id=register[0].id,
            status=AssumptionStatus.CARRIED_INTO_SUBMISSION,
            resolution="Declared in the assumptions annex.",
        )
        assert carried.status is AssumptionStatus.CARRIED_INTO_SUBMISSION
        assert carried.resolution
        session.commit()


# --- version immutability ----------------------------------------------------


def test_a_version_under_review_cannot_be_edited(app_sessionmaker, seeded_orgs, users):
    """Editing what a reviewer is reading is how an approval ends up attached to
    text nobody approved."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        _a, ctx, _t, artifact = _scene(session, org_id, users)
        artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[
                {"statement_class": "HUMAN_PROVIDED_INPUT", "text": "First wording."}
            ],
        )
        artifacts.submit(session, ctx, artifact_id=artifact.id, approval_roles=["BID_MANAGER"])

        with pytest.raises(StateConflict, match="under review"):
            artifacts.save_draft(
                session,
                ctx,
                artifact_id=artifact.id,
                statements=[
                    {"statement_class": "HUMAN_PROVIDED_INPUT", "text": "Sneaky rewording."}
                ],
            )
        session.rollback()


def test_an_approved_version_keeps_its_text_when_a_new_draft_starts(
    app_sessionmaker, seeded_orgs, users
):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        _a, ctx, _t, artifact = _scene(session, org_id, users)
        artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[
                {"statement_class": "HUMAN_PROVIDED_INPUT", "text": "Approved wording."}
            ],
        )
        artifacts.submit(session, ctx, artifact_id=artifact.id, approval_roles=["BID_MANAGER"])
        approval = artifacts.approval_chain(
            session,
            ctx,
            entity_type="ARTIFACT_VERSION",
            entity_id=artifact.current_version_id,
        )[0]
        artifacts.decide_approval(
            session, ctx, approval_id=approval.id, decision=ApprovalStepState.APPROVED
        )

        v1 = artifacts.version_by_number(session, ctx, artifact_id=artifact.id, version_no=1)
        v1_text = v1.rendered_text
        v1_statements = [
            s.text for s in artifacts.statements_for_version(session, ctx, version_id=v1.id)
        ]

        artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[
                {"statement_class": "HUMAN_PROVIDED_INPUT", "text": "Completely different."}
            ],
            change_reason="Rework after review.",
        )

        session.refresh(v1)
        assert v1.state is ArtifactVersionState.APPROVED
        assert v1.rendered_text == v1_text
        assert [
            s.text for s in artifacts.statements_for_version(session, ctx, version_id=v1.id)
        ] == v1_statements
        assert artifact.approved_version_id == v1.id
        session.commit()


def test_rework_returns_the_artifact_to_an_editable_draft(
    app_sessionmaker, seeded_orgs, users
):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        _a, ctx, _t, artifact = _scene(session, org_id, users)
        artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[{"statement_class": "HUMAN_PROVIDED_INPUT", "text": "Needs work."}],
        )
        artifacts.submit(session, ctx, artifact_id=artifact.id, approval_roles=["BID_MANAGER"])
        approval = artifacts.approval_chain(
            session,
            ctx,
            entity_type="ARTIFACT_VERSION",
            entity_id=artifact.current_version_id,
        )[0]
        artifacts.decide_approval(
            session,
            ctx,
            approval_id=approval.id,
            decision=ApprovalStepState.REJECTED,
            comment="The scope paragraph is unsupported.",
        )

        # A rejected artifact is editable again, and the rejection is on record.
        again = artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[
                {"statement_class": "HUMAN_PROVIDED_INPUT", "text": "Reworked wording."}
            ],
            change_reason="Addressed the reviewer's point.",
        )
        assert again.state is ArtifactVersionState.DRAFT
        assert again.version_no == 2
        session.commit()


def test_replacing_a_draft_does_not_leave_orphaned_statements(
    app_sessionmaker, seeded_orgs, users
):
    """Re-saving a draft replaces its statements rather than accumulating them.

    Otherwise the rendered text and the statement list disagree, and the
    statement list is what validation reads.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        _a, ctx, _t, artifact = _scene(session, org_id, users)
        artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[
                {"statement_class": "HUMAN_PROVIDED_INPUT", "text": "One."},
                {"statement_class": "HUMAN_PROVIDED_INPUT", "text": "Two."},
            ],
        )
        version = artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[{"statement_class": "HUMAN_PROVIDED_INPUT", "text": "Only one now."}],
        )
        rows = artifacts.statements_for_version(session, ctx, version_id=version.id)
        assert [s.text for s in rows] == ["Only one now."]
        assert version.rendered_text and "Two." not in version.rendered_text
        assert [s.order_index for s in rows] == [0]
        session.commit()


def test_statements_and_artifacts_do_not_cross_tenants(
    app_sessionmaker, seeded_orgs, users
):
    org_a, org_b = seeded_orgs
    with app_sessionmaker() as session:
        session.begin()
        _a, ctx, _t, artifact = _scene(session, org_a, users)
        version = artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[{"statement_class": "HUMAN_PROVIDED_INPUT", "text": "Tenant A text."}],
        )
        version_id = version.id
        session.commit()

    with app_sessionmaker() as session:
        session.begin()
        from app.db.session import apply_tenant_context

        apply_tenant_context(session, _context(org_b, uuid.uuid4()))
        assert (
            session.execute(
                select(ArtifactStatement).where(
                    ArtifactStatement.artifact_version_id == version_id
                )
            )
            .scalars()
            .all()
            == []
        )
        assert (
            session.execute(select(ArtifactVersion).where(ArtifactVersion.id == version_id))
            .scalars()
            .all()
            == []
        )
        session.rollback()


def test_commitments_and_assumptions_are_scoped_to_their_bid(
    app_sessionmaker, seeded_orgs, users
):
    """Two bids in one tenant must not share a commitments register."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor, ctx, _t, artifact_one = _scene(session, org_id, users)
        _ctx2, _task2, artifact_two = _make_task(session, org_id, actor.id)

        artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact_one.id,
            statements=[
                {
                    "statement_class": "PROPOSED_COMMITMENT",
                    "text": "Bid one commitment.",
                    "required_approver_role": "BID_MANAGER",
                    "commitment_owner_user_id": actor.id,
                }
            ],
        )
        assert len(artifacts.list_commitments(session, ctx, bid_id=artifact_one.bid_id)) == 1
        assert artifacts.list_commitments(session, ctx, bid_id=artifact_two.bid_id) == []

        # Every commitment in this tenant belongs to a bid, and none of them
        # leaked into the second one. (The tenant may hold commitments from
        # other tests; what matters is that bid two's register is empty.)
        for row in (
            session.execute(select(Commitment).where(Commitment.org_id == org_id))
            .scalars()
            .all()
        ):
            assert row.bid_id != artifact_two.bid_id
        for row in (
            session.execute(select(Assumption).where(Assumption.org_id == org_id))
            .scalars()
            .all()
        ):
            assert row.bid_id != artifact_two.bid_id
        session.commit()


def test_statements_start_unapproved(app_sessionmaker, seeded_orgs, users):
    """Nothing is approved by being written."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        _a, ctx, _t, artifact = _scene(session, org_id, users)
        version = artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[{"statement_class": "HUMAN_PROVIDED_INPUT", "text": "Fresh text."}],
        )
        rows = artifacts.statements_for_version(session, ctx, version_id=version.id)
        assert all(s.approval_state is StatementApproval.DRAFT for s in rows)
        assert all(s.statement_class is StatementClass.HUMAN_PROVIDED_INPUT for s in rows)
        assert all(s.approved_by is None for s in rows)
        session.commit()
