"""The tenant-isolation guarantee, exercised against a real database.

These are the highest-value tests in the foundation: a defect here is a
cross-tenant disclosure, so each mechanism is verified independently rather than
trusting that the others hold.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.models import AuditEvent, Department, TaskParticipant, User
from app.models.enums import DepartmentCode, ParticipantRole
from tests.integration.conftest import make_task

pytestmark = pytest.mark.integration


def _ctx(org_id: uuid.UUID, user_id: uuid.UUID | None = None) -> ExecutionContext:
    return ExecutionContext(org_id=org_id, actor_user_id=user_id)


def _make_user(session, org_id: uuid.UUID, email: str) -> User:
    user = User(org_id=org_id, email=email, display_name=email, password_hash=None)
    session.add(user)
    session.flush()
    return user


# --- RLS -------------------------------------------------------------------


def test_rls_hides_other_tenants_rows(app_sessionmaker, seeded_orgs):
    org_a, org_b = seeded_orgs
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, _ctx(org_a))
        _make_user(s, org_a, f"a-{uuid.uuid4().hex[:8]}@a.test")
        s.commit()

    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, _ctx(org_b))
        rows = s.execute(select(User)).scalars().all()
        assert all(u.org_id == org_b for u in rows)
        assert not any(u.org_id == org_a for u in rows)
        s.commit()


def test_rls_blocks_writing_a_row_into_another_tenant(app_sessionmaker, seeded_orgs):
    """WITH CHECK must stop a forged org_id, not merely hide reads."""
    org_a, org_b = seeded_orgs
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, _ctx(org_a))
        s.add(
            User(org_id=org_b, email=f"forged-{uuid.uuid4().hex[:6]}@x.test", display_name="forged")
        )
        with pytest.raises(DBAPIError):
            s.flush()
        s.rollback()


def test_no_tenant_context_matches_no_rows(app_sessionmaker, seeded_orgs):
    """A query with no tenant fails closed rather than returning everything."""
    org_a, _ = seeded_orgs
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, _ctx(org_a))
        _make_user(s, org_a, f"visible-{uuid.uuid4().hex[:8]}@a.test")
        s.commit()

    with app_sessionmaker() as s:
        s.begin()  # deliberately no apply_tenant_context
        assert s.execute(select(User)).scalars().all() == []
        s.commit()


# --- SET LOCAL / pooled connections ----------------------------------------


def test_tenant_context_does_not_survive_the_transaction(app_sessionmaker, seeded_orgs):
    """Correction 11: SET LOCAL must not leak into the next transaction."""
    org_a, _ = seeded_orgs
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, _ctx(org_a))
        inside = s.execute(text("SELECT current_setting('app.current_org', true)")).scalar()
        assert inside == str(org_a)
        s.commit()

        # Same Session object, same pooled connection, new transaction.
        s.begin()
        after = s.execute(text("SELECT current_setting('app.current_org', true)")).scalar()
        assert after in (None, ""), f"tenant leaked across transactions: {after!r}"
        s.commit()


def test_second_request_on_a_pooled_connection_sees_only_its_own_tenant(
    app_sessionmaker, seeded_orgs
):
    org_a, org_b = seeded_orgs
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, _ctx(org_a))
        _make_user(s, org_a, f"pool-a-{uuid.uuid4().hex[:8]}@a.test")
        s.commit()

    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, _ctx(org_b))
        rows = s.execute(select(User)).scalars().all()
        assert all(u.org_id == org_b for u in rows)
        s.commit()


# --- composite tenant-aware foreign keys -----------------------------------


def test_join_table_cannot_span_tenants(app_sessionmaker, owner_engine, seeded_orgs):
    """Correction 1: the database refuses a cross-tenant relationship.

    Written as the OWNER so RLS is not what stops it -- this proves the
    composite foreign key alone is sufficient.
    """
    from sqlalchemy.orm import sessionmaker as sm

    org_a, org_b = seeded_orgs
    SessionOwner = sm(bind=owner_engine, future=True)
    with SessionOwner() as s:
        dept = Department(org_id=org_a, code=DepartmentCode.SECURITY, name="Sec")
        s.add(dept)
        s.flush()
        task = make_task(s, org_a, title="Task in A", department=dept)
        user_b = User(
            org_id=org_b, email=f"b-{uuid.uuid4().hex[:8]}@b.test", display_name="User in B"
        )
        s.add(user_b)
        s.flush()

        # Tenant A's task, tenant B's user, in a row that must carry one org_id.
        s.add(
            TaskParticipant(
                org_id=org_a,
                task_id=task.id,
                user_id=user_b.id,
                participant_role=ParticipantRole.CONTRIBUTOR,
                added_by=task.id,
            )
        )
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()


def test_same_tenant_relationship_is_accepted(app_sessionmaker, seeded_orgs):
    org_a, _ = seeded_orgs
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, _ctx(org_a))
        user = _make_user(s, org_a, f"ok-{uuid.uuid4().hex[:8]}@a.test")
        task = make_task(s, org_a, title="Task")
        s.add(
            TaskParticipant(
                org_id=org_a,
                task_id=task.id,
                user_id=user.id,
                participant_role=ParticipantRole.REVIEWER,
                added_by=user.id,
            )
        )
        s.flush()
        s.commit()


# --- audit is append-only ---------------------------------------------------


def test_audit_events_cannot_be_updated_or_deleted(app_sessionmaker, seeded_orgs):
    org_a, _ = seeded_orgs
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, _ctx(org_a))
        event = AuditEvent(
            org_id=org_a, actor_kind="SYSTEM", action="test.event", entity_type="test"
        )
        s.add(event)
        s.flush()
        event_id = event.id
        s.commit()

    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, _ctx(org_a))
        with pytest.raises(DBAPIError):
            s.execute(
                text("UPDATE audit_event SET action = 'tampered' WHERE id = :i"),
                {"i": event_id},
            )
        s.rollback()

    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, _ctx(org_a))
        with pytest.raises(DBAPIError):
            s.execute(text("DELETE FROM audit_event WHERE id = :i"), {"i": event_id})
        s.rollback()
