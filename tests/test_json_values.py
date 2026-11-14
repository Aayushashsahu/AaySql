import pytest

from aaysql.errors import ConstraintError
from aaysql.json_value import (
    ABSENT,
    INT64_MAX,
    INT64_MIN,
    JSON_NULL,
    MAX_DOCUMENT_BYTES,
    MAX_KEY_LENGTH,
    MAX_NESTING_DEPTH,
    canonical_json_bytes,
    canonical_json_text,
    deep_copy_json,
    extract_path,
    json_structural_equal,
    parse_json_document,
    parse_json_path,
    remove_path,
    set_path,
    validate_json_value,
)


def test_json_parse_primitives():
    assert parse_json_document("true") is True
    assert parse_json_document("false") is False
    assert parse_json_document("null") is JSON_NULL
    assert parse_json_document("1042") == 1042
    assert parse_json_document("-42") == -42
    assert parse_json_document("3.1415") == 3.1415
    assert parse_json_document('"hello world"') == "hello world"


def test_json_parse_structures():
    doc = parse_json_document('{"name": "Aayush", "scores": [10, 20], "active": true, "meta": null}')
    assert doc["name"] == "Aayush"
    assert doc["scores"] == [10, 20]
    assert doc["active"] is True
    assert doc["meta"] is JSON_NULL


def test_json_empty_structures():
    assert parse_json_document("{}") == {}
    assert parse_json_document("[]") == []


def test_json_int64_boundaries():
    assert parse_json_document(str(INT64_MAX)) == INT64_MAX
    assert parse_json_document(str(INT64_MIN)) == INT64_MIN

    with pytest.raises(ConstraintError, match="exceeds 64-bit signed integer range"):
        parse_json_document(str(INT64_MAX + 1))

    with pytest.raises(ConstraintError, match="exceeds 64-bit signed integer range"):
        parse_json_document(str(INT64_MIN - 1))


def test_json_reject_non_finite_floats():
    with pytest.raises(ConstraintError, match="Non-finite"):
        parse_json_document("NaN")

    with pytest.raises(ConstraintError, match="Non-finite"):
        parse_json_document("Infinity")

    with pytest.raises(ConstraintError, match="Non-finite"):
        parse_json_document("-Infinity")

    with pytest.raises(ConstraintError, match="Non-finite"):
        validate_json_value(float("nan"))


def test_json_reject_duplicate_keys():
    with pytest.raises(ConstraintError, match="Duplicate JSON object key 'a' is rejected"):
        parse_json_document('{"a": 1, "b": 2, "a": 3}')


def test_json_reject_null_bytes():
    with pytest.raises(ConstraintError, match="null bytes"):
        parse_json_document('{"key\\u0000": 1}')

    with pytest.raises(ConstraintError, match="null bytes"):
        validate_json_value("bad\u0000str")


def test_json_nesting_depth_limit():
    # Build nested object at limit
    valid = "val"
    for _ in range(MAX_NESTING_DEPTH - 1):
        valid = {"k": valid}
    assert validate_json_value(valid, depth=1) is not None

    too_deep = "val"
    for _ in range(MAX_NESTING_DEPTH + 1):
        too_deep = {"k": too_deep}
    with pytest.raises(ConstraintError, match="nesting depth"):
        validate_json_value(too_deep, depth=1)


def test_json_key_length_limit():
    huge_key = "k" * (MAX_KEY_LENGTH + 1)
    with pytest.raises(ConstraintError, match="maximum allowed length"):
        validate_json_value({huge_key: 1})


def test_json_document_size_limit():
    large_doc = '{"data":"' + 'a' * MAX_DOCUMENT_BYTES + '"}'
    with pytest.raises(ConstraintError, match="exceeds maximum of"):
        parse_json_document(large_doc)


def test_json_canonical_serialization():
    # Key order in input should not change canonical output
    d1 = parse_json_document('{"z": 1, "a": 2, "m": 3}')
    d2 = parse_json_document('{"a": 2, "m": 3, "z": 1}')
    c1 = canonical_json_text(d1)
    c2 = canonical_json_text(d2)
    assert c1 == c2
    assert c1 == '{"a":2,"m":3,"z":1}'
    assert canonical_json_bytes(d1) == b'{"a":2,"m":3,"z":1}'


def test_json_structural_equality():
    d1 = {"a": 1, "b": 2.0}
    d2 = {"b": 2, "a": 1.0}
    assert json_structural_equal(d1, d2)

    # Boolean is not equal to integer
    assert not json_structural_equal(True, 1)
    assert not json_structural_equal(False, 0)
    assert not json_structural_equal(True, 1.0)

    # JSON_NULL vs SQL NULL vs ABSENT
    assert json_structural_equal(JSON_NULL, JSON_NULL)
    assert json_structural_equal(ABSENT, ABSENT)
    assert not json_structural_equal(JSON_NULL, None)
    assert not json_structural_equal(JSON_NULL, ABSENT)
    assert not json_structural_equal(None, ABSENT)


def test_path_parsing():
    assert parse_json_path("$.user.profile.skills[0]") == ("user", "profile", "skills", 0)
    assert parse_json_path("user.profile.skills[0]") == ("user", "profile", "skills", 0)
    assert parse_json_path('["a.b"].c') == ("a.b", "c")
    assert parse_json_path("a[2][3]") == ("a", 2, 3)


def test_path_extraction():
    doc = parse_json_document('{"user": {"name": "Aayush", "tags": ["db", "sql"], "meta": null}}')

    assert extract_path(doc, ("user", "name")) == "Aayush"
    assert extract_path(doc, ("user", "tags", 0)) == "db"
    assert extract_path(doc, ("user", "tags", 1)) == "sql"
    assert extract_path(doc, ("user", "meta")) is JSON_NULL

    # Absent paths
    assert extract_path(doc, ("user", "missing")) is ABSENT
    assert extract_path(doc, ("user", "tags", 99)) is ABSENT
    assert extract_path(doc, ("user", "tags", -1)) is ABSENT
    assert extract_path(doc, ("user", "name", "child")) is ABSENT
    assert extract_path(None, ("user", "name")) is ABSENT


def test_path_mutation_set_copy_on_write():
    original = parse_json_document('{"a": 1, "b": {"c": 2}, "arr": [10, 20]}')
    mutated = set_path(original, ("b", "c"), 99)

    assert extract_path(mutated, ("b", "c")) == 99
    assert extract_path(original, ("b", "c")) == 2

    # Set in array
    mutated_arr = set_path(original, ("arr", 1), 500)
    assert extract_path(mutated_arr, ("arr", 1)) == 500
    assert extract_path(original, ("arr", 1)) == 20

    # Extend array
    extended = set_path(original, ("arr", 3), 30)
    assert extract_path(extended, ("arr", 3)) == 30
    assert extract_path(extended, ("arr", 2)) is JSON_NULL


def test_path_mutation_remove_copy_on_write():
    original = parse_json_document('{"a": 1, "b": {"c": 2, "d": 3}, "arr": [10, 20, 30]}')
    removed_obj = remove_path(original, ("b", "c"))

    assert extract_path(removed_obj, ("b", "c")) is ABSENT
    assert extract_path(removed_obj, ("b", "d")) == 3
    assert extract_path(original, ("b", "c")) == 2

    removed_arr = remove_path(original, ("arr", 1))
    assert extract_path(removed_arr, ("arr",)) == [10, 30]
    assert extract_path(original, ("arr",)) == [10, 20, 30]


def test_deep_copy_isolation():
    doc = {"items": [{"id": 1, "name": "first"}]}
    isolated = deep_copy_json(doc)

    isolated["items"][0]["name"] = "MODIFIED"
    assert doc["items"][0]["name"] == "first"
