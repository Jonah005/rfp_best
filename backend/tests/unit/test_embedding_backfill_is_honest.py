"""A backfill that embedded nothing must not report success.

`backfill_evidence_vectors` exists for one purpose: turn excerpts into vectors.
It used to catch every exception from the embedding provider, log a warning and
return `{"embedded": 0, "remaining": <unchanged>}` with HTTP 200 — so a missing
model, undownloaded weights or a dimension mismatch all reached the user as
"Evidence embedding backfill completed." The only record of the truth was a
WARNING line in a server log, and pressing the button again produced the same
cheerful nothing.

That is the recurring defect in this codebase wearing another hat: the value
that decides the outcome exists, and the thing the user sees does not read it.

Two failure modes are covered, because they are different bugs:

    the provider raises        -> the caller is told, and told what to do
    the provider under-returns -> a short vector list is a bug, not a partial
                                  success to be reported as a count

The tests drive the service function rather than asserting on its source, so a
rewrite that keeps the behaviour keeps the tests.
"""

from __future__ import annotations

import pytest

from app.core.errors import ProviderError
from app.services import knowledge


class _Evidence:
    """Enough of an Evidence row for the loop under test."""

    def __init__(self, excerpt: str) -> None:
        self.excerpt = excerpt
        self.excerpt_vector = None


class _Session:
    """A session that yields rows once and then reports what is left.

    `remaining` is recomputed from the rows themselves, so a test cannot pass
    by having the function report a number nobody checked.
    """

    def __init__(self, rows: list[_Evidence]) -> None:
        self.rows = rows
        self.flushed = False

    def execute(self, _statement):
        session = self

        class _Result:
            def scalars(self_inner):
                return iter(session.rows)

            def scalar_one(self_inner):
                return sum(1 for row in session.rows if row.excerpt_vector is None)

        return _Result()

    def flush(self) -> None:
        self.flushed = True


class _Ctx:
    org_id = "org"


@pytest.fixture
def rows() -> list[_Evidence]:
    return [_Evidence("ISO 27001 certified since 2019"), _Evidence("CREST accredited")]


def test_a_provider_failure_is_raised_not_swallowed(monkeypatch, rows):
    """The whole bug in one assertion: the caller asked for embedding and the
    embedding did not happen, so the caller must hear about it."""

    class _Broken:
        def embed(self, _texts, **_kwargs):
            raise RuntimeError("No module named 'sentence_transformers'")

    monkeypatch.setattr(knowledge, "build_gateway", lambda: _Broken())

    with pytest.raises(ProviderError) as raised:
        knowledge.backfill_evidence_vectors(_Session(rows), _Ctx(), limit=10)

    message = str(raised.value)
    # The underlying cause travels with it. "Something went wrong" sends the
    # reader back to the log this error exists to replace.
    assert "sentence_transformers" in message
    # And it says what the failure costs, because the system is still usable
    # without embeddings — lexical matching keeps working.
    assert "shared words" in message


def test_nothing_is_half_written_when_the_provider_fails(monkeypatch, rows):
    """No vector may be assigned when the batch call raised: a partially
    embedded corpus with no error is worse than an unembedded one."""

    class _Broken:
        def embed(self, _texts, **_kwargs):
            raise RuntimeError("weights not found")

    monkeypatch.setattr(knowledge, "build_gateway", lambda: _Broken())
    with pytest.raises(ProviderError):
        knowledge.backfill_evidence_vectors(_Session(rows), _Ctx(), limit=10)
    assert all(row.excerpt_vector is None for row in rows)


def test_a_short_vector_list_is_an_error_not_a_partial_success(monkeypatch, rows):
    """`zip(..., strict=False)` would embed the first row, leave the second and
    report `embedded: 1` as though that were the job finishing. The count would
    look plausible and the corpus would be quietly half-matched."""

    class _Short:
        def embed(self, texts, **_kwargs):
            return [[0.1, 0.2]][: len(texts) - 1]

    monkeypatch.setattr(knowledge, "build_gateway", lambda: _Short())
    with pytest.raises(ValueError):
        knowledge.backfill_evidence_vectors(_Session(rows), _Ctx(), limit=10)


def test_a_working_provider_embeds_every_row_and_reports_zero_remaining(monkeypatch, rows):
    """The success path still has to work, and `remaining` has to reflect the
    rows rather than being asserted independently."""

    class _Working:
        def embed(self, texts, **_kwargs):
            return [[float(len(text))] for text in texts]

    monkeypatch.setattr(knowledge, "build_gateway", lambda: _Working())
    result = knowledge.backfill_evidence_vectors(_Session(rows), _Ctx(), limit=10)

    assert result == {"embedded": 2, "remaining": 0}
    assert all(row.excerpt_vector is not None for row in rows)


def test_an_empty_excerpt_is_skipped_rather_than_embedded(monkeypatch):
    """Embedding whitespace produces a vector that matches nothing meaningfully
    and hides the row from this backfill for ever, because it is no longer
    NULL."""
    blank = _Evidence("   ")
    real = _Evidence("ISO 27001")

    class _Working:
        def embed(self, texts, **_kwargs):
            assert texts == ["ISO 27001"], "a blank excerpt reached the model"
            return [[1.0]]

    monkeypatch.setattr(knowledge, "build_gateway", lambda: _Working())
    result = knowledge.backfill_evidence_vectors(_Session([blank, real]), _Ctx(), limit=10)

    assert result["embedded"] == 1
    assert blank.excerpt_vector is None
    # Still counted as remaining, honestly: it has no vector. A blank excerpt
    # is a data problem to notice, not a number to hide.
    assert result["remaining"] == 1
