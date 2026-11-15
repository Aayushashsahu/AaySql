"""Documented JSON Schema subset validator for AayushSQL collections.

Implements the contract defined in ADR-08:
- Supported keywords: type, required, properties, additionalProperties, items,
  enum, minimum, maximum, minLength, maxLength, pattern.
- Supported types: object, array, string, integer, number, boolean, null.
- Strict type discipline: no implicit type coercion ("5" is not 5, True is not 1).
- Bounded regular expressions using standard re with length limits.
- Explicit rejection of unsupported keywords ($ref, allOf, anyOf, oneOf).
"""

from __future__ import annotations

import math
import re
from typing import Any

from .errors import ConstraintError, ExecutionError
from .json_value import JSON_NULL, json_structural_equal

SUPPORTED_KEYWORDS = {
    "type",
    "required",
    "properties",
    "additionalProperties",
    "items",
    "enum",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
    "title",
    "description",
}

SUPPORTED_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}

MAX_PATTERN_LENGTH = 256


def validate_schema_definition(schema: Any) -> None:
    """Validate that a schema definition uses only the supported keyword subset."""
    if not isinstance(schema, dict):
        raise ExecutionError("Schema definition must be a JSON object")

    for key, value in schema.items():
        if key not in SUPPORTED_KEYWORDS:
            raise ExecutionError(f"Unsupported schema keyword '{key}'")

        if key == "type":
            types = [value] if isinstance(value, str) else value
            if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
                raise ExecutionError("Schema 'type' must be a string or list of strings")
            for t in types:
                if t not in SUPPORTED_TYPES:
                    raise ExecutionError(f"Unsupported schema type '{t}'")

        elif key == "required":
            if not isinstance(value, list) or not all(isinstance(r, str) for r in value):
                raise ExecutionError("Schema 'required' must be a list of strings")

        elif key == "properties":
            if not isinstance(value, dict):
                raise ExecutionError("Schema 'properties' must be an object")
            for prop_schema in value.values():
                validate_schema_definition(prop_schema)

        elif key == "additionalProperties":
            if not isinstance(value, (bool, dict)):
                raise ExecutionError("Schema 'additionalProperties' must be a boolean or object")
            if isinstance(value, dict):
                validate_schema_definition(value)

        elif key == "items":
            if not isinstance(value, dict):
                raise ExecutionError("Schema 'items' must be an object")
            validate_schema_definition(value)

        elif key == "enum":
            if not isinstance(value, list) or not value:
                raise ExecutionError("Schema 'enum' must be a non-empty list")

        elif key in ("minimum", "maximum"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ExecutionError(f"Schema '{key}' must be a number")
            if isinstance(value, float) and not math.isfinite(value):
                raise ExecutionError(f"Schema '{key}' must be finite")

        elif key in ("minLength", "maxLength"):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExecutionError(f"Schema '{key}' must be a non-negative integer")

        elif key == "pattern":
            if not isinstance(value, str):
                raise ExecutionError("Schema 'pattern' must be a string")
            if len(value) > MAX_PATTERN_LENGTH:
                raise ExecutionError(f"Schema 'pattern' exceeds maximum length of {MAX_PATTERN_LENGTH}")
            try:
                re.compile(value)
            except re.error as exc:
                raise ExecutionError(f"Invalid regular expression in 'pattern': {exc}") from exc


def _check_type(val: Any, expected_type: str) -> bool:
    if expected_type == "null":
        return val is JSON_NULL or val is None
    if expected_type == "boolean":
        return isinstance(val, bool)
    if expected_type == "integer":
        return isinstance(val, int) and not isinstance(val, bool)
    if expected_type == "number":
        return (isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val))
    if expected_type == "string":
        return isinstance(val, str)
    if expected_type == "array":
        return isinstance(val, list)
    if expected_type == "object":
        return isinstance(val, dict)
    return False


def validate_document(
    doc: Any,
    schema: dict[str, Any],
    path: str = "$",
) -> tuple[bool, str | None, str | None]:
    """Validate a document against a compiled schema.

    Returns:
        (is_valid, violation_path, reason)
    """
    # 1. Type validation
    if "type" in schema:
        types = [schema["type"]] if isinstance(schema["type"], str) else schema["type"]
        if not any(_check_type(doc, t) for t in types):
            type_names = ", ".join(types)
            actual_type = "null" if (doc is JSON_NULL or doc is None) else type(doc).__name__
            return False, path, f"expected type {type_names}, got {actual_type}"

    # If document is null/None, further property/item checks do not apply unless required
    if doc is None or doc is JSON_NULL:
        return True, None, None

    # 2. Enum validation
    if "enum" in schema:
        match = any(
            json_structural_equal(doc, enum_val) if isinstance(doc, (dict, list)) else (
                type(doc) is type(enum_val) and doc == enum_val
            )
            for enum_val in schema["enum"]
        )
        if not match:
            return False, path, f"value {doc!r} is not in enum {schema['enum']!r}"

    # 3. Numeric bounds
    if isinstance(doc, (int, float)) and not isinstance(doc, bool):
        if "minimum" in schema and doc < schema["minimum"]:
            return False, path, f"value {doc} is less than minimum {schema['minimum']}"
        if "maximum" in schema and doc > schema["maximum"]:
            return False, path, f"value {doc} is greater than maximum {schema['maximum']}"

    # 4. String constraints
    if isinstance(doc, str):
        if "minLength" in schema and len(doc) < schema["minLength"]:
            return False, path, f"string length {len(doc)} is less than minLength {schema['minLength']}"
        if "maxLength" in schema and len(doc) > schema["maxLength"]:
            return False, path, f"string length {len(doc)} is greater than maxLength {schema['maxLength']}"
        if "pattern" in schema and not re.search(schema["pattern"], doc):
            return False, path, f"string {doc!r} does not match pattern {schema['pattern']!r}"

    # 5. Object constraints
    if isinstance(doc, dict):
        if "required" in schema:
            for req_key in schema["required"]:
                if req_key not in doc:
                    return False, f"{path}.{req_key}", f"missing required property '{req_key}'"

        properties = schema.get("properties", {})
        additional_props = schema.get("additionalProperties", True)

        for key, val in doc.items():
            prop_path = f"{path}.{key}"
            if key in properties:
                valid, err_path, err_reason = validate_document(val, properties[key], prop_path)
                if not valid:
                    return False, err_path, err_reason
            else:
                if additional_props is False:
                    return False, prop_path, f"additional property '{key}' is not allowed"
                if isinstance(additional_props, dict):
                    valid, err_path, err_reason = validate_document(val, additional_props, prop_path)
                    if not valid:
                        return False, err_path, err_reason

    # 6. Array constraints
    if isinstance(doc, list) and "items" in schema:
        item_schema = schema["items"]
        for idx, item in enumerate(doc):
            item_path = f"{path}[{idx}]"
            valid, err_path, err_reason = validate_document(item, item_schema, item_path)
            if not valid:
                return False, err_path, err_reason

    return True, None, None


def enforce_document_schema(doc: Any, schema: dict[str, Any] | None) -> None:
    """Validate doc against schema, raising ConstraintError if invalid."""
    if schema is None:
        return
    valid, path, reason = validate_document(doc, schema)
    if not valid:
        raise ConstraintError(f"Document validation failed at '{path}': {reason}")
