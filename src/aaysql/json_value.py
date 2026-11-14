"""Native JSON value representation, validation, and manipulation for AayushSQL.

This module implements:
- First-class JSON value model (objects, arrays, strings, int64, float64, booleans, and null)
- Three-state sentinels: ``SQL NULL`` (``None``), ``JSON NULL`` (``JSON_NULL``), and ``ABSENT``
- Rejection of duplicate object keys, non-finite floats, out-of-range int64s, and null bytes
- Resource limits: 1 MiB document size, 64 nesting depth, 1 KiB key length, 32 path segments
- Canonical UTF-8 serialization with lexicographically sorted keys
- Pure copy-on-write path extraction, mutation (``set_path``, ``remove_path``), and deep copying
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Sequence
from typing import Any

from .errors import ConstraintError, ExecutionError

# --- Constants & Limits (ADR-07, Architecture §13.3) -------------------------

MAX_DOCUMENT_BYTES: int = 1024 * 1024  # 1 MiB
MAX_NESTING_DEPTH: int = 64
MAX_KEY_LENGTH: int = 1024  # 1 KiB
MAX_PATH_SEGMENTS: int = 32
INT64_MIN: int = -9223372036854775808
INT64_MAX: int = 9223372036854775807


# --- Three-state Sentinels (ADR-07, Architecture §13.1) -----------------------


class _JsonNull:
    """Sentinel for explicit JSON null (distinct from SQL NULL and ABSENT)."""

    __slots__ = ()
    _instance: _JsonNull | None = None

    def __new__(cls) -> _JsonNull:  # noqa: PYI034
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "JSON_NULL"

    def __copy__(self) -> _JsonNull:
        return self

    def __deepcopy__(self, memo: Any) -> _JsonNull:
        return self

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _JsonNull)

    def __hash__(self) -> int:
        return hash(self.__class__)

    def __bool__(self) -> bool:
        return False


class _Absent:
    """Sentinel for missing JSON path/property (distinct from SQL NULL and JSON NULL)."""

    __slots__ = ()
    _instance: _Absent | None = None

    def __new__(cls) -> _Absent:  # noqa: PYI034
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "ABSENT"

    def __copy__(self) -> _Absent:
        return self

    def __deepcopy__(self, memo: Any) -> _Absent:
        return self

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Absent)

    def __hash__(self) -> int:
        return hash(self.__class__)

    def __bool__(self) -> bool:
        return False


JSON_NULL = _JsonNull()
ABSENT = _Absent()


# --- Deep Copying & Mutability Isolation -------------------------------------


def deep_copy_json(value: Any) -> Any:
    """Deeply copy a JSON structure so caller mutation cannot affect stored roots."""
    if value is None or value is JSON_NULL or value is ABSENT:
        return value
    if isinstance(value, (int, float, str, bool)):
        return value
    if isinstance(value, list):
        return [deep_copy_json(elem) for elem in value]
    if isinstance(value, dict):
        return {k: deep_copy_json(v) for k, v in value.items()}
    # Fallback to general copy
    return copy.deepcopy(value)


# --- JSON Validation & Strict Parsing ---------------------------------------


def _check_object_pairs(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    res: dict[str, Any] = {}
    for k, v in pairs:
        if not isinstance(k, str):
            raise ConstraintError(f"JSON object key must be a string, got {type(k).__name__}")
        if len(k.encode("utf-8")) > MAX_KEY_LENGTH:
            raise ConstraintError(
                f"JSON object key exceeds maximum allowed length of {MAX_KEY_LENGTH} bytes"
            )
        if "\u0000" in k:
            raise ConstraintError("JSON string cannot contain null bytes (\\u0000)")
        if k in seen:
            raise ConstraintError(f"Duplicate JSON object key '{k}' is rejected")
        seen.add(k)
        res[k] = v
    return res


def _reject_constant(c: str) -> None:
    raise ConstraintError(f"Non-finite JSON constant '{c}' is not supported")


def _check_int(s: str) -> int:
    val = int(s)
    if val < INT64_MIN or val > INT64_MAX:
        raise ConstraintError(f"JSON integer {s} exceeds 64-bit signed integer range")
    return val


def _check_float(s: str) -> float:
    val = float(s)
    if math.isnan(val) or math.isinf(val):
        raise ConstraintError(f"Non-finite JSON float '{s}' is not supported")
    return val


def validate_json_value(val: Any, depth: int = 1) -> Any:
    """Validate a Python value against all AayushSQL JSON rules and limits.

    Replaces internal Python ``None`` with ``JSON_NULL`` when encountered inside
    a document structure.
    """
    if depth > MAX_NESTING_DEPTH:
        raise ConstraintError(f"JSON nesting depth exceeds maximum limit of {MAX_NESTING_DEPTH}")

    if val is None or val is JSON_NULL:
        return JSON_NULL
    if val is ABSENT:
        raise ConstraintError("ABSENT is an internal query sentinel and cannot be stored")

    if isinstance(val, bool):
        return val

    if isinstance(val, int):
        if val < INT64_MIN or val > INT64_MAX:
            raise ConstraintError(f"JSON integer {val} exceeds 64-bit signed integer range")
        return val

    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            raise ConstraintError(f"Non-finite JSON float '{val}' is not supported")
        return val

    if isinstance(val, str):
        if "\u0000" in val:
            raise ConstraintError("JSON string cannot contain null bytes (\\u0000)")
        return val

    if isinstance(val, list):
        return [validate_json_value(elem, depth=depth + 1) for elem in val]

    if isinstance(val, dict):
        seen: set[str] = set()
        clean: dict[str, Any] = {}
        for k, v in val.items():
            if not isinstance(k, str):
                raise ConstraintError(f"JSON object key must be a string, got {type(k).__name__}")
            if len(k.encode("utf-8")) > MAX_KEY_LENGTH:
                raise ConstraintError(
                    f"JSON object key exceeds maximum allowed length of {MAX_KEY_LENGTH} bytes"
                )
            if "\u0000" in k:
                raise ConstraintError("JSON string cannot contain null bytes (\\u0000)")
            if k in seen:
                raise ConstraintError(f"Duplicate JSON object key '{k}' is rejected")
            seen.add(k)
            clean[k] = validate_json_value(v, depth=depth + 1)
        return clean

    raise ConstraintError(f"Type '{type(val).__name__}' is not a valid JSON value")


def parse_json_document(text: str) -> Any:
    """Parse a JSON string with strict validation and resource limits.

    Rejects duplicate keys, non-finite floats, out-of-range integers, and oversized documents.
    """
    if not isinstance(text, str):
        raise ConstraintError(f"Expected JSON text, got {type(text).__name__}")
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise ConstraintError(
            f"JSON document size ({len(encoded)} bytes) exceeds maximum of {MAX_DOCUMENT_BYTES} bytes"
        )
    if "\u0000" in text:
        raise ConstraintError("JSON string cannot contain null bytes (\\u0000)")

    decoder = json.JSONDecoder(
        object_pairs_hook=_check_object_pairs,
        parse_constant=_reject_constant,
        parse_int=_check_int,
        parse_float=_check_float,
    )
    stripped = text.strip()
    if not stripped:
        raise ConstraintError("Empty JSON string is invalid")

    try:
        val, idx = decoder.raw_decode(stripped)
    except json.JSONDecodeError as exc:
        raise ConstraintError(f"Invalid JSON: {exc.msg} (at position {exc.pos})") from exc

    if idx < len(stripped):
        trailing = stripped[idx:].strip()
        if trailing:
            raise ConstraintError(f"Invalid JSON: unexpected trailing characters '{trailing[:20]}'")

    validated = validate_json_value(val)
    # Check total canonical serialized size
    canon_bytes = canonical_json_bytes(validated)
    if len(canon_bytes) > MAX_DOCUMENT_BYTES:
        raise ConstraintError(
            f"JSON document canonical size ({len(canon_bytes)} bytes) exceeds maximum of {MAX_DOCUMENT_BYTES} bytes"
        )
    return validated


# --- Canonical Serialization ------------------------------------------------


def _to_serializable(val: Any) -> Any:
    if val is ABSENT:
        raise ConstraintError("Cannot serialize ABSENT sentinel")
    if val is JSON_NULL or val is None:
        return None
    if isinstance(val, (int, float, str, bool)):
        return val
    if isinstance(val, list):
        return [_to_serializable(elem) for elem in val]
    if isinstance(val, dict):
        return {k: _to_serializable(v) for k, v in val.items()}
    raise ConstraintError(f"Cannot serialize value of type '{type(val).__name__}' to JSON")


def canonical_json_text(val: Any) -> str:
    """Serialize value to canonical JSON string with sorted keys and no extra whitespace."""
    serializable = _to_serializable(val)
    return json.dumps(
        serializable,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_json_bytes(val: Any) -> bytes:
    """Serialize value to canonical UTF-8 JSON bytes."""
    return canonical_json_text(val).encode("utf-8")


# --- Structural Equality ----------------------------------------------------


def json_structural_equal(left: Any, right: Any) -> bool:
    """Order-insensitive structural equality for JSON values.

    Numbers compare equal numerically across int and float (e.g. 1 == 1.0),
    but boolean True never equals 1.
    """
    if left is ABSENT or right is ABSENT:
        return left is right
    if left is JSON_NULL or right is JSON_NULL:
        return left is right
    if left is None or right is None:
        return left is right

    if isinstance(left, (dict, list)) and isinstance(right, str):
        try:
            right = parse_json_document(right)
        except (ConstraintError, ExecutionError, ValueError, TypeError):
            return False
    elif isinstance(right, (dict, list)) and isinstance(left, str):
        try:
            left = parse_json_document(left)
        except (ConstraintError, ExecutionError, ValueError, TypeError):
            return False

    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right

    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right

    if isinstance(left, str) and isinstance(right, str):
        return left == right

    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return False
        return all(json_structural_equal(a, b) for a, b in zip(left, right))

    if isinstance(left, dict) and isinstance(right, dict):
        if set(left.keys()) != set(right.keys()):
            return False
        return all(json_structural_equal(left[k], right[k]) for k in left)

    return False


# --- Path Parsing & Traversal -----------------------------------------------

_PATH_SEGMENT_RE = re.compile(r"""
    \.?(?:
        (?P<ident>[A-Za-z_][A-Za-z0-9_]*) |
        "(?P<quoted_key>(?:\\.|[^"\\])*)" |
        '(?P<s_quoted_key>(?:\\.|[^'\\])*)' |
        \[(?P<idx>-?\d+)\] |
        \["(?P<bracket_qkey>(?:\\.|[^"\\])*)"\] |
        \['(?P<bracket_sqkey>(?:\\.|[^'\\])*)'\]
    )
""", re.VERBOSE)


def parse_json_path(path_expr: str | Sequence[str | int]) -> tuple[str | int, ...]:
    """Parse a path string like '$.profile.skills[0]' or 'a.b.c' into path segments."""
    if isinstance(path_expr, (list, tuple)):
        if len(path_expr) > MAX_PATH_SEGMENTS:
            raise ExecutionError(f"JSON path exceeds maximum of {MAX_PATH_SEGMENTS} segments")
        return tuple(path_expr)

    if not isinstance(path_expr, str):
        raise ExecutionError(f"JSON path must be a string, got {type(path_expr).__name__}")

    raw = path_expr.strip()
    raw = raw.removeprefix("$")
    raw = raw.removeprefix(".")

    if not raw:
        return ()

    segments: list[str | int] = []
    pos = 0
    while pos < len(raw):
        m = _PATH_SEGMENT_RE.match(raw, pos)
        if not m:
            raise ExecutionError(f"Malformed JSON path '{path_expr}' at position {pos}")
        pos = m.end()

        ident = m.group("ident")
        quoted = m.group("quoted_key") or m.group("s_quoted_key") or m.group("bracket_qkey") or m.group("bracket_sqkey")
        idx = m.group("idx")

        if ident is not None:
            segments.append(ident)
        elif quoted is not None:
            # Handle basic unescaping
            key = quoted.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")
            segments.append(key)
        elif idx is not None:
            segments.append(int(idx))

        if len(segments) > MAX_PATH_SEGMENTS:
            raise ExecutionError(f"JSON path exceeds maximum of {MAX_PATH_SEGMENTS} segments")

    return tuple(segments)


def extract_path(value: Any, path: Sequence[str | int]) -> Any:
    """Traverse a JSON structure using path segments.

    Returns:
    - Target value if present.
    - ``JSON_NULL`` if the path exists and its value is explicit JSON null.
    - ``ABSENT`` if the path does not exist, index is out of bounds, or target is missing.
    """
    if value is None or value is ABSENT:
        return ABSENT
    if value is JSON_NULL:
        return JSON_NULL if not path else ABSENT

    curr = value
    for seg in path:
        if curr is None or curr is ABSENT:
            return ABSENT
        if curr is JSON_NULL:
            return ABSENT

        if isinstance(seg, int):
            if not isinstance(curr, list):
                return ABSENT
            if 0 <= seg < len(curr):
                curr = curr[seg]
            else:
                return ABSENT
        else:
            if not isinstance(curr, dict):
                return ABSENT
            if seg in curr:
                curr = curr[seg]
            else:
                return ABSENT

    if curr is None:
        return JSON_NULL
    return curr


def set_path(doc: Any, path: Sequence[str | int], new_value: Any) -> Any:
    """Return a new document with ``new_value`` set at ``path`` (copy-on-write).

    Intermediate objects/arrays are created as needed. Original ``doc`` is never mutated.
    """
    if not path:
        return validate_json_value(new_value)

    # Clean the new value
    clean_new_val = validate_json_value(new_value)

    # Base document: if doc is None or ABSENT or empty, initialize as dict or list based on first segment
    if doc is None or doc is ABSENT or doc is JSON_NULL:
        base = [] if isinstance(path[0], int) else {}
    else:
        base = deep_copy_json(doc)

    def _set_recursive(curr: Any, remaining: Sequence[str | int]) -> Any:
        seg = remaining[0]
        is_last = len(remaining) == 1

        if isinstance(seg, int):
            # Target is list
            lst = list(curr) if isinstance(curr, list) else []
            target_val = clean_new_val if is_last else None

            if not is_last:
                next_seg = remaining[1]
                default_next = [] if isinstance(next_seg, int) else {}

            if seg < 0:
                # Disallow negative indexing in set_path
                raise ExecutionError(f"Negative array index {seg} is not supported in JSON_SET")

            # Extend list with JSON_NULL if index is beyond length
            while len(lst) <= seg:
                lst.append(JSON_NULL)

            if is_last:
                lst[seg] = target_val
            else:
                existing_child = lst[seg]
                if existing_child is None or existing_child is JSON_NULL or not isinstance(existing_child, (dict, list)):
                    existing_child = default_next
                lst[seg] = _set_recursive(existing_child, remaining[1:])
            return lst
        else:
            # Target is dict
            d = dict(curr) if isinstance(curr, dict) else {}
            if is_last:
                d[seg] = clean_new_val
            else:
                next_seg = remaining[1]
                default_next = [] if isinstance(next_seg, int) else {}
                existing_child = d.get(seg)
                if existing_child is None or existing_child is JSON_NULL or not isinstance(existing_child, (dict, list)):
                    existing_child = default_next
                d[seg] = _set_recursive(existing_child, remaining[1:])
            return d

    result = _set_recursive(base, path)
    # Ensure result satisfies limits
    validated = validate_json_value(result)
    canon = canonical_json_bytes(validated)
    if len(canon) > MAX_DOCUMENT_BYTES:
        raise ConstraintError(
            f"Mutated JSON document size ({len(canon)} bytes) exceeds maximum of {MAX_DOCUMENT_BYTES} bytes"
        )
    return validated


def remove_path(doc: Any, path: Sequence[str | int]) -> Any:
    """Return a new document with the element at ``path`` removed (copy-on-write).

    If the path does not exist, returns the document unmodified. Original ``doc`` is never mutated.
    """
    if not path or doc is None or doc is ABSENT or doc is JSON_NULL:
        return doc

    base = deep_copy_json(doc)

    def _remove_recursive(curr: Any, remaining: Sequence[str | int]) -> Any:
        if not remaining:
            return curr

        seg = remaining[0]
        is_last = len(remaining) == 1

        if isinstance(seg, int):
            if not isinstance(curr, list) or seg < 0 or seg >= len(curr):
                return curr
            lst = list(curr)
            if is_last:
                del lst[seg]
                return lst
            lst[seg] = _remove_recursive(lst[seg], remaining[1:])
            return lst
        else:
            if not isinstance(curr, dict) or seg not in curr:
                return curr
            d = dict(curr)
            if is_last:
                del d[seg]
                return d
            d[seg] = _remove_recursive(d[seg], remaining[1:])
            return d

    result = _remove_recursive(base, path)
    return validate_json_value(result)
