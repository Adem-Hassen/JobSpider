from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from jobfinder.ai.client import ChatRequest, ChatResult, TokenUsage
from jobfinder.ai.errors import AIOutputError
from jobfinder.ai.structured import output_schema, structured_completion
from jobfinder.config import Purpose


class Thing(BaseModel):
    a: int
    b: str | None


EXAMPLE = Thing(a=1, b=None)


@dataclass
class FakeClient:
    replies: list[str]
    requests: list[ChatRequest] = field(default_factory=list)
    finish_reason: str = "stop"

    @property
    def provider_name(self) -> str:
        return "groq"

    async def complete(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return ChatResult(
            content=self.replies.pop(0),
            finish_reason=self.finish_reason,
            usage=TokenUsage(10, 0, 5),
            provider="groq",
            model=request.model,
            requested_at=datetime(2026, 10, 14, tzinfo=UTC),
            latency_s=0.1,
        )


@dataclass
class Recorder:
    calls: list[tuple[ChatResult, Purpose]] = field(default_factory=list)

    def record(self, result: ChatResult, purpose: Purpose) -> float:
        self.calls.append((result, purpose))
        return 0.0


async def _run(client: FakeClient, recorder: Recorder | None = None) -> Thing:
    result = await structured_completion(
        client,
        purpose="score",
        model="openai/gpt-oss-120b",
        system="sys",
        user="usr",
        output_model=Thing,
        output=output_schema("thing", Thing, EXAMPLE),
        recorder=recorder,
    )
    return result.value


async def test_valid_first_time() -> None:
    client = FakeClient(['{"a": 3, "b": "x"}'])
    assert await _run(client) == Thing(a=3, b="x")
    assert len(client.requests) == 1


async def test_code_fences_tolerated() -> None:
    assert await _run(FakeClient(['```json\n{"a": 3, "b": null}\n```'])) == Thing(a=3, b=None)


async def test_retry_includes_validation_error() -> None:
    client = FakeClient(['{"a": "three"}', '{"a": 3, "b": null}'])
    recorder = Recorder()
    assert await _run(client, recorder) == Thing(a=3, b=None)
    retry = client.requests[1].messages
    assert [m.role for m in retry] == ["system", "user", "assistant", "user"]
    assert retry[2].content == '{"a": "three"}'
    assert "a: Input should be a valid integer" in retry[3].content
    assert "b: Field required" in retry[3].content
    assert len(recorder.calls) == 2  # both calls are paid for


async def test_malformed_json_twice_raises() -> None:
    client = FakeClient(["{not json", '{"a": 1'])
    recorder = Recorder()
    with pytest.raises(AIOutputError, match="invalid thing output after retry"):
        await _run(client, recorder)
    assert len(client.requests) == 2
    assert len(recorder.calls) == 2


async def test_empty_reply_explained() -> None:
    client = FakeClient(["", '{"a": 1, "b": null}'])
    await _run(client)
    assert "the reply was empty" in client.requests[1].messages[3].content


async def test_truncated_reply_explained() -> None:
    client = FakeClient(['{"a": 1, "b": "lo', '{"a": 1, "b": null}'], finish_reason="length")
    await _run(client)
    assert "cut off at the token limit" in client.requests[1].messages[3].content


async def test_extra_fields_rejected() -> None:
    class StrictThing(BaseModel):
        model_config = {"extra": "forbid"}
        a: int

    client = FakeClient(['{"a": 1, "z": 2}', '{"a": 1, "z": 2}'])
    with pytest.raises(AIOutputError, match="Extra inputs are not permitted"):
        await structured_completion(
            client,
            purpose="score",
            model="m",
            system="s",
            user="u",
            output_model=StrictThing,
            output=output_schema("t", StrictThing, StrictThing(a=1)),
        )
