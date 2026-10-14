"""LLM client abstraction and the single OpenAI-compatible implementation.

Every provider is reached through the openai SDK with a provider-specific base_url.
Provider differences (strict JSON schema vs JSON mode, reasoning effort, extra request
fields) come from providers.yaml, never from code branches on the provider name.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, cast

import httpx2
import openai
from openai import AsyncOpenAI
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from jobfinder.ai.errors import AIProviderError, AIRateLimitedError
from jobfinder.ai.ratelimit import RateLimitTracker, retry_after_seconds
from jobfinder.ai.schema import schema_text
from jobfinder.config import ProviderConfig, Purpose, StructuredOutput
from jobfinder.models import utcnow

MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class OutputSchema:
    name: str
    schema: dict[str, Any]
    example_json: str


@dataclass(frozen=True)
class ChatRequest:
    purpose: Purpose
    model: str
    messages: Sequence[Message]
    output: OutputSchema


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ChatResult:
    content: str
    finish_reason: str | None
    usage: TokenUsage
    provider: str
    model: str
    requested_at: datetime
    latency_s: float


class LLMClient(Protocol):
    @property
    def provider_name(self) -> str: ...

    async def complete(self, request: ChatRequest) -> ChatResult: ...


def json_mode_instructions(output: OutputSchema) -> str:
    """Schema and example appended to the system prompt when only JSON mode is available.

    Deterministic so the system prompt prefix stays byte-identical across calls.
    """
    return (
        "\n\n## Output format\n"
        "Reply with a single JSON object and nothing else. It must validate against this "
        f"JSON Schema:\n```json\n{schema_text(output.schema)}\n```\n"
        f"Example of a valid reply:\n```json\n{output.example_json}\n```\n"
    )


def build_messages(
    messages: Sequence[Message], output: OutputSchema, mode: StructuredOutput
) -> list[dict[str, str]]:
    out = [{"role": m.role, "content": m.content} for m in messages]
    if mode is StructuredOutput.JSON_OBJECT:
        block = json_mode_instructions(output)
        if out and out[0]["role"] == "system":
            out[0] = {"role": "system", "content": out[0]["content"] + block}
        else:
            out.insert(0, {"role": "system", "content": block.lstrip()})
    return out


def response_format(output: OutputSchema, mode: StructuredOutput) -> dict[str, Any]:
    if mode is StructuredOutput.STRICT_JSON_SCHEMA:
        return {
            "type": "json_schema",
            "json_schema": {"name": output.name, "schema": output.schema, "strict": True},
        }
    return {"type": "json_object"}


def usage_from_completion(usage: Any) -> TokenUsage:
    """Token counts from an OpenAI-style usage object, including provider cache fields."""
    if usage is None:
        return TokenUsage()
    cached = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None and getattr(details, "cached_tokens", None):
        cached = int(details.cached_tokens)
    extra = getattr(usage, "model_extra", None) or {}
    if extra.get("prompt_cache_hit_tokens") is not None:  # DeepSeek
        cached = int(extra["prompt_cache_hit_tokens"])
    return TokenUsage(
        input_tokens=int(usage.prompt_tokens or 0),
        cached_input_tokens=cached,
        output_tokens=int(usage.completion_tokens or 0),
    )


JSON_VALIDATE_FAILED = "json_validate_failed"


def failed_generation(exc: openai.BadRequestError) -> str | None:
    """The model's rejected output when a provider refused it for not matching the
    JSON schema (Groq: code ``json_validate_failed``); None for any other 400."""
    body = exc.body if isinstance(exc.body, dict) else {}
    if body.get("code") != JSON_VALIDATE_FAILED and exc.code != JSON_VALIDATE_FAILED:
        return None
    generated = body.get("failed_generation")
    return generated if isinstance(generated, str) else ""


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(
        exc,
        openai.RateLimitError
        | openai.APITimeoutError
        | openai.APIConnectionError
        | openai.InternalServerError,
    )


@dataclass
class OpenAICompatibleClient:
    """LLMClient for any OpenAI-compatible endpoint described in providers.yaml."""

    name: str
    provider: ProviderConfig
    api_key: str
    user_agent: str
    http_client: httpx2.AsyncClient | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], datetime] = utcnow
    monotonic: Callable[[], float] = time.monotonic
    max_wait_s: float = 60.0
    timeout_s: float = 120.0
    tracker: RateLimitTracker = field(default_factory=RateLimitTracker)
    _sdk: AsyncOpenAI = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._sdk = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.provider.base_url,
            max_retries=0,  # retries are ours, so Retry-After and caps are honoured
            timeout=self.timeout_s,
            default_headers={"User-Agent": self.user_agent},
            http_client=self.http_client,
        )

    @property
    def provider_name(self) -> str:
        return self.name

    async def __aenter__(self) -> OpenAICompatibleClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._sdk.close()

    # ------------------------------------------------------------------ request

    def _extra_body(self, request: ChatRequest) -> dict[str, Any]:
        body = dict(self.provider.models[request.model].extra_body)
        effort = self.provider.reasoning_effort.get(request.purpose)
        if self.provider.supports_reasoning_effort and effort is not None:
            body["reasoning_effort"] = effort
        return body

    def _estimate_tokens(self, messages: list[dict[str, str]]) -> int:
        return sum(len(m["content"]) for m in messages) // 4 + 256

    async def _throttle(self, estimated_tokens: int) -> None:
        delay = self.tracker.delay_before(estimated_tokens, self.monotonic())
        if delay > self.max_wait_s:
            raise AIRateLimitedError(
                f"{self.name}: rate limit resets in {delay:.0f}s", retry_after_s=delay
            )
        if delay > 0:
            await self.sleep(delay)

    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, openai.RateLimitError):
            requested = retry_after_seconds(exc.response.headers)
            if requested is not None:
                return requested
        return float(wait_exponential_jitter(initial=1, max=30)(state))

    def _should_retry(self, exc: BaseException) -> bool:
        if isinstance(exc, openai.RateLimitError):
            requested = retry_after_seconds(exc.response.headers)
            return requested is None or requested <= self.max_wait_s
        return _is_retryable(exc)

    async def _call_once(self, request: ChatRequest, messages: list[dict[str, str]]) -> ChatResult:
        requested_at = self.clock()
        started = self.monotonic()
        raw = await self._sdk.chat.completions.with_raw_response.create(
            model=request.model,
            messages=cast(Any, messages),
            response_format=cast(
                Any, response_format(request.output, self.provider.structured_output)
            ),
            max_tokens=self.provider.max_output_tokens.get(request.purpose, 2048),
            extra_body=self._extra_body(request),
        )
        self.tracker.update(dict(raw.headers), self.monotonic())
        completion = raw.parse()
        choice = completion.choices[0] if completion.choices else None
        return ChatResult(
            # Only the final answer is used; reasoning / reasoning_content is ignored.
            content=(choice.message.content or "") if choice else "",
            finish_reason=choice.finish_reason if choice else None,
            usage=usage_from_completion(completion.usage),
            provider=self.name,
            model=request.model,
            requested_at=requested_at,
            latency_s=self.monotonic() - started,
        )

    async def complete(self, request: ChatRequest) -> ChatResult:
        if request.model not in self.provider.models:
            raise AIProviderError(f"{self.name}: model {request.model!r} not in providers.yaml")
        messages = build_messages(request.messages, request.output, self.provider.structured_output)
        await self._throttle(self._estimate_tokens(messages))
        requested_at = self.clock()
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(MAX_ATTEMPTS),
                retry=retry_if_exception(self._should_retry),
                wait=self._wait,
                sleep=self.sleep,
                reraise=True,
            ):
                with attempt:
                    return await self._call_once(request, messages)
        except openai.BadRequestError as exc:
            failed = failed_generation(exc)
            if failed is None:
                raise AIProviderError(
                    f"{self.name}: HTTP 400: {exc.message}", exc.status_code
                ) from exc
            # The provider validated the model's JSON against the schema and refused it.
            # Hand the model's reply back as ordinary output: schema validation then
            # explains what is wrong and the structured layer retries once.
            return ChatResult(
                content=failed,
                finish_reason=JSON_VALIDATE_FAILED,
                usage=TokenUsage(),
                provider=self.name,
                model=request.model,
                requested_at=requested_at,
                latency_s=0.0,
            )
        except openai.RateLimitError as exc:
            raise AIRateLimitedError(
                f"{self.name}: rate limited (429)",
                retry_after_s=retry_after_seconds(exc.response.headers),
            ) from exc
        except openai.APIStatusError as exc:
            raise AIProviderError(
                f"{self.name}: HTTP {exc.status_code}: {exc.message}", exc.status_code
            ) from exc
        except openai.APIConnectionError as exc:
            raise AIProviderError(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        raise AssertionError("unreachable")  # pragma: no cover
