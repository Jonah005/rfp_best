"""Model gateway interfaces.

Correction 3 of the first review pass: LLM and embeddings are SEPARATE
interfaces with separate configuration and separate failure handling. Nothing
outside this package speaks to a model provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.core.config import EMBEDDING_DIM


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class LLMResponse:
    text: str
    model_id: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    attempts: int = 1
    # Why the model stopped. "length" means the reply was cut off at the token
    # cap, which is the difference between "the model got the schema wrong" and
    # "the model never got to finish" — two failures with entirely different
    # fixes, indistinguishable from the truncated JSON alone.
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class LLMProvider(ABC):
    """Text generation. Never called directly by domain code -- see ModelGateway."""

    @abstractmethod
    def generate(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResponse: ...

    @abstractmethod
    def chat(self, messages: list[Message], *, temperature: float | None = None) -> LLMResponse: ...


class EmbeddingProvider(ABC):
    """Vector embeddings.

    `dimension` must equal the schema constant EMBEDDING_DIM: pgvector column
    widths are fixed at DDL time, so a mismatch is a startup failure rather than
    a mysterious insert error later.
    """

    dimension: int = EMBEDDING_DIM
    model_id: str = "unset"

    @abstractmethod
    def embed(self, texts: list[str], *, kind: str = "passage") -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def assert_dimension(self) -> None:
        if self.dimension != EMBEDDING_DIM:
            raise RuntimeError(
                f"Embedding provider '{self.model_id}' produces {self.dimension}-dimensional "
                f"vectors but the schema column is vector({EMBEDDING_DIM}). Changing model is a "
                "migration (see docs/05 section 14.4), not a configuration change."
            )
