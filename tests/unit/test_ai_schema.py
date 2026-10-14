from __future__ import annotations

from typing import Any

from jobfinder.ai.examples import PROFILE_EXAMPLE
from jobfinder.ai.schema import schema_text, strict_schema
from jobfinder.ai.structured import output_schema
from jobfinder.models import Profile


def _objects(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append(node)
        for value in node.values():
            found.extend(_objects(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_objects(item))
    return found


def _keys(node: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(key)
            if key not in ("properties", "$defs"):
                keys |= _keys(value)
            else:
                for sub in value.values():
                    keys |= _keys(sub)
    elif isinstance(node, list):
        for item in node:
            keys |= _keys(item)
    return keys


def test_every_object_is_strict() -> None:
    schema = strict_schema(Profile)
    objects = _objects(schema)
    assert len(objects) == 4  # Profile (root), Skill, SpokenLanguage, Education
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])


def test_optional_fields_are_nullable_types() -> None:
    schema = strict_schema(Profile)
    level = schema["$defs"]["SpokenLanguage"]["properties"]["level"]
    assert level["type"] == ["string", "null"]
    assert "anyOf" not in level
    assert "level" in schema["$defs"]["SpokenLanguage"]["required"]


def test_nullable_enums_are_inlined_without_anyof() -> None:
    """Groq strict mode rejects anyOf [$ref-to-enum, null] (seen in production)."""
    from jobfinder.models import ScoreResult

    schema = strict_schema(ScoreResult)
    employment = schema["properties"]["employment_type"]
    assert employment["type"] == ["string", "null"]
    assert employment["enum"][-1] is None and "full_time" in employment["enum"]
    assert schema["properties"]["remote_type"]["enum"] == ["onsite", "hybrid", "remote", "unknown"]
    assert "$defs" not in schema  # all ScoreResult definitions were enums

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            assert "$ref" not in node or not node["$ref"].endswith(("Type", "Seniority"))
            if "anyOf" in node:
                assert {"type": "null"} not in node["anyOf"], node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    walk(strict_schema(Profile))


def test_unsupported_keywords_removed() -> None:
    keys = _keys(strict_schema(Profile))
    assert not keys & {"default", "title", "minimum", "maximum", "format", "pattern"}


def test_schema_text_is_deterministic() -> None:
    assert schema_text(strict_schema(Profile)) == schema_text(strict_schema(Profile))


def test_example_is_valid() -> None:
    output = output_schema("profile", Profile, PROFILE_EXAMPLE)
    assert Profile.model_validate_json(output.example_json) == PROFILE_EXAMPLE
