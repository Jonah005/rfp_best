"""AccessGrant lifecycle and the three cross-department outcomes."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.core.errors import ValidationError
from app.db.session import apply_tenant_context
from app.models import AccessGrant, AuditEvent, User
from app.models.enums import GrantPermission, GrantResourceType, GrantState
from app.services import access_grants
from app.services.permission_evaluator import PermissionEvaluator, ResourceRef
from tests.integration.conftest import make_task

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario(app_sessionmaker, seeded_orgs):
    """A granter, a grantee and a task, all in tenant A."""
    org_a, _ = seeded_orgs
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org_a)
        apply_tenant_context(s, ctx)
        granter = User(org_id=org_a, email=f"hr-{uuid.uuid4().hex[:8]}@a.test", display_name="HR")
        grantee = User(
            org_id=org_a, email=f"sec-{uuid.uuid4().hex[:8]}@a.test", display_name="Security"
        )
        s.add_all([granter, grantee])
        s.flush()
        task = make_task(s, org_a, code=f"SEC-{uuid.uuid4().hex[:3]}", title="VAPT method")
        result = (org_a, granter.id, grantee.id, task.id)
        s.commit()
    return result


def test_grant_access_creates_one_grant_and_an_audit_event(app_sessionmaker, scenario):
    org, granter, grantee, task = scenario
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org, actor_user_id=granter)
        apply_tenant_context(s, ctx)
        grant = access_grants.create_grant(
            s,
            ctx,
            grantee_user_id=grantee,
            task_id=task,
            resource_type=GrantResourceType.DOCUMENT_VERSION,
            resource_id=uuid.uuid4(),
            reason="R-042 needs the availability sheet",
        )
        assert grant.effective_state() is GrantState.ACTIVE
        events = (
            s.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "access_grant.created", AuditEvent.entity_id == grant.id
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1, "every grant must leave an audit trail"
        s.commit()


def test_share_evidence_creates_one_narrow_grant_per_evidence_id(app_sessionmaker, scenario):
    """Correction 3: Share Evidence is narrow and per-item, never a blanket grant."""
    org, granter, grantee, task = scenario
    evidence_ids = [uuid.uuid4() for _ in range(3)]
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org, actor_user_id=granter)
        apply_tenant_context(s, ctx)
        grants = access_grants.share_evidence(
            s,
            ctx,
            grantee_user_id=grantee,
            task_id=task,
            evidence_ids=evidence_ids,
            reason="sharing the three certificates",
        )
        assert len(grants) == 3
        assert {g.resource_id for g in grants} == set(evidence_ids)
        assert all(g.resource_type is GrantResourceType.EVIDENCE for g in grants)
        assert all(g.permission is GrantPermission.READ for g in grants)
        assert all(g.task_id == task for g in grants), "grants must be task-scoped"
        s.commit()


def test_share_evidence_requires_at_least_one_id(app_sessionmaker, scenario):
    org, granter, grantee, task = scenario
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org, actor_user_id=granter)
        apply_tenant_context(s, ctx)
        with pytest.raises(ValidationError):
            access_grants.share_evidence(
                s,
                ctx,
                grantee_user_id=grantee,
                task_id=task,
                evidence_ids=[],
                reason="nothing to share",
            )
        s.rollback()


def test_grant_requires_a_reason_and_a_future_expiry(app_sessionmaker, scenario):
    org, granter, grantee, task = scenario
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org, actor_user_id=granter)
        apply_tenant_context(s, ctx)
        with pytest.raises(ValidationError):
            access_grants.create_grant(
                s,
                ctx,
                grantee_user_id=grantee,
                task_id=task,
                resource_type=GrantResourceType.EVIDENCE,
                resource_id=uuid.uuid4(),
                reason="   ",
            )
        with pytest.raises(ValidationError):
            access_grants.create_grant(
                s,
                ctx,
                grantee_user_id=grantee,
                task_id=task,
                resource_type=GrantResourceType.EVIDENCE,
                resource_id=uuid.uuid4(),
                reason="valid reason",
                expires_at=datetime.now(timezone.utc) - timedelta(days=1),
            )
        s.rollback()


def test_revocation_takes_effect_on_the_next_permission_check(app_sessionmaker, scenario):
    """No cache, no job latency: the very next query sees the revocation."""
    org, granter, grantee, task = scenario
    evidence_id = uuid.uuid4()
    resource = ResourceRef(GrantResourceType.EVIDENCE, evidence_id, classification="HR_RECORD")

    with app_sessionmaker() as s:
        s.begin()
        granter_ctx = ExecutionContext(org_id=org, actor_user_id=granter)
        apply_tenant_context(s, granter_ctx)
        grant = access_grants.create_grant(
            s,
            granter_ctx,
            grantee_user_id=grantee,
            task_id=task,
            resource_type=GrantResourceType.EVIDENCE,
            resource_id=evidence_id,
            reason="temporary access for R-042",
        )
        grant_id = grant.id

        grantee_ctx = ExecutionContext(org_id=org, actor_user_id=grantee)
        evaluator = PermissionEvaluator(s, grantee_ctx)
        before = evaluator.can_read_resource(resource, task_id=task)
        assert before.allowed and before.basis == "GRANT"

        access_grants.revoke_grant(s, granter_ctx, grant_id=grant_id, reason="no longer needed")

        after = evaluator.can_read_resource(resource, task_id=task)
        assert not after.allowed, "a revoked grant must stop working immediately"
        s.commit()


def test_expired_grant_denies_even_though_the_sweep_has_not_run(app_sessionmaker, scenario):
    org, granter, grantee, task = scenario
    evidence_id = uuid.uuid4()
    with app_sessionmaker() as s:
        s.begin()
        granter_ctx = ExecutionContext(org_id=org, actor_user_id=granter)
        apply_tenant_context(s, granter_ctx)
        grant = access_grants.create_grant(
            s,
            granter_ctx,
            grantee_user_id=grantee,
            task_id=task,
            resource_type=GrantResourceType.EVIDENCE,
            resource_id=evidence_id,
            reason="short-lived",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
        # Force expiry without running any job.
        grant.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.flush()
        assert grant.materialized_state is None

        grantee_ctx = ExecutionContext(org_id=org, actor_user_id=grantee)
        decision = PermissionEvaluator(s, grantee_ctx).can_read_resource(
            ResourceRef(GrantResourceType.EVIDENCE, evidence_id, classification="PRICING"),
            task_id=task,
        )
        assert not decision.allowed
        s.commit()


def test_grant_is_scoped_to_one_task(app_sessionmaker, scenario):
    """A grant for task X must not open the resource inside task Y."""
    org, granter, grantee, task = scenario
    evidence_id = uuid.uuid4()
    other_task = uuid.uuid4()
    with app_sessionmaker() as s:
        s.begin()
        granter_ctx = ExecutionContext(org_id=org, actor_user_id=granter)
        apply_tenant_context(s, granter_ctx)
        access_grants.create_grant(
            s,
            granter_ctx,
            grantee_user_id=grantee,
            task_id=task,
            resource_type=GrantResourceType.EVIDENCE,
            resource_id=evidence_id,
            reason="task-scoped only",
        )
        grantee_ctx = ExecutionContext(org_id=org, actor_user_id=grantee)
        evaluator = PermissionEvaluator(s, grantee_ctx)
        resource = ResourceRef(GrantResourceType.EVIDENCE, evidence_id, classification="PRICING")
        assert evaluator.can_read_resource(resource, task_id=task).allowed
        assert not evaluator.can_read_resource(resource, task_id=other_task).allowed
        assert not evaluator.can_read_resource(resource, task_id=None).allowed
        s.commit()


def test_revoke_for_task_revokes_every_grant_on_that_task(app_sessionmaker, scenario):
    org, granter, grantee, task = scenario
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org, actor_user_id=granter)
        apply_tenant_context(s, ctx)
        access_grants.share_evidence(
            s,
            ctx,
            grantee_user_id=grantee,
            task_id=task,
            evidence_ids=[uuid.uuid4() for _ in range(4)],
            reason="bulk",
        )
        count = access_grants.revoke_for_task(s, ctx, task_id=task, reason="task completed")
        assert count == 4
        remaining = access_grants.list_grants(
            s, ctx, task_id=task, effective_state=GrantState.ACTIVE
        )
        assert remaining == []
        s.commit()


def test_materialize_states_never_changes_authorization(app_sessionmaker, scenario):
    """The sweep is cosmetic. It must not be able to alter an access decision."""
    org, granter, grantee, task = scenario
    evidence_id = uuid.uuid4()
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org, actor_user_id=granter)
        apply_tenant_context(s, ctx)
        grant = access_grants.create_grant(
            s,
            ctx,
            grantee_user_id=grantee,
            task_id=task,
            resource_type=GrantResourceType.EVIDENCE,
            resource_id=evidence_id,
            reason="sweep test",
        )
        grantee_ctx = ExecutionContext(org_id=org, actor_user_id=grantee)
        resource = ResourceRef(GrantResourceType.EVIDENCE, evidence_id, classification="PRICING")
        before = PermissionEvaluator(s, grantee_ctx).can_read_resource(resource, task_id=task)
        access_grants.materialize_states(s, ctx)
        after = PermissionEvaluator(s, grantee_ctx).can_read_resource(resource, task_id=task)
        assert before.allowed == after.allowed is True
        s.refresh(grant)
        assert grant.materialized_state is GrantState.ACTIVE
        s.commit()


def test_grants_do_not_leak_across_tenants(app_sessionmaker, seeded_orgs, scenario):
    org_a, org_b = seeded_orgs
    _, granter, grantee, task = scenario
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org_a, actor_user_id=granter)
        apply_tenant_context(s, ctx)
        access_grants.create_grant(
            s,
            ctx,
            grantee_user_id=grantee,
            task_id=task,
            resource_type=GrantResourceType.EVIDENCE,
            resource_id=uuid.uuid4(),
            reason="tenant A only",
        )
        s.commit()

    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ExecutionContext(org_id=org_b))
        rows = s.execute(select(AccessGrant)).scalars().all()
        assert all(g.org_id == org_b for g in rows)
        s.commit()
