"""A withdrawn document's excerpts are not evidence.

The company corpus holds 610 evidence rows over 28 document records for 14
files -- one live record and one WITHDRAWN record per file, with 304 of the 610
rows hanging off the withdrawn half. Every shortlist was drawing from a pool
that was close to half retracted material, and nothing downstream showed it: a
withdrawn duplicate scores exactly as well as its live twin, ranks beside it,
and renders identically in the drafter's context.

`Document.withdrawn_at.is_(None)` was already applied in `precedence.py`,
`requirements.py`, `tender_analysis.py` and the knowledge API. Evidence
retrieval is the fifth place that needs it and was the one that did not have
it -- the same shape as the 25 dangling requirement codes that reappeared a day
after being fixed, because one of two scripts doing the same job was looked at
and the other was not.

These assert on the COMPILED SQL rather than on the source of `_admissible`.
A test in this project once grepped a function's source for a field name and
kept passing after the field was dropped from the output; reading the statement
the database will actually receive is the difference between checking that a
predicate is written and checking that it is applied.

The behavioural test -- ingest, withdraw, confirm the excerpt stops being a
candidate -- belongs beside the other Postgres tests in tests/integration, and
the routine command here is `pytest tests/unit`. This is the part that runs on
every commit.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models import Evidence


def _sql() -> str:
    """The admissibility predicate as the database receives it."""
    from app.services.assessment import _admissible

    statement = select(Evidence.id).where(*_admissible())
    return str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}
        )
    ).lower()


def test_admissibility_reaches_the_document_it_came_from() -> None:
    """Not just "a predicate mentioning documents" -- the withdrawal check
    itself, resolved against the document table."""
    sql = _sql()
    assert "withdrawn_at" in sql
    assert "document" in sql


def test_the_check_is_for_not_withdrawn_and_not_merely_present() -> None:
    """`withdrawn_at IS NOT NULL` would compile, mention the column, and admit
    exactly the wrong half of the corpus."""
    sql = _sql()
    assert "withdrawn_at is null" in sql
    assert "withdrawn_at is not null" not in sql


def test_the_subquery_carries_the_tenant_boundary_itself() -> None:
    """Six queries splice these predicates into their own WHERE clause. If the
    correlation on org_id were dropped, admissibility would depend on each
    caller restating it, and the one that forgot would cross tenants -- under
    RLS FORCE that reads as an empty result rather than an error, which is the
    failure this codebase is built to prevent."""
    sql = _sql()
    assert "org_id" in sql


def test_the_earlier_predicates_survived() -> None:
    """Adding the third must not have displaced the first two: an approval
    check silently dropped would admit every PROPOSED row in the corpus."""
    sql = _sql()
    assert "approval_status" in sql
    assert "status in" in sql or "status = any" in sql
