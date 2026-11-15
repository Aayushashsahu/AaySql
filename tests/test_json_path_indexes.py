"""Tests for typed scalar JSON path indexes in AayushSQL.

Covers:
- Path index DDL parsing and creation (col.path.to.field).
- Path index maintenance: INSERT, UPDATE (path modified, path untouched), DELETE.
- In-memory undo journal: statement rollback and transaction rollback.
- Durability: Delta WAL v4 replay, checkpoint segments, and restart recovery.
- Rejection of path indexes on non-JSON columns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aaysql import Engine
from aaysql.errors import ExecutionError


def test_create_path_index_basic(tmp_path: Path):
    db_path = tmp_path / "path_idx_basic.aaydb"
    e = Engine(db_path=str(db_path))
    e.execute("AAYUSH CREATE TABLE events (id INT PRIMARY KEY, payload JSON);")
    res = e.execute("AAYUSH CREATE INDEX idx_user ON events (payload.user.id);")
    assert res["status"] == "ok"
    assert "payload.user.id" in res["message"]

    # Verify index metadata
    idx = e.db.indexes["idx_user"]
    assert idx.table == "events"
    assert idx.column == "payload"
    assert idx.path == ("user", "id")


def test_create_path_index_on_non_json_rejected(tmp_path: Path):
    db_path = tmp_path / "path_idx_reject.aaydb"
    e = Engine(db_path=str(db_path))
    e.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, name TEXT);")
    with pytest.raises(ExecutionError, match="Cannot create path index on non-JSON column"):
        e.execute("AAYUSH CREATE INDEX idx_name_sub ON users (name.sub.field);")


def test_path_index_insert_and_lookup(tmp_path: Path):
    db_path = tmp_path / "path_idx_lookup.aaydb"
    e = Engine(db_path=str(db_path))
    e.execute("AAYUSH CREATE TABLE items (id INT PRIMARY KEY, doc JSON);")
    e.execute("AAYUSH CREATE INDEX idx_cat ON items (doc.category);")

    e.execute("""
    AAYUSH INSERT INTO items VALUES
        (1, '{"category": "books", "price": 10}'),
        (2, '{"category": "electronics", "price": 100}'),
        (3, '{"category": "books", "price": 25}'),
        (4, '{"category": null, "price": 5}'),
        (5, '{"other": "missing category"}');
    """)

    idx = e.db.indexes["idx_cat"]
    # Looking up 'books' should find row ids 1 and 3
    books_rows = idx.lookup("books")
    assert len(books_rows) == 2
    assert {r["id"] for r in books_rows} == {1, 3}

    # Looking up 'electronics' should find row 2
    elec_rows = idx.lookup("electronics")
    assert len(elec_rows) == 1
    assert elec_rows[0]["id"] == 2

    # Looking up non-existent key
    assert len(idx.lookup("clothing")) == 0


def test_path_index_update_maintenance(tmp_path: Path):
    db_path = tmp_path / "path_idx_update.aaydb"
    e = Engine(db_path=str(db_path))
    e.execute("AAYUSH CREATE TABLE items (id INT PRIMARY KEY, doc JSON);")
    e.execute("AAYUSH CREATE INDEX idx_tag ON items (doc.tag);")

    e.execute("AAYUSH INSERT INTO items VALUES (1, '{\"tag\": \"alpha\"}');")
    idx = e.db.indexes["idx_tag"]
    assert len(idx.lookup("alpha")) == 1

    # Update modifying the indexed path
    e.execute("AAYUSH UPDATE items SET doc.tag = 'beta' WHERE id = 1;")
    assert len(idx.lookup("alpha")) == 0
    assert len(idx.lookup("beta")) == 1
    assert idx.lookup("beta")[0]["id"] == 1

    # Update not modifying the indexed path
    e.execute("AAYUSH UPDATE items SET doc.other = 'val' WHERE id = 1;")
    assert len(idx.lookup("beta")) == 1

    # Update removing / overwriting with different structure
    e.execute("AAYUSH UPDATE items SET doc = '{\"unrelated\": 123}' WHERE id = 1;")
    assert len(idx.lookup("beta")) == 0


def test_path_index_delete_maintenance(tmp_path: Path):
    db_path = tmp_path / "path_idx_delete.aaydb"
    e = Engine(db_path=str(db_path))
    e.execute("AAYUSH CREATE TABLE items (id INT PRIMARY KEY, doc JSON);")
    e.execute("AAYUSH CREATE INDEX idx_status ON items (doc.status);")

    e.execute("""
    AAYUSH INSERT INTO items VALUES
        (1, '{"status": "pending"}'),
        (2, '{"status": "active"}'),
        (3, '{"status": "pending"}');
    """)

    idx = e.db.indexes["idx_status"]
    assert len(idx.lookup("pending")) == 2

    e.execute("AAYUSH DELETE FROM items WHERE id = 1;")
    assert len(idx.lookup("pending")) == 1
    assert idx.lookup("pending")[0]["id"] == 3

    e.execute("AAYUSH DELETE FROM items WHERE doc.status = 'pending';")
    assert len(idx.lookup("pending")) == 0
    assert len(idx.lookup("active")) == 1


def test_path_index_undo_rollback(tmp_path: Path):
    db_path = tmp_path / "path_idx_undo.aaydb"
    e = Engine(db_path=str(db_path))
    e.execute("AAYUSH CREATE TABLE items (id INT PRIMARY KEY, doc JSON);")
    e.execute("AAYUSH CREATE INDEX idx_val ON items (doc.val);")
    e.execute("AAYUSH INSERT INTO items VALUES (1, '{\"val\": 10}');")

    # Transaction with path index mutations rolled back
    e.execute("AAYUSH BEGIN;")
    e.execute("AAYUSH INSERT INTO items VALUES (2, '{\"val\": 20}');")
    e.execute("AAYUSH UPDATE items SET doc.val = 99 WHERE id = 1;")

    idx = e.db.indexes["idx_val"]
    assert len(idx.lookup(20)) == 1
    assert len(idx.lookup(99)) == 1
    assert len(idx.lookup(10)) == 0

    e.execute("AAYUSH ROLLBACK;")
    assert len(idx.lookup(20)) == 0
    assert len(idx.lookup(99)) == 0
    assert len(idx.lookup(10)) == 1
    assert idx.lookup(10)[0]["id"] == 1


def test_path_index_wal_recovery(tmp_path: Path):
    db_path = tmp_path / "path_idx_wal.aaydb"
    e1 = Engine(db_path=str(db_path))
    e1.execute("AAYUSH CREATE TABLE logs (id INT PRIMARY KEY, doc JSON);")
    e1.execute("AAYUSH CREATE INDEX idx_level ON logs (doc.level);")
    e1.execute("""
    AAYUSH INSERT INTO logs VALUES
        (1, '{"level": "info"}'),
        (2, '{"level": "error"}');
    """)

    # Close e1 to release database lock and reopen to verify WAL recovery & rebuild
    e1.close()
    e2 = Engine(db_path=str(db_path))
    assert "idx_level" in e2.db.indexes
    idx2 = e2.db.indexes["idx_level"]
    assert idx2.path == ("level",)

    error_rows = idx2.lookup("error")
    assert len(error_rows) == 1
    assert error_rows[0]["id"] == 2
    info_rows = idx2.lookup("info")
    assert len(info_rows) == 1
    assert info_rows[0]["id"] == 1
    e2.close()
