"""Phases M–P: the gates that stand between a draft and a customer.

Everything here is about refusing to ship. The interesting assertions are the
ones where the product declines to do the convenient thing: a placeholder that
blocks release, a non-waivable finding with no exception path even through raw
SQL, a released package that cannot be edited, and an amendment that
invalidates approved work instead of leaving it looking finished.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from app.core.errors import IllegalTransition, StateConflict
from app.core.errors import ValidationError as AppValidationError
from app.models import Department, ReleaseException, ValidationFinding
from app.models.enums import (
    ApprovalStepState,
    ExceptionState,
    FindingWaivability,
    OutputMode,
    PackageState,
)
from app.services import artifacts, bids, release
from app.services import tasks as task_service
from app.services.storage import InMemoryStorage
from tests.integration.test_release_lifecycle import (
    _approved_artifact,
    _ctx,
    _requirement_for,
    _user,
    clear_open_findings,
)

pytestmark = pytest.mark.integration


def _approved_artifact_with(session, org_id, actor, body: str):
    """An approved artifact whose text is whatever the test needs it to be.

    Several gates only fire on the *content* of an approved answer, so the
    fixture has to be able to plant that content deliberately rather than hope
    the default wording trips a rule.
    """
    ctx = _ctx(org_id, actor.id, "BID_MANAGER")
    opportunity = bids.create_opportunity(
        session,
        ctx,
        customer_name="Gate Customer",
        tender_name=f"Gate Tender {uuid.uuid4().hex[:6]}",
    )
    bid = bids.promote_to_bid(session, ctx, opportunity_id=opportunity.id)
    department = (
        session.execute(select(Department).where(Department.org_id == org_id))
        .scalars()
        .first()
    )
    task = task_service.create_task(
        session,
        ctx,
        bid_id=bid.id,
        title="Technical response",
        department_id=department.id,
        owner_user_id=actor.id,
        requirement_ids=[_requirement_for(session, ctx, org_id, bid).id],
    )
    artifact = artifacts.for_task(session, ctx, task_id=task.id)
    version = artifacts.save_draft(
        session,
        ctx,
        artifact_id=artifact.id,
        statements=[{"statement_class": "HUMAN_PROVIDED_INPUT", "text": body}],
    )
    artifacts.submit(session, ctx, artifact_id=artifact.id, approval_roles=["BID_MANAGER"])
    approval = artifacts.approval_chain(
        session, ctx, entity_type="ARTIFACT_VERSION", entity_id=version.id
    )[0]
    artifacts.decide_approval(
        session, ctx, approval_id=approval.id, decision=ApprovalStepState.APPROVED
    )
    return ctx, bid, artifact


def _packaged_with(session, org_id, users, body, *, mode=OutputMode.SUBMISSION):
    """Assemble, package and validate a bid whose approved answer says `body`."""
    storage = InMemoryStorage()
    maker = _user(session, org_id, users[org_id]["manager"])
    ctx, bid, artifact = _approved_artifact_with(session, org_id, maker, body)
    release.derive_structure(session, ctx, bid_id=bid.id)
    generated = release.assemble(
        session, ctx, bid_id=bid.id, output_mode=mode, storage=storage
    )
    package = release.create_package(session, ctx, bid_id=bid.id, output_mode=mode)
    return ctx, bid, artifact, package, generated, storage, maker


def _packaged(session, org_id, users, *, mode=OutputMode.SUBMISSION, storage=None):
    """An assembled, packaged bid ready for validation."""
    storage = storage or InMemoryStorage()
    maker = _user(session, org_id, users[org_id]["manager"])
    ctx, bid, artifact = _approved_artifact(session, org_id, maker)
    release.derive_structure(session, ctx, bid_id=bid.id)
    generated = release.assemble(
        session, ctx, bid_id=bid.id, output_mode=mode, storage=storage
    )
    package = release.create_package(session, ctx, bid_id=bid.id, output_mode=mode)
    return ctx, bid, artifact, package, generated, storage, maker


# --- validation catches what a human would miss ------------------------------


def test_a_placeholder_blocks_release_and_is_waivable(app_sessionmaker, seeded_orgs, users):
    """"TBD" reaching a customer is the cheapest possible way to lose a bid.

    It blocks, but it is waivable: sometimes a placeholder is deliberate and a
    named person accepts it on the record.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, _bid, _artifact, package, _gen, storage, _maker = _packaged_with(
            session, org_id, users, "Pricing for the optional module is TBD."
        )
        release.validate_package(session, ctx, package_id=package.id, storage=storage)

        findings = release.validation_findings(session, ctx, package_id=package.id)
        placeholder = next(
            (row for row in findings if row.check_code == "PLACEHOLDER_PRESENT"), None
        )
        assert placeholder is not None, "a TBD in an approved answer must be caught"
        assert placeholder.waivability is FindingWaivability.WAIVABLE
        assert package.state is PackageState.BLOCKED_PENDING_EXCEPTION
        session.commit()


def test_an_unapproved_artifact_blocks_the_package(app_sessionmaker, seeded_orgs, users):
    """Assembly is not approval. An unapproved answer is an unanswered one."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, artifact, package, _gen, storage, _maker = _packaged(
            session, org_id, users
        )
        # Reopen the approved artifact: it is now a draft again.
        artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[
                {"statement_class": "HUMAN_PROVIDED_INPUT", "text": "Still being edited."}
            ],
            change_reason="More work needed.",
        )
        release.validate_package(session, ctx, package_id=package.id, storage=storage)
        codes = {
            row.check_code
            for row in release.validation_findings(session, ctx, package_id=package.id)
        }
        assert "MANDATORY_UNANSWERED" in codes
        assert package.state is not PackageState.RELEASE_PENDING_APPROVALS
        session.commit()


def test_a_blocked_package_cannot_be_submitted_for_release(
    app_sessionmaker, seeded_orgs, users
):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, artifact, package, gen, storage, _maker = _packaged(session, org_id, users)
        storage.put(gen[0].storage_key, b"tampered", content_type="application/octet-stream")
        release.validate_package(session, ctx, package_id=package.id, storage=storage)
        assert package.state is PackageState.BLOCKED_HARD

        with pytest.raises(IllegalTransition):
            release.submit_for_release(session, ctx, package_id=package.id)
        session.commit()


# --- waivability is data, not a code path ------------------------------------


def test_a_non_waivable_finding_has_no_exception_path_even_through_sql(
    app_sessionmaker, seeded_orgs, users
):
    """The service refusal is the polite one. This is the guarantee.

    A validation rule enforced only in Python survives exactly until someone
    adds a second write path under deadline pressure.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, artifact, package, gen, storage, _maker = _packaged(session, org_id, users)
        storage.put(gen[0].storage_key, b"tampered", content_type="application/octet-stream")
        release.validate_package(session, ctx, package_id=package.id, storage=storage)
        corrupted = next(
            row
            for row in release.validation_findings(session, ctx, package_id=package.id)
            if row.check_code == "CORRUPTED_ARTIFACT"
        )

        with pytest.raises(StateConflict, match="no exception path"):
            release.request_exception(
                session, ctx, finding_id=corrupted.id, justification="Ship it anyway."
            )

        with pytest.raises(Exception, match="WAIVABLE"):
            session.execute(
                text(
                    "INSERT INTO release_exception "
                    "(id, org_id, finding_id, requested_by, justification, requested_at, "
                    " state, scope, created_at) "
                    "VALUES (:id, :org, :finding, :who, 'bypass', now(), "
                    "        'REQUESTED', 'THIS_PACKAGE_VERSION', now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "org": org_id,
                    "finding": corrupted.id,
                    "who": ctx.actor_user_id,
                },
            )
        session.rollback()


def test_a_waivable_finding_clears_only_when_the_exception_is_approved(
    app_sessionmaker, seeded_orgs, users
):
    """Requesting an exception is not receiving one."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, _bid, _artifact, package, _gen, storage, maker = _packaged_with(
            session, org_id, users, "The rate card is TBD pending commercial sign-off."
        )
        release.validate_package(session, ctx, package_id=package.id, storage=storage)

        findings = [
            row
            for row in release.validation_findings(session, ctx, package_id=package.id)
            if row.waivability is FindingWaivability.WAIVABLE and row.resolved_at is None
        ]
        assert findings, "the planted placeholder must produce a waivable finding"
        finding = findings[0]
        requested = release.request_exception(
            session,
            ctx,
            finding_id=finding.id,
            justification="Accepted by the bid director for this submission.",
        )
        assert requested.state is ExceptionState.REQUESTED
        session.refresh(finding)
        assert finding.resolved_at is None, "requesting must not clear the finding"

        with pytest.raises(AppValidationError):
            release.decide_exception(
                session,
                ctx,
                exception_id=requested.id,
                decision=ExceptionState.REQUESTED,
            )

        release.decide_exception(
            session,
            ctx,
            exception_id=requested.id,
            decision=ExceptionState.APPROVED,
            comment="Signed off.",
        )
        session.refresh(finding)
        assert finding.resolved_at is not None

        with pytest.raises(StateConflict, match="already been decided"):
            release.decide_exception(
                session,
                ctx,
                exception_id=requested.id,
                decision=ExceptionState.REJECTED,
            )
        session.commit()


def test_a_rejected_exception_leaves_the_finding_open(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, _bid, _artifact, package, _gen, storage, _maker = _packaged_with(
            session, org_id, users, "The delivery schedule is TBD."
        )
        release.validate_package(session, ctx, package_id=package.id, storage=storage)
        findings = [
            row
            for row in release.validation_findings(session, ctx, package_id=package.id)
            if row.waivability is FindingWaivability.WAIVABLE and row.resolved_at is None
        ]
        assert findings, "the planted placeholder must produce a waivable finding"

        requested = release.request_exception(
            session, ctx, finding_id=findings[0].id, justification="Please waive."
        )
        release.decide_exception(
            session,
            ctx,
            exception_id=requested.id,
            decision=ExceptionState.REJECTED,
            comment="Not acceptable to the customer.",
        )
        session.refresh(findings[0])
        assert findings[0].resolved_at is None
        assert requested.approved_by is None
        session.commit()


# --- release, and what a release freezes -------------------------------------


def test_a_released_package_is_frozen(app_sessionmaker, seeded_orgs, users):
    """What was sent must remain exactly what was sent."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        storage = InMemoryStorage()
        maker = _user(session, org_id, users[org_id]["manager"])
        checker = _user(session, org_id, users[org_id]["auditor"])
        ctx, bid, _artifact = _approved_artifact(session, org_id, maker)
        checker_ctx = _ctx(org_id, checker.id, "EXECUTIVE_APPROVER")

        release.derive_structure(session, ctx, bid_id=bid.id)
        release.assemble(
            session, ctx, bid_id=bid.id, output_mode=OutputMode.REVIEW, storage=storage
        )
        package = release.create_package(
            session, ctx, bid_id=bid.id, output_mode=OutputMode.REVIEW
        )
        release.validate_package(session, ctx, package_id=package.id, storage=storage)
        clear_open_findings(session, ctx, package.id)
        release.submit_for_release(session, ctx, package_id=package.id)
        release.approve_release(session, checker_ctx, package_id=package.id)

        assert package.immutable is True
        with pytest.raises(StateConflict, match="immutable"):
            release.validate_package(session, ctx, package_id=package.id, storage=storage)

        archived = release.archive(
            session, checker_ctx, package_id=package.id, reason="Submission closed."
        )
        assert archived.state is PackageState.ARCHIVED
        session.commit()


def test_an_unsubmitted_package_cannot_be_approved(app_sessionmaker, seeded_orgs, users):
    """Approval follows submission. Skipping the step skips the maker."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        storage = InMemoryStorage()
        maker = _user(session, org_id, users[org_id]["manager"])
        checker = _user(session, org_id, users[org_id]["auditor"])
        ctx, bid, _artifact = _approved_artifact(session, org_id, maker)
        checker_ctx = _ctx(org_id, checker.id, "EXECUTIVE_APPROVER")

        release.derive_structure(session, ctx, bid_id=bid.id)
        release.assemble(
            session, ctx, bid_id=bid.id, output_mode=OutputMode.REVIEW, storage=storage
        )
        package = release.create_package(
            session, ctx, bid_id=bid.id, output_mode=OutputMode.REVIEW
        )
        release.validate_package(session, ctx, package_id=package.id, storage=storage)

        with pytest.raises(IllegalTransition, match="not been submitted"):
            release.approve_release(session, checker_ctx, package_id=package.id)
        session.commit()


def test_only_a_released_package_can_be_archived(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, _artifact, package, _gen, storage, _maker = _packaged(
            session, org_id, users, mode=OutputMode.REVIEW
        )
        with pytest.raises(IllegalTransition, match="Only a released package"):
            release.archive(session, ctx, package_id=package.id, reason="Too early.")
        session.commit()


# --- tenant isolation of the release trail -----------------------------------


def test_findings_and_exceptions_do_not_cross_tenants(
    app_sessionmaker, seeded_orgs, users
):
    org_a, org_b = seeded_orgs
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid, _artifact, package, gen, storage, _maker = _packaged(session, org_a, users)
        storage.put(gen[0].storage_key, b"tampered", content_type="application/octet-stream")
        release.validate_package(session, ctx, package_id=package.id, storage=storage)
        package_id = package.id
        session.commit()

    with app_sessionmaker() as session:
        session.begin()
        from app.db.session import apply_tenant_context

        apply_tenant_context(session, _ctx(org_b, uuid.uuid4()))
        assert (
            session.execute(
                select(ValidationFinding).where(ValidationFinding.org_id == org_a)
            )
            .scalars()
            .all()
            == []
        )
        assert (
            session.execute(select(ReleaseException).where(ReleaseException.org_id == org_a))
            .scalars()
            .all()
            == []
        )
        assert release.packages(session, _ctx(org_b, uuid.uuid4()), bid_id=package_id) == []
        session.rollback()
