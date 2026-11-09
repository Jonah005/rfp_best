"""Embedding providers.

The v1 model is a local multilingual sentence encoder, so indexing never sends
company text to an external vendor and re-embedding the corpus never depends on
one. Its width is locked to the schema constant.
"""

from __future__ import annotations

import hashlib
import struct

from app.core.config import EMBEDDING_DIM, get_settings
from app.gateway.base import EmbeddingProvider

# The loaded model, shared by every provider instance in the process.
#
# `build_gateway()` constructs a fresh provider on each call, and assessment
# calls it once per requirement — so a 55-requirement bid loaded a 1.1 GB model
# 55 times. Observed live: three loads in nine seconds, then the process died
# under the memory churn and the browser reported a request that never
# completed. The weights are read-only and thread-safe to share, and the key
# includes the device so switching it in a test still reloads.
_MODEL_CACHE: dict[tuple[str, str], object] = {}


class LocalEmbeddingProvider(EmbeddingProvider):
    """sentence-transformers, loaded once per process and shared."""

    def __init__(self, model_name: str | None = None, device: str | None = None) -> None:
        s = get_settings()
        self.model_id = model_name or s.embedding_model
        self.device = device or s.embedding_device
        self._model = None
        self.dimension = EMBEDDING_DIM

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            # The device is named, never inferred. Left to itself
            # sentence-transformers picks cuda:0 when any GPU is visible, and a
            # GPU that is out of memory, driver-mismatched or busy takes the
            # whole process down inside native code — no Python traceback, the
            # server simply exits mid-request. That was observed: the embedder
            # loaded onto cuda:0, encoded one batch, and the process ended.
            #
            # CPU is the default because this workload is a few hundred short
            # excerpts, where the difference is seconds, and a slow answer beats
            # a dead server. Set BIDREADY_EMBEDDING_DEVICE=cuda deliberately.
            key = (self.model_id, self.device)
            cached = _MODEL_CACHE.get(key)
            if cached is None:
                cached = SentenceTransformer(self.model_id, device=self.device)
                _MODEL_CACHE[key] = cached
            self._model = cached
            # Renamed in sentence-transformers 6; the old name still works but
            # warns. Try the new one first and fall back for older versions.
            # Not getattr(obj, new, obj.old): the default is evaluated eagerly,
            # so that form still requires the old attribute to exist and raises
            # on any version that has actually removed it.
            if hasattr(self._model, "get_embedding_dimension"):
                width = int(self._model.get_embedding_dimension())
            else:
                width = int(self._model.get_sentence_embedding_dimension())
            self.dimension = width
            self.assert_dimension()
        return self._model

    def embed(self, texts: list[str], *, kind: str = "passage") -> list[list[float]]:
        model = self._load()
        vectors = model.encode(self._prepare(texts, kind), normalize_embeddings=True)
        return [list(map(float, v)) for v in vectors]

    def _prepare(self, texts: list[str], kind: str) -> list[str]:
        """Apply the model's expected input convention.

        The e5 family is trained with "query: " and "passage: " prefixes and is
        explicit that both must be used. Omitting them does not merely lose a
        little accuracy — it collapses the useful range of the similarity
        scores, so unrelated documents sit close together and a threshold
        chosen for one corpus means nothing on another. Anything asymmetric is
        worse still: comparing an unprefixed query against unprefixed passages
        is a different space from the one the model was trained in.

        Applied only to models that ask for it. A prefix on a model that does
        not expect one is just noise in the input.
        """
        if "e5" not in self.model_id.lower():
            return texts
        prefix = "query: " if kind == "query" else "passage: "
        return [prefix + text for text in texts]


class HashingEmbeddingProvider(EmbeddingProvider):
    """Deterministic offline stand-in.

    Used by tests and by environments without the model weights. It is NOT a
    semantic encoder -- it exists so wiring, dimensions and storage paths can be
    exercised without a multi-hundred-megabyte download.
    """

    model_id = "hashing-stub-v1"

    def __init__(self, dimension: int = EMBEDDING_DIM) -> None:
        self.dimension = dimension

    def embed(self, texts: list[str], *, kind: str = "passage") -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha512(text.encode("utf-8")).digest()
            raw = (digest * ((self.dimension * 4) // len(digest) + 1))[: self.dimension * 4]
            values = struct.unpack(f"{self.dimension}i", raw)
            norm = sum(v * v for v in values) ** 0.5 or 1.0
            out.append([v / norm for v in values])
        return out
