"""JSON Schema generation for structured AI output.

``strict_schema`` produces the subset accepted by OpenAI-style strict structured
outputs (Groq ``strict: true``): every object lists all properties as required and
sets ``additionalProperties: false``; optional values are unions with null. Keywords
outside that subset are dropped; pydantic still enforces them on the reply.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

_KEEP = frozenset(
    {
        "type",
        "properties",
        "required",
        "items",
        "anyOf",
        "enum",
        "const",
        "description",
        "additionalProperties",
        "$ref",
        "$defs",
    }
)


def _strictify(node: Any) -> Any:
    if isinstance(node, list):
        return [_strictify(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _KEEP:
            continue
        if key in ("properties", "$defs"):
            out[key] = {name: _strictify(sub) for name, sub in value.items()}
        else:
            out[key] = _strictify(value)
    if out.get("type") == "object" or "properties" in out:
        out["type"] = "object"
        out["required"] = list(out.get("properties", {}))
        out["additionalProperties"] = False
    return out


_PRIMITIVES = frozenset({"string", "integer", "number", "boolean"})


def _is_enum_def(node: Any) -> bool:
    return isinstance(node, dict) and "enum" in node and "properties" not in node


def _simplify(node: Any, defs: dict[str, Any]) -> Any:
    """Inline enum definitions and turn ``anyOf: [<primitive or enum>, null]`` into
    ``type: [<t>, "null"]``. Groq's strict mode rejects anyOf branches that are not
    disambiguated by a discriminator, which includes a $ref-to-enum paired with null."""
    if isinstance(node, list):
        return [_simplify(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        target = defs.get(ref.removeprefix("#/$defs/"))
        if isinstance(target, dict) and _is_enum_def(target):
            inlined = {k: v for k, v in target.items() if k in ("type", "enum", "description")}
            inlined.update({k: v for k, v in node.items() if k == "description"})
            return inlined
    out = {
        key: (
            {name: _simplify(sub, defs) for name, sub in value.items()}
            if key in ("properties", "$defs")
            else _simplify(value, defs)
        )
        for key, value in node.items()
    }
    branches = out.get("anyOf")
    if isinstance(branches, list) and len(branches) == 2 and {"type": "null"} in branches:
        other = next(b for b in branches if b != {"type": "null"})
        if isinstance(other, dict) and other.get("type") in _PRIMITIVES and "anyOf" not in other:
            merged = {k: v for k, v in out.items() if k != "anyOf"}
            merged.update({k: v for k, v in other.items() if k != "type"})
            merged["type"] = [other["type"], "null"]
            if "enum" in other:
                merged["enum"] = [*other["enum"], None]
            return merged
    return out


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = dict(_strictify(model.model_json_schema(mode="validation")))
    defs = schema.get("$defs", {})
    simplified = dict(_simplify(schema, defs))
    remaining = {k: v for k, v in simplified.get("$defs", {}).items() if not _is_enum_def(v)}
    if remaining:
        simplified["$defs"] = remaining
    else:
        simplified.pop("$defs", None)
    return simplified


def schema_text(schema: dict[str, Any]) -> str:
    """Deterministic, compact serialisation, so prompts embedding it stay byte-identical."""
    return json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
