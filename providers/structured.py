"""Structured output helpers shared by every provider.

Whatever a provider's native JSON support looks like, the reply is always parsed and
validated locally with Pydantic, so all providers honour the same contract.
"""

from __future__ import annotations

import copy
import json
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from providers.base import StructuredOutputError

T = TypeVar("T", bound=BaseModel)

# Keywords that strict JSON-schema modes commonly reject. Local Pydantic validation still
# enforces the constraints; they are only removed from the schema sent to the provider.
_STRICT_UNSUPPORTED = (
    "default",
    "examples",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
)


def schema_instruction(model_cls: type[BaseModel]) -> str:
    """Prompt text describing the required JSON shape (for JSON modes without a native schema)."""
    schema = json.dumps(model_cls.model_json_schema(), separators=(",", ":"))
    return (
        "Respond with a single JSON object that validates against this JSON Schema. "
        "Output only the JSON: no markdown fences, no commentary.\n"
        f"JSON Schema: {schema}"
    )


def to_strict_schema(schema: dict) -> dict:
    """Adapt a Pydantic JSON schema for strict structured-output modes.

    Strict modes need ``additionalProperties: false`` and every property listed in
    ``required`` (optional values become nullable via ``anyOf`` in Pydantic's output).
    """
    result = copy.deepcopy(schema)

    def walk(node):
        if not isinstance(node, dict):
            return
        for key in _STRICT_UNSUPPORTED:
            node.pop(key, None)
        properties = node.get("properties")
        if node.get("type") == "object" or isinstance(properties, dict):
            node["additionalProperties"] = False
            node["required"] = list(properties.keys()) if isinstance(properties, dict) else []
        if isinstance(properties, dict):
            for sub in properties.values():
                walk(sub)
        for container in ("$defs", "definitions"):
            if isinstance(node.get(container), dict):
                for sub in node[container].values():
                    walk(sub)
        if isinstance(node.get("items"), dict):
            walk(node["items"])
        for combinator in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for sub in node.get(combinator, []) or []:
                walk(sub)

    walk(result)
    return result


def extract_json(text: str):
    """Parse JSON from a model reply, tolerating code fences and surrounding prose."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped[3:]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
        stripped = stripped.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char in "{[":
            try:
                value, _ = decoder.raw_decode(stripped[index:])
                return value
            except json.JSONDecodeError:
                continue
    raise StructuredOutputError("The reply did not contain valid JSON.")


def parse_structured(text: str, model_cls: type[T]) -> T:
    """Validate a model reply against ``model_cls`` or raise :class:`StructuredOutputError`."""
    data = extract_json(text)
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        # Report where the problems are, not the offending values.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}" for err in exc.errors()[:8]
        )
        raise StructuredOutputError(f"JSON did not match the schema: {problems}") from exc
