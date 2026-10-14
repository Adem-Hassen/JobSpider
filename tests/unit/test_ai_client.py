from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from jobfinder.ai.client import (
    ChatRequest,
    Message,
    OpenAICompatibleClient,
    OutputSchema,
)
from jobfinder.ai.errors import AIProviderError, AIRateLimitedError
from jobfinder.config import AppConfig, ProviderConfig
from tests.helpers import FakeSleep, ScriptedTransport, completion_body

OUTPUT = OutputSchema(
    name="thing",
    schema={
        "type": "object",
        "properties": {"a": {"type": "integer"}},
        "required": ["a"],
        "additionalProperties": False,
    },
    example_json='{"a":1}',
)
T0 = datetime(2026, 10, 14, 12, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _client(
    name: str, provider: ProviderConfig, transport: ScriptedTransport, sleep: FakeSleep
) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        name=name,
        provider=provider,
        api_key="sk-test",
        user_agent="jobfinder/test (+mailto:me@example.com)",
        http_client=transport.client(),
        sleep=sleep,
        clock=lambda: T0,
        monotonic=Clock(),
    )


def _request(model: str, purpose: str = "score") -> ChatRequest:
    return ChatRequest(
        purpose=purpose,  # type: ignore[arg-type]
        model=model,
        messages=[Message("system", "You score jobs."), Message("user", "Job text")],
        output=OUTPUT,
    )


@pytest.fixture
def groq(app_config: AppConfig) -> ProviderConfig:
    return app_config.providers.providers["groq"]  # type: ignore[index]


@pytest.fixture
def deepseek(app_config: AppConfig) -> ProviderConfig:
    return app_config.providers.providers["deepseek"]  # type: ignore[index]


async def test_groq_strict_json_schema_request(groq: ProviderConfig) -> None:
    transport = ScriptedTransport().add(200, completion_body('{"a": 1}'))
    async with _client("groq", groq, transport, FakeSleep()) as client:
        result = await client.complete(_request("openai/gpt-oss-120b", "profile"))
    assert result.content == '{"a": 1}'
    assert result.usage.input_tokens == 100
    (request,) = transport.requests
    assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert request.headers["User-Agent"] == "jobfinder/test (+mailto:me@example.com)"
    body = json.loads(request.content)
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "thing", "schema": OUTPUT.schema, "strict": True},
    }
    assert body["reasoning_effort"] == "medium"
    assert body["include_reasoning"] is False
    assert body["max_tokens"] == 8192
    assert body["messages"][0] == {"role": "system", "content": "You score jobs."}


async def test_groq_qwen_extra_body(groq: ProviderConfig) -> None:
    transport = ScriptedTransport().add(200, completion_body('{"a": 1}'))
    async with _client("groq", groq, transport, FakeSleep()) as client:
        await client.complete(_request("qwen/qwen3.8-27b"))
    body = transport.bodies()[0]
    assert body["reasoning_format"] == "hidden"
    assert body["reasoning_effort"] == "low"
    assert "include_reasoning" not in body


async def test_deepseek_json_object_request(deepseek: ProviderConfig) -> None:
    body = completion_body('{"a": 2}', model="deepseek-v4-pro")
    body["choices"][0]["message"]["reasoning_content"] = '{"a": 999}'
    transport = ScriptedTransport().add(200, body).add(200, body)
    async with _client("deepseek", deepseek, transport, FakeSleep()) as client:
        first = await client.complete(_request("deepseek-v4-pro"))
        await client.complete(_request("deepseek-v4-pro"))
    assert first.content == '{"a": 2}'  # reasoning_content ignored
    sent = transport.bodies()
    assert str(transport.requests[0].url) == "https://api.deepseek.com/chat/completions"
    assert sent[0]["response_format"] == {"type": "json_object"}
    assert sent[0]["reasoning_effort"] == "low"
    system = sent[0]["messages"][0]["content"]
    assert system.startswith("You score jobs.")
    assert "JSON Schema" in system
    assert '{"a":1}' in system  # the example
    # Prefix caching: the system prompt is byte-identical across calls.
    assert sent[0]["messages"][0] == sent[1]["messages"][0]


async def test_deepseek_cache_usage(deepseek: ProviderConfig) -> None:
    body = completion_body('{"a": 2}', model="deepseek-v4-pro")
    body["usage"].update(prompt_cache_hit_tokens=64, prompt_cache_miss_tokens=36)
    transport = ScriptedTransport().add(200, body)
    async with _client("deepseek", deepseek, transport, FakeSleep()) as client:
        result = await client.complete(_request("deepseek-v4-pro"))
    assert result.usage.cached_input_tokens == 64


async def test_429_retry_after_honoured(groq: ProviderConfig) -> None:
    transport = (
        ScriptedTransport()
        .add(429, {"error": {"message": "rate limited"}}, {"retry-after": "2"})
        .add(200, completion_body('{"a": 1}'))
    )
    sleep = FakeSleep()
    async with _client("groq", groq, transport, sleep) as client:
        result = await client.complete(_request("openai/gpt-oss-120b"))
    assert result.content == '{"a": 1}'
    assert sleep.calls == [2.0]
    assert len(transport.requests) == 2


async def test_429_with_long_retry_after_gives_up(groq: ProviderConfig) -> None:
    transport = ScriptedTransport().add(
        429, {"error": {"message": "daily limit"}}, {"retry-after": "3600"}
    )
    sleep = FakeSleep()
    async with _client("groq", groq, transport, sleep) as client:
        with pytest.raises(AIRateLimitedError) as info:
            await client.complete(_request("openai/gpt-oss-120b"))
    assert info.value.retry_after_s == 3600
    assert sleep.calls == []
    assert len(transport.requests) == 1


async def test_5xx_retried_then_fails(groq: ProviderConfig) -> None:
    transport = ScriptedTransport()
    for _ in range(3):
        transport.add(503, {"error": {"message": "overloaded"}})
    sleep = FakeSleep()
    async with _client("groq", groq, transport, sleep) as client:
        with pytest.raises(AIProviderError, match="HTTP 503") as info:
            await client.complete(_request("openai/gpt-oss-120b"))
    assert info.value.status_code == 503
    assert len(transport.requests) == 3
    assert len(sleep.calls) == 2
    assert all(0 < s <= 30 for s in sleep.calls)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_other_4xx_not_retried(groq: ProviderConfig, status: int) -> None:
    transport = ScriptedTransport().add(status, {"error": {"message": "nope"}})
    async with _client("groq", groq, transport, FakeSleep()) as client:
        with pytest.raises(AIProviderError) as info:
            await client.complete(_request("openai/gpt-oss-120b"))
    assert info.value.status_code == status
    assert len(transport.requests) == 1


async def test_rate_limit_headers_slow_down_next_call(groq: ProviderConfig) -> None:
    headers = {
        "x-ratelimit-remaining-requests": "900",
        "x-ratelimit-reset-requests": "1m",
        "x-ratelimit-remaining-tokens": "5",
        "x-ratelimit-reset-tokens": "7.66s",
    }
    transport = (
        ScriptedTransport()
        .add(200, completion_body('{"a": 1}'), headers)
        .add(200, completion_body('{"a": 1}'))
    )
    sleep = FakeSleep()
    async with _client("groq", groq, transport, sleep) as client:
        await client.complete(_request("openai/gpt-oss-120b"))
        assert sleep.calls == []
        await client.complete(_request("openai/gpt-oss-120b"))
    assert sleep.calls == [pytest.approx(7.66)]


async def test_exhausted_daily_requests_raise_without_calling(groq: ProviderConfig) -> None:
    headers = {"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "3h"}
    transport = ScriptedTransport().add(200, completion_body('{"a": 1}'), headers)
    async with _client("groq", groq, transport, FakeSleep()) as client:
        await client.complete(_request("openai/gpt-oss-120b"))
        with pytest.raises(AIRateLimitedError):
            await client.complete(_request("openai/gpt-oss-120b"))
    assert len(transport.requests) == 1


async def test_unknown_model_rejected(groq: ProviderConfig) -> None:
    transport = ScriptedTransport()
    async with _client("groq", groq, transport, FakeSleep()) as client:
        with pytest.raises(AIProviderError, match=r"not in providers\.yaml"):
            await client.complete(_request("gpt-4o"))
    assert transport.requests == []
