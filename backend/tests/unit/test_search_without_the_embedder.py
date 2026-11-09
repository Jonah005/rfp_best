"""Search must not die because the embedding model cannot load on this machine.

Observed on the user's Windows machine: `OSError: The paging file is too small
for this operation to complete`, raised inside the model load, turned every
feature that searches (proposing an assumption, the copilot) into a 500.
"""

from __future__ import annotations

from app.services import retrieval


class _Broken:
    model_id = "broken"

    def embed_one(self, text):
        raise OSError("The paging file is too small for this operation to complete.")


def test_an_embedder_that_cannot_load_means_keyword_only_not_an_error(monkeypatch):
    monkeypatch.setattr(retrieval, "_EMBEDDER_UNUSABLE", False)
    service = retrieval.RetrievalService.__new__(retrieval.RetrievalService)
    assert service._vector_candidates("deadline", None, 10, _Broken()) == []
    assert retrieval._EMBEDDER_UNUSABLE is True
    # And it is not retried: a second call returns at once without touching the model.
    assert service._vector_candidates("deadline", None, 10, _Broken()) == []
    monkeypatch.setattr(retrieval, "_EMBEDDER_UNUSABLE", False)
