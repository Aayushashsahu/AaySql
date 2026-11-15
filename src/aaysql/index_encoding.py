"""Typed memcomparable index key encoding for JSON values.

Provides a total, order-preserving byte encoding across heterogeneous JSON
scalars in accordance with ADR-13 and AayushSQL 2.0 architecture:
    NULL (0x10) < BOOL (0x20) < NUMBER (0x30) < TEXT (0x40)

Key layout:
    type_tag (1B) | encoded_value (variable) | terminator (0x00) | row_id (8B big-endian)

Invariants:
- 1 and 1.0 compare numerically equal and encode to the identical byte prefix.
- 0, 0.0, and -0.0 all encode to the identical zero byte prefix.
- Booleans are never equivalent to numbers (False != 0, True != 1).
- Exact 64-bit signed integers and finite float64s share a unified binary scientific space.
- Lexicographical byte comparison of encoded bytes exactly matches reference ordering.
- Non-scalars (arrays, objects) and absence (SQL NULL, ABSENT) are not indexed.
"""

from __future__ import annotations

import math
import struct
from typing import Any

from .errors import ExecutionError
from .json_value import ABSENT, JSON_NULL

# Type tags
TAG_NULL = 0x10
TAG_BOOL = 0x20
TAG_NUMBER = 0x30
TAG_TEXT = 0x40

# Number sub-tags
NUM_NEG = 0x01
NUM_ZERO = 0x02
NUM_POS = 0x03

# Exponent bias
EXPONENT_BIAS = 2000


def is_indexable_scalar(value: Any) -> bool:
    """Return True if ``value`` is a scalar that can be indexed in a path index."""
    if value is JSON_NULL:
        return True
    if value is None or value is ABSENT:
        return False
    if isinstance(value, (dict, list)):
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return -(1 << 63) <= value < (1 << 63)
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return "\x00" not in value
    return False


def _type_order(value: Any) -> int:
    """Return relative type rank for total ordering."""
    if value is JSON_NULL:
        return 1
    if isinstance(value, bool):
        return 2
    if isinstance(value, (int, float)):
        return 3
    if isinstance(value, str):
        return 4
    return 5


def compare_json_values(a: Any, b: Any) -> int:
    """Reference comparator establishing total order across JSON scalars.

    Returns:
        < 0 if a < b
        == 0 if a == b
        > 0 if a > b
    """
    rank_a = _type_order(a)
    rank_b = _type_order(b)
    if rank_a != rank_b:
        return -1 if rank_a < rank_b else 1

    if a is JSON_NULL:
        return 0

    if isinstance(a, bool):
        if a == b:
            return 0
        return -1 if (not a and b) else 1

    if isinstance(a, (int, float)):
        # Exact numerical comparison
        if a == b:
            return 0
        return -1 if a < b else 1

    if isinstance(a, str):
        if a == b:
            return 0
        return -1 if a < b else 1

    return 0


def encode_value_prefix(value: Any) -> bytes:
    """Encode a scalar JSON value into an order-preserving, memcomparable byte string."""
    if value is JSON_NULL:
        return bytes([TAG_NULL, 0x00])

    if isinstance(value, bool):
        byte_val = 0x01 if value else 0x00
        return bytes([TAG_BOOL, byte_val, 0x00])

    if isinstance(value, (int, float)):
        if not math.isfinite(value) if isinstance(value, float) else False:
            raise ExecutionError("Cannot index non-finite float value")

        # Zero normalization: 0, 0.0, -0.0
        if value == 0:
            return bytes([TAG_NUMBER, NUM_ZERO, 0x00])

        is_neg = value < 0
        abs_val = -value if is_neg else value

        # Represent abs_val as exact binary ratio num / 2^k
        num, den = abs_val.as_integer_ratio()
        k = den.bit_length() - 1
        b = num.bit_length() - 1
        e = b - k
        rem = num - (1 << b)

        # Scale fraction into 64-bit unsigned integer (b <= 62 so 64 - b >= 2)
        shift = 64 - b
        frac_u64 = rem << shift

        biased_e = e + EXPONENT_BIAS
        if not (0 <= biased_e <= 0xFFFF):
            raise ExecutionError(f"Numeric exponent out of indexable range: {e}")

        payload = struct.pack(">HQ", biased_e, frac_u64)
        if is_neg:
            inverted = bytes(255 - byte for byte in payload)
            return bytes([TAG_NUMBER, NUM_NEG]) + inverted + b"\x00"
        return bytes([TAG_NUMBER, NUM_POS]) + payload + b"\x00"

    if isinstance(value, str):
        if "\x00" in value:
            raise ExecutionError("Null byte not permitted in indexed string")
        return bytes([TAG_TEXT]) + value.encode("utf-8") + b"\x00"

    raise ExecutionError(f"Cannot encode non-scalar or unsupported value: {type(value).__name__}")


def decode_value_prefix(encoded: bytes) -> Any:
    """Decode an encoded value prefix back to a native Python scalar value."""
    if not encoded:
        raise ValueError("Empty encoded prefix")

    tag = encoded[0]
    if tag == TAG_NULL:
        return JSON_NULL

    if tag == TAG_BOOL:
        return bool(encoded[1])

    if tag == TAG_NUMBER:
        sub = encoded[1]
        if sub == NUM_ZERO:
            return 0
        is_neg = sub == NUM_NEG
        payload = encoded[2:-1]
        if is_neg:
            payload = bytes(255 - byte for byte in payload)
        biased_e, frac_u64 = struct.unpack(">HQ", payload)
        e = biased_e - EXPONENT_BIAS

        # Reconstruct exact numerical magnitude
        # Magnitude = 2^e * (1 + frac_u64 / 2^64) = (2^64 + frac_u64) * 2^(e - 64)
        mantissa = (1 << 64) + frac_u64
        shift = e - 64
        if shift >= 0:
            val = mantissa << shift
        else:
            div = 1 << (-shift)
            if mantissa % div == 0:
                val = mantissa // div
            else:
                val = mantissa / div

        return -val if is_neg else val

    if tag == TAG_TEXT:
        return encoded[1:-1].decode("utf-8")

    raise ValueError(f"Unknown type tag: {tag:#x}")


def encode_composite_key(value: Any, row_id: int) -> bytes:
    """Encode value and 64-bit row identifier into composite key bytes."""
    prefix = encode_value_prefix(value)
    return prefix + struct.pack(">Q", row_id)


def split_composite_key(key: bytes) -> tuple[bytes, int]:
    """Split composite key into value prefix bytes and row identifier."""
    if len(key) < 8:
        raise ValueError("Composite key too short")
    return key[:-8], struct.unpack(">Q", key[-8:])[0]
