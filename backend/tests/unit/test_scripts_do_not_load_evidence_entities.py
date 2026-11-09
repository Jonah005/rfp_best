"""No script may `select(Evidence)`.

`Evidence.excerpt_vector` chooses its column type at import time from
`settings.vector_backend`. Loading whole Evidence entities runs pgvector's
result converter over every row, and against the portable backend that converter
receives a Python list and calls `.split()` on it:

    AttributeError: 'list' object has no attribute 'split'

raised from deep inside SQLAlchemy's cython result proxy, in a script whose only
ambition was to read some text. The traceback names neither Evidence nor the
setting, so the cause is not visible from the failure.

This has now happened twice -- once in `export_matrix.py`, then again in
`quarantine_fake_evidence.py`, written after the first was fixed and the reason
written down. A rule that lives only in a comment in the file that already
learned it does not reach the next file.

The fix in both cases was to select named columns and to write with an `update()`
statement rather than by assigning to ORM attributes.

This is a source-text check, which is a weak kind of test: it constrains how the
code is written rather than what it does, and a script could still load
Evidence by some spelling this misses. It earns its place anyway, because the
failure mode is mechanical, the correct form is a one-line habit, and nothing
else catches it until someone runs the script against a live database.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

# `select(Evidence)` and `select(Evidence,` -- the entity, alone or first.
# `select(Evidence.id)` and friends are exactly what this asks for instead.
WHOLE_ENTITY = re.compile(r"select\s*\(\s*Evidence\s*[,)]")


def script_files() -> list[Path]:
    return sorted(p for p in SCRIPTS.glob("*.py") if p.name != "__init__.py")


def code_only(source: str) -> str:
    """The source with comments and string literals removed.

    Written after the first version of this test failed on the comment that
    explains the rule -- "NAMED COLUMNS, never `select(Evidence)`" is the file
    doing the right thing and saying so, and a checker that cannot tell prose
    from code punishes exactly the person who documented the fix.
    """
    pieces: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        pieces.append(token.string)
    return " ".join(pieces)


def test_there_are_scripts_to_check():
    """Guard against the glob silently matching nothing, which would make every
    assertion below pass for the wrong reason."""
    assert len(script_files()) >= 5


@pytest.mark.parametrize("path", script_files(), ids=lambda p: p.name)
def test_a_script_selects_evidence_columns_not_evidence_rows(path: Path):
    match = WHOLE_ENTITY.search(code_only(path.read_text(encoding="utf-8")))
    assert match is None, (
        f"{path.name} loads whole Evidence entities. That runs pgvector's converter "
        "over excerpt_vector and fails with \"'list' object has no attribute 'split'\" "
        "against the portable backend. Select the named columns the script actually "
        "needs, and write with update() rather than attribute assignment."
    )
