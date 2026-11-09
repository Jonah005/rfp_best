"""Find the PostgreSQL that is actually running, and print the command to use it.

`run_local.py` needs a superuser URL, and on Windows that URL has to be supplied
by hand. Guessing it one attempt at a time is slow and the error messages are
not very forthcoming: "password authentication failed" is what you get whether
the user is wrong, the password is wrong, or the server is a different
PostgreSQL than the one you meant.

So this tries the handful of combinations this project actually produces, on
the ports it actually uses, and reports each one separately:

    backend\\.venv\\Scripts\\python scripts\\find_database.py

It also reads shell history and PostgreSQL's own `pgpass` file for connection
strings this machine has used before, and tries those first -- a password you
already used beats one this project happens to document. Those files are read,
never written, and the only password ever printed is the one that works, in the
command you are about to run anyway.

It only opens connections and runs `SELECT version()`. It creates nothing and
changes nothing.

What the answers mean:

* "nothing listening" -- no server on that port. Start one.
* "wrong password" -- a server IS there and it rejected these credentials. The
  port is taken by a PostgreSQL that is not this project's.
* "no such database" -- the credentials are RIGHT; only the database name is
  wrong. That is a good result, and the script says so.
* a version string -- that URL works. Use the command it prints.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

try:
    import psycopg
except ImportError:
    print("psycopg is not installed. Run this with the project's interpreter:")
    print("  backend\\.venv\\Scripts\\python scripts\\find_database.py")
    sys.exit(1)

PORTS = [5432, 5433]

# Every combination this repository can produce, most likely first.
#
#   docker-compose.yml sets POSTGRES_USER=bidready, which makes `bidready` the
#   superuser and leaves `postgres` with no password at all -- so a compose
#   stack refuses `postgres` no matter what password you send it.
#
#   The `docker run` line in the README omits POSTGRES_USER, so that container
#   has `postgres` as its superuser instead. Both are in here because both
#   appear in this project's own documentation.
CANDIDATES = [
    ("bidready", "bidready", "bidready", "docker compose (docker-compose.yml)"),
    ("postgres", "bidready", "bidready", "the README's docker run line"),
    ("postgres", "postgres", "postgres", "a stock local install"),
    ("bidready", "bidready", "postgres", "compose credentials, default database"),
]


def classify(exc: Exception) -> str:
    """Turn a connection failure into the one fact that decides what to do."""
    text = str(exc).lower()
    if "does not exist" in text and "database" in text:
        return "no such database"
    if "password authentication failed" in text or "no password supplied" in text:
        return "wrong user or password"
    # "connection timeout expired" is psycopg's wording when nothing answers.
    # Reading it as a live server is the difference between "your PostgreSQL
    # has a password I don't know" and "there is no PostgreSQL there", which
    # are opposite instructions.
    if (
        "connection refused" in text
        or "could not connect" in text
        or "timed out" in text
        or "timeout expired" in text
        or "actively refused" in text
        or "host is unreachable" in text
    ):
        return "nothing listening"
    return str(exc).splitlines()[0][:100]


# --- looking for a password you already used --------------------------------
#
# A server that refuses every credential in this repository is one you set up
# yourself, with a password chosen at install time. It is not guessable and
# there is no point trying. But the machine that connected to it before very
# likely still has the string written down, in one of three places, and reading
# them beats a person hunting through terminal scrollback.


def _history_files() -> list[Path]:
    """Shell history, where a working --database-url would have been typed."""
    candidates: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(
            Path(appdata)
            / "Microsoft"
            / "Windows"
            / "PowerShell"
            / "PSReadLine"
            / "ConsoleHost_history.txt"
        )
    home = Path.home()
    candidates += [home / ".bash_history", home / ".zsh_history"]
    return [p for p in candidates if p.is_file()]


_URL = re.compile(r"postgresql(?:\+psycopg)?://([^:\s/]+):([^@\s]+)@([^:/\s]+):(\d+)/(\S+)")


def urls_from_history() -> list[tuple[str, str, str, str]]:
    """(user, password, database, where) for every connection string typed before."""
    found: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for path in _history_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for user, password, host, port, database in _URL.findall(text):
            if host not in {"localhost", "127.0.0.1"}:
                continue
            database = database.strip().strip("\"'")
            key = (user, password, database)
            if key in seen:
                continue
            seen.add(key)
            found.append((user, password, database, f"{path.name}, port {port}"))
    return found


def urls_from_pgpass() -> list[tuple[str, str, str, str]]:
    """PostgreSQL's own password file, if the installer or pgAdmin wrote one."""
    appdata = os.environ.get("APPDATA")
    paths = [Path.home() / ".pgpass"]
    if appdata:
        paths.append(Path(appdata) / "postgresql" / "pgpass.conf")
    found: list[tuple[str, str, str, str]] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) < 5:
                continue
            _host, _port, database, user, password = (
                parts[0],
                parts[1],
                parts[2],
                parts[3],
                parts[4],
            )
            found.append((user, password, "postgres" if database == "*" else database, path.name))
    return found


def main() -> int:
    working: list[tuple[int, str, str, str]] = []
    right_credentials: list[tuple[int, str, str]] = []
    live_ports: set[int] = set()

    # A password you used before beats one this project happens to document, so
    # these go first and the documented defaults act as the fallback.
    recovered = urls_from_history() + urls_from_pgpass()
    if recovered:
        print(f"found {len(recovered)} credential(s) already on this machine; trying those too")

    candidates = recovered + CANDIDATES

    for port in PORTS:
        print(f"\nport {port}")
        for user, password, database, label in candidates:
            url = f"postgresql://{user}:{password}@localhost:{port}/{database}"
            try:
                with psycopg.connect(url, connect_timeout=4) as connection:
                    version = connection.execute("SELECT version()").fetchone()[0]
            except Exception as exc:  # noqa: BLE001 - every failure is a result here
                reason = classify(exc)
                print(f"  {user}@{database:10} {reason:28} ({label})")
                if reason == "nothing listening":
                    break  # nothing is there; the other three will say the same
                live_ports.add(port)
                if reason == "no such database":
                    right_credentials.append((port, user, password))
                continue

            live_ports.add(port)
            print(f"  {user}@{database:10} WORKS  -- {version.split(',')[0]}")
            working.append((port, user, password, database))

    print("\n" + "-" * 72)

    if working:
        port, user, password, database = working[0]
        print("Use this:\n")
        print(
            f"  python run_local.py --database-url "
            f"postgresql://{user}:{password}@localhost:{port}/{database}\n"
        )
        if len(working) > 1:
            print("Other URLs also worked; the one above is the best match for this project.")
        # A superuser is required: run_local.py creates roles and the pgvector
        # extension. Saying so now beats a confusing permission error later.
        try:
            with psycopg.connect(
                f"postgresql://{user}:{password}@localhost:{port}/{database}", connect_timeout=4
            ) as connection:
                superuser = connection.execute(
                    "SELECT usesuper FROM pg_user WHERE usename = current_user"
                ).fetchone()
                available = connection.execute(
                    "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
                ).fetchone()
        except Exception:  # noqa: BLE001
            return 0
        if superuser and not superuser[0]:
            print(
                f"\nWarning: {user} is not a superuser. run_local.py creates roles and the\n"
                "pgvector extension, so it needs one."
            )
        if not available:
            print(
                "\nWarning: this server does not offer the `vector` extension. Semantic\n"
                "retrieval needs it. The pgvector/pgvector image includes it; a stock\n"
                "PostgreSQL install does not."
            )
        return 0

    if right_credentials:
        port, user, password = right_credentials[0]
        print(
            f"A server on port {port} accepted {user}, but the database was missing.\n"
            "The credentials are right. Create the database:\n"
        )
        print(
            f'  psql "postgresql://{user}:{password}@localhost:{port}/postgres" '
            '-c "CREATE DATABASE bidready"\n'
        )
        print("or, if you use Docker, start the project's stack and let it create one:\n")
        print("  docker compose up -d postgres")
        return 1

    if live_ports:
        port = sorted(live_ports)[0]
        print(
            f"A PostgreSQL is listening on {port} and refused every credential tried,\n"
            f"including {len(recovered)} found on this machine. It is a server you set up\n"
            "yourself, with a password chosen at install time.\n"
        )
        print("Three ways to recover it, cheapest first:\n")
        print(
            "  1. If it is a Docker container, ask it what password it was started with:\n"
            "         docker ps\n"
            '         docker inspect <name> --format "{{range .Config.Env}}{{println .}}{{end}}"\n'
            "     POSTGRES_USER and POSTGRES_PASSWORD are in that output.\n"
        )
        print(
            "  2. If you installed PostgreSQL for Windows, the password is the one you\n"
            "     typed in the installer, and pgAdmin may have it saved. Then:\n"
            f"         python run_local.py --database-url postgresql://postgres:"
            f"YOUR_PASSWORD@localhost:{port}/postgres\n"
        )
        print(
            "  3. Failing both, run this project's database beside it on a free port:\n"
            "         docker compose up -d postgres\n"
            "     -- but change the compose port mapping to 5433:5432 first, since 5432\n"
            "     is taken, and then:\n"
            "         python run_local.py --database-url "
            "postgresql://bidready:bidready@localhost:5433/bidready\n"
        )
        print(
            "  Before doing 3: a new database is EMPTY. Your bid, its requirements and\n"
            "  the approved evidence live in the server you were using before, so option\n"
            "  3 means re-uploading the RFP and the company pack and re-running the\n"
            "  analysis. Worth ten minutes on options 1 and 2 first."
        )
        return 1

    print("No PostgreSQL is listening on 5432 or 5433. Start this project's stack:\n")
    print("  docker compose up -d postgres\n")
    print("then re-run this script.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
