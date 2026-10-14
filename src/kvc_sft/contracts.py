"""Strict JSON helpers shared by dataset preparation and inference.

The encoder action is an executable control object.  Treating it as ordinary
free-form assistant text is unsafe: duplicate keys, unknown keys, missing keys,
or out-of-domain values must all be rejected before an encoder sees it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator


# This is a training/inference contract, not a user prompt.  Qwen3.5's empty
# thinking block is input context and is excluded from the SFT loss.  The model
# must generate the *entire* action JSON, including its schema discriminator.
# Keep this JSON-serializable so that the run manifest can pin the same values.
TEMPLATE_CONTRACT: dict[str, Any] = {
    "template": "qwen3_5",
    "template_backend": "swift",
    "enable_thinking": False,
    "add_non_thinking_prefix": True,
    "loss_scale": "ignore_empty_think",
    "response_prefix": None,
}


def sft_template_kwargs() -> dict[str, Any]:
    """Return fresh kwargs for ms-swift's ``get_template`` Python API."""

    result = dict(TEMPLATE_CONTRACT)
    result["template_type"] = result.pop("template")
    return result


class ContractError(ValueError):
    """Raised when a dataset row or generated action violates its contract."""


def _reject_duplicate_pairs(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def loads_strict(text: str, *, what: str = "JSON") -> Any:
    """Parse JSON while rejecting NaN/Infinity and duplicate object keys."""

    def reject_constant(value: str) -> None:
        raise ContractError(f"{what} contains non-finite number {value}")

    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=reject_constant,
        )
    except ContractError:
        raise
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid {what}: {exc}") from exc


def load_schema(path: str | Path) -> dict[str, Any]:
    schema_path = Path(path).expanduser().resolve()
    try:
        schema = loads_strict(schema_path.read_text(encoding="utf-8"), what="JSON Schema")
    except OSError as exc:
        raise ContractError(f"cannot read schema {schema_path}: {exc}") from exc
    if not isinstance(schema, dict):
        raise ContractError("JSON Schema root must be an object")
    Draft202012Validator.check_schema(schema)
    return schema


def required_key_order(schema: dict[str, Any]) -> list[str]:
    required = schema.get("required")
    if not isinstance(required, list) or not all(isinstance(key, str) for key in required):
        raise ContractError("action schema must contain an ordered string list in 'required'")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ContractError("action schema must contain object 'properties'")
    if set(required) != set(properties):
        raise ContractError("for this action contract, 'required' must contain every property exactly once")
    return required


def action_response_prefix(schema: dict[str, Any]) -> str:
    """Return the immutable schema discriminator as a decoder prefix."""

    expected = required_key_order(schema)
    if not expected or expected[0] != "schema":
        raise ContractError("action schema must place the 'schema' discriminator first")
    schema_property = schema["properties"].get("schema")
    if not isinstance(schema_property, dict) or not isinstance(schema_property.get("const"), str):
        raise ContractError("action schema property 'schema' must define a string const")
    discriminator = json.dumps(
        {"schema": schema_property["const"]},
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return discriminator[:-1] + ","


def validate_action_object(
    action: Any,
    schema: dict[str, Any],
    *,
    require_order: bool = True,
) -> dict[str, Any]:
    if not isinstance(action, dict):
        raise ContractError("action must be one JSON object")
    errors = sorted(Draft202012Validator(schema).iter_errors(action), key=lambda error: list(error.path))
    if errors:
        messages = []
        for error in errors:
            location = ".".join(str(item) for item in error.absolute_path) or "<root>"
            messages.append(f"{location}: {error.message}")
        raise ContractError("action violates schema: " + "; ".join(messages))
    expected = required_key_order(schema)
    if require_order and list(action) != expected:
        raise ContractError(f"action key order must be {expected}, got {list(action)}")
    return action


def canonical_action(action: dict[str, Any], schema: dict[str, Any]) -> str:
    # Raw measured records are JSON objects, so their source key order has no
    # semantic meaning.  Reorder only after all keys and values are validated.
    validate_action_object(action, schema, require_order=False)
    ordered = {key: action[key] for key in required_key_order(schema)}
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def validate_action_text(
    text: str,
    schema: dict[str, Any],
    *,
    require_canonical: bool = True,
) -> dict[str, Any]:
    if not isinstance(text, str):
        raise ContractError("assistant action must be a string")
    if text != text.strip():
        raise ContractError("assistant action must not have leading or trailing whitespace")
    action = loads_strict(text, what="assistant action")
    validate_action_object(action, schema)
    if require_canonical and text != canonical_action(action, schema):
        raise ContractError("assistant action is valid JSON but is not in canonical compact form")
    return action
