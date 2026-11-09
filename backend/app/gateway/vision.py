"""Reading a picture. A separate provider from text generation, on purpose.

The text model cannot see. A vision model is a different vendor, a different
data-handling decision and a different cost, so it is configured separately,
off by default, and reached only through the gateway.
"""

from __future__ import annotations

import base64
import time
from abc import ABC, abstractmethod

import httpx

from app.core.config import get_settings
from app.core.errors import ProviderError
from app.gateway.base import LLMResponse


class VisionProvider(ABC):
    model_id: str = "unset"

    @abstractmethod
    def read(
        self,
        system: str,
        instruction: str,
        image: bytes,
        mime: str,
        *,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...


class NullVisionProvider(VisionProvider):
    """No vision model configured. Asking it to read says so plainly."""

    model_id = "none"

    def read(self, system, instruction, image, mime, *, max_tokens=None):
        raise ProviderError(
            "No vision model is configured, so pictures cannot be read. Set VISION_PROVIDER, "
            "BIDREADY_VISION_BASE_URL and BIDREADY_VISION_MODEL, and VISION_API_KEY in the "
            "environment. Meanwhile a diagram can be pasted as Mermaid or built by hand."
        )


class OpenAICompatibleVisionProvider(VisionProvider):
    def __init__(self, *, client: httpx.Client | None = None) -> None:
        s = get_settings()
        self._key = s.vision_api_key
        self._base = s.vision_base_url.rstrip("/")
        self.model_id = s.vision_model
        self._timeout = s.vision_timeout_seconds
        self._client = client

    def read(self, system, instruction, image, mime, *, max_tokens=None):
        if not self._key:
            raise ProviderError("VISION_API_KEY is not set in the environment.")
        if not self._base or not self.model_id:
            raise ProviderError("The vision base URL and model are not configured.")
        url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        payload = {
            "model": self.model_id,
            "temperature": 0,
            "max_tokens": max_tokens or 3000,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                },
            ],
        }
        started = time.monotonic()
        client = self._client or httpx.Client(base_url=self._base, timeout=self._timeout)
        try:
            resp = client.post(
                "/chat/completions" if self._client is None else f"{self._base}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._key}"},
            )
        except httpx.HTTPError as exc:
            raise ProviderError("The vision model could not be reached.") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"The vision model rejected the request ({resp.status_code}).")
        data = resp.json()
        usage = data.get("usage", {}) or {}
        return LLMResponse(
            text=data["choices"][0]["message"]["content"],
            model_id=data.get("model", self.model_id),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            latency_ms=int((time.monotonic() - started) * 1000),
            raw={},
        )


def build_vision() -> VisionProvider:
    if get_settings().vision_provider == "openai_compatible":
        return OpenAICompatibleVisionProvider()
    return NullVisionProvider()
