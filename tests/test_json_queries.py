"""Tests for Phase 5 Native JSON & Document Model query capabilities.

Covers:
- CREATE TABLE with JSON and DOCUMENT columns
- CREATE COLLECTION collections
- INSERT with JSON strings and native structures
- SELECT with dot navigation and bracket indexing
- WHERE predicates on JSON properties
- Three-state truth table: IS NULL, IS MISSING, IS JSON NULL
- Built-in JSON functions: JSON_EXTRACT, JSON_VALUE, JSON_EXISTS, JSON_SET, JSON_REMOVE
- Ambiguity resolution between table aliases, columns, and properties
"""

from __future__ import annotations

import pytest

from aaysql import Engine as AayushSQL
from aaysql.errors import ConstraintError
from aaysql.json_value import JSON_NULL


def test_create_table_with_json_and_document_types():
    db = AayushSQL()
    db.execute("AAYUSH CREATE TABLE events (id INT PRIMARY KEY, payload JSON, meta DOCUMENT);")
    tbl = db.db.table("events")
    assert tbl.column_names == ["id", "payload", "meta"]
    assert tbl.columns[1].type_name == "JSON"
    assert tbl.columns[2].type_name == "DOCUMENT"


def test_create_collection():
    db = AayushSQL()
    res = db.execute("AAYUSH CREATE COLLECTION customers;")
    assert res["status"] == "ok"
    tbl = db.db.table("customers")
    assert tbl.column_names == ["_id", "_rev", "body"]
    assert tbl.columns[0].primary_key is True
    assert tbl.columns[0].type_name == "TEXT"
    assert tbl.columns[1].type_name == "INT"
    assert tbl.columns[2].type_name == "JSON"


def test_insert_and_select_json_documents():
    db = AayushSQL()
    db.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, profile JSON);")
    db.execute(
        "AAYUSH INSERT INTO users (id, profile) VALUES "
        "(1, '{\"name\": \"Alice\", \"role\": \"admin\", \"tags\": [\"core\", \"db\"]}'), "
        "(2, '{\"name\": \"Bob\", \"role\": \"engineer\", \"tags\": [\"ui\"]}');"
    )

    res = db.execute("AAYUSH SELECT id, profile.name, profile.role FROM users ORDER BY id;")
    rows = res["rows"]
    assert len(rows) == 2
    assert rows[0]["id"] == 1
    assert rows[0]["profile.name"] == "Alice"
    assert rows[0]["profile.role"] == "admin"
    assert rows[1]["id"] == 2
    assert rows[1]["profile.name"] == "Bob"
    assert rows[1]["profile.role"] == "engineer"


def test_select_bracket_indexing():
    db = AayushSQL()
    db.execute("AAYUSH CREATE TABLE logs (id INT PRIMARY KEY, tags JSON);")
    db.execute("AAYUSH INSERT INTO logs (id, tags) VALUES (1, '[\"red\", \"green\", \"blue\"]');")

    res = db.execute("AAYUSH SELECT tags[0], tags[1], tags[2] FROM logs;")
    assert res["rows"][0]["tags[0]"] == "red"
    assert res["rows"][0]["tags[1]"] == "green"
    assert res["rows"][0]["tags[2]"] == "blue"


def test_select_table_qualified_json_paths():
    db = AayushSQL()
    db.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, profile JSON);")
    db.execute(
        "AAYUSH INSERT INTO users (id, profile) VALUES "
        "(1, '{\"address\": {\"city\": \"Bengaluru\", \"geo\": {\"lat\": 12.97}}}');"
    )

    res = db.execute("AAYUSH SELECT u.profile.address.city, u.profile.address.geo.lat FROM users u;")
    assert res["rows"][0]["u.profile.address.city"] == "Bengaluru"
    assert res["rows"][0]["u.profile.address.geo.lat"] == 12.97

    res2 = db.execute("AAYUSH SELECT users.profile.address.city FROM users;")
    assert res2["rows"][0]["users.profile.address.city"] == "Bengaluru"


def test_where_predicate_on_json_path():
    db = AayushSQL()
    db.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, profile JSON);")
    db.execute(
        "AAYUSH INSERT INTO users (id, profile) VALUES "
        "(1, '{\"name\": \"Alice\", \"score\": 95}'), "
        "(2, '{\"name\": \"Bob\", \"score\": 80}'), "
        "(3, '{\"name\": \"Charlie\", \"score\": 95}');"
    )

    res = db.execute("AAYUSH SELECT id, profile.name FROM users WHERE profile.score = 95 ORDER BY id;")
    assert [r["id"] for r in res["rows"]] == [1, 3]

    res2 = db.execute("AAYUSH SELECT id FROM users WHERE profile.score > 85;")
    assert [r["id"] for r in res2["rows"]] == [1, 3]


def test_three_state_truth_table():
    """Verify ADR-07 and Validation Plan section 3.1 truth table.

    States:
    Row 1: sql_null (SQL column is NULL)
    Row 2: json_null (profile.status is explicit JSON null)
    Row 3: absent (profile.status key does not exist)
    Row 4: present (profile.status is 'active')
    """
    db = AayushSQL()
    db.execute("AAYUSH CREATE TABLE items (id INT PRIMARY KEY, profile JSON);")
    db.execute(
        "AAYUSH INSERT INTO items (id, profile) VALUES "
        "(1, NULL), "
        "(2, '{\"status\": null}'), "
        "(3, '{\"other\": 1}'), "
        "(4, '{\"status\": \"active\"}');"
    )

    # IS NULL: true for SQL NULL, JSON NULL, ABSENT (rows 1, 2, 3)
    res_null = db.execute("AAYUSH SELECT id FROM items WHERE profile.status IS NULL ORDER BY id;")
    assert [r["id"] for r in res_null["rows"]] == [1, 2, 3]

    # IS NOT NULL: true only for present (row 4)
    res_not_null = db.execute("AAYUSH SELECT id FROM items WHERE profile.status IS NOT NULL ORDER BY id;")
    assert [r["id"] for r in res_not_null["rows"]] == [4]

    # IS MISSING: true only for ABSENT (row 1, 3)
    res_missing = db.execute("AAYUSH SELECT id FROM items WHERE profile.status IS MISSING ORDER BY id;")
    assert [r["id"] for r in res_missing["rows"]] == [1, 3]

    # IS NOT MISSING: true when path exists, even if JSON null (row 2, 4)
    res_not_missing = db.execute("AAYUSH SELECT id FROM items WHERE profile.status IS NOT MISSING ORDER BY id;")
    assert [r["id"] for r in res_not_missing["rows"]] == [2, 4]

    # IS JSON NULL: true only for explicit JSON null (row 2)
    res_jnull = db.execute("AAYUSH SELECT id FROM items WHERE profile.status IS JSON NULL ORDER BY id;")
    assert [r["id"] for r in res_jnull["rows"]] == [2]

    # IS NOT JSON NULL: true for non-JSON-null (row 1, 3, 4)
    res_not_jnull = db.execute("AAYUSH SELECT id FROM items WHERE profile.status IS NOT JSON NULL ORDER BY id;")
    assert [r["id"] for r in res_not_jnull["rows"]] == [1, 3, 4]


def test_builtin_json_functions():
    db = AayushSQL()
    db.execute("AAYUSH CREATE TABLE docs (id INT PRIMARY KEY, data JSON);")
    db.execute(
        "AAYUSH INSERT INTO docs (id, data) VALUES "
        "(1, '{\"user\": {\"name\": \"Alice\", \"role\": \"admin\"}, \"tags\": [\"sec\", \"ops\"]}');"
    )

    # JSON_EXTRACT
    res = db.execute("AAYUSH SELECT JSON_EXTRACT(data, '$.user.name') AS name FROM docs;")
    assert res["rows"][0]["name"] == "Alice"

    res = db.execute("AAYUSH SELECT JSON_EXTRACT(data, '$.tags[1]') AS tag FROM docs;")
    assert res["rows"][0]["tag"] == "ops"

    # JSON_EXISTS
    res = db.execute("AAYUSH SELECT JSON_EXISTS(data, '$.user.role') AS has_role, JSON_EXISTS(data, '$.user.email') AS has_email FROM docs;")
    assert res["rows"][0]["has_role"] is True
    assert res["rows"][0]["has_email"] is False

    # JSON_EXISTS in WHERE clause
    res = db.execute("AAYUSH SELECT id FROM docs WHERE JSON_EXISTS(data, '$.user.name');")
    assert len(res["rows"]) == 1

    # JSON_SET function
    res = db.execute("AAYUSH SELECT JSON_SET(data, '$.user.name', 'Alicia') AS updated FROM docs;")
    assert res["rows"][0]["updated"]["user"]["name"] == "Alicia"

    # JSON_REMOVE function
    res = db.execute("AAYUSH SELECT JSON_REMOVE(data, '$.user.role') AS cleaned FROM docs;")
    assert "role" not in res["rows"][0]["cleaned"]["user"]


def test_numeric_type_equivalence_in_predicate():
    db = AayushSQL()
    db.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, doc JSON);")
    db.execute("AAYUSH INSERT INTO t (id, doc) VALUES (1, '{\"count\": 10}'), (2, '{\"count\": 10.0}');")

    # 10 matches 10.0 in JSON numeric comparisons
    res = db.execute("AAYUSH SELECT id FROM t WHERE doc.count = 10 ORDER BY id;")
    assert [r["id"] for r in res["rows"]] == [1, 2]

    res = db.execute("AAYUSH SELECT id FROM t WHERE doc.count = 10.0 ORDER BY id;")
    assert [r["id"] for r in res["rows"]] == [1, 2]


def test_structural_equality_in_predicate():
    db = AayushSQL()
    db.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, doc JSON);")
    db.execute("AAYUSH INSERT INTO t (id, doc) VALUES (1, '{\"a\": 1, \"b\": 2}'), (2, '{\"b\": 2, \"a\": 1}');")

    # Both rows are structurally equal
    res = db.execute("AAYUSH SELECT id FROM t WHERE doc = '{\"b\": 2, \"a\": 1}' ORDER BY id;")
    assert [r["id"] for r in res["rows"]] == [1, 2]


def test_invalid_json_insert_rejected():
    db = AayushSQL()
    db.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, doc JSON);")

    # Duplicate keys
    with pytest.raises(ConstraintError):
        db.execute("AAYUSH INSERT INTO t (id, doc) VALUES (1, '{\"a\": 1, \"a\": 2}');")

    # Non-finite float
    with pytest.raises(ConstraintError):
        db.execute("AAYUSH INSERT INTO t (id, doc) VALUES (2, '{\"a\": NaN}');")

    # Null byte
    with pytest.raises(ConstraintError):
        db.execute("AAYUSH INSERT INTO t (id, doc) VALUES (3, '{\"a\\u0000\": 1}');")


def test_json_extract_vs_value_vs_exists_semantics():
    db = AayushSQL()
    db.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, doc JSON);")
    # Row 1: object with present null, scalar, sub-object, array
    db.execute(
        "AAYUSH INSERT INTO t (id, doc) VALUES (1, "
        "'{\"present_null\": null, \"scalar\": \"text_val\", \"obj\": {\"k\": 1}, \"arr\": [10, 20]}');"
    )
    # Row 2: SQL NULL document
    db.execute("AAYUSH INSERT INTO t (id, doc) VALUES (2, NULL);")
    # Row 3: top-level JSON null
    db.execute("AAYUSH INSERT INTO t (id, doc) VALUES (3, 'null');")

    # 1. JSON_EXTRACT preserves JSON null as JSON_NULL
    res = db.execute("AAYUSH SELECT JSON_EXTRACT(doc, '$.present_null') AS val FROM t WHERE id = 1;")
    assert res["rows"][0]["val"] is JSON_NULL

    # 2. JSON_EXTRACT returns SQL NULL (None) for missing path
    res = db.execute("AAYUSH SELECT JSON_EXTRACT(doc, '$.missing') AS val FROM t WHERE id = 1;")
    assert res["rows"][0]["val"] is None

    # 3. JSON_VALUE returns SQL NULL (None) for JSON null
    res = db.execute("AAYUSH SELECT JSON_VALUE(doc, '$.present_null') AS val FROM t WHERE id = 1;")
    assert res["rows"][0]["val"] is None

    # 4. JSON_VALUE returns SQL NULL (None) for non-scalar objects and arrays
    res = db.execute("AAYUSH SELECT JSON_VALUE(doc, '$.obj') AS val FROM t WHERE id = 1;")
    assert res["rows"][0]["val"] is None
    res = db.execute("AAYUSH SELECT JSON_VALUE(doc, '$.arr') AS val FROM t WHERE id = 1;")
    assert res["rows"][0]["val"] is None

    # 5. JSON_VALUE returns the scalar value
    res = db.execute("AAYUSH SELECT JSON_VALUE(doc, '$.scalar') AS val FROM t WHERE id = 1;")
    assert res["rows"][0]["val"] == "text_val"

    # 6. JSON_EXISTS distinguishes present null from missing path
    res = db.execute(
        "AAYUSH SELECT JSON_EXISTS(doc, '$.present_null') AS has_null, "
        "JSON_EXISTS(doc, '$.missing') AS has_missing FROM t WHERE id = 1;"
    )
    assert res["rows"][0]["has_null"] is True
    assert res["rows"][0]["has_missing"] is False

    # 7. IS JSON NULL applied to extracted values
    res = db.execute("AAYUSH SELECT id FROM t WHERE JSON_EXTRACT(doc, '$.present_null') IS JSON NULL;")
    assert [r["id"] for r in res["rows"]] == [1]

    # JSON_VALUE returns SQL NULL, so IS JSON NULL is False
    res = db.execute("AAYUSH SELECT id FROM t WHERE JSON_VALUE(doc, '$.present_null') IS JSON NULL;")
    assert [r["id"] for r in res["rows"]] == []

    # 8. SQL NULL document input behavior
    res = db.execute(
        "AAYUSH SELECT JSON_EXTRACT(doc, '$.any') AS e, JSON_VALUE(doc, '$.any') AS v, "
        "JSON_EXISTS(doc, '$.any') AS ex FROM t WHERE id = 2;"
    )
    assert res["rows"][0]["e"] is None
    assert res["rows"][0]["v"] is None
    assert res["rows"][0]["ex"] is False

    # 9. Top-level JSON null document input behavior
    res = db.execute("AAYUSH SELECT JSON_EXTRACT(doc, '$') AS root FROM t WHERE id = 3;")
    assert res["rows"][0]["root"] is JSON_NULL

    res = db.execute("AAYUSH SELECT JSON_EXTRACT(doc, '$.child') AS child FROM t WHERE id = 3;")
    assert res["rows"][0]["child"] is None
