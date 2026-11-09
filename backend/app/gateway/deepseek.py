"""DeepSeek implementation of LLMProvider.

Credentials come only from DEEPSEEK_API_KEY in the process environment or the
ignored local `.env` loaded into application settings. Model id, temperature,
timeout and retry policy are configuration. No other module constructs a
request to this vendor, and the key is never stored in source or the database.
"""

from __future__ import annotations

import time

import httpx

from app.core.config import get_settings
from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.gateway.base import LLMProvider, LLMResponse, Message

log = get_logger("gateway.deepseek")


class DeepSeekProvider(LLMProvider):
    def __init__(self, api_key: str | None = None, *, client: httpx.Client | None = None) -> None:
        s = get_settings()
        self._api_key = api_key or s.deepseek_api_key
        self._base_url = s.llm_base_url.rstrip("/")
        self._model = s.llm_model
        self._temperature = s.llm_temperature
        self._timeout = s.llm_timeout_seconds
        self._max_retries = s.llm_max_retries
        self._max_tokens = s.llm_max_output_tokens
        self._client = client

    @property
    def model_id(self) -> str:
        return self._model

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self._base_url, timeout=self._timeout)
        return self._client

    def generate(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResponse:
        if not self._api_key:
            raise ProviderError(
                "DEEPSEEK_API_KEY is not configured. Set it in backend/.env or the process "
                "environment; it is never stored in source or the database."
            )
        payload = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": self._temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self._max_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None
        started = time.monotonic()
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = self._http().post(
                    "/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=timeout or self._timeout,
                )
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise ProviderError(f"Provider returned {resp.status_code}.")
                if resp.status_code >= 400:
                    raise ProviderError(f"Provider rejected the request ({resp.status_code}).")
                data = resp.json()
                usage = data.get("usage", {}) or {}
                choice = (data.get("choices") or [{}])[0]
                return LLMResponse(
                    text=data["choices"][0]["message"]["content"],
                    finish_reason=choice.get("finish_reason"),
                    model_id=data.get("model", self._model),
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                    latency_ms=int((time.monotonic() - started) * 1000),
                    attempts=attempt,
                    raw=data,
                )
            except (httpx.HTTPError, ProviderError) as exc:
                last_error = exc
                if attempt < self._max_retries:
                    time.sleep(min(2 ** (attempt - 1) * 0.5, 8.0))
                    continue
        log.error("llm_call_failed", attempts=self._max_retries)
        raise ProviderError(f"DeepSeek call failed after {self._max_retries} attempts.") from (
            last_error
        )

    def chat(self, messages: list[Message], *, temperature: float | None = None) -> LLMResponse:
        return self.generate(messages, temperature=temperature)


class NullLLMProvider(LLMProvider):
    """Deterministic stand-in for tests and for running without a key."""

    model_id = "null"

    def __init__(self, canned: str = "{}") -> None:
        self.canned = canned
        self.calls: list[list[Message]] = []

    def generate(self, messages, *, temperature=None, max_tokens=None, timeout=None):
        self.calls.append(messages)
        return LLMResponse(text=self.canned, model_id=self.model_id, attempts=1, latency_ms=0)

    def chat(self, messages, *, temperature=None):
        return self.generate(messages)
