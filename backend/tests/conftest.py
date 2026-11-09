"""Shared fixtures, and one loud warning.

## Why the terminal summary shouts

`python -m pytest -q` reported **838 passed, 285 skipped** on a developer's
machine and **1123 passed, 0 skipped** in CI, and nothing in the output said the
difference mattered. Every integration test -- which is every test that touches
the database, the governance gates, the release rules, the tenant isolation --
was skipped because `pgserver` was not installed, and `pytest.importorskip`
turns that into a dot-less `s` in a progress bar nobody reads.

Four separate defects were found today by asking "what is declared and never
enforced": a validation code with no rule that raised it, six red tests read as
noise, a lifecycle value nothing queried, an index for a query that had changed.
A test suite that silently skips 25% of itself and still prints a green line is
the same bug, aimed at the people fixing the others.

So: if the integration tests do not run, the summary says so in the place a
person actually looks, and says how to fix it.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

os.environ.setdefault("BIDREADY_ENV", "test")
os.environ.setdefault("BIDREADY_SECRET_KEY", "test-secret-key-not-for-production")


def _choose_vector_backend() -> None:
    """Decide pgvector vs portable BEFORE any test module imports `app.models`.

    This has to happen here, at conftest import, and nowhere later. pytest
    imports every test module during collection; each of those imports
    `app.models`; and `app.models` builds its embedding columns from
    `settings.vector_backend` once, at import. A fixture that chooses the
    backend has already lost -- the schema was decided during collection, and
    the result is `type "vector" does not exist` when the classes say VECTOR(768)
    and the server has no extension.

    `scripts/_vector_backend.py` documents the same trap for standalone scripts
    and solves it the same way, which is a duplication worth naming: the rule
    belongs in one importable place, and it currently lives in a script that
    tests cannot import without adding `scripts/` to the path. Left alone for
    now rather than moved half-way.

    Only probes when the suite has been pointed at a real server. `pgserver`
    starts a throwaway one that always has the extension, and it has not started
    yet at this point in the run.
    """
    if os.environ.get("BIDREADY_VECTOR_BACKEND"):
        return  # stated explicitly; nothing to detect
    url = os.environ.get("BIDREADY_TEST_DATABASE_URL")
    if not url:
        return  # pgserver: bundled with pgvector
    try:
        import psycopg

        with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://")) as conn:
            found = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
            if found:
                return
            # Not an error, and not silent either. A person reading a failure
            # needs to know which storage the run used.
            installable = conn.execute(
                "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
            ).fetchone()
        os.environ["BIDREADY_VECTOR_BACKEND"] = "portable"
        print(
            "\npgvector is not installed on the test database; running on the portable"
            "\n(real[]) backend, which is what this project's own development database"
            "\nuses. "
            + (
                "The extension IS available: `CREATE EXTENSION vector` as a"
                "\nsuperuser would switch the run to pgvector."
                if installable
                else "The extension is not available on this server either."
            )
        )
    except Exception as exc:  # noqa: BLE001 - detection is best effort
        print(f"\nCould not probe for pgvector ({type(exc).__name__}); leaving the default.")


_choose_vector_backend()


@pytest.fixture
def org_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def other_org_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def task_id() -> uuid.UUID:
    return uuid.uuid4()


# --- the skip that used to be silent ----------------------------------------


def _database_available() -> tuple[bool, str]:
    """Can the integration tests run, and if not, what is missing?"""
    if os.environ.get("BIDREADY_TEST_DATABASE_URL"):
        return True, ""
    try:
        import pgserver  # noqa: F401
    except ImportError:
        return False, "pgserver is not installed and BIDREADY_TEST_DATABASE_URL is not set"
    return True, ""


def _windows() -> bool:
    return sys.platform.startswith("win")


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """Say it where a person looks.

    pytest reports skip REASONS only under `-rs`, which nobody passes. The
    default output gives an `s` in a progress bar and a count in a line that
    otherwise reads as success, so "838 passed, 285 skipped" and "1123 passed"
    look like the same result at a glance.
    """
    skipped = len(terminalreporter.stats.get("skipped", []))
    if not skipped:
        return
    available, reason = _database_available()
    if available:
        return
    terminalreporter.write_sep("=", "INTEGRATION TESTS DID NOT RUN", red=True, bold=True)
    terminalreporter.write_line(
        f"{skipped} test(s) were skipped: {reason}.\n"
        "\nThese are the tests that exercise the database -- tenant isolation, the\n"
        "approval gates, release validation, requirement identity. A green line above\n"
        "means the unit tests passed, and NOTHING about any of that.\n"
        + (
            "\nOn Windows, point the suite at a real server. `pgserver` is POSIX only:\n"
            "it starts, then dies cleaning up its own temp directory.\n"
            "\n  createdb -U postgres bidready_test\n"
            "  set BIDREADY_TEST_DATABASE_URL=postgresql://postgres:PW@localhost:5432/"
            "bidready_test\n"
            if _windows()
            else "\nEither:\n"
            "  pip install pgserver            # runs a throwaway Postgres per session\n"
            "  export BIDREADY_TEST_DATABASE_URL=postgresql://user:pw@localhost:5432/"
            "bidready_test\n"
        )
        + "\nUse a SEPARATE database. The suite creates a role and writes organizations,\n"
        "users and bids; pointed at your working database it would leave them there\n"
        "among real data. The URL needs an OWNER-level login: it creates a role and\n"
        "the schema.",
        red=True,
    )
