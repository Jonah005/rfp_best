"""Create the database the integration tests run against.

`createdb` is a PostgreSQL command-line tool, and on Windows it is not on PATH
unless somebody put it there -- it lives under `C:\\Program Files\\PostgreSQL\\18\\bin`.
`psycopg` is already a dependency of this project, so there is no reason to go
looking for it.

## Why a separate database at all

The integration suite creates the `bidready_app` role and builds the entire
schema, then writes organizations, users, bids and requirements. Nothing is
dropped, so the risk is not destruction: it is test rows accumulating in the
database that holds the live corpus, where the next diagnostic counts them as
real. `tests/integration/conftest.py` refuses to run against `bidready` for
exactly that reason.

## Why this needs an owner login

Creating a database and a role is not something the application role can do --
`bidready_app` is deliberately NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS,
which is the whole point of it. So this asks for the superuser (usually
`postgres`), uses it once, and prints the URL to set afterwards.

The password is read from the environment or typed at a prompt. It is never
written to a file and never echoed.

    set BIDREADY_OWNER_PASSWORD=...
    python scripts/create_test_database.py

    python scripts/create_test_database.py            # prompts instead
    python scripts/create_test_database.py --name other_test_db
"""

from __future__ import annotations

import getpass
import os
import sys

DEFAULT_NAME = "bidready_test"
DEFAULT_USER = "postgres"
DEFAULT_HOST = "localhost"
DEFAULT_PORT = "5432"

#: Never create or point tests at these. See tests/integration/conftest.py.
PROTECTED = frozenset({"bidready", "postgres", "template0", "template1"})


def _arg(flag: str, default: str) -> str:
    if flag in sys.argv:
        index = sys.argv.index(flag)
        if index + 1 < len(sys.argv):
            return sys.argv[index + 1]
    return default


def main() -> int:
    name = _arg("--name", DEFAULT_NAME)
    user = _arg("--user", os.environ.get("BIDREADY_OWNER_USER", DEFAULT_USER))
    host = _arg("--host", os.environ.get("BIDREADY_DB_HOST", DEFAULT_HOST))
    port = _arg("--port", os.environ.get("BIDREADY_DB_PORT", DEFAULT_PORT))

    if name in PROTECTED:
        print(f"Refusing to use {name!r}: that is a working or system database.")
        print("The tests write organizations, users and bids; they need one of their own.")
        return 1

    password = os.environ.get("BIDREADY_OWNER_PASSWORD")
    if not password:
        print(f"Owner password for {user}@{host}:{port} (not echoed, not stored):")
        password = getpass.getpass("  password: ")
    if not password:
        print("No password given.")
        return 1

    try:
        import psycopg
    except ImportError:
        print("psycopg is not installed. Run: pip install -e .[dev]")
        return 1

    # CREATE DATABASE cannot run inside a transaction block, so this connects
    # with autocommit rather than using a normal session.
    admin = f"postgresql://{user}:{password}@{host}:{port}/postgres"
    try:
        with psycopg.connect(admin, autocommit=True) as conn:
            existing = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
            ).fetchone()
            if existing:
                print(f"{name!r} already exists; nothing to do.")
            else:
                # The name is validated against PROTECTED above and comes from a
                # developer's own command line, but it still cannot be a bound
                # parameter in DDL, so it is quoted by the driver.
                from psycopg import sql

                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                print(f"Created {name!r}.")
    except Exception as exc:  # noqa: BLE001 - the message is the whole output
        print(f"\nCould not connect or create: {type(exc).__name__}: {exc}")
        print(f"\nCheck that PostgreSQL is running on {host}:{port} and that {user!r} is")
        print("a superuser or has CREATEDB. The application role bidready_app cannot do")
        print("this, by design.")
        return 1

    url = f"postgresql://{user}:PASSWORD@{host}:{port}/{name}"
    print("\nNow point the tests at it:\n")
    if sys.platform.startswith("win"):
        print(f"  set BIDREADY_TEST_DATABASE_URL={url}")
    else:
        print(f"  export BIDREADY_TEST_DATABASE_URL={url}")
    print("\n(substitute the real password) then:\n")
    print("  python -m pytest -q -p no:randomly")
    print("\nExpect 1124 passed (1123 and 1 skip if reportlab is not installed).")
    print("Hundreds of skips mean the suite is not reaching a database, and")
    print("tests/conftest.py will say so in red. The suite wipes and rebuilds this")
    print("database's schema on every run, which is why it must be its own.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
