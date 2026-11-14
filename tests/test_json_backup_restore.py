"""Integration tests for JSON database offline backup and restore.

Covers:
- Offline backup and restore for tables with JSON columns and collections
- Preserving distinction between SQL NULL, top-level JSON null, nested JSON null,
  JSON string, and JSON number across backup and restore
- Preserving collection constraints (_id, _rev, body NOT NULL)
- Verifying the source database remains unchanged
- Tampered or corrupted backup artifacts fail closed
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aaysql import Engine
from aaysql.backup import (
    BackupError,
    backup_database,
    restore_database,
    verify_backup,
)
from aaysql.errors import ConstraintError
from aaysql.json_value import JSON_NULL


def test_json_backup_and_restore_all_values_and_collections(tmp_path: Path):
    src_db = tmp_path / "source.aaydb"
    backup_dir = tmp_path / "backup_dir"
    restored_db = tmp_path / "restored.aaydb"

    # Step 1: Create source database with JSON table and Collection
    src_engine = Engine(db_path=str(src_db))
    src_engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, doc JSON);")
    src_engine.execute("AAYUSH INSERT INTO t (id, doc) VALUES (1, NULL);")
    src_engine.execute("AAYUSH INSERT INTO t (id, doc) VALUES (2, 'null');")
    src_engine.execute("AAYUSH INSERT INTO t (id, doc) VALUES (3, '{\"value\": null, \"meta\": [1, null]}');")
    src_engine.execute("AAYUSH INSERT INTO t (id, doc) VALUES (4, '\"null\"');")
    src_engine.execute("AAYUSH INSERT INTO t (id, doc) VALUES (5, '42');")

    src_engine.execute("AAYUSH CREATE COLLECTION customers;")
    src_engine.execute("AAYUSH INSERT INTO customers VALUES ('c1', 1, '{\"name\": \"Alice\"}');")
    src_engine.execute("AAYUSH INSERT INTO customers VALUES ('c2', 2, 'null');")

    src_engine.close()

    # Step 2: Perform offline backup
    manifest = backup_database(src_db, backup_dir)
    assert manifest["backup_format"] == "aaysql-backup"

    # Step 3: Verify the backup artifact without restoring
    verified = verify_backup(backup_dir)
    assert verified["backup_format"] == "aaysql-backup"

    # Step 4: Restore backup into a new database target
    restore_report = restore_database(backup_dir, restored_db)
    assert restore_report["restored"] == str(restored_db)

    # Step 5: Verify restored database contents and constraints
    restored_engine = Engine(db_path=str(restored_db))

    # Verify table t rows
    res = restored_engine.execute("AAYUSH SELECT id, doc FROM t ORDER BY id;")
    rows = res["rows"]
    assert len(rows) == 5

    # Row 1: SQL NULL
    assert rows[0]["id"] == 1
    assert rows[0]["doc"] is None

    # Row 2: Top-level JSON null
    assert rows[1]["id"] == 2
    assert rows[1]["doc"] is JSON_NULL

    # Row 3: Nested JSON null
    assert rows[2]["id"] == 3
    assert rows[2]["doc"]["value"] is JSON_NULL
    assert rows[2]["doc"]["meta"][1] is JSON_NULL

    # Row 4: JSON string "null"
    assert rows[3]["id"] == 4
    assert rows[3]["doc"] == "null"
    assert isinstance(rows[3]["doc"], str)

    # Row 5: JSON number 42
    assert rows[4]["id"] == 5
    assert rows[4]["doc"] == 42
    assert isinstance(rows[4]["doc"], int)

    # Verify queries on restored database
    r_null = restored_engine.execute("AAYUSH SELECT id FROM t WHERE doc IS NULL ORDER BY id;")
    assert [r["id"] for r in r_null["rows"]] == [1, 2]

    r_jnull = restored_engine.execute("AAYUSH SELECT id FROM t WHERE doc IS JSON NULL ORDER BY id;")
    assert [r["id"] for r in r_jnull["rows"]] == [2]

    # Verify collection on restored database
    c_rows = restored_engine.execute("AAYUSH SELECT _id, _rev, body FROM customers ORDER BY _id;")["rows"]
    assert len(c_rows) == 2
    assert c_rows[0]["_id"] == "c1"
    assert c_rows[0]["body"]["name"] == "Alice"
    assert c_rows[1]["_id"] == "c2"
    assert c_rows[1]["body"] is JSON_NULL

    # Verify constraints enforced on restored collection
    with pytest.raises(ConstraintError, match="cannot be NULL"):
        restored_engine.execute("AAYUSH INSERT INTO customers VALUES ('c3', 3, NULL);")

    restored_engine.close()

    # Step 6: Verify source database remains valid and unchanged
    src_engine2 = Engine(db_path=str(src_db))
    src_rows = src_engine2.execute("AAYUSH SELECT id, doc FROM t ORDER BY id;")["rows"]
    assert len(src_rows) == 5
    assert src_rows[1]["doc"] is JSON_NULL
    src_engine2.close()


def test_tampered_json_backup_fails_closed(tmp_path: Path):
    src_db = tmp_path / "src_tamper.aaydb"
    backup_dir = tmp_path / "tamper_backup"
    target_db = tmp_path / "target_tamper.aaydb"

    e = Engine(db_path=str(src_db))
    e.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, doc JSON);")
    e.execute("AAYUSH INSERT INTO t VALUES (1, '{\"secret\": 123}');")
    e.close()

    backup_database(src_db, backup_dir)

    # Tamper with the manifest file in the backup
    manifest_file = backup_dir / "manifest.json"
    manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest_data["database_sha256"] = "0" * 64
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    with pytest.raises(BackupError, match="checksum mismatch"):
        verify_backup(backup_dir)

    with pytest.raises(BackupError, match="checksum mismatch"):
        restore_database(backup_dir, target_db)
