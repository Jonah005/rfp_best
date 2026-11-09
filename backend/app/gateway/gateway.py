"""The model gateway: the single choke point for model calls.

Responsibilities that live here and nowhere else:
  * resolving a named call type to its prompt, schema and version,
  * schema validation with a bounded repair loop,
  * retries, timeouts and token limits,
  * usage accounting.

A structured call that cannot be made to satisfy its schema FAILS. Malformed
model output is never written to the system of record.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, TypeVar

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.context import ExecutionContext
from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.gateway.base import EmbeddingProvider, LLMProvider, LLMResponse, Message
from app.gateway.registry import CallType
from app.gateway.registry import get as get_call_type
from app.gateway.vision import NullVisionProvider, VisionProvider
from app.models import ModelResultCache, ModelUsage

log = get_logger("gateway")
T = TypeVar("T", bound=BaseModel)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


def _extract_json(text: str) -> str:
    match = _JSON_BLOCK.search(text)
    if match:
        return match.group(1)
    start = min(
        (i for i in (text.find("{"), text.find("[")) if i != -1),
        default=-1,
    )
    if start == -1:
        return text.strip()
    end = max(text.rfind("}"), text.rfind("]"))
    return text[start : end + 1] if end > start else text.strip()


class ModelGateway:
    def __init__(
        self,
        llm: LLMProvider,
        embeddings: EmbeddingProvider | None = None,
        *,
        max_repair_attempts: int = 2,
        vision: VisionProvider | None = None,
    ) -> None:
        self.llm = llm
        self.embeddings = embeddings
        self.max_repair_attempts = max_repair_attempts
        #: Reads pictures. Separate from `llm` because the text model cannot see.
        self.vision = vision
        #: Whether the most recent structured call was served from the cache.
        #: Read by callers that report cost; reset at the start of every call.
        self.last_call_cached = False

    # -- text ---------------------------------------------------------------
    def generate(
        self,
        call_type_name: str,
        user_content: str,
        *,
        ctx: ExecutionContext | None = None,
        session: Session | None = None,
        extra_system: str | None = None,
    ) -> LLMResponse:
        call_type = get_call_type(call_type_name)
        messages = self._build_messages(call_type, user_content, extra_system)
        started = time.monotonic()
        try:
            response = self.llm.generate(
                messages,
                temperature=call_type.temperature,
                max_tokens=call_type.max_output_tokens,
            )
        except Exception as exc:
            self._log_usage(
                session,
                ctx,
                call_type,
                None,
                succeeded=False,
                error_kind=type(exc).__name__,
                started=started,
            )
            raise
        self._log_usage(session, ctx, call_type, response, succeeded=True, started=started)
        return response

    # -- structured ---------------------------------------------------------
    def generate_structured(
        self,
        call_type_name: str,
        user_content: str,
        *,
        ctx: ExecutionContext | None = None,
        session: Session | None = None,
        extra_system: str | None = None,
        cache: bool = False,
    ) -> Any:
        """Call a structured call type, optionally reusing an identical answer.

        `cache=True` is for deterministic reads only — a call type at
        temperature zero whose answer depends on nothing but its prompt. It is
        opt-in per call site rather than a global setting, because the caller is
        the only one who knows whether repeating the question would repeat the
        answer.
        """
        call_type = get_call_type(call_type_name)
        if call_type.response_model is None:
            raise ValueError(f"Call type '{call_type_name}' has no response schema.")

        self.last_call_cached = False
        messages = self._build_messages(call_type, user_content, extra_system)
        cache_key = (
            self._cache_key(call_type, messages, user_content)
            if cache and session is not None and ctx is not None
            else None
        )
        if cache_key is not None and session is not None and ctx is not None:
            hit = self._cache_get(session, ctx, call_type, cache_key)
            if hit is not None:
                self.last_call_cached = True
                return hit
        messages.append(
            Message(
                role="system",
                content=(
                    "Respond with a single JSON object matching this schema. No prose, no "
                    "markdown fences.\n" + json.dumps(call_type.response_model.model_json_schema())
                ),
            )
        )

        started = time.monotonic()
        last_error: str | None = None
        response: LLMResponse | None = None

        for attempt in range(self.max_repair_attempts + 1):
            if attempt > 0:
                messages.append(
                    Message(
                        role="user",
                        content=(
                            "Your previous reply did not satisfy the schema. Error:\n"
                            f"{last_error}\nReturn only corrected JSON."
                        ),
                    )
                )
            try:
                response = self.llm.generate(
                    messages,
                    temperature=call_type.temperature,
                    max_tokens=call_type.max_output_tokens,
                )
            except Exception as exc:
                self._log_usage(
                    session,
                    ctx,
                    call_type,
                    None,
                    succeeded=False,
                    error_kind=type(exc).__name__,
                    started=started,
                )
                raise
            try:
                parsed = call_type.response_model.model_validate_json(_extract_json(response.text))
            except (PydanticValidationError, ValueError) as exc:
                last_error = str(exc)[:2000]
                # The reason belongs in the log, not only in the repair prompt.
                # Without it a failed call says "did not produce schema-valid
                # output" and nothing more, and the only way to learn which
                # field the model got wrong is to run the whole pass again with
                # a debugger attached.
                log.warning(
                    "structured_output_invalid",
                    call_type=call_type.name,
                    attempt=attempt + 1,
                    finish_reason=response.finish_reason,
                    output_chars=len(response.text),
                    output_tokens=response.output_tokens,
                    # Truncated: a validation error over a long array repeats
                    # per element, and the first few lines identify the field.
                    error=last_error[:400],
                )
                continue
            self._log_usage(
                session,
                ctx,
                call_type,
                response,
                succeeded=True,
                started=started,
                attempts=attempt + 1,
            )
            if cache_key is not None and session is not None and ctx is not None:
                self._cache_put(session, ctx, call_type, cache_key, parsed)
            return parsed

        self._log_usage(
            session,
            ctx,
            call_type,
            response,
            succeeded=False,
            error_kind="SchemaValidationFailed",
            started=started,
            attempts=self.max_repair_attempts + 1,
        )
        # A reply cut off at the token cap is not a schema disagreement, and
        # saying so saves the reader from hunting for a field that was never
        # wrong. The cap is the fix in that case, not the prompt.
        truncated = response is not None and response.finish_reason == "length"
        cause = (
            f"the reply was cut off at the {call_type.max_output_tokens}-token limit"
            if truncated
            else "the reply did not match the expected schema"
        )
        raise ProviderError(
            f"Call type '{call_type.name}' did not produce schema-valid output after "
            f"{self.max_repair_attempts + 1} attempts: {cause}. Nothing was written."
        )

    # -- pictures -----------------------------------------------------------
    def read_image(
        self,
        call_type_name: str,
        instruction: str,
        image: bytes,
        mime: str,
        *,
        ctx: ExecutionContext | None = None,
        session: Session | None = None,
    ) -> Any:
        """Have the vision model read one picture into a structured answer.

        Same discipline as text: a registered call type, a schema, one repair
        attempt, usage logged. The picture is untrusted content and the answer
        is data to be validated by the caller, never an instruction.
        """
        call_type = get_call_type(call_type_name)
        if call_type.response_model is None:
            raise ValueError(f"Call type '{call_type_name}' has no response schema.")
        vision = self.vision or NullVisionProvider()
        system = (
            call_type.system_prompt
            + "\nRespond with a single JSON object matching this schema. No prose, no "
            "markdown fences.\n" + json.dumps(call_type.response_model.model_json_schema())
        )
        started = time.monotonic()
        last_error = ""
        response: LLMResponse | None = None
        for attempt in range(2):
            text = (
                instruction
                if attempt == 0
                else (
                    instruction + f"\nYour previous reply did not satisfy the schema: {last_error}"
                )
            )
            try:
                response = vision.read(
                    system, text, image, mime, max_tokens=call_type.max_output_tokens
                )
            except Exception as exc:
                self._log_usage(
                    session,
                    ctx,
                    call_type,
                    None,
                    succeeded=False,
                    error_kind=type(exc).__name__,
                    started=started,
                    provider=vision,
                )
                raise
            try:
                parsed = call_type.response_model.model_validate_json(_extract_json(response.text))
            except (PydanticValidationError, ValueError) as exc:
                last_error = str(exc)[:600]
                continue
            self._log_usage(
                session,
                ctx,
                call_type,
                response,
                succeeded=True,
                started=started,
                attempts=attempt + 1,
                provider=vision,
            )
            return parsed
        self._log_usage(
            session,
            ctx,
            call_type,
            response,
            succeeded=False,
            error_kind="SchemaValidationFailed",
            started=started,
            attempts=2,
            provider=vision,
        )
        raise ProviderError(
            "The vision model's reply could not be understood. Nothing was written."
        )

    # -- result cache -------------------------------------------------------
    @staticmethod
    def _cache_key(call_type: CallType, messages: list[Message], user_content: str) -> str:
        """Hash the whole request, not a summary of it.

        Every system message is included as well as the user content, so an
        `extra_system` block — which is how a caller narrows what the model may
        use — cannot be silently dropped from the key and answered from a wider
        context's cached reply.
        """
        digest = hashlib.sha256()
        for message in messages:
            digest.update(message.role.encode())
            digest.update(b"\x00")
            digest.update(message.content.encode())
            digest.update(b"\x00")
        digest.update(user_content.encode())
        return digest.hexdigest()

    def _cache_get(
        self, session: Session, ctx: ExecutionContext, call_type: CallType, key: str
    ) -> BaseModel | None:
        model = call_type.response_model
        if model is None:
            return None
        try:
            row = session.execute(
                select(ModelResultCache).where(
                    ModelResultCache.org_id == ctx.org_id,
                    ModelResultCache.call_type == call_type.name,
                    ModelResultCache.prompt_version == call_type.prompt_version,
                    ModelResultCache.input_sha256 == key,
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            parsed = model.model_validate(row.response)
        except Exception as exc:  # noqa: BLE001 - a cache is never load-bearing
            # Includes the case where a stored reply no longer satisfies a
            # schema that has since changed. Treated as a miss: the fresh call
            # is always correct, and failing here would break assessment to
            # protect an optimisation.
            log.warning("model_cache_read_failed", reason=type(exc).__name__)
            return None
        row.last_used_at = datetime.now(timezone.utc)
        log.info("model_cache_hit", call_type=call_type.name, version=call_type.prompt_version)
        return parsed

    def _cache_put(
        self,
        session: Session,
        ctx: ExecutionContext,
        call_type: CallType,
        key: str,
        parsed: BaseModel,
    ) -> None:
        try:
            session.add(
                ModelResultCache(
                    org_id=ctx.org_id,
                    call_type=call_type.name,
                    prompt_version=call_type.prompt_version,
                    input_sha256=key,
                    response=parsed.model_dump(mode="json"),
                )
            )
            session.flush()
        except Exception as exc:  # noqa: BLE001 - losing a cache write costs money, not correctness
            log.warning("model_cache_write_failed", reason=type(exc).__name__)

    # -- embeddings ---------------------------------------------------------
    def embed(self, texts: list[str], *, kind: str = "passage") -> list[list[float]]:
        if self.embeddings is None:
            raise ProviderError("No embedding provider is configured.")
        return self.embeddings.embed(texts, kind=kind)

    # -- internals ----------------------------------------------------------
    @staticmethod
    def _build_messages(
        call_type: CallType, user_content: str, extra_system: str | None
    ) -> list[Message]:
        messages = [Message(role="system", content=call_type.system_prompt)]
        if extra_system:
            messages.append(Message(role="system", content=extra_system))
        messages.append(Message(role="user", content=user_content))
        return messages

    def _log_usage(
        self,
        session: Session | None,
        ctx: ExecutionContext | None,
        call_type: CallType,
        response: LLMResponse | None,
        *,
        succeeded: bool,
        started: float,
        error_kind: str | None = None,
        attempts: int | None = None,
        provider: object | None = None,
    ) -> None:
        source = provider if provider is not None else self.llm
        latency = int((time.monotonic() - started) * 1000)
        log.info(
            "model_call",
            call_type=call_type.name,
            prompt_version=call_type.prompt_version,
            succeeded=succeeded,
            latency_ms=latency,
            error_kind=error_kind,
        )
        if session is None or ctx is None:
            return
        session.add(
            ModelUsage(
                org_id=ctx.org_id,
                call_type=call_type.name,
                provider=type(source).__name__,
                model_id=getattr(source, "model_id", "unknown"),
                prompt_version=call_type.prompt_version,
                input_tokens=response.input_tokens if response else None,
                output_tokens=response.output_tokens if response else None,
                latency_ms=latency,
                attempts=attempts or (response.attempts if response else 1),
                succeeded=succeeded,
                error_kind=error_kind,
                request_id=ctx.request_id or uuid.uuid4(),
            )
        )


def build_gateway() -> ModelGateway:
    from app.gateway.deepseek import DeepSeekProvider, NullLLMProvider
    from app.gateway.embeddings import HashingEmbeddingProvider, LocalEmbeddingProvider

    s = get_settings()
    llm: LLMProvider = DeepSeekProvider() if s.llm_provider == "deepseek" else NullLLMProvider()
    embeddings: EmbeddingProvider = (
        LocalEmbeddingProvider() if s.embedding_provider == "local" else HashingEmbeddingProvider()
    )
    from app.gateway.vision import build_vision

    return ModelGateway(llm, embeddings, vision=build_vision())
