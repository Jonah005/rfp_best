"""Integration fixtures backed by a real PostgreSQL instance.

RLS cannot be tested as a superuser: PostgreSQL exempts superusers and
BYPASSRLS roles from every policy. These fixtures therefore mirror production
exactly -- migrations run as the owner, and the application connects as a
separate non-superuser role.
"""

from __future__ import annotations

import os
import pathlib
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

#: The application role, by the name the MIGRATIONS grant to. Not configurable:
#: `test_migration_and_seed.py` asserts the real grants against this literal, so
#: renaming it here would mean the migration tests no longer test the migrations.
APP_ROLE = "bidready_app"

#: The password the suite gives that role -- and the one hazard of running
#: against a shared server.
#:
#: PostgreSQL roles are CLUSTER-wide, not per-database. On a throwaway server
#: that is invisible. Pointed at a developer's own PostgreSQL, the suite
#: re-asserts this password on `bidready_app`, and if it differs from the one in
#: `.env` then every script and `run_local.py` stops authenticating the moment
#: the tests are run -- in a different database, which is where nobody would
#: think to look.
#:
#: So it is configurable, and a developer sharing a server sets it to the
#: password their role already has. The ALTER then writes the value that is
#: already there, and dev is untouched:
#:
#:     set BIDREADY_TEST_APP_PASSWORD=bidready-local
APP_PASSWORD = os.environ.get("BIDREADY_TEST_APP_PASSWORD", "bidready_test")

pytestmark = pytest.mark.integration


#: Databases these tests must never be pointed at.
#:
#: The suite creates a role, DROPS AND REBUILDS the public schema of the
#: database it is given (see `_reset_schema`), and writes organizations, users,
#: bids and requirements. Against the working database that would destroy the
#: live corpus; the reset is separately gated on the name containing "test".
#:
#: `bidready` is the working database named in HANDOVER.md and in every script's
#: default URL, so it is the one somebody reaches for.
PROTECTED_DATABASES = frozenset({"bidready"})


def _database_name(uri: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(uri).path or "").lstrip("/").split("?")[0]


@pytest.fixture(scope="session")
def pg_uri() -> Iterator[str]:
    external = os.environ.get("BIDREADY_TEST_DATABASE_URL")
    if external:
        name = _database_name(external)
        if name in PROTECTED_DATABASES and not os.environ.get("BIDREADY_TEST_ALLOW_LIVE_DB"):
            raise pytest.UsageError(
                f"BIDREADY_TEST_DATABASE_URL points at {name!r}, which is the working "
                "database.\nThese tests write organizations, users and bids; running them "
                "there would leave\ntest rows among the live corpus. Create a separate "
                "database:\n"
                "\n  createdb bidready_test\n"
                "  set BIDREADY_TEST_DATABASE_URL=postgresql://...@localhost:5432/bidready_test\n"
                "\nSet BIDREADY_TEST_ALLOW_LIVE_DB=1 only if you genuinely mean it."
            )
        yield external
        return
    pgserver = pytest.importorskip("pgserver")
    import tempfile

    with tempfile.TemporaryDirectory(prefix="bidready-pg-") as pgdata:
        server = pgserver.get_server(pgdata)
        yield server.get_uri()


@pytest.fixture(scope="session")
def owner_engine(pg_uri: str):
    engine = create_engine(pg_uri.replace("postgresql://", "postgresql+psycopg://"), future=True)
    yield engine
    engine.dispose()


def _reset_schema(pg_uri: str, owner_engine) -> None:
    """Start every run from an empty schema, as the throwaway server always did.

    The setup below is not idempotent -- CREATE POLICY, CREATE TRIGGER and ADD
    CONSTRAINT all refuse to run twice -- and it never had to be, because
    pgserver built a fresh cluster in a temporary directory on every run. Pointed
    at a persistent `bidready_test`, the first run passed and the second produced
    271 errors, every one `DuplicateObject` from the leftovers of the first.

    Making each statement conditional would fix today's list and break on the
    next statement someone adds. Resetting the schema fixes the pattern: the
    suite owns this database, so it starts from nothing, exactly as before.

    Dropping a schema is the one destructive thing in this harness, so it is
    gated on the NAME, not on a flag: the database must say it is for tests.
    `BIDREADY_TEST_ALLOW_LIVE_DB` deliberately does not unlock it -- that flag
    means "I accept test rows in my data", never "drop my data".
    """
    name = _database_name(pg_uri)
    if "test" not in name.lower():
        raise pytest.UsageError(
            f"Refusing to reset {name!r}: the integration suite drops and rebuilds the "
            "schema of the database it runs against, and only does so for a database "
            "whose name contains 'test'.\nCreate one with: python scripts/create_test_database.py"
        )
    with owner_engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        # A freshly created schema is NOT the stock `public`: the stock one grants
        # USAGE to every role, a new one grants it to nobody but its owner. Left
        # out, `auth_lookup_user` -- owned by `bidready_auth` -- could not see
        # `app_user`, and every login failed with "relation does not exist".
        # This restores exactly the default (USAGE, not CREATE, as on PG15+).
        conn.execute(text("GRANT USAGE ON SCHEMA public TO PUBLIC"))


@pytest.fixture(scope="session")
def app_engine(pg_uri: str, owner_engine):
    """Schema created by the owner; application connects as a constrained role."""
    if os.environ.get("BIDREADY_TEST_DATABASE_URL"):
        _reset_schema(pg_uri, owner_engine)
    with owner_engine.begin() as conn:
        # CREATE the role if it is absent, and ALTER it either way.
        #
        # The create was conditional and the password was not set afterwards, so
        # on a server where `bidready_app` ALREADY EXISTS -- which is every
        # developer machine that has ever run this application -- the branch was
        # skipped, the role kept whatever password it had, and the app
        # connection below failed to authenticate with no hint as to why. A
        # throwaway server never hit it because the role never existed.
        #
        # The attributes are re-asserted too. They are the subject of the
        # tenant-isolation tests: a role that had drifted to BYPASSRLS would
        # make every one of them pass while proving nothing.
        conn.execute(
            text(
                f"""
                DO $$ BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                    CREATE ROLE {APP_ROLE} LOGIN;
                  END IF;
                END $$;
                """
            )
        )
        conn.execute(
            text(
                f"ALTER ROLE {APP_ROLE} WITH LOGIN PASSWORD '{APP_PASSWORD}' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
            )
        )

    # pgvector is optional, because the application already treats it as
    # optional.
    #
    # `settings.vector_backend` chooses between a pgvector column and a portable
    # `real[]`, and `scripts/_vector_backend.py` exists solely to detect which a
    # live database uses. The test harness alone assumed pgvector, so a suite
    # pointed at an ordinary PostgreSQL died on `CREATE EXTENSION vector` --
    # including the developer database this project actually runs against, which
    # reports "portable" on every seed.
    #
    # Installing the extension needs superuser on most builds and a compiled
    # extension on Windows. Neither is a reasonable price for running the tests,
    # and testing the portable path is MORE representative for anyone whose own
    # database uses it.
    with owner_engine.begin() as conn:
        try:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        except Exception as exc:  # noqa: BLE001 - absence is a supported state
            # Not swallowed silently: the reason is printed, because "the tests
            # are running on the portable backend" is something a person reading
            # a failure needs to know, and because a rule that produces no
            # signal is the failure mode this project keeps finding.
            print(f"\npgvector unavailable ({type(exc).__name__}); using the portable backend.")

    # The BACKEND is chosen in tests/conftest.py, at import time, and not here.
    #
    # It was here first, and it did not work: pytest imports every test module
    # during collection, each of those imports `app.models`, and the embedding
    # column type is resolved from `settings.vector_backend` at import. By the
    # time any fixture runs, the schema has already been decided -- so setting
    # the environment variable in this function produced `type "vector" does not
    # exist` on a server without the extension, which is the failure it was
    # written to prevent.
    #
    # `scripts/_vector_backend.py` documents this exact trap for scripts. The
    # test harness needed the same treatment, one import earlier.
    from app.models import TENANT_TABLES, Base

    Base.metadata.create_all(bind=owner_engine)

    with owner_engine.begin() as conn:
        # Mirrors migration 0003: evidence_link's target is polymorphic, so no
        # composite foreign key can express the tenant constraint.
        conn.execute(
            text(
                """
                CREATE OR REPLACE FUNCTION evidence_link_target_same_tenant()
                RETURNS trigger AS $$
                DECLARE target_org uuid; target_table text;
                BEGIN
                  target_table := CASE NEW.target_type
                    WHEN 'KNOWLEDGE_FACT'     THEN 'knowledge_fact'
                    WHEN 'KNOWLEDGE_RELATION' THEN 'knowledge_relation'
                    WHEN 'KNOWLEDGE_ENTITY'   THEN 'knowledge_entity'
                    WHEN 'ARTIFACT_STATEMENT' THEN 'artifact_statement'
                    ELSE NULL END;
                  IF target_table IS NULL THEN
                    RAISE EXCEPTION 'evidence_link target_type % is not yet supported',
                      NEW.target_type;
                  END IF;
                  EXECUTE format('SELECT org_id FROM %I WHERE id = $1', target_table)
                    INTO target_org USING NEW.target_id;
                  IF target_org IS NULL THEN
                    RAISE EXCEPTION 'evidence_link target %/% does not exist',
                      NEW.target_type, NEW.target_id;
                  END IF;
                  IF target_org <> NEW.org_id THEN
                    RAISE EXCEPTION 'evidence_link would cross tenants: link org %, target org %',
                      NEW.org_id, target_org;
                  END IF;
                  RETURN NEW;
                END; $$ LANGUAGE plpgsql;
                """
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER evidence_link_tenant_guard "
                "BEFORE INSERT OR UPDATE ON evidence_link "
                "FOR EACH ROW EXECUTE FUNCTION evidence_link_target_same_tenant()"
            )
        )

        # Mirrors migration 0004. `document.bid_id` could not carry a foreign
        # key in Phase F because `bid` did not exist yet; the composite key and
        # the side/bid CHECK are what make the tender/company authority boundary
        # a schema guarantee rather than a service convention.
        conn.execute(
            text(
                "ALTER TABLE document ADD CONSTRAINT fk_document_bid_org "
                "FOREIGN KEY (bid_id, org_id) REFERENCES bid (id, org_id)"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE document ADD CONSTRAINT ck_document_side_bid CHECK ("
                "(side = 'TENDER' AND bid_id IS NOT NULL) OR "
                "(side = 'COMPANY' AND bid_id IS NULL))"
            )
        )

    with owner_engine.begin() as conn:
        # Mirrors migration 0006. Phase E's access_grant could not point a
        # composite key at `task`; Phase I makes a cross-tenant grant
        # unrepresentable rather than merely unlikely.
        conn.execute(
            text(
                "ALTER TABLE access_grant ADD CONSTRAINT fk_access_grant_task_org "
                "FOREIGN KEY (task_id, org_id) REFERENCES task (id, org_id)"
            )
        )

    with owner_engine.begin() as conn:
        for table in TENANT_TABLES:
            conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            conn.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
            conn.execute(
                text(
                    f"""
                    CREATE POLICY tenant_isolation ON {table}
                      USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
                      WITH CHECK
                        (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
                    """
                )
            )
        conn.execute(
            text(
                """
                CREATE OR REPLACE FUNCTION audit_event_is_append_only()
                RETURNS trigger AS $$
                BEGIN
                  RAISE EXCEPTION 'audit_event is append-only (attempted %)', TG_OP;
                END;
                $$ LANGUAGE plpgsql
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TRIGGER audit_event_no_mutation
                BEFORE UPDATE OR DELETE ON audit_event
                FOR EACH ROW EXECUTE FUNCTION audit_event_is_append_only()
                """
            )
        )
        # Mirrors migration 0008. A NON_WAIVABLE finding has no exception path,
        # including through raw SQL -- the service guard is the polite refusal,
        # this trigger is the one that still holds when the service is wrong.
        conn.execute(
            text(
                """
                CREATE OR REPLACE FUNCTION release_exception_is_waivable()
                RETURNS trigger AS $$
                DECLARE finding_waivability finding_waivability;
                BEGIN
                  SELECT waivability INTO finding_waivability
                  FROM validation_finding
                  WHERE id = NEW.finding_id AND org_id = NEW.org_id;
                  IF finding_waivability IS DISTINCT FROM 'WAIVABLE'::finding_waivability THEN
                    RAISE EXCEPTION 'release exception requires a WAIVABLE finding';
                  END IF;
                  RETURN NEW;
                END; $$ LANGUAGE plpgsql;
                """
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER release_exception_waivable_guard "
                "BEFORE INSERT OR UPDATE ON release_exception "
                "FOR EACH ROW EXECUTE FUNCTION release_exception_is_waivable()"
            )
        )

        # The pre-tenant login lookup, mirroring the production migration.
        conn.execute(
            text(
                """
                DO $$ BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bidready_auth') THEN
                    CREATE ROLE bidready_auth NOLOGIN NOSUPERUSER BYPASSRLS;
                  END IF;
                END $$;
                """
            )
        )
        sql_path = pathlib.Path(__file__).resolve().parents[2] / "app" / "db" / "auth_lookup.sql"
        conn.execute(text(sql_path.read_text(encoding="utf-8")))
        conn.execute(text("GRANT SELECT ON app_user TO bidready_auth"))
        conn.execute(text("ALTER FUNCTION auth_lookup_user(text) OWNER TO bidready_auth"))
        conn.execute(text(f"GRANT EXECUTE ON FUNCTION auth_lookup_user(text) TO {APP_ROLE}"))

        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
        conn.execute(
            text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
            )
        )
        conn.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}"))
        conn.execute(text(f"REVOKE UPDATE, DELETE ON audit_event FROM {APP_ROLE}"))

    # Connect as the constrained role to the same database. ``make_url``
    # handles both pgserver's Unix-socket URI and a normal TCP URL supplied by
    # BIDREADY_TEST_DATABASE_URL.
    owner_url = make_url(pg_uri.replace("postgresql://", "postgresql+psycopg://"))
    app_uri = owner_url.set(username=APP_ROLE, password=APP_PASSWORD)
    engine = create_engine(app_uri, future=True, poolclass=None)
    yield engine
    engine.dispose()


@pytest.fixture
def app_sessionmaker(app_engine) -> sessionmaker[Session]:
    return sessionmaker(bind=app_engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture(scope="session")
def seeded_orgs(app_engine, owner_engine) -> tuple[uuid.UUID, uuid.UUID]:
    """Two organizations, created as the owner so RLS does not block setup.

    ``app_engine`` is depended on purely for ordering: it is the fixture that
    builds the schema, and without naming it here pytest is free to seed the
    organizations first, which fails on whichever module happens to run first.
    """
    from app.models import Organization

    a, b = uuid.uuid4(), uuid.uuid4()
    SessionOwner = sessionmaker(bind=owner_engine, future=True)
    with SessionOwner() as s:
        s.add(Organization(id=a, name="Tenant A", slug=f"tenant-a-{a.hex[:8]}"))
        s.add(Organization(id=b, name="Tenant B", slug=f"tenant-b-{b.hex[:8]}"))
        s.commit()
    return a, b


PASSWORD = "Integration-Test-Pw-2026"


@pytest.fixture(scope="session")
def seeded_users(app_engine, seeded_orgs):
    """A bid manager and an auditor in each tenant, shared across API modules."""
    from app.core.context import ExecutionContext
    from app.core.permissions import RoleCode
    from app.db.session import apply_tenant_context
    from app.models import Department
    from app.models.enums import DepartmentCode
    from app.services import identity

    factory = sessionmaker(bind=app_engine, autoflush=False, future=True)
    org_a, org_b = seeded_orgs
    made: dict = {}
    with factory() as s:
        s.begin()
        identity.sync_permission_catalogue(s)
        for org in (org_a, org_b):
            ctx = ExecutionContext(org_id=org)
            apply_tenant_context(s, ctx)
            identity.provision_roles(s, org)
            dept = s.execute(
                select(Department).where(
                    Department.org_id == org, Department.code == DepartmentCode.BID_MANAGEMENT
                )
            ).scalar_one_or_none()
            if dept is None:
                dept = Department(
                    org_id=org, code=DepartmentCode.BID_MANAGEMENT, name="Bid Management"
                )
                s.add(dept)
                s.flush()
            suffix = uuid.uuid4().hex[:6]
            domain = f"t{org.hex[:4]}.example.com"
            manager = identity.create_user(
                s,
                ctx,
                email=f"mgr-{suffix}@{domain}",
                display_name="Manager",
                password=PASSWORD,
                role_codes_to_assign=[RoleCode.BID_MANAGER.value],
                department_ids_to_assign=[dept.id],
            )
            auditor = identity.create_user(
                s,
                ctx,
                email=f"aud-{suffix}@{domain}",
                display_name="Auditor",
                password=PASSWORD,
                role_codes_to_assign=[RoleCode.AUDITOR.value],
            )
            # Release is maker-checker by RBAC as well as by service guard: a
            # BID_MANAGER may submit but never approve, so the API tests need a
            # second person who holds release:approve.
            approver = identity.create_user(
                s,
                ctx,
                email=f"exec-{suffix}@{domain}",
                display_name="Approver",
                password=PASSWORD,
                role_codes_to_assign=[RoleCode.EXECUTIVE_APPROVER.value],
                department_ids_to_assign=[dept.id],
            )
            # The governed-learning queue belongs to knowledge administrators,
            # not to whoever recorded the outcome.
            knowledge_admin = identity.create_user(
                s,
                ctx,
                email=f"know-{suffix}@{domain}",
                display_name="Knowledge Admin",
                password=PASSWORD,
                role_codes_to_assign=[RoleCode.KNOWLEDGE_ADMIN.value],
            )
            org_admin = identity.create_user(
                s,
                ctx,
                email=f"adm-{suffix}@{domain}",
                display_name="Org Admin",
                password=PASSWORD,
                role_codes_to_assign=[RoleCode.ORG_ADMIN.value],
            )
            made[org] = {
                "admin": org_admin.email,
                "knowledge_admin": knowledge_admin.email,
                "manager": manager.email,
                "auditor": auditor.email,
                "approver": approver.email,
            }
        s.commit()
    return made


@pytest.fixture
def api(app_engine, monkeypatch):
    """A TestClient whose dependencies resolve against the test database."""
    from fastapi.testclient import TestClient

    import app.api.deps as deps_module
    import app.db.session as session_module
    from app.main import create_app

    factory = sessionmaker(bind=app_engine, autoflush=False, expire_on_commit=False, future=True)
    monkeypatch.setattr(session_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(session_module, "get_engine", lambda: app_engine)
    monkeypatch.setattr(deps_module, "get_session_factory", lambda: factory)
    return TestClient(create_app())


@pytest.fixture
def users(seeded_users):
    return seeded_users


def api_login(client, email: str) -> str:
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def make_task(
    session,
    org_id,
    *,
    code: str | None = None,
    title: str = "Task",
    task_id=None,
    owner_user_id=None,
    department=None,
):
    """A real, minimal Task with the bid and department its foreign keys need.

    Phase E carried a stub `task` row so `access_grant` and `task_participant`
    had a parent to point at. Phase I replaced it with the real model, so tests
    that only need "a task that exists" build one here rather than each
    reinventing the bid and department it now requires.
    """
    from sqlalchemy import select as _select

    from app.core.context import ExecutionContext
    from app.models import Department, Task
    from app.models.enums import DepartmentCode, TaskPriority, TaskState
    from app.services import bids as bids_service

    ctx = ExecutionContext(org_id=org_id)
    dept = None
    if department is not None:
        dept = department
    else:
        dept = (
            session.execute(_select(Department).where(Department.org_id == org_id))
            .scalars()
            .first()
        )
    if dept is None:
        dept = Department(org_id=org_id, code=DepartmentCode.BID_MANAGEMENT, name="Bid Management")
        session.add(dept)
        session.flush()

    opportunity = bids_service.create_opportunity(
        session, ctx, customer_name="Test Customer", tender_name=f"T {uuid.uuid4().hex[:8]}"
    )
    bid = bids_service.promote_to_bid(session, ctx, opportunity_id=opportunity.id)

    task = Task(
        org_id=org_id,
        id=task_id or uuid.uuid4(),
        bid_id=bid.id,
        code=code or f"T-{uuid.uuid4().hex[:6]}",
        title=title,
        department_id=dept.id,
        owner_user_id=owner_user_id,
        priority=TaskPriority.NORMAL,
        state=TaskState.NOT_STARTED,
        retrieval_scope={},
    )
    session.add(task)
    session.flush()
    return task


# --- a task answers a requirement -----------------------------------------------------------
def _any_requirement(session, ctx, bid_id):
    """One live requirement of the bid, made if the bid has none."""
    import uuid as _uuid

    from sqlalchemy import select as _select

    from app.gateway.embeddings import HashingEmbeddingProvider
    from app.ingestion.safety import EicarAwareScanner
    from app.models import Bid, Requirement
    from tests.integration.test_assessment_readiness import ISO_TEXT, _company_version, _requirement

    existing = (
        session.execute(
            _select(Requirement).where(
                Requirement.org_id == ctx.org_id, Requirement.bid_id == bid_id
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing
    from app.services.storage import InMemoryStorage

    bid = session.get(Bid, bid_id)
    version = _company_version(
        session,
        ctx,
        (ctx.org_id, InMemoryStorage(), EicarAwareScanner(), HashingEmbeddingProvider()),
    )
    return _requirement(
        session,
        ctx,
        bid=bid,
        doc_version=version,
        code=f"FX-{_uuid.uuid4().hex[:4]}",
        text=ISO_TEXT,
    )


@pytest.fixture(autouse=True)
def _tasks_answer_a_requirement(request, monkeypatch):
    """The task service refuses a task that answers no requirement of its bid (a rule added
    after most of these scenes were written). A scene that only needs "a task that exists"
    gets the bid's first requirement attached here, so the rule itself is still enforced in the
    service and in every test marked `bare_tasks`."""
    if request.node.get_closest_marker("bare_tasks"):
        return
    from app.services import tasks as task_service

    real = task_service.create_task

    def create_task(session, ctx, *, bid_id, requirement_ids=None, **kwargs):
        if not requirement_ids:
            requirement_ids = [_any_requirement(session, ctx, bid_id).id]
        return real(session, ctx, bid_id=bid_id, requirement_ids=requirement_ids, **kwargs)

    monkeypatch.setattr(task_service, "create_task", create_task)
