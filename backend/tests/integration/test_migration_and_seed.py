"""The real Alembic path, not just metadata.create_all().

The other integration tests build the schema from SQLAlchemy metadata, which is
fast but proves nothing about the migration that will actually run in
production. This module runs `alembic upgrade head` against a fresh database,
asserts the security objects it is responsible for, exercises the constrained
application role against it, then runs the seed and a downgrade.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from app.db.session import escape_for_alembic_config
from app.models.enums import DepartmentCode

pytestmark = pytest.mark.integration

# Same value the shared conftest uses, and configurable for the same reason:
# roles are cluster-wide, so on a developer's own server this would otherwise
# overwrite the password their `.env` and every script depend on.
from tests.integration.conftest import APP_PASSWORD  # noqa: E402

INIT_ROLES = f"""
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='bidready_app') THEN
    CREATE ROLE bidready_app LOGIN PASSWORD '{APP_PASSWORD}'
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='bidready_auth') THEN
    CREATE ROLE bidready_auth NOLOGIN NOSUPERUSER BYPASSRLS;
  END IF;
END $$;
"""


@contextmanager
def _migration_database() -> Iterator[tuple[URL, URL]]:
    """Yield owner/app URLs for an isolated real database.

    The full-suite path uses the externally supplied PostgreSQL server and
    creates a throwaway database on it. The pgserver fallback remains useful
    for developer machines that do not provide BIDREADY_TEST_DATABASE_URL.
    """
    external = os.environ.get("BIDREADY_TEST_DATABASE_URL")
    if external:
        base_url = make_url(external.replace("postgresql://", "postgresql+psycopg://"))
        admin_url = base_url.set(database="postgres")
        database = f"bidready_migration_{uuid.uuid4().hex}"
        admin = create_engine(admin_url, future=True, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{database}"'))
        owner_url = base_url.set(database=database)
        app_url = owner_url.set(username="bidready_app", password=APP_PASSWORD)
        try:
            yield owner_url, app_url
        finally:
            if os.environ.get("BIDREADY_KEEP_TEST_DATABASES") != "1":
                with admin.connect() as conn:
                    conn.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = :database AND pid <> pg_backend_pid()"
                        ),
                        {"database": database},
                    )
                    conn.execute(text(f'DROP DATABASE "{database}"'))
            admin.dispose()
        return

    pgserver = pytest.importorskip("pgserver")
    with tempfile.TemporaryDirectory(prefix="bidready-mig-") as pgdata:
        server = pgserver.get_server(pgdata)
        owner_url = make_url(server.get_uri().replace("postgresql://", "postgresql+psycopg://"))
        app_url = owner_url.set(username="bidready_app", password=APP_PASSWORD)
        yield owner_url, app_url


@pytest.fixture(scope="module")
def migrated():
    """A database built by the real migration, plus both engines."""
    from alembic.config import Config

    from alembic import command

    with _migration_database() as (owner_url, app_url):
        owner = create_engine(owner_url, future=True)
        with owner.begin() as conn:
            conn.execute(text(INIT_ROLES))

        previous = os.environ.get("BIDREADY_DATABASE_URL")
        rendered_owner_url = owner_url.render_as_string(hide_password=False)
        os.environ["BIDREADY_DATABASE_URL"] = rendered_owner_url
        from app.core.config import get_settings

        get_settings.cache_clear()

        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", escape_for_alembic_config(rendered_owner_url))
        command.upgrade(cfg, "head")  # runs the full revision chain

        app_engine = create_engine(app_url, future=True)
        try:
            yield owner, app_engine, cfg
        finally:
            app_engine.dispose()
            owner.dispose()
            if previous is None:
                os.environ.pop("BIDREADY_DATABASE_URL", None)
            else:
                os.environ["BIDREADY_DATABASE_URL"] = previous
            get_settings.cache_clear()


def test_every_tenant_table_has_rls_enabled_and_forced(migrated):
    """FORCE matters: without it the table owner silently bypasses the policy."""
    from app.models import TENANT_TABLES

    owner, _, _ = migrated
    with owner.begin() as conn:
        protected = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname='public' AND c.relrowsecurity "
                    "AND c.relforcerowsecurity"
                )
            )
        }
    missing = set(TENANT_TABLES) - protected
    assert missing == set(), f"tables without ENABLE+FORCE RLS: {sorted(missing)}"


def test_auth_lookup_function_is_security_definer_and_correctly_owned(migrated):
    owner, _, _ = migrated
    with owner.begin() as conn:
        row = conn.execute(
            text(
                "SELECT prosecdef, pg_get_userbyid(proowner) AS owner "
                "FROM pg_proc WHERE proname = 'auth_lookup_user'"
            )
        ).first()
    assert row is not None, "the pre-tenant login lookup was not installed"
    assert row.prosecdef is True
    assert row.owner == "bidready_auth", (
        "the lookup must be owned by the dedicated bypass role, not by the app role"
    )


def test_audit_trigger_and_effective_grant_view_exist(migrated):
    owner, _, _ = migrated
    with owner.begin() as conn:
        assert conn.execute(
            text("SELECT 1 FROM pg_trigger WHERE tgname = 'audit_event_no_mutation'")
        ).first()
        assert conn.execute(
            text("SELECT 1 FROM pg_views WHERE viewname = 'access_grant_effective'")
        ).first()


def test_app_role_sees_nothing_without_a_tenant_and_its_own_rows_with_one(migrated):
    owner, app_engine, _ = migrated
    org = uuid.uuid4()
    with owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO organization (id,name,slug,retention_days,default_language,"
                "is_active,created_at) VALUES (:i,'Migrated',:s,2555,'en',true,now())"
            ),
            {"i": org, "s": f"mig-{org.hex[:8]}"},
        )
    with app_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(org)})
        conn.execute(
            text(
                "INSERT INTO app_user (id,org_id,email,display_name,is_active,mfa_enrolled,"
                "created_at) VALUES (gen_random_uuid(),:o,:e,'A',true,false,now())"
            ),
            {"o": str(org), "e": f"u-{org.hex[:6]}@example.com"},
        )

    with app_engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM app_user")).scalar() == 0

    with app_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(org)})
        assert conn.execute(text("SELECT count(*) FROM app_user")).scalar() == 1


def test_app_role_can_call_the_lookup_but_cannot_read_the_table_freely(migrated):
    """The escape hatch must be the function, not a blanket privilege."""
    owner, app_engine, _ = migrated
    org = uuid.uuid4()
    email = f"lookup-{org.hex[:6]}@example.com"
    with owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO organization (id,name,slug,retention_days,default_language,"
                "is_active,created_at) VALUES (:i,'L',:s,2555,'en',true,now())"
            ),
            {"i": org, "s": f"lk-{org.hex[:8]}"},
        )
        conn.execute(
            text(
                "INSERT INTO app_user (id,org_id,email,display_name,is_active,mfa_enrolled,"
                "created_at) VALUES (gen_random_uuid(),:o,:e,'L',true,false,now())"
            ),
            {"o": org, "e": email},
        )
    with app_engine.begin() as conn:
        row = conn.execute(
            text("SELECT user_id, org_id FROM auth_lookup_user(:e)"), {"e": email}
        ).first()
        assert row is not None and row.org_id == org
        # ... yet a direct read with no tenant still yields nothing.
        assert (
            conn.execute(
                text("SELECT count(*) FROM app_user WHERE email = :e"), {"e": email}
            ).scalar()
            == 0
        )


def test_seed_is_idempotent_against_the_migrated_schema(migrated):
    owner, _, _ = migrated
    from sqlalchemy.orm import sessionmaker

    import app.db.session as session_module

    factory = sessionmaker(bind=owner, autoflush=False, expire_on_commit=False, future=True)
    original = session_module.get_session_factory
    session_module.get_session_factory = lambda: factory
    try:
        from app.db.seed import seed

        first = seed()
        second = seed()
        assert first == second, "re-seeding must not create a second organization"
    finally:
        session_module.get_session_factory = original


# --- SECURITY DEFINER hardening --------------------------------------------
# A SECURITY DEFINER function runs with its owner's privileges. Three properties
# make that safe, and each is asserted rather than assumed.


def test_auth_lookup_has_an_explicitly_fixed_search_path(migrated):
    """Without a fixed search_path, a caller could shadow `app_user` with a
    table of their own in a schema earlier on the path, and the function would
    read that instead -- while running as a BYPASSRLS role."""
    owner, _, _ = migrated
    with owner.begin() as conn:
        config = conn.execute(
            text("SELECT proconfig FROM pg_proc WHERE proname = 'auth_lookup_user'")
        ).scalar()
    assert config is not None, "auth_lookup_user has no search_path pinned"
    settings = {c.split("=", 1)[0]: c.split("=", 1)[1] for c in config}
    assert "search_path" in settings
    assert settings["search_path"] == "public, pg_temp", settings["search_path"]


def test_public_execute_is_revoked_on_auth_lookup(migrated):
    """PUBLIC must not be able to call the lookup: every database role is a
    member of PUBLIC, so a grant there is a grant to everyone."""
    owner, _, _ = migrated
    with owner.begin() as conn:
        granted = conn.execute(
            text("SELECT has_function_privilege('public', 'auth_lookup_user(text)', 'EXECUTE')")
        ).scalar()
    assert granted is False, "PUBLIC still holds EXECUTE on auth_lookup_user"


def test_only_the_application_role_holds_execute(migrated):
    owner, _, _ = migrated
    with owner.begin() as conn:
        acl = conn.execute(
            text("SELECT coalesce(proacl::text, '') FROM pg_proc WHERE proname='auth_lookup_user'")
        ).scalar()
        app_can = conn.execute(
            text(
                "SELECT has_function_privilege('bidready_app', 'auth_lookup_user(text)', 'EXECUTE')"
            )
        ).scalar()
    assert app_can is True, "the application role cannot call the login lookup"
    # The ACL should mention only the owner and the application role.
    assert "bidready_app=X" in acl, acl
    assert "=X/" not in acl.replace("bidready_app=X", "").replace("bidready_auth=X", ""), (
        f"an unexpected role holds EXECUTE: {acl}"
    )


def test_lookup_function_is_owned_by_a_role_that_cannot_log_in(migrated):
    """The bypass role is reachable only through the function, never directly."""
    owner, _, _ = migrated
    with owner.begin() as conn:
        row = conn.execute(
            text(
                "SELECT rolcanlogin, rolbypassrls, rolsuper FROM pg_roles "
                "WHERE rolname = 'bidready_auth'"
            )
        ).first()
    assert row.rolcanlogin is False, "the bypass role must not be able to log in"
    assert row.rolbypassrls is True
    assert row.rolsuper is False, "the bypass role must not be a superuser"


def test_application_role_cannot_bypass_rls_or_log_in_as_the_bypass_role(migrated):
    owner, _, _ = migrated
    with owner.begin() as conn:
        row = conn.execute(
            text("SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = 'bidready_app'")
        ).first()
    assert row.rolbypassrls is False
    assert row.rolsuper is False


def test_global_email_uniqueness_survives_the_migration(migrated):
    owner, _, _ = migrated
    with owner.begin() as conn:
        idx = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_app_user_email_lower'")
        ).scalar()
        stale = conn.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = 'uq_app_user_org_email'")
        ).first()
    assert idx is not None, "the global email index is missing"
    # PostgreSQL renders the expression with the column's cast, e.g.
    # lower((email)::text) for a varchar column.
    assert "UNIQUE INDEX" in idx, idx
    assert "lower(" in idx and "email" in idx, idx
    assert " ON public.app_user " in idx, idx
    assert stale is None, "the redundant per-organization email constraint was left behind"


# NOTE: this must remain the LAST test in the module -- it drops the schema the
# module-scoped `migrated` fixture provides to everything above.
def test_downgrade_removes_everything(migrated):
    from alembic import command

    owner, _, cfg = migrated
    command.downgrade(cfg, "base")
    with owner.begin() as conn:
        remaining = {
            r[0]
            for r in conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
        }
    assert remaining == {"alembic_version"}, f"downgrade left {sorted(remaining)}"


def test_an_empty_department_is_removed_and_a_staffed_one_is_kept(app_sessionmaker, seeded_orgs):
    """A department exists because a person works in it."""

    from app.core.context import ExecutionContext
    from app.db.seed import _remove_if_unused
    from app.db.session import apply_tenant_context
    from app.models import Department, UserDepartment

    org = seeded_orgs[0]
    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ExecutionContext(org_id=org))
        from app.models import User

        person = User(org_id=org, email="dept-test@example.com", display_name="Dept Test")
        staffed = Department(org_id=org, code=DepartmentCode.FINANCE, name="Finance")
        s.add_all([person, staffed])
        s.flush()
        s.add(UserDepartment(org_id=org, user_id=person.id, department_id=staffed.id))
        s.flush()
        staffed_id = staffed.id
        assert _remove_if_unused(s, staffed) is False
        assert s.get(Department, staffed_id) is not None

        empty = Department(org_id=org, code=DepartmentCode.PRODUCT, name="Product")
        s.add(empty)
        s.flush()
        assert _remove_if_unused(s, empty) is True
        s.rollback()
