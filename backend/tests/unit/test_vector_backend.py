"""Which distance operator this database can actually evaluate.

`vector_backend` is a setting that defaults to "pgvector", and the only thing
that ever corrected it was an environment variable the local launcher exported
into the process it started. So the application worked and everything else did
not:

    operator does not exist: real[] <=> unknown

That is what a script, a worker, or a deployment that forgot the variable got
the first time it searched -- at query time, from inside a request, with a
stack trace mentioning an operator nobody configured.

A configuration flag that must agree with the schema, with nothing checking
that it does, is a trap rather than a setting. The schema cannot be wrong about
its own column type, so it is asked instead, and the flag survives only as the
fallback for a database that cannot be introspected.
"""

from __future__ import annotations

import pytest

from app.services import retrieval
from app.services.retrieval import _BACKEND_BY_UDT, _detect_vector_backend


class _Bind:
    url = "postgresql://example/one"


class _Session:
    """A session that answers exactly one question, or refuses to."""

    def __init__(self, udt=None, fail=False):
        self._udt = udt
        self._fail = fail
        self.queries = 0

    def get_bind(self):
        return _Bind()

    def execute(self, _statement):
        self.queries += 1
        if self._fail:
            raise RuntimeError("information_schema is not readable here")
        return _Scalar(self._udt)


class _Scalar:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


@pytest.fixture(autouse=True)
def _clear_cache():
    retrieval._BACKEND_CACHE.clear()
    yield
    retrieval._BACKEND_CACHE.clear()


def test_a_pgvector_column_is_detected():
    assert _detect_vector_backend(_Session(udt="vector")) == "pgvector"


def test_a_real_array_column_is_detected():
    """The case that was failing. `real[]` is `_float4` to postgres, and it
    cannot evaluate `<=>`."""
    assert _detect_vector_backend(_Session(udt="_float4")) == "portable"


def test_a_double_precision_array_is_portable_too():
    assert _detect_vector_backend(_Session(udt="_float8")) == "portable"


def test_an_unknown_column_type_gives_no_answer():
    """Better to fall back to the configured value than to guess an operator."""
    assert _detect_vector_backend(_Session(udt="jsonb")) is None


def test_a_missing_table_gives_no_answer():
    assert _detect_vector_backend(_Session(udt=None)) is None


def test_detection_failure_is_not_fatal():
    """A database this cannot introspect is no worse off than before: the
    caller falls back to the setting, which is the old behaviour exactly."""
    assert _detect_vector_backend(_Session(fail=True)) is None


def test_the_answer_is_cached_per_database():
    """One query per database for the life of the process, not one per search."""
    session = _Session(udt="_float4")
    assert _detect_vector_backend(session) == "portable"
    assert _detect_vector_backend(session) == "portable"
    assert session.queries == 1


def test_a_failure_is_not_cached():
    """A transient failure must not pin the wrong answer for the process."""
    failing = _Session(fail=True)
    _detect_vector_backend(failing)
    assert retrieval._BACKEND_CACHE == {}


def test_the_search_path_asks_rather_than_trusting_the_setting():
    """The regression guard.

    If this goes back to reading `get_settings().vector_backend` directly, the
    trap is reopened and nothing else in this file would notice.
    """
    import inspect

    source = inspect.getsource(retrieval.RetrievalService._vector_candidates)
    assert "self._vector_backend()" in source
    assert "get_settings().vector_backend" not in source


def test_the_setting_remains_the_fallback():
    """Detection is an improvement on the setting, not a replacement for it."""
    import inspect

    source = inspect.getsource(retrieval.RetrievalService._vector_backend)
    assert "get_settings().vector_backend" in source
    assert "detected or configured" in source


def test_every_mapped_type_names_a_real_backend():
    assert set(_BACKEND_BY_UDT.values()) == {"pgvector", "portable"}
