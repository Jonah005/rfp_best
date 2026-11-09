"""`python run_local.py` on its own must start the app, not explain itself.

That is what a person types. On Windows it used to fail -- pgserver has no
Windows build, so with no `--database-url` the script printed a wall of help
about Docker containers and exited, and the real command had to be found in a
chat log every time:

    python run_local.py --database-url postgresql://postgres:PASSWORD@localhost:5432/bidready

The password is the only part of that a machine cannot work out for itself. So
it is asked for once, and remembered in backend/.local/database-url -- a file,
not a constant in this script, because the script is committed and the URL
carries a password. `.local/` is already gitignored.

These tests read the source rather than running it: the branch only executes on
win32, and the property worth protecting is that the password never enters a
tracked file.
"""

from __future__ import annotations

import re
from pathlib import Path

RUN_LOCAL = Path(__file__).resolve().parents[3] / "run_local.py"


def source() -> str:
    return RUN_LOCAL.read_text(encoding="utf-8")


def test_the_script_is_where_the_tests_think_it_is():
    """Otherwise every assertion below passes against an empty string."""
    assert RUN_LOCAL.exists(), RUN_LOCAL
    assert "def start_postgres" in source()


def test_no_password_is_written_into_the_committed_script():
    """The whole reason the URL lives in a file. A password here would be in
    git history for good, and this project keeps local credentials out of
    committed files by rule."""
    text = source()
    # A postgres URL with a password filled in, as opposed to a placeholder.
    for match in re.finditer(r"postgresql://([^:\s]+):([^@\s]+)@", text):
        password = match.group(2)
        assert password in {
            "PASSWORD",
            "pw",
            "YOUR_PASSWORD",
            "bidready",
        }, f"a real-looking password is committed in run_local.py: {password}"


def test_the_remembered_url_is_read_before_anything_is_asked():
    text = source()
    assert "remembered = remembered_database_url()" in text
    ask = text.index("remembered = ask_for_database_url()")
    read = text.index("remembered = remembered_database_url()")
    assert read < ask, "ask only when there is nothing remembered"


def test_an_explicit_url_still_wins():
    """--database-url and BIDREADY_LOCAL_DATABASE_URL must override the file;
    the remembering is a convenience, not a new source of truth."""
    # Inside start_postgres, not the whole file: the helper is DEFINED above
    # it and CALLED below the early return, and only the call order matters.
    text = source()
    body = text[text.index("def start_postgres(") :]
    assert body.index("if database_url:") < body.index("remembered_database_url()")
    assert 'default=os.environ.get("BIDREADY_LOCAL_DATABASE_URL")' in text


def test_the_prompt_is_skipped_when_nobody_is_there_to_answer():
    """A scheduled run, a pipe, a service wrapper: hanging on a password
    prompt nobody can see is worse than failing with the command to use."""
    text = source()
    assert "if not sys.stdin.isatty():" in text


def test_the_saved_file_is_inside_the_gitignored_local_directory():
    text = source()
    assert 'LOCAL_URL_FILE = LOCAL / "database-url"' in text
    assert 'LOCAL = BACKEND / ".local"' in text


def test_the_password_is_escaped_before_it_goes_in_a_url():
    """A password with @ or / in it builds a URL that parses as a different
    host, and the failure reads as a wrong password."""
    text = source()
    assert "from urllib.parse import quote" in text
    assert "quote(password, safe='')" in text


def test_the_help_says_how_to_be_asked_again():
    """The recoverable mistake: the prompt answered with the wrong password,
    remembered, and every later run failing to connect. It is the FIRST thing
    the failure help says, because the version that buried it under a Docker
    example sent a real run to a port with nothing on it."""
    text = source()
    assert "delete it to be asked again" in text
    help_text = text[text.index('DB_HELP = """') :]
    assert help_text.index("delete the remembered URL") < help_text.index("docker run")


def test_a_password_is_tried_before_it_is_remembered():
    """The first version saved the answer and let the normal connection path
    fail: a wrong password written to disk as though it were right, and the
    failure arriving as a screen of help. Check, then save."""
    text = source()
    body = text[text.index("def ask_for_database_url(") : text.index("def start_postgres(")]
    assert body.index("why_not(") < body.index("save_database_url(url)")
    assert "if problem is None:" in body


def test_a_refused_password_is_asked_again_rather_than_explained():
    text = source()
    body = text[text.index("def ask_for_database_url(") : text.index("def start_postgres(")]
    assert "for attempt in range(1, ATTEMPTS + 1):" in body
    assert "Try again" in body


def test_a_failure_that_is_not_the_password_gives_up_at_once():
    """Nothing listening, no such database: three goes at the password cannot
    fix those, and pretending otherwise hides the real advice."""
    text = source()
    assert "def looks_like_a_wrong_password(reason: str) -> bool:" in text
    assert "That is not a password problem." in text


def test_a_remembered_url_that_stopped_working_leads_back_to_the_prompt():
    text = source()
    body = text[text.index("def start_postgres(") :]
    assert "ask_for_database_url(reason=problem)" in body


def test_the_project_env_file_is_where_the_url_can_live():
    """backend/.env is already gitignored and already holds the DeepSeek key and
    the application's own database URL. A second, separate place for one more
    local secret is a place to forget about, so this reads that file too -- and
    it wins over the file the prompt writes, because .env is the one a person
    edits on purpose."""
    text = source()
    assert 'ENV_FILE = BACKEND / ".env"' in text
    assert 'ENV_KEY = "BIDREADY_LOCAL_DATABASE_URL"' in text
    body = text[
        text.index("def remembered_database_url(") : text.index("def ask_for_database_url(")
    ]
    assert body.index("url_from_env_file()") < body.index("LOCAL_URL_FILE.read_text")


def test_the_env_file_is_parsed_without_a_dependency():
    """This script runs before anything is installed."""
    text = source()
    body = text[text.index("def url_from_env_file(") : text.index("def remembered_database_url(")]
    assert "import dotenv" not in body and "from dotenv" not in body
    assert 'line.partition("=")' in body
    # Comments and unrelated keys are left alone.
    assert 'line.startswith("#")' in body
    assert "if key.strip() != ENV_KEY:" in body


def test_the_output_names_which_source_was_used():
    """Two places it can come from; being told which one saves the next hour of
    editing the file that is not being read."""
    text = source()
    assert 'f"{ENV_KEY} in backend/.env" if url_from_env_file() else str(LOCAL_URL_FILE)' in text
