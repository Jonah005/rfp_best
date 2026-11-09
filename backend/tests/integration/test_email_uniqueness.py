"""Globally unique email: the precondition auth_lookup_user() depends on.

The login lookup resolves a tenant FROM an email with no tenant context. That is
only well defined if an address identifies at most one user across the entire
deployment.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.context import ExecutionContext
from app.core.errors import ValidationError
from app.db.session import apply_tenant_context
from app.models import User
from app.services import identity

pytestmark = pytest.mark.integration


def test_the_same_email_cannot_exist_in_two_organizations(app_sessionmaker, seeded_orgs):
    org_a, org_b = seeded_orgs
    email = f"shared-{uuid.uuid4().hex[:8]}@example.com"

    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ExecutionContext(org_id=org_a))
        s.add(User(org_id=org_a, email=email, display_name="In A"))
        s.flush()
        s.commit()

    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ExecutionContext(org_id=org_b))
        s.add(User(org_id=org_b, email=email, display_name="In B"))
        with pytest.raises(IntegrityError) as exc:
            s.flush()
        assert "uq_app_user_email_lower" in str(exc.value.orig)
        s.rollback()


def test_casing_cannot_be_used_to_duplicate_an_address(app_sessionmaker, seeded_orgs):
    """The lookup compares case-insensitively; the constraint must agree."""
    org_a, org_b = seeded_orgs
    local = f"CaseTest-{uuid.uuid4().hex[:8]}"

    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ExecutionContext(org_id=org_a))
        s.add(User(org_id=org_a, email=f"{local.lower()}@example.com", display_name="lower"))
        s.flush()
        s.commit()

    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ExecutionContext(org_id=org_b))
        s.add(User(org_id=org_b, email=f"{local.upper()}@EXAMPLE.com", display_name="upper"))
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()


def test_create_user_reports_a_clean_error_without_naming_the_other_tenant(
    app_sessionmaker, seeded_orgs
):
    """A duplicate must not disclose which organization already holds it."""
    org_a, org_b = seeded_orgs
    email = f"dup-{uuid.uuid4().hex[:8]}@example.com"

    with app_sessionmaker() as s:
        s.begin()
        ctx_a = ExecutionContext(org_id=org_a)
        apply_tenant_context(s, ctx_a)
        identity.create_user(s, ctx_a, email=email, display_name="First")
        s.commit()

    with app_sessionmaker() as s:
        s.begin()
        ctx_b = ExecutionContext(org_id=org_b)
        apply_tenant_context(s, ctx_b)
        with pytest.raises(ValidationError) as exc:
            identity.create_user(s, ctx_b, email=email, display_name="Second")
        message = str(exc.value)
        assert "already registered" in message
        assert str(org_a) not in message
        assert "organization" not in message.lower()
        s.rollback()


def test_auth_lookup_returns_exactly_one_row_for_a_known_address(app_sessionmaker, seeded_orgs):
    org_a, _ = seeded_orgs
    email = f"unique-{uuid.uuid4().hex[:8]}@example.com"
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org_a)
        apply_tenant_context(s, ctx)
        identity.create_user(s, ctx, email=email, display_name="Only")
        s.commit()

    with app_sessionmaker() as s:
        s.begin()
        rows = s.execute(
            text("SELECT user_id, org_id FROM auth_lookup_user(:e)"), {"e": email}
        ).all()
        assert len(rows) == 1
        assert rows[0].org_id == org_a
        s.commit()


def test_auth_lookup_is_case_insensitive(app_sessionmaker, seeded_orgs):
    org_a, _ = seeded_orgs
    local = f"MixedCase-{uuid.uuid4().hex[:8]}"
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org_a)
        apply_tenant_context(s, ctx)
        identity.create_user(s, ctx, email=f"{local}@Example.com", display_name="Mixed")
        s.commit()

    with app_sessionmaker() as s:
        s.begin()
        row = s.execute(
            text("SELECT org_id FROM auth_lookup_user(:e)"),
            {"e": f"{local.upper()}@EXAMPLE.COM"},
        ).first()
        assert row is not None and row.org_id == org_a
        s.commit()
