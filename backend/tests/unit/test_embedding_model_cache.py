"""The embedding model loads once per process, not once per call.

Observed live: assessment calls `build_gateway()` once per requirement, and
`build_gateway()` constructs a fresh provider each time. Every one of those
loaded the 1.1 GB model from disk again — three loads in nine seconds in the
log, and on a 55-requirement bid the process died under the churn while the
browser reported a request that never completed.

The weights are read-only, so sharing them is safe; the cost of not sharing
them is the server.
"""

from __future__ import annotations

import sys
import types

import pytest

import app.gateway.embeddings as embeddings


class _FakeModel:
    """Counts how many times the weights were loaded."""

    loads = 0

    def __init__(self, *args, **kwargs) -> None:
        _FakeModel.loads += 1
        self.device = kwargs.get("device")

    def get_embedding_dimension(self) -> int:
        return 768

    def encode(self, texts, **kwargs):
        return [[0.0] * 768 for _ in texts]


class _OlderApiModel(_FakeModel):
    """sentence-transformers before the dimension getter was renamed."""

    def __getattr__(self, name):
        if name == "get_embedding_dimension":
            raise AttributeError(name)
        raise AttributeError(name)

    def get_sentence_embedding_dimension(self) -> int:
        return 768


@pytest.fixture
def fake_sentence_transformers(monkeypatch):
    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _FakeModel
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    _FakeModel.loads = 0
    embeddings._MODEL_CACHE.clear()
    yield module
    embeddings._MODEL_CACHE.clear()


def test_many_providers_share_one_loaded_model(fake_sentence_transformers):
    """This is the regression: one load, however many providers are built."""
    for _ in range(5):
        embeddings.LocalEmbeddingProvider().embed(["anything"])
    assert _FakeModel.loads == 1


def test_the_shared_model_really_is_the_same_object(fake_sentence_transformers):
    first = embeddings.LocalEmbeddingProvider()
    second = embeddings.LocalEmbeddingProvider()
    first.embed(["a"])
    second.embed(["b"])
    assert first._model is second._model


def test_a_different_device_gets_its_own_model(fake_sentence_transformers):
    """The cache key includes the device, so switching it still reloads."""
    embeddings.LocalEmbeddingProvider(device="cpu").embed(["a"])
    embeddings.LocalEmbeddingProvider(device="cuda").embed(["b"])
    assert _FakeModel.loads == 2


def test_the_device_is_passed_through_rather_than_left_to_autodetect(
    fake_sentence_transformers,
):
    """Autodetect picks cuda:0 and an unusable GPU kills the process."""
    provider = embeddings.LocalEmbeddingProvider(device="cpu")
    provider.embed(["a"])
    assert provider._model.device == "cpu"


def test_an_older_sentence_transformers_still_works(fake_sentence_transformers):
    """The dimension getter was renamed in 6.x; both names must be handled.

    `getattr(model, "new", model.old)` looks like it does this and does not:
    the default is evaluated eagerly, so it still requires the old attribute.
    """
    fake_sentence_transformers.SentenceTransformer = _OlderApiModel
    embeddings._MODEL_CACHE.clear()
    provider = embeddings.LocalEmbeddingProvider()
    assert provider.embed(["a"])
