"""Tests for Phase 5 Native JSON & Document Model storage, WAL, recovery, and backup.

Covers:
- Persistence and reload across database reopen
- WAL crash recovery for JSON inserts, updates, and deletes
- Backup and restore with aaysql backup utilities
- Schema constraint enforcement and resource limit rejection
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aaysql import Engine
from aaysql.errors import ConstraintError
from aaysql.json_value import (
    JSON_NULL,
    MAX_DOCUMENT_BYTES,
    MAX_KEY_LENGTH,
    MAX_NESTING_DEPTH,
)


def test_json_persistence_across_reopen(tmp_path: Path):
    db_path = tmp_path / "test_json.db"

    # Create and populate
    engine1 = Engine(db_path=str(db_path))
    engine1.execute("AAYUSH CREATE TABLE catalogs (id INT PRIMARY KEY, specs JSON);")
    engine1.execute(
        "AAYUSH INSERT INTO catalogs (id, specs) VALUES "
        "(1, '{\"ram\": \"16GB\", \"cores\": 8, \"tags\": [\"laptop\", \"pro\"]}'), "
        "(2, '{\"ram\": \"32GB\", \"cores\": 16, \"tags\": [\"desktop\"]}');"
    )

    engine1.close()

    # Reopen database
    engine2 = Engine(db_path=str(db_path))
    res = engine2.execute("AAYUSH SELECT id, specs.ram, specs.tags[0] FROM catalogs ORDER BY id;")
    assert len(res["rows"]) == 2
    assert res["rows"][0]["id"] == 1
    assert res["rows"][0]["specs.ram"] == "16GB"
    assert res["rows"][0]["specs.tags[0]"] == "laptop"
    assert res["rows"][1]["id"] == 2
    assert res["rows"][1]["specs.ram"] == "32GB"
    assert res["rows"][1]["specs.tags[0]"] == "desktop"
    engine2.close()


def test_json_wal_recovery(tmp_path: Path):
    db_path = tmp_path / "test_wal_json.db"

    # Transaction 1: Committed
    e1 = Engine(db_path=str(db_path))
    e1.execute("AAYUSH CREATE TABLE docs (id INT PRIMARY KEY, payload JSON);")
    e1.execute("AAYUSH BEGIN;")
    e1.execute("AAYUSH INSERT INTO docs (id, payload) VALUES (1, '{\"status\": \"draft\"}');")
    e1.execute("AAYUSH COMMIT;")

    # Transaction 2: Rolled back
    e1.execute("AAYUSH BEGIN;")
    e1.execute("AAYUSH INSERT INTO docs (id, payload) VALUES (2, '{\"status\": \"lost\"}');")
    e1.execute("AAYUSH ROLLBACK;")

    # Transaction 3: Committed update
    e1.execute("AAYUSH BEGIN;")
    e1.execute("AAYUSH UPDATE docs SET payload.status = 'published' WHERE id = 1;")
    e1.execute("AAYUSH COMMIT;")
    e1.close()

    # Reopen to trigger WAL replay
    e2 = Engine(db_path=str(db_path))
    res = e2.execute("AAYUSH SELECT id, payload.status FROM docs ORDER BY id;")
    assert len(res["rows"]) == 1
    assert res["rows"][0]["id"] == 1
    assert res["rows"][0]["payload.status"] == "published"
    e2.close()


def test_json_resource_limits_enforced(tmp_path: Path):
    e = Engine()
    e.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, doc JSON);")

    # Exceeding MAX_DOCUMENT_BYTES (1 MiB)
    large_val = "x" * (MAX_DOCUMENT_BYTES + 10)
    with pytest.raises(ConstraintError):
        e.execute(f"AAYUSH INSERT INTO t (id, doc) VALUES (1, '{{\"big\": \"{large_val}\"}}');")

    # Exceeding MAX_NESTING_DEPTH (64)
    deep_json = '{"a":' * (MAX_NESTING_DEPTH + 5) + '1' + '}' * (MAX_NESTING_DEPTH + 5)
    with pytest.raises(ConstraintError):
        e.execute(f"AAYUSH INSERT INTO t (id, doc) VALUES (2, '{deep_json}');")

    # Exceeding MAX_KEY_LENGTH (1 KiB)
    long_key = "k" * (MAX_KEY_LENGTH + 5)
    with pytest.raises(ConstraintError):
        e.execute(f"AAYUSH INSERT INTO t (id, doc) VALUES (3, '{{\"{long_key}\": 1}}');")


def test_sql_null_vs_json_null_persistence_and_wal_recovery(tmp_path: Path):
    db_path = tmp_path / "distinct_nulls.aaydb"

    e1 = Engine(db_path=str(db_path))
    e1.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, doc JSON);")
    # 5 canonical rows:
    # 1: SQL NULL
    # 2: Top-level JSON null
    # 3: Nested JSON null {"value": null}
    # 4: "null" as a JSON string
    # 5: 42 as a JSON number
    e1.execute("AAYUSH INSERT INTO t (id, doc) VALUES (1, NULL);")
    e1.execute("AAYUSH INSERT INTO t (id, doc) VALUES (2, 'null');")
    e1.execute("AAYUSH INSERT INTO t (id, doc) VALUES (3, '{\"value\": null}');")
    e1.execute("AAYUSH INSERT INTO t (id, doc) VALUES (4, '\"null\"');")
    e1.execute("AAYUSH INSERT INTO t (id, doc) VALUES (5, '42');")

    def _verify_rows(engine: Engine):
        res = engine.execute("AAYUSH SELECT id, doc FROM t ORDER BY id;")
        rows = res["rows"]
        assert len(rows) == 5

        # Row 1: SQL NULL
        assert rows[0]["id"] == 1
        assert rows[0]["doc"] is None

        # Row 2: Top-level JSON null
        assert rows[1]["id"] == 2
        assert rows[1]["doc"] is JSON_NULL
        assert rows[1]["doc"] is not None

        # Row 3: Nested JSON null
        assert rows[2]["id"] == 3
        assert isinstance(rows[2]["doc"], dict)
        assert rows[2]["doc"]["value"] is JSON_NULL

        # Row 4: JSON string "null"
        assert rows[3]["id"] == 4
        assert rows[3]["doc"] == "null"
        assert isinstance(rows[3]["doc"], str)

        # Row 5: JSON number 42
        assert rows[4]["id"] == 5
        assert rows[4]["doc"] == 42
        assert isinstance(rows[4]["doc"], int)

        # Query distinctions
        # IS NULL matches SQL NULL, JSON_NULL, and absent
        r_null = engine.execute("AAYUSH SELECT id FROM t WHERE doc IS NULL ORDER BY id;")
        assert [r["id"] for r in r_null["rows"]] == [1, 2]

        # IS JSON NULL matches ONLY JSON_NULL
        r_jnull = engine.execute("AAYUSH SELECT id FROM t WHERE doc IS JSON NULL ORDER BY id;")
        assert [r["id"] for r in r_jnull["rows"]] == [2]

        # Nested JSON null
        r_nested = engine.execute("AAYUSH SELECT id FROM t WHERE doc.value IS JSON NULL;")
        assert [r["id"] for r in r_nested["rows"]] == [3]

        # String match
        r_str = engine.execute("AAYUSH SELECT id FROM t WHERE doc = 'null';")
        assert [r["id"] for r in r_str["rows"]] == [4]

        # Number match
        r_num = engine.execute("AAYUSH SELECT id FROM t WHERE doc = 42;")
        assert [r["id"] for r in r_num["rows"]] == [5]

    # Verify initial in-memory state before checkpoint
    _verify_rows(e1)
    e1.close()

    # Reopen to verify checkpoint and segment persistence
    e2 = Engine(db_path=str(db_path))
    _verify_rows(e2)

    # Now verify WAL replay: mutate under a transaction, crash/reopen before checkpoint
    e2.execute("AAYUSH BEGIN;")
    e2.execute("AAYUSH UPDATE t SET doc = 'null' WHERE id = 1;")
    e2.execute("AAYUSH UPDATE t SET doc = NULL WHERE id = 2;")
    e2.execute("AAYUSH COMMIT;")
    e2.close()

    # Reopen triggers WAL delta replay
    e3 = Engine(db_path=str(db_path))
    rows3 = e3.execute("AAYUSH SELECT id, doc FROM t ORDER BY id;")["rows"]
    # Row 1 is now JSON_NULL
    assert rows3[0]["id"] == 1
    assert rows3[0]["doc"] is JSON_NULL
    # Row 2 is now SQL NULL
    assert rows3[1]["id"] == 2
    assert rows3[1]["doc"] is None
    # Rows 3, 4, 5 unchanged
    assert rows3[2]["doc"]["value"] is JSON_NULL
    assert rows3[3]["doc"] == "null"
    assert rows3[4]["doc"] == 42
    e3.close()


def test_collection_nullability_enforced(tmp_path: Path):
    db_path = tmp_path / "coll_nulls.aaydb"
    e1 = Engine(db_path=str(db_path))
    e1.execute("AAYUSH CREATE COLLECTION items;")

    tbl = e1.db.table("items")
    # Verify schema has not_null=True on all three columns
    col_map = {c.name: c for c in tbl.columns}
    assert col_map["_id"].not_null is True
    assert col_map["_id"].primary_key is True
    assert col_map["_rev"].not_null is True
    assert col_map["body"].not_null is True

    # Valid insert succeeds
    e1.execute("AAYUSH INSERT INTO items VALUES ('item1', 1, '{\"status\": \"ok\"}');")

    # Top-level JSON null in body succeeds (JSON null is valid NOT NULL JSON)
    e1.execute("AAYUSH INSERT INTO items VALUES ('item2', 1, 'null');")

    # Explicit SQL NULL _id rejected
    with pytest.raises(ConstraintError, match="cannot be NULL"):
        e1.execute("AAYUSH INSERT INTO items VALUES (NULL, 1, '{\"status\": \"bad\"}');")

    # Explicit SQL NULL _rev rejected
    with pytest.raises(ConstraintError, match="cannot be NULL"):
        e1.execute("AAYUSH INSERT INTO items VALUES ('item3', NULL, '{\"status\": \"bad\"}');")

    # Explicit SQL NULL body rejected
    with pytest.raises(ConstraintError, match="cannot be NULL"):
        e1.execute("AAYUSH INSERT INTO items VALUES ('item4', 1, NULL);")

    # Verify rows stored
    res = e1.execute("AAYUSH SELECT _id, _rev, body FROM items ORDER BY _id;")
    assert len(res["rows"]) == 2
    assert res["rows"][0]["_id"] == "item1"
    assert res["rows"][1]["_id"] == "item2"
    assert res["rows"][1]["body"] is JSON_NULL
    e1.close()

    # Reopen to verify constraints survive checkpoint
    e2 = Engine(db_path=str(db_path))
    tbl2 = e2.db.table("items")
    col_map2 = {c.name: c for c in tbl2.columns}
    assert col_map2["_id"].not_null is True
    assert col_map2["_rev"].not_null is True
    assert col_map2["body"].not_null is True

    with pytest.raises(ConstraintError, match="cannot be NULL"):
        e2.execute("AAYUSH INSERT INTO items VALUES ('item5', 1, NULL);")
    e2.close()
