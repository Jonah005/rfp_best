from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.core.errors import StateConflict
from app.db.session import apply_tenant_context
from app.models import Department, User
from app.models.enums import (
    ApprovalStepState,
    OutputMode,
    PackageState,
)
from app.services import artifacts, bids, release, tasks
from app.services.storage import InMemoryStorage

pytestmark = pytest.mark.integration


def _ctx(org_id: uuid.UUID, actor_id: uuid.UUID, *roles: str) -> ExecutionContext:
    return ExecutionContext(
        org_id=org_id,
        actor_user_id=actor_id,
        role_codes=frozenset(roles),
    )


def _user(session, org_id: uuid.UUID, email: str) -> User:
    apply_tenant_context(session, ExecutionContext(org_id=org_id))
    return session.execute(
        select(User).where(User.org_id == org_id, User.email == email)
    ).scalar_one()


def _requirement_for(session, ctx: ExecutionContext, org_id: uuid.UUID, bid):
    """A task must answer at least one requirement of its bid, so the bid gets one."""
    from app.gateway.embeddings import HashingEmbeddingProvider
    from app.ingestion.safety import EicarAwareScanner
    from tests.integration.test_assessment_readiness import (
        ISO_TEXT,
        _company_version,
        _requirement,
    )

    version = _company_version(
        session, ctx, (org_id, InMemoryStorage(), EicarAwareScanner(), HashingEmbeddingProvider())
    )
    return _requirement(
        session,
        ctx,
        bid=bid,
        doc_version=version,
        code=f"REL-{uuid.uuid4().hex[:4]}",
        text=ISO_TEXT,
    )


def clear_open_findings(session, ctx: ExecutionContext, package_id) -> None:
    """Accept every open waivable finding, as the release process allows a named person to.

    Review packages now carry honest advisory findings (a placeholder marker, an unverified
    matrix row, a missing tender reference), so a bare package is no longer born clean.
    """
    from app.models.enums import ExceptionState, FindingWaivability

    for finding in release.validation_findings(session, ctx, package_id=package_id):
        if finding.resolved_at is None and finding.waivability is FindingWaivability.WAIVABLE:
            requested = release.request_exception(
                session, ctx, finding_id=finding.id, justification="Accepted for this test."
            )
            release.decide_exception(
                session, ctx, exception_id=requested.id, decision=ExceptionState.APPROVED
            )


def _approved_artifact(session, org_id: uuid.UUID, actor: User):
    ctx = _ctx(org_id, actor.id, "BID_MANAGER")
    opportunity = bids.create_opportunity(
        session,
        ctx,
        customer_name="Release Customer",
        tender_name=f"Release Tender {uuid.uuid4().hex[:6]}",
    )
    bid = bids.promote_to_bid(session, ctx, opportunity_id=opportunity.id)
    department = session.execute(
        select(Department).where(Department.org_id == org_id)
    ).scalars().first()
    assert department is not None
    requirement = _requirement_for(session, ctx, org_id, bid)
    task = tasks.create_task(
        session,
        ctx,
        bid_id=bid.id,
        title="Technical response",
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
                "text": "The approved response contains no unresolved placeholders.",
            }
        ],
    )
    artifacts.submit(
        session,
        ctx,
        artifact_id=artifact.id,
        approval_roles=["BID_MANAGER"],
    )
    approval = artifacts.approval_chain(
        session,
        ctx,
        entity_type="ARTIFACT_VERSION",
        entity_id=version.id,
    )[0]
    artifacts.decide_approval(
        session,
        ctx,
        approval_id=approval.id,
        decision=ApprovalStepState.APPROVED,
    )
    return ctx, bid, artifact


def test_approved_artifacts_assemble_validate_and_release_with_maker_checker(
    app_sessionmaker, seeded_orgs, users
):
    org_id = seeded_orgs[0]
    storage = InMemoryStorage()
    with app_sessionmaker() as session:
        session.begin()
        maker = _user(session, org_id, users[org_id]["manager"])
        checker = _user(session, org_id, users[org_id]["auditor"])
        maker_ctx, bid, _artifact = _approved_artifact(session, org_id, maker)
        checker_ctx = _ctx(org_id, checker.id, "EXECUTIVE_APPROVER")

        structure = release.derive_structure(session, maker_ctx, bid_id=bid.id)
        assert release.coverage(session, maker_ctx, bid_id=bid.id)["uncovered_sections"] == []
        generated = release.assemble(
            session,
            maker_ctx,
            bid_id=bid.id,
            output_mode=OutputMode.REVIEW,
            storage=storage,
        )
        assert storage.exists(generated[0].storage_key)

        package = release.create_package(
            session, maker_ctx, bid_id=bid.id, output_mode=OutputMode.REVIEW
        )
        run = release.validate_package(
            session, maker_ctx, package_id=package.id, storage=storage
        )
        assert run.status.value == "COMPLETE"
        assert package.state in (
            PackageState.BLOCKED_PENDING_EXCEPTION,
            PackageState.RELEASE_PENDING_APPROVALS,
        )
        clear_open_findings(session, maker_ctx, package.id)
        assert package.state is PackageState.RELEASE_PENDING_APPROVALS
        release.submit_for_release(session, maker_ctx, package_id=package.id)
        with pytest.raises(StateConflict, match="own release"):
            release.approve_release(session, maker_ctx, package_id=package.id)
        release.approve_release(session, checker_ctx, package_id=package.id)
        assert package.state is PackageState.RELEASED
        assert package.immutable is True
        assert package.submitted_by != package.released_by
        with pytest.raises(StateConflict, match="immutable"):
            release.validate_package(
                session, maker_ctx, package_id=package.id, storage=storage
            )
        assert structure.bid_id == bid.id
        session.commit()


def test_corrupted_output_is_non_waivable_and_has_no_exception_path(
    app_sessionmaker, seeded_orgs, users
):
    org_id = seeded_orgs[0]
    storage = InMemoryStorage()
    with app_sessionmaker() as session:
        session.begin()
        maker = _user(session, org_id, users[org_id]["manager"])
        ctx, bid, _artifact = _approved_artifact(session, org_id, maker)
        release.derive_structure(session, ctx, bid_id=bid.id)
        generated = release.assemble(
            session,
            ctx,
            bid_id=bid.id,
            output_mode=OutputMode.SUBMISSION,
            storage=storage,
        )[0]
        storage.put(generated.storage_key, b"tampered", content_type="application/octet-stream")
        package = release.create_package(
            session, ctx, bid_id=bid.id, output_mode=OutputMode.SUBMISSION
        )
        release.validate_package(session, ctx, package_id=package.id, storage=storage)
        findings = release.validation_findings(session, ctx, package_id=package.id)
        corrupted = next(row for row in findings if row.check_code == "CORRUPTED_ARTIFACT")
        assert package.state is PackageState.BLOCKED_HARD
        assert corrupted.waivability.value == "NON_WAIVABLE"
        with pytest.raises(StateConflict, match="no exception path"):
            release.request_exception(
                session,
                ctx,
                finding_id=corrupted.id,
                justification="Accept the altered file.",
            )
        session.commit()
