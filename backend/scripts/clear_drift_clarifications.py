"""Close the identity-drift notes that were wrongly raised as client questions.

One run of the analysis raised a drift note for every requirement whose wording
changed on re-extraction — which was most of them — and each note was linked to
its requirement. A clarification linked to a requirement makes that requirement
report "needs client clarification": an open question with the CUSTOMER that
blocks a reliable answer. Fifty-one of fifty-nine rows said so, and the matrix
said nothing else.

The code no longer links them. This closes the ones already in the database so
the rows come back, without re-running the analysis and without a model call.

    python scripts/clear_drift_clarifications.py            # show what it would do
    python scripts/clear_drift_clarifications.py --apply    # do it

Only touches clarifications whose detector is the drift check. Anything raised
by the tender analysis, the precedence check or a person is left alone.

Row-level security is FORCEd on these tables and applies to the table owner too,
so the work is done one organization at a time with `app.current_org` set —
the same way the application reaches its own rows.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

DETECTOR = "requirement_identity_drift@v1"
OPEN_STATES = ("OPEN", "SENT_TO_CLIENT")

# Where run_local.py keeps the embedded server, relative to this file.
PGDATA = Path(__file__).resolve().parent.parent / ".local" / "pgdata"


def _candidate_urls() -> list[tuple[str, str]]:
    """Every place the real connection string might be, in order, with a label.

    The local stack does not put its working credential anywhere a script can
    just read. `run_local.py` creates the constrained role and sets its password
    to a constant of its own, then exports the resulting URL into the child
    process it launches — so it exists only in that process's environment. The
    `.env` file keeps the packaged placeholder (`bidready_app:bidready`), which
    is a real-looking URL for the right server with the wrong password, and the
    failure is an authentication error that says nothing about the cause.

    So: the environment first (in case this is run from inside that process),
    then the embedded server if there is one, then the configured URL with the
    launcher's own constant substituted, then the configured URL untouched.
    """
    candidates: list[tuple[str, str]] = []

    # First: the connection the export scripts use. They read the application's
    # own settings and work on this machine, so it is the one known to be right.
    # The candidates below guessed at credentials and failed with "password
    # authentication failed" when the launcher's constant had drifted from the
    # role actually created in the database.
    try:
        from app.core.config import get_settings as _settings

        _s = _settings()
        _url = _s.database_url or _s.migration_database_url
        if _url:
            candidates.append(("application settings", str(_url)))
    except Exception:  # noqa: BLE001, S110 - fall through to the guesses below
        pass

    # The local superuser URL kept in backend/.env (gitignored). It is the
    # credential the local database was actually created with, so it is tried
    # before the application role, whose password can differ from the packaged
    # placeholder.
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in (
                "BIDREADY_LOCAL_DATABASE_URL",
                "BIDREADY_MIGRATION_DATABASE_URL",
            ):
                value = value.strip().strip('"').strip("'")
                if value:
                    candidates.insert(
                        0,
                        (key.strip(), value.replace("postgresql://", "postgresql+psycopg://", 1)),
                    )

    # The local superuser URL kept in backend/.env (gitignored). It is the
    # credential the local database was created with, so it is tried before the
    # application role, whose password can differ from the packaged placeholder.
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in (
                "BIDREADY_LOCAL_DATABASE_URL",
                "BIDREADY_MIGRATION_DATABASE_URL",
            ):
                value = value.strip().strip('"').strip("'")
                if value:
                    candidates.insert(
                        0,
                        (key.strip(), value.replace("postgresql://", "postgresql+psycopg://", 1)),
                    )

    for name in ("BIDREADY_MIGRATION_DATABASE_URL", "BIDREADY_DATABASE_URL"):
        url = os.environ.get(name)
        if url:
            candidates.append((name, url.replace("postgresql://", "postgresql+psycopg://", 1)))

    if PGDATA.exists():
        try:
            import pgserver
        except ImportError:
            pass
        else:
            # Attaches to the running server if one is up, starts it if not.
            server = pgserver.get_server(str(PGDATA))
            candidates.append(
                (
                    "embedded server",
                    server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1),
                )
            )

    from sqlalchemy.engine import make_url

    from app.core.config import get_settings

    settings = get_settings()
    configured = settings.migration_database_url or settings.database_url

    # The launcher is the single source of truth for the role and its password.
    # Importing it beats copying the constant, which would then be free to drift.
    launcher = Path(__file__).resolve().parent.parent.parent / "run_local.py"
    if launcher.exists():
        sys.path.insert(0, str(launcher.parent))
        try:
            import run_local

            candidates.append(
                (
                    "run_local credentials",
                    str(
                        make_url(configured).set(
                            username=run_local.APP_ROLE, password=run_local.APP_PASSWORD
                        )
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001, S110 - a convenience, not a dependency
            print(f"(could not read run_local.py for credentials: {type(exc).__name__})")

    candidates.append(("configured", configured))
    return candidates


def connect():
    """Return the first engine that actually connects, and say which it was."""
    from sqlalchemy.exc import OperationalError

    failures = []
    for label, url in _candidate_urls():
        engine = create_engine(url, future=True)
        try:
            with engine.connect():
                pass
        except OperationalError as exc:
            failures.append(f"  {label}: {str(exc).splitlines()[0][:120]}")
            continue
        print(f"database: {url.split('@')[-1]}  (via {label})")
        return engine

    print("Could not connect to the database. Tried:")
    for line in failures:
        print(line)
    print(
        "\nIf the stack is running, the surest fix is to run this from the same "
        "environment:\n"
        "  set BIDREADY_DATABASE_URL=<the URL run_local printed>  &&  "
        "python scripts\\clear_drift_clarifications.py --apply"
    )
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()

    engine = connect()
    if engine is None:
        return 1

    total = 0
    with engine.begin() as connection:
        org_ids = [row[0] for row in connection.execute(text("SELECT id FROM organization")).all()]
        for org_id in org_ids:
            connection.execute(
                text("SELECT set_config('app.current_org', :org, true)"), {"org": str(org_id)}
            )
            rows = connection.execute(
                text(
                    "SELECT id, description FROM clarification_issue "
                    "WHERE detected_by = :detector AND status = ANY(:states)"
                ),
                {"detector": DETECTOR, "states": list(OPEN_STATES)},
            ).all()
            if not rows:
                continue

            total += len(rows)
            print(f"\norg {org_id}: {len(rows)} open identity-drift clarification(s)")
            for _id, description in rows[:5]:
                print(f"  - {description[:110]}")
            if len(rows) > 5:
                print(f"  ... and {len(rows) - 5} more")

            if not args.apply:
                continue

            ids = [row[0] for row in rows]
            # Unlink first. The link is what made a requirement read as blocked,
            # and leaving it behind on a closed issue is a trap for the next
            # reader of this table.
            connection.execute(
                text("DELETE FROM clarification_requirement WHERE clarification_id = ANY(:ids)"),
                {"ids": ids},
            )
            connection.execute(
                text("UPDATE clarification_issue SET status = 'CLOSED' WHERE id = ANY(:ids)"),
                {"ids": ids},
            )

    if total == 0:
        print("No open identity-drift clarifications found. Nothing to do.")
    elif args.apply:
        print(f"\nClosed and unlinked {total} clarification(s).")
        print("Run the assessment again — it needs no model calls, the answers are cached.")
    else:
        print(f"\nDry run: {total} would be closed. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
