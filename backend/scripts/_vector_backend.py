"""Make a standalone script agree with the database about embedding storage.

## The bug this removes

`settings.vector_backend` picks the storage type for every embedding column:

    app/models/documents.py:58
        if get_settings().vector_backend == "portable":
            return ARRAY(Float(precision=24))
        return Vector(EMBEDDING_DIM)

It defaults to `"pgvector"`, it is read from the environment, and -- this is the
part that bites -- it is resolved ONCE AT IMPORT, because "the schema and the
ORM must agree, and a column type that changed underneath a running process
would be far worse than one chosen at startup." That reasoning is right.

`run_local.py` sets the variable. A script run directly does not, so it builds
its ORM with pgvector columns against a database whose columns are real[], and
the mismatch surfaces at the first write or read as one of:

    malformed array literal: "[-0.00050508,...]"      -- writing a vector into real[]
    AttributeError: 'list' object has no attribute 'split'   -- reading real[] as a vector

Both have now cost a debugging round. The second one broke
`check_evidence_units.py`; the first killed the fixture seeder on its first run
after the comment explaining it had already been written, in
`_export_queries.py`, and not applied anywhere else. Fix the pattern, not the
instance -- so this lives in one importable place.

## Why detection rather than a hardcoded value

Setting `BIDREADY_VECTOR_BACKEND=portable` in each script would work today and
would be wrong the moment somebody provisions a database with pgvector. The
database already knows which it is; asking it cannot drift.

`retrieval._detect_vector_backend` does the same job at query time, but it
cannot help here: by the time anything under `app/` is importable the column
types are already fixed. So this uses a bare SQLAlchemy engine, with no `app`
import at all, and must be called BEFORE the first `app.models` import.

## Use

    import os, sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    os.environ.setdefault("BIDREADY_DATABASE_URL", ...)

    from _vector_backend import align_vector_backend
    align_vector_backend()          # <- before any app.models import

    from app.models import ...
"""

from __future__ import annotations

import os

# `udt_name` for the embedding column, by backend. A pgvector column reports
# the extension type by name; a real[] column reports the element type with a
# leading underscore, which is Postgres's internal spelling for "array of".
_PORTABLE_UDTS = {"_float4", "_float8", "_numeric"}


def detect(database_url: str | None = None) -> str | None:
    """Ask the database which storage the embedding column actually uses.

    Returns "pgvector", "portable", or None when the question cannot be
    answered -- an unreachable database, or a schema with no embedding table
    yet (a fresh database before `alembic upgrade head`). None means "do not
    touch the environment": guessing here would replace a clear connection
    error with a confusing type error later.
    """
    from sqlalchemy import create_engine, text

    url = database_url or os.environ.get("BIDREADY_DATABASE_URL")
    if not url:
        return None
    try:
        engine = create_engine(url, pool_pre_ping=False)
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT udt_name FROM information_schema.columns "
                    "WHERE table_name = 'embedding' AND column_name = 'vector'"
                )
            ).first()
    except Exception:
        # Deliberately broad, and deliberately silent. This helper runs before
        # the application's own error handling exists, and a script that cannot
        # reach the database should fail on its own connection with its own
        # message rather than on this probe.
        return None
    finally:
        try:
            engine.dispose()
        except Exception:
            pass
    if row is None:
        return None
    udt = (row[0] or "").lower()
    if udt in _PORTABLE_UDTS:
        return "portable"
    if udt == "vector":
        return "pgvector"
    return None


def align_vector_backend(*, verbose: bool = False) -> str | None:
    """Set BIDREADY_VECTOR_BACKEND to match the live schema. Call before any
    `app.models` import.

    An explicit value already in the environment is left alone: somebody who
    set it meant it, and silently overriding a deliberate choice is how a
    debugging session loses an hour. The mismatch is still reported when
    `verbose`, because that combination -- a stated backend the database
    disagrees with -- is worth seeing.
    """
    detected = detect()
    stated = os.environ.get("BIDREADY_VECTOR_BACKEND")
    if stated:
        if verbose and detected and detected != stated:
            print(
                f"  ! BIDREADY_VECTOR_BACKEND={stated} but the database stores "
                f"embeddings as {detected}; leaving your setting alone."
            )
        return stated
    if detected is None:
        return None
    os.environ["BIDREADY_VECTOR_BACKEND"] = detected
    if verbose:
        print(f"vector backend: {detected} (detected from the embedding column)")
    return detected
