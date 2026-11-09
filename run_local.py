#!/usr/bin/env python3
"""Run BidReady locally in one process, with no Docker and no Node.

What this replaces, and with what:

    MinIO                  ->  a directory on disk (LocalDiskStorage)
    Redis + Celery worker  ->  queued work runs inline (celery_eager)
    Next.js server         ->  the statically exported UI, served by FastAPI
    PostgreSQL + pgvector  ->  embedded via pgserver on Linux and macOS

PostgreSQL is the one piece that cannot be conjured away. The product depends
on row level security, pgvector, JSONB and full-text search, so a lighter
database is not an option. On Linux and macOS this script embeds one with
`pgserver`, which needs no install and no root. `pgserver` has no Windows
build, so on Windows point the script at any PostgreSQL 16+ with pgvector:

    python run_local.py --database-url postgresql://postgres:pw@localhost:5432/bidready

One container is enough to provide that, and is far lighter than the full stack:

    docker run -d --name bidready-pg -p 5432:5432 \
      -e POSTGRES_PASSWORD=bidready -e POSTGRES_DB=bidready pgvector/pgvector:pg16

Nothing about the security model is relaxed. The database still gets both
roles, row level security is still ENABLEd and FORCEd, migrations still run as
the owner while the application connects as the constrained role, and the same
seed runs under the same policies. This is a smaller deployment, not a
weaker one.

    python run_local.py            # start it
    python run_local.py --rebuild-ui   # rebuild the UI first (needs pnpm once)
    python run_local.py --reset    # throw away the local database and objects

On Windows the first run asks once for the PostgreSQL superuser password and
remembers the URL in backend/.local/database-url (gitignored). After that,
`python run_local.py` on its own is the whole command -- which is the point:
the obvious thing to type should start the right thing. Delete that file to be
asked again, or override it any time with --database-url or
BIDREADY_LOCAL_DATABASE_URL.

Data lives in backend/.local/ and persists between runs.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
LOCAL = BACKEND / ".local"
PGDATA = LOCAL / "pgdata"
OBJECTS = LOCAL / "objects"
UI_OUT = FRONTEND / "out"
# A prebuilt static export, so the UI needs no Node toolchain to run. Built with
# a relative API base, so it works on whatever port the server is given.
UI_BUNDLE = FRONTEND / "ui-dist.zip"

APP_ROLE = "bidready_app"
APP_PASSWORD = "bidready-local"  # noqa: S105 - local-only role, never leaves this machine
HOST = "127.0.0.1"
PORT = 8000

# Where this machine's superuser URL is remembered, so `python run_local.py`
# with no arguments starts the right thing.
#
# A file rather than a constant in this script, because the URL carries a
# password and this script is committed. It is written on first run, from an
# answer typed at the prompt, and it is in .gitignore. Anyone who would rather
# not have it on disk can set BIDREADY_LOCAL_DATABASE_URL instead, or keep
# passing --database-url; both win over the file.
LOCAL_URL_FILE = LOCAL / "database-url"
# The project's own local configuration file, already gitignored and already
# where the DeepSeek key and the application's database URL live. Putting
# BIDREADY_LOCAL_DATABASE_URL there is the obvious thing to do, so this reads
# it: one place for local secrets rather than two.
ENV_FILE = BACKEND / ".env"
ENV_KEY = "BIDREADY_LOCAL_DATABASE_URL"
# The parts of the URL that are not secret, and are the same on every machine
# this project has run on. Only the password is asked for.
DEFAULT_SUPERUSER = "postgres"
DEFAULT_DB_HOST = "localhost"
DEFAULT_DB_PORT = 5432
DEFAULT_DB_NAME = "bidready"


def fail(message: str, *, hint: str = "") -> None:
    print(f"\n  {message}")
    if hint:
        print(f"  {hint}")
    sys.exit(1)


WINDOWS_HELP = """PostgreSQL is required and cannot be embedded on Windows.

  Either start one container:

      docker run -d --name bidready-pg -p 5432:5432 ^
        -e POSTGRES_PASSWORD=bidready -e POSTGRES_DB=bidready pgvector/pgvector:pg16

  or install PostgreSQL 16 with the pgvector extension, then re-run:

      python run_local.py --database-url postgresql://postgres:bidready@localhost:5432/bidready
"""


REMEMBERED_HELP = """No database URL, and nothing remembered for this machine.

  This is not an interactive terminal, so there is nowhere to ask. Either pass
  the URL:

      python run_local.py --database-url postgresql://postgres:PASSWORD@localhost:5432/bidready

  or set it once for good:

      setx BIDREADY_LOCAL_DATABASE_URL "postgresql://postgres:PASSWORD@localhost:5432/bidready"
"""


def url_from_env_file() -> str | None:
    """`BIDREADY_LOCAL_DATABASE_URL` from backend/.env, if it is set there.

    Read by hand rather than with a dotenv library: this script runs before
    anything is installed, and the parsing needed is one `split("=", 1)`. Only
    this one key is read, and nothing else in the file is touched -- the
    application loads it properly through its own settings.
    """
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != ENV_KEY:
            continue
        value = value.strip().strip('"').strip("'")
        if value:
            return value
    return None


def remembered_database_url() -> str | None:
    """The URL this machine should use, if it has been written down anywhere.

    backend/.env first, because that is a file a person edits on purpose and
    can see; the prompt's own file second, because that one is written by this
    script and a person may have forgotten it exists.
    """
    from_env = url_from_env_file()
    if from_env:
        return from_env
    try:
        url = LOCAL_URL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return url or None


def why_not(url: str) -> str | None:
    """None if the URL connects, else the driver's one-line reason.

    Separate from `connect_or_explain` because the answer is used to DECIDE
    rather than to give up: a refused password is worth asking again about, and
    a refused port is not.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError

    try:
        engine = create_engine(url, future=True, isolation_level="AUTOCOMMIT")
        engine.connect().close()
    except OperationalError as exc:
        detail = str(exc.orig).strip().splitlines()[0] if exc.orig else str(exc)
        return detail
    except Exception as exc:  # noqa: BLE001 - a bad URL shape lands here
        return str(exc).strip().splitlines()[0]
    return None


def looks_like_a_wrong_password(reason: str) -> bool:
    lowered = reason.lower()
    return "password authentication failed" in lowered or "authentication" in lowered


def save_database_url(url: str) -> None:
    LOCAL_URL_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_URL_FILE.write_text(url, encoding="utf-8")
    try:
        # Owner-only where the platform has the concept. Windows ignores it,
        # which is why the file lives under backend/.local/ and is gitignored
        # rather than relying on permissions alone.
        LOCAL_URL_FILE.chmod(0o600)
    except OSError:
        pass
    print(f"  saved      {LOCAL_URL_FILE} (gitignored; delete it to be asked again)")
    print(f"             or put it in backend/.env as {ENV_KEY}=... , which wins")


ATTEMPTS = 3


def ask_for_database_url(*, reason: str = "") -> str:
    """Ask for the password, CHECK it, and only then remember it.

    The first version of this saved the answer and then let the normal
    connection path fail, which is the worst of both: a wrong password was
    written to disk as though it were right, and the failure arrived as a
    screen of help about Docker containers -- whose example URL, followed
    literally, pointed at a port with nothing on it. Two dead ends from one
    typo.

    So the password is tried before it is written, a refusal is said in one
    line, and the question is asked again. Only a reason that is not about the
    password -- nothing listening, no such database -- gives up, because asking
    again cannot fix those.
    """
    import getpass
    from urllib.parse import quote

    if not sys.stdin.isatty():
        fail("No usable database URL.", hint=REMEMBERED_HELP)

    if reason:
        print(f"\n  The remembered password was refused: {reason}")
        print("  Asking again.\n")
    else:
        print("\n  First run on this machine. One question, asked once.\n")
    print(f"  PostgreSQL superuser  {DEFAULT_SUPERUSER}")
    print(f"  Server                {DEFAULT_DB_HOST}:{DEFAULT_DB_PORT}")
    print(f"  Database              {DEFAULT_DB_NAME}")
    print("\n  If those are wrong, stop and pass --database-url instead.\n")

    for attempt in range(1, ATTEMPTS + 1):
        try:
            password = getpass.getpass(f"  Password for {DEFAULT_SUPERUSER}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            fail("Cancelled.", hint=REMEMBERED_HELP)
        if not password:
            print("  Nothing typed. The password is not echoed -- type it and press Enter.")
            continue

        url = (
            f"postgresql://{DEFAULT_SUPERUSER}:{quote(password, safe='')}"
            f"@{DEFAULT_DB_HOST}:{DEFAULT_DB_PORT}/{DEFAULT_DB_NAME}"
        )
        print("  checking   ", end="", flush=True)
        problem = why_not(url.replace("postgresql://", "postgresql+psycopg://", 1))
        if problem is None:
            print("connected")
            save_database_url(url)
            return url
        print(problem)
        if not looks_like_a_wrong_password(problem):
            # Nothing listening, no such database, no pgvector: asking for the
            # password again cannot help, and pretending otherwise wastes three
            # goes before the real advice appears.
            fail("That is not a password problem.", hint=DB_HELP)
        if attempt < ATTEMPTS:
            print(f"  Try again ({ATTEMPTS - attempt} left).")

    fail("The password was refused three times; nothing was saved.", hint=DB_HELP)
    raise SystemExit(1)  # unreachable; keeps the return type honest


def start_postgres(database_url: str | None):
    """Return (server_or_None, owner_url).

    A supplied URL is used as-is and must belong to a superuser: this script
    creates roles and the pgvector extension. Otherwise an embedded server is
    started, which works on Linux and macOS only.
    """
    if database_url:
        url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        print("  postgres   using the database you supplied")
        return None, url

    # Nothing passed, nothing in the environment: on a platform that cannot
    # embed a server, use what this machine answered last time -- and if it has
    # never been asked, ask now. `python run_local.py` is what a person types,
    # and it should start the right thing rather than explain why it cannot.
    if sys.platform.startswith("win"):
        remembered = remembered_database_url()
        if remembered:
            where = f"{ENV_KEY} in backend/.env" if url_from_env_file() else str(LOCAL_URL_FILE)
            print(f"  postgres   using {where}")
            # Checked here rather than left to the normal connection path,
            # because a remembered password that has stopped working has an
            # obvious remedy -- ask for it again -- and the generic failure
            # help does not mention it early enough to be seen.
            problem = why_not(remembered.replace("postgresql://", "postgresql+psycopg://", 1))
            if problem is not None:
                if not looks_like_a_wrong_password(problem):
                    fail(f"PostgreSQL refused the connection: {problem}", hint=DB_HELP)
                remembered = ask_for_database_url(reason=problem)
        else:
            remembered = ask_for_database_url()
        return None, remembered.replace("postgresql://", "postgresql+psycopg://", 1)

    try:
        import pgserver
    except ImportError:
        fail(
            "pgserver is not installed, and no --database-url was given.",
            hint='Run:  pip install -e "backend[dev]"',
        )

    PGDATA.mkdir(parents=True, exist_ok=True)
    print(f"  postgres   starting in {PGDATA}")
    try:
        server = pgserver.get_server(str(PGDATA))
    except OSError as exc:
        # A PostgreSQL Unix socket cannot live on every filesystem; network and
        # some virtualised mounts refuse to bind one.
        fail(
            f"The embedded PostgreSQL could not start in {PGDATA}: {exc}",
            hint="That filesystem may not support Unix sockets. Pass "
                 "--database-url to use a PostgreSQL you already run.",
        )
    return server, server.get_uri().replace("postgresql://", "postgresql+psycopg://")


DB_HELP = """Could not connect to PostgreSQL with that URL.

  In order, the things that are actually wrong when this appears:

  1. The password. If it was remembered from an earlier run and has since
     changed, delete the remembered URL and you will be asked again:

         del backend\\.local\\database-url

  2. Nothing is listening on that port. Check:

         netstat -ano | findstr :5432

  3. There IS a PostgreSQL there, but it is one installed earlier with a
     password chosen at install time. Use that password:

         python run_local.py --database-url postgresql://postgres:YOUR_PASSWORD@localhost:5432/bidready

  Only if you have no PostgreSQL at all: a container gives you one. Note the
  port -- 5433 -- and that this command must be RUN before the URL below can
  work; pasting the second line alone connects to nothing.

      docker run -d --name bidready-pg -p 5433:5432 ^
        -e POSTGRES_PASSWORD=bidready -e POSTGRES_DB=bidready pgvector/pgvector:pg16

      python run_local.py --database-url postgresql://postgres:bidready@localhost:5433/bidready

  The database must be PostgreSQL 16+ and have the pgvector extension
  available; the pgvector/pgvector image includes it.
"""


def connect_or_explain(url: str):
    """Open the owner connection, or explain the failure in one paragraph.

    A driver traceback here is a hundred lines of SQLAlchemy internals about a
    problem that is nearly always a password or a port. Catch it and say so.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError

    engine = create_engine(url, future=True, isolation_level="AUTOCOMMIT")
    try:
        engine.connect().close()
    except OperationalError as exc:
        detail = str(exc.orig).strip().splitlines()[0] if exc.orig else str(exc)
        fail(f"PostgreSQL refused the connection: {detail}", hint=DB_HELP)
    return engine


def prepare_roles(owner_url: str) -> tuple[str, str]:
    """Create the two roles the design depends on, then return the app URL.

    `bidready_app` is NOSUPERUSER with no CREATE on schema public and no
    BYPASSRLS -- that is what makes FORCE ROW LEVEL SECURITY actually bind the
    application. `bidready_auth` owns the single SECURITY DEFINER function used
    to resolve an email to a tenant before any tenant context exists.
    """
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    engine = connect_or_explain(owner_url)
    with engine.connect() as conn:
        # pgvector is preferred, not required. Without it the same embeddings
        # are stored in a real[] column and ranked by cosine in Python: same
        # results, no index, so it suits modest corpora.
        try:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            vector_backend = "pgvector"
        except Exception:
            vector_backend = "portable"
        # Written out literally rather than interpolated: these are fixed role
        # names, and a plain string keeps the SQL obviously free of any input.
        conn.exec_driver_sql(
            """
            DO $$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bidready_app') THEN
                CREATE ROLE bidready_app LOGIN NOSUPERUSER NOCREATEDB
                  NOCREATEROLE NOBYPASSRLS;
              END IF;
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bidready_auth') THEN
                CREATE ROLE bidready_auth NOLOGIN NOSUPERUSER NOCREATEDB
                  NOCREATEROLE BYPASSRLS;
              END IF;
            END $$;
            """
        )
        conn.execute(  # noqa: S608 - constants, not user input
            text(f"ALTER ROLE {APP_ROLE} WITH PASSWORD '{APP_PASSWORD}'")
        )
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
        conn.exec_driver_sql(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
        )
        conn.exec_driver_sql(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}"
        )
    engine.dispose()
    print("  roles      bidready_app (constrained) and bidready_auth ready")
    app_url = make_url(owner_url).set(
        username=APP_ROLE, password=APP_PASSWORD
    ).render_as_string(hide_password=False)
    return app_url, vector_backend


def run_step(label: str, command: list[str], env: dict[str, str]) -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv built in this file
        command, cwd=BACKEND, env=env, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        print((result.stderr or result.stdout)[-2500:])
        fail(f"{label} failed.")
    print(f"  {label}")


NO_NODE_HELP = """The UI needs a Node toolchain to build, and one is not available.

  Easiest: use the prebuilt UI. Put `ui-dist.zip` in `frontend/` and re-run;
  this script unpacks it automatically and never touches Node.

  Otherwise install Node 20+ (https://nodejs.org) and make sure `node` itself
  is on PATH, not just `npm`, then re-run.

  Or skip the UI entirely and drive the API from its own docs:

      python run_local.py --no-ui
"""


def unpack_ui() -> bool:
    """Use the prebuilt export when one is present. Returns True if unpacked."""
    if not UI_BUNDLE.is_file():
        return False
    import zipfile

    print(f"  ui         unpacking {UI_BUNDLE.name} (no Node needed)")
    if UI_OUT.exists():
        shutil.rmtree(UI_OUT, ignore_errors=True)
    UI_OUT.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(UI_BUNDLE) as archive:
        archive.extractall(UI_OUT)
    return (UI_OUT / "index.html").is_file()


def _read_env_file(path: Path) -> dict[str, str]:
    """Minimal dotenv reader: KEY=value, ignoring comments and blank lines.

    Deliberately not python-dotenv — this launcher has to work before any
    optional dependency is installed, and the file it reads is one this repo
    writes itself.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _ui_is_stale() -> bool:
    """True when `out/` is missing or older than the shipped bundle.

    Comparing mtimes rather than only checking for `index.html` is what makes a
    shipped UI fix actually reach the browser: without it, a stale `out/` from
    an earlier run silently wins over a newer `ui-dist.zip` and the fix looks
    like it did not work.
    """
    index = UI_OUT / "index.html"
    if not index.is_file():
        return True
    if UI_BUNDLE.is_file() and UI_BUNDLE.stat().st_mtime > index.stat().st_mtime:
        print("  ui         bundle is newer than out/ — re-unpacking")
        return True
    return False


def build_ui(api_base: str) -> None:
    # `npm` on Windows is a shim that shells out to `node`; a machine can have
    # the shim on PATH and no working node behind it, which fails deep inside
    # the build with an unhelpful message. Check the thing that actually runs.
    node = shutil.which("node")
    pnpm = shutil.which("pnpm") or shutil.which("npm")
    if not node or not pnpm:
        fail("Cannot build the UI.", hint=NO_NODE_HELP)
    env = {
        **os.environ,
        "BIDREADY_STATIC_EXPORT": "1",
        "NEXT_PUBLIC_API_URL": api_base,
    }
    if not (FRONTEND / "node_modules").is_dir():
        print("  ui         installing dependencies (first run only)…")
        subprocess.run(  # noqa: S603 - resolved from PATH by shutil.which
            [pnpm, "install"], cwd=FRONTEND, env=env, check=True
        )
    print("  ui         building the static export…")
    result = subprocess.run(  # noqa: S603 - resolved from PATH by shutil.which
        [pnpm, "run", "build"], cwd=FRONTEND, env=env, check=False
    )
    if result.returncode != 0:
        fail("The UI build failed (output above).", hint=NO_NODE_HELP)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run BidReady locally, no Docker.")
    parser.add_argument("--rebuild-ui", action="store_true", help="rebuild the static UI first")
    parser.add_argument(
        "--reset", action="store_true", help="delete the local database and objects"
    )
    parser.add_argument(
        "--no-ui", action="store_true", help="serve the API only, without the web UI"
    )
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("BIDREADY_LOCAL_DATABASE_URL"),
        help="superuser URL of an existing PostgreSQL 16+ with pgvector. On "
             "Windows this is asked for once and remembered, so it is only "
             "needed to override what was remembered.",
    )
    args = parser.parse_args()

    if args.reset:
        for path in (PGDATA, OBJECTS):
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        print("  reset      local database and objects removed")

    # The UI is served from the same origin as the API, so a relative base
    # works on any port and needs no rebuild when the port changes.
    api_base = "/api/v1"
    if args.no_ui:
        print("  ui         skipped (--no-ui)")
    elif args.rebuild_ui:
        build_ui(api_base)
    elif _ui_is_stale() and not unpack_ui():
        build_ui(api_base)

    print("\n  BidReady — local, single process\n")
    sys.path.insert(0, str(BACKEND))
    server, owner_url = start_postgres(args.database_url)
    app_url, vector_backend = prepare_roles(owner_url)
    if vector_backend == "pgvector":
        print("  vectors    pgvector present: indexed semantic search")
    else:
        print("  vectors    no pgvector: portable cosine search (unindexed, "
              "fine for modest data)")

    OBJECTS.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "BIDREADY_ENV": "development",
        "BIDREADY_DATABASE_URL": app_url,
        "BIDREADY_MIGRATION_DATABASE_URL": owner_url,
        "BIDREADY_STORAGE_BACKEND": "local",
        "BIDREADY_STORAGE_LOCAL_PATH": str(OBJECTS),
        "BIDREADY_CELERY_EAGER": "true",
        # One process, so shared job state would be ceremony for nothing.
        # A deployment with more than one API replica must leave this on
        # "auto" (or "redis"), or streamed answers hang on the wrong replica.
        "BIDREADY_JOB_STORE": "memory",
        "BIDREADY_VECTOR_BACKEND": vector_backend,
        # One origin: the API serves the UI, so there is no cross-origin call.
        "BIDREADY_CORS_ORIGINS": f"http://{HOST}:{args.port}",
        "BIDREADY_UI_DIR": str(UI_OUT),
    }
    # Without a DeepSeek key, drafting and the Copilot are the only things that
    # break. Say so once rather than letting it surface as a runtime error.
    #
    # The key is read from backend/.env as well as the environment. It has to
    # be: this launcher exports BIDREADY_LLM_PROVIDER, and a real environment
    # variable outranks a dotenv value in pydantic-settings — so checking only
    # os.environ meant a key sitting correctly in backend/.env was ignored *and*
    # overridden with the null provider, with the banner cheerfully reporting
    # the key was absent.
    if not os.environ.get("DEEPSEEK_API_KEY"):
        from_file = _read_env_file(BACKEND / ".env").get("DEEPSEEK_API_KEY", "")
        if from_file:
            os.environ["DEEPSEEK_API_KEY"] = from_file
    if not os.environ.get("DEEPSEEK_API_KEY"):
        env["BIDREADY_LLM_PROVIDER"] = "null"
        print("  llm        no DEEPSEEK_API_KEY: drafting and Copilot disabled")
        print(f"             (looked in the environment and {BACKEND / '.env'})")
    else:
        print("  llm        DeepSeek key loaded: drafting and Copilot enabled")
    # sentence-transformers is a large optional dependency, and its absence is
    # not cosmetic. The stand-in is a bag-of-words hash: it scores two texts
    # that mean the same thing but share no words at exactly 0.0, which is the
    # lexical failure it was supposed to cover for. Semantic evidence matching
    # is therefore OFF, not degraded, and the banner has to say so — a silent
    # fallback here reads later as "the system cannot find our certificates".
    # Native math libraries size their thread pools from the CPU count, and on some
    # machines that number is more than the OS will let one process start
    # ("pthread_create() is 22"), which kills the process before it can say why.
    # A few threads is plenty for a handful of short texts.
    for _name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(_name, "4")
        env.setdefault(_name, os.environ[_name])
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    env.setdefault("TOKENIZERS_PARALLELISM", "false")
    try:
        # Looked for, not imported: importing torch here is what started the thread pools
        # inside this launcher, and the server process does its own loading later.
        import importlib.util

        if importlib.util.find_spec("sentence_transformers") is None:
            raise ImportError("sentence_transformers")

        # Loading the model needs room in the page file as well as in RAM. When
        # there is not enough, Windows ends the process inside native code with no
        # traceback ("The paging file is too small", os error 1455) -- the server
        # simply disappears mid-request. Checked up front so the model is switched
        # off rather than the server lost.
        if sys.platform == "win32" and os.environ.get("BIDREADY_FORCE_EMBEDDINGS") != "1":
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            _status = _MemoryStatus()
            _status.dwLength = ctypes.sizeof(_MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(_status))
            _free_gb = _status.ullAvailPageFile / 1024**3
            if _free_gb < 4:
                print(f"  embeddings only {_free_gb:.1f} GB of memory left for the model (needs about 4)")
                print("             raise the Windows page file, or set BIDREADY_FORCE_EMBEDDINGS=1")
                raise ImportError("not enough free memory")

        # Pinned to CPU here regardless of what the machine has. A GPU that is
        # out of memory or driver-mismatched terminates the process in native
        # code partway through a request, which presents as the server simply
        # vanishing — far worse to diagnose than a few seconds of extra latency.
        env.setdefault("BIDREADY_EMBEDDING_DEVICE", "cpu")
        print("  embeddings local model on CPU: semantic evidence matching enabled")
    except ImportError:
        env["BIDREADY_EMBEDDING_PROVIDER"] = "null"
        print("  embeddings NO MODEL: semantic matching disabled, keyword only")
        print("             evidence that shares no words with a requirement will not be found.")
        print(f"             (If the library is missing: {sys.executable} -m pip install sentence-transformers)")

    run_step("migrate    schema up to date (as owner)",
             [sys.executable, "-m", "alembic", "upgrade", "head"], env)
    run_step("seed       demo organization ready",
             [sys.executable, "-m", "app.db.seed"], env)

    os.environ.update(env)
    if args.no_ui or not (UI_OUT / "index.html").is_file():
        print(f"\n  API only   http://{HOST}:{args.port}/api/v1/docs")
    else:
        print(f"\n  Open       http://{HOST}:{args.port}/intake/")
    print(f"  API docs   http://{HOST}:{args.port}/api/v1/docs")
    print("  Sign in    admin@acme-cyber.example.com / ChangeMe-Dev-2026\n")

    import uvicorn

    # Everything the console shows also goes to a file. Without this, the only
    # record of a failure is a terminal window that gets closed, which makes
    # "check the log" impossible to answer.
    #
    # One handler, on the root logger. uvicorn's own loggers propagate to root,
    # so also adding the handler to uvicorn's dictConfig writes every line
    # twice — which is worse than no log, because a duplicated log reads like
    # the request actually happened twice.
    import logging
    import logging.handlers
    from copy import deepcopy

    log_path = ROOT / "run_local.log"
    # Rotate once at startup, before any handler holds the file. Rotating while
    # running fails on Windows (WinError 32): the root handler and uvicorn's
    # handler both have the file open, so neither can rename it.
    try:
        if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
            log_path.replace(log_path.with_name("run_local.log.1"))
    except OSError:
        pass
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(file_handler)
    logging.getLogger().setLevel(logging.INFO)

    # uvicorn re-applies its own dictConfig at startup, which replaces the
    # handlers on its loggers — so a handler attached to them here would be
    # silently discarded. It has to go in through log_config instead. Its
    # loggers also carry propagate=False, so the root handler above never sees
    # them: without this the file would hold the application's tracebacks and
    # none of the HTTP status lines that give them context.
    log_config = deepcopy(uvicorn.config.LOGGING_CONFIG)
    log_config["formatters"]["logfile"] = {
        "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
    }
    log_config["handlers"]["logfile"] = {
        "class": "logging.FileHandler",
        "filename": str(log_path),
        "formatter": "logfile",
        "encoding": "utf-8",
    }
    # Only "uvicorn.error" and "uvicorn.access": the bare "uvicorn" logger
    # carries the same startup records as "uvicorn.error", so listing both
    # writes the banner lines twice.
    for name in ("uvicorn.error", "uvicorn.access"):
        entry = log_config["loggers"].setdefault(name, {"level": "INFO"})
        # uvicorn.error carries no "handlers" key of its own.
        entry.setdefault("handlers", []).append("logfile")
        # ...and it propagates to root by default, where the handler added
        # above would write the same record a second time.
        entry["propagate"] = False

    print(f"  log        {log_path}\n")

    try:
        uvicorn.run(
            "app.main:app",
            host=HOST,
            port=args.port,
            log_level="info",
            log_config=log_config,
        )
    finally:
        if server is not None:
            server.cleanup()


if __name__ == "__main__":
    main()
