"""Schema-validated completions with one corrective retry."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ValidationError

from jobfinder.ai.client import ChatRequest, ChatResult, LLMClient, Message, OutputSchema
from jobfinder.ai.errors import AIOutputError
from jobfinder.ai.schema import strict_schema
from jobfinder.config import Purpose

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
_MAX_ERROR_CHARS = 1500


class SpendRecorder(Protocol):
    def record(self, result: ChatResult, purpose: str) -> float: ...


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    value: T
    calls: list[ChatResult]


def output_schema(name: str, model: type[BaseModel], example: BaseModel) -> OutputSchema:
    return OutputSchema(
        name=name, schema=strict_schema(model), example_json=example.model_dump_json()
    )


def _strip_fences(content: str) -> str:
    match = _FENCE.match(content)
    return match.group(1) if match else content


def describe_validation_error(exc: ValidationError | ValueError, result: ChatResult) -> str:
    if not result.content.strip():
        text = "the reply was empty"
    elif isinstance(exc, ValidationError):
        text = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
            for e in exc.errors(include_url=False)
        )
    else:
        text = str(exc)
    if result.finish_reason == "length":
        text += " (the reply was cut off at the token limit; be more concise)"
    elif result.finish_reason == "json_validate_failed":
        text += " (every field in the schema is required; use null or [] when unknown)"
    return text[:_MAX_ERROR_CHARS]


async def structured_completion[T: BaseModel](
    client: LLMClient,
    *,
    purpose: Purpose,
    model: str,
    system: str,
    user: str,
    output_model: type[T],
    output: OutputSchema,
    recorder: SpendRecorder | None = None,
) -> StructuredResult[T]:
    """Call the model and validate the reply as ``output_model``.

    On invalid output, retry once with the validation error appended to the
    conversation; if that also fails, raise ``AIOutputError``. Every call's spend is
    recorded, including failed ones.
    """
    messages = [Message("system", system), Message("user", user)]
    calls: list[ChatResult] = []
    error = ""
    for attempt in range(2):
        result = await client.complete(
            ChatRequest(purpose=purpose, model=model, messages=messages, output=output)
        )
        calls.append(result)
        if recorder is not None:
            recorder.record(result, purpose)
        try:
            value = output_model.model_validate_json(_strip_fences(result.content))
        except ValidationError as exc:
            error = describe_validation_error(exc, result)
        else:
            return StructuredResult(value=value, calls=calls)
        if attempt == 0:
            messages = [
                *messages,
                Message("assistant", result.content or "(empty)"),
                Message(
                    "user",
                    f"Your reply was invalid: {error}. Reply again with only the corrected "
                    "JSON object.",
                ),
            ]
    raise AIOutputError(f"invalid {output.name} output after retry: {error}")
