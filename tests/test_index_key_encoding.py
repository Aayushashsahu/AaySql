"""Property-based and boundary test suite for typed memcomparable index key encoding."""

from __future__ import annotations

import random
from typing import Any

from aaysql.index_encoding import (
    TAG_BOOL,
    TAG_NULL,
    TAG_NUMBER,
    TAG_TEXT,
    compare_json_values,
    decode_value_prefix,
    encode_composite_key,
    encode_value_prefix,
    is_indexable_scalar,
    split_composite_key,
)
from aaysql.json_value import ABSENT, JSON_NULL


def test_type_ordering_tags():
    """Verify that type tags follow the specified order: NULL < BOOL < NUMBER < TEXT."""
    assert TAG_NULL < TAG_BOOL < TAG_NUMBER < TAG_TEXT
    assert TAG_NULL == 0x10
    assert TAG_BOOL == 0x20
    assert TAG_NUMBER == 0x30
    assert TAG_TEXT == 0x40


def test_indexable_scalar_classification():
    """Check is_indexable_scalar correctly classifies supported vs unsupported types."""
    assert is_indexable_scalar(JSON_NULL) is True
    assert is_indexable_scalar(True) is True
    assert is_indexable_scalar(False) is True
    assert is_indexable_scalar(0) is True
    assert is_indexable_scalar(1) is True
    assert is_indexable_scalar(1.5) is True
    assert is_indexable_scalar("hello") is True

    # Non-indexable: ABSENT, SQL NULL, dict, list, NaN, Inf, null-byte string
    assert is_indexable_scalar(None) is False
    assert is_indexable_scalar(ABSENT) is False
    assert is_indexable_scalar({}) is False
    assert is_indexable_scalar([]) is False
    assert is_indexable_scalar(float("nan")) is False
    assert is_indexable_scalar(float("inf")) is False
    assert is_indexable_scalar(float("-inf")) is False
    assert is_indexable_scalar("hello\x00world") is False


def test_numeric_equality_canonicalization():
    """Verify that numerically equal values produce identical encoded bytes."""
    # 1 and 1.0
    enc_int_1 = encode_value_prefix(1)
    enc_flt_1 = encode_value_prefix(1.0)
    assert enc_int_1 == enc_flt_1

    # 0, 0.0, -0.0
    enc_0 = encode_value_prefix(0)
    enc_0_f = encode_value_prefix(0.0)
    enc_neg_0 = encode_value_prefix(-0.0)
    assert enc_0 == enc_0_f == enc_neg_0

    # Negative numbers
    assert encode_value_prefix(-5) == encode_value_prefix(-5.0)
    assert encode_value_prefix(-100) == encode_value_prefix(-100.0)

    # Large integers / floats where exact
    for exp in range(1, 53):
        val = 1 << exp
        assert encode_value_prefix(val) == encode_value_prefix(float(val))


def test_boolean_numeric_separation():
    """Verify that booleans are strictly separated from numbers."""
    enc_false = encode_value_prefix(False)
    enc_true = encode_value_prefix(True)
    enc_0 = encode_value_prefix(0)
    enc_1 = encode_value_prefix(1)

    assert enc_false != enc_0
    assert enc_true != enc_1
    assert enc_false < enc_true

    # Type order: BOOL < NUMBER, so True < 0 and True < -100
    assert enc_false < enc_0
    assert enc_true < enc_0
    assert enc_true < encode_value_prefix(-1000)


def test_boundary_integer_values():
    """Verify int64 boundary values encode deterministically and preserve order."""
    boundaries = [
        -(1 << 63),
        -(1 << 63) + 1,
        -(1 << 62),
        -(1 << 53) - 1,
        -(1 << 53),
        -(1 << 53) + 1,
        -1000,
        -1,
        0,
        1,
        1000,
        (1 << 53) - 1,
        (1 << 53),
        (1 << 53) + 1,
        (1 << 60),
        (1 << 60) + 1,
        (1 << 62),
        (1 << 63) - 2,
        (1 << 63) - 1,
    ]

    encoded = [encode_value_prefix(v) for v in boundaries]

    # Verify strict monotonic ordering of encoded byte strings
    for i in range(len(boundaries) - 1):
        assert boundaries[i] < boundaries[i + 1]
        assert encoded[i] < encoded[i + 1], f"Failed at {boundaries[i]} vs {boundaries[i+1]}"


def test_float_subnormals():
    """Verify subnormal float values are correctly encoded and ordered."""
    subnormals = [
        5e-324,  # Smallest positive float64 (2^-1074)
        1e-323,
        1e-320,
        1e-315,
        1e-310,
        2.2250738585072014e-308,  # Smallest normal float64
        1e-300,
        1e-100,
        1e-10,
        0.1,
        0.5,
        1.0,
    ]

    encoded = [encode_value_prefix(v) for v in subnormals]
    for i in range(len(subnormals) - 1):
        assert subnormals[i] < subnormals[i + 1]
        assert encoded[i] < encoded[i + 1]

    # Verify negative subnormals
    neg_subnormals = [-v for v in reversed(subnormals)]
    neg_encoded = [encode_value_prefix(v) for v in neg_subnormals]
    for i in range(len(neg_subnormals) - 1):
        assert neg_subnormals[i] < neg_subnormals[i + 1]
        assert neg_encoded[i] < neg_encoded[i + 1]


def test_text_encoding_and_prefix_safety():
    """Verify string encoding is binary-order preserving and prefix-safe."""
    strings = [
        "",
        "a",
        "aa",
        "ab",
        "abc",
        "abcd",
        "b",
        "ba",
        "z",
        "zoo",
        "\u00e9",
        "\u4e16\u754c",
        "\U0001f600",
    ]

    encoded = [encode_value_prefix(s) for s in strings]
    for i in range(len(strings) - 1):
        assert strings[i] < strings[i + 1]
        assert encoded[i] < encoded[i + 1], f"Mismatch for {strings[i]!r} vs {strings[i+1]!r}"


def test_cross_type_ordering():
    """Verify the cross-type total ordering: NULL < BOOL < NUMBER < TEXT."""
    samples = [
        JSON_NULL,
        False,
        True,
        -1000,
        -0.5,
        0,
        0.5,
        1,
        1000,
        "",
        "abc",
    ]

    encoded = [encode_value_prefix(s) for s in samples]
    for i in range(len(samples) - 1):
        a = samples[i]
        b = samples[i + 1]
        cmp_result = compare_json_values(a, b)
        assert cmp_result < 0, f"Expected {a!r} < {b!r}"
        assert encoded[i] < encoded[i + 1], f"Encoded order failed for {a!r} vs {b!r}"


def test_round_trip_decode():
    """Verify decode_value_prefix round-trips values."""
    cases = [
        JSON_NULL,
        False,
        True,
        0,
        1,
        -1,
        42,
        -999,
        (1 << 50),
        -(1 << 50),
        (1 << 62),
        -(1 << 62),
        1.5,
        -2.75,
        0.125,
        "",
        "hello",
        "AayushSQL 2.0",
        "\u4e16\u754c",
    ]

    for c in cases:
        encoded = encode_value_prefix(c)
        decoded = decode_value_prefix(encoded)
        if isinstance(c, (int, float)):
            assert decoded == c
        else:
            assert decoded == c


def test_composite_key_encoding_and_splitting():
    """Verify composite key encoding appends row_id and splits cleanly."""
    val = "user_event"
    row_id = 4294967296  # 2^32

    composite = encode_composite_key(val, row_id)
    prefix, extracted_row_id = split_composite_key(composite)

    assert prefix == encode_value_prefix(val)
    assert extracted_row_id == row_id

    # Same value, different row IDs
    c1 = encode_composite_key(val, 1)
    c2 = encode_composite_key(val, 2)
    assert c1 < c2
    assert c1[:-8] == c2[:-8]


def test_property_randomized_order_agreement():
    """Deterministic randomized property test: 1,000 pairs of mixed scalars."""
    rng = random.Random(42)

    def _random_scalar() -> Any:
        choice = rng.randint(0, 4)
        if choice == 0:
            return JSON_NULL
        if choice == 1:
            return rng.choice([False, True])
        if choice == 2:
            # int
            return rng.choice([
                rng.randint(-100, 100),
                rng.randint(-(1 << 63), -(1 << 62)),
                rng.randint(1 << 62, (1 << 63) - 1),
                0,
            ])
        if choice == 3:
            # float
            return rng.choice([
                rng.uniform(-1000.0, 1000.0),
                rng.uniform(-1.0, 1.0),
                0.0,
                -0.0,
                5e-324,
                1e-100,
            ])
        # text
        chars = "abcdefghijklmnopqrstuvwxyz0123456789 _-"
        length = rng.randint(0, 15)
        return "".join(rng.choice(chars) for _ in range(length))

    for _ in range(1000):
        a = _random_scalar()
        b = _random_scalar()

        ref_cmp = compare_json_values(a, b)
        enc_a = encode_value_prefix(a)
        enc_b = encode_value_prefix(b)

        # Antisymmetry & equality
        if ref_cmp == 0:
            assert enc_a == enc_b, f"Expected {a!r} == {b!r} in encoding"
        elif ref_cmp < 0:
            assert enc_a < enc_b, f"Expected {a!r} < {b!r} in encoding"
            assert enc_b > enc_a
        else:
            assert enc_a > enc_b, f"Expected {a!r} > {b!r} in encoding"
            assert enc_b < enc_a


try:
    from hypothesis import given
    from hypothesis import strategies as st

    @given(st.integers(min_value=-(1 << 63), max_value=(1 << 63) - 1))
    def test_hypothesis_int64_roundtrip(val):
        assert decode_value_prefix(encode_value_prefix(val)) == val

    @given(st.floats(allow_nan=False, allow_infinity=False))
    def test_hypothesis_float_ordering(f1, f2):
        enc1 = encode_value_prefix(f1)
        enc2 = encode_value_prefix(f2)
        ref = compare_json_values(f1, f2)
        if ref == 0:
            assert enc1 == enc2
        elif ref < 0:
            assert enc1 < enc2
        else:
            assert enc1 > enc2

except ImportError:
    pass

