"""Tests for Phase 5 Native JSON & Document Model mutations and transaction semantics.

Covers:
- Point mutation with path assignment (UPDATE ... SET profile.role = 'lead')
- Point mutation with JSON_SET
- Point deletion with JSON_REMOVE
- Whole document replace
- Transaction rollback with in-memory undo journal restoring exact pre-transaction JSON
- Failed statement atomicity
- Collections mutation and isolation
"""

from __future__ import annotations

import pytest

from aaysql import Engine as AayushSQL
from aaysql.errors import ConstraintError


def test_update_path_assignment():
    db = AayushSQL()
    db.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, profile JSON);")
    db.execute(
        "AAYUSH INSERT INTO users (id, profile) VALUES "
        "(1, '{\"name\": \"Alice\", \"role\": \"engineer\"}');"
    )

    db.execute("AAYUSH UPDATE users SET profile.role = 'lead' WHERE id = 1;")
    res = db.execute("AAYUSH SELECT profile.role FROM users WHERE id = 1;")
    assert res["rows"][0]["profile.role"] == "lead"


def test_update_with_json_set_and_remove():
    db = AayushSQL()
    db.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, profile JSON);")
    db.execute(
        "AAYUSH INSERT INTO users (id, profile) VALUES "
        "(1, '{\"name\": \"Alice\", \"role\": \"engineer\", \"dept\": \"eng\"}');"
    )

    # JSON_SET adds a new property
    db.execute("AAYUSH UPDATE users SET profile = JSON_SET(profile, '$.level', 'senior') WHERE id = 1;")
    res = db.execute("AAYUSH SELECT profile.level FROM users WHERE id = 1;")
    assert res["rows"][0]["profile.level"] == "senior"

    # JSON_REMOVE removes a property
    db.execute("AAYUSH UPDATE users SET profile = JSON_REMOVE(profile, '$.dept') WHERE id = 1;")
    res = db.execute("AAYUSH SELECT profile.dept IS MISSING AS is_missing FROM users WHERE id = 1;")
    assert res["rows"][0]["is_missing"] is True


def test_update_whole_document_replace():
    db = AayushSQL()
    db.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, profile JSON);")
    db.execute(
        "AAYUSH INSERT INTO users (id, profile) VALUES (1, '{\"name\": \"Alice\"}');"
    )

    db.execute("AAYUSH UPDATE users SET profile = '{\"name\": \"Alicia\", \"archived\": false}' WHERE id = 1;")
    res = db.execute("AAYUSH SELECT profile.name, profile.archived FROM users WHERE id = 1;")
    assert res["rows"][0]["profile.name"] == "Alicia"
    assert res["rows"][0]["profile.archived"] is False


def test_transaction_rollback_restores_json():
    db = AayushSQL()
    db.execute("AAYUSH CREATE TABLE accounts (id INT PRIMARY KEY, meta JSON);")
    db.execute(
        "AAYUSH INSERT INTO accounts (id, meta) VALUES "
        "(1, '{\"balance\": 100, \"history\": [100]}');"
    )

    db.execute("AAYUSH BEGIN;")
    db.execute("AAYUSH UPDATE accounts SET meta.balance = 50 WHERE id = 1;")
    res_in_tx = db.execute("AAYUSH SELECT meta.balance FROM accounts WHERE id = 1;")
    assert res_in_tx["rows"][0]["meta.balance"] == 50

    db.execute("AAYUSH ROLLBACK;")
    res_after = db.execute("AAYUSH SELECT meta.balance, meta.history[0] FROM accounts WHERE id = 1;")
    assert res_after["rows"][0]["meta.balance"] == 100
    assert res_after["rows"][0]["meta.history[0]"] == 100


def test_failed_update_atomicity():
    db = AayushSQL()
    db.execute("AAYUSH CREATE TABLE items (id INT PRIMARY KEY, data JSON);")
    db.execute(
        "AAYUSH INSERT INTO items (id, data) VALUES "
        "(1, '{\"val\": 10}'), "
        "(2, '{\"val\": 20}');"
    )

    # Attempting to assign invalid JSON must fail and leave all rows unmodified
    with pytest.raises(ConstraintError):
        db.execute("AAYUSH UPDATE items SET data = '{\"duplicate\": 1, \"duplicate\": 2}';")

    res = db.execute("AAYUSH SELECT id, data.val FROM items ORDER BY id;")
    assert [r["data.val"] for r in res["rows"]] == [10, 20]


def test_collection_crud_operations():
    db = AayushSQL()
    db.execute("AAYUSH CREATE COLLECTION orders;")

    # Insert into collection
    db.execute(
        "AAYUSH INSERT INTO orders (_id, _rev, body) VALUES "
        "('ord-001', 1, '{\"customer\": \"Alice\", \"total\": 49.99}'), "
        "('ord-002', 1, '{\"customer\": \"Bob\", \"total\": 19.99}');"
    )

    res = db.execute("AAYUSH SELECT _id, body.customer, body.total FROM orders WHERE body.total > 25.0;")
    assert len(res["rows"]) == 1
    assert res["rows"][0]["_id"] == "ord-001"
    assert res["rows"][0]["body.customer"] == "Alice"

    # Update collection body
    db.execute("AAYUSH UPDATE orders SET body.total = 59.99 WHERE _id = 'ord-001';")
    res2 = db.execute("AAYUSH SELECT body.total FROM orders WHERE _id = 'ord-001';")
    assert res2["rows"][0]["body.total"] == 59.99

    # Delete from collection
    db.execute("AAYUSH DELETE FROM orders WHERE _id = 'ord-002';")
    res3 = db.execute("AAYUSH SELECT count(*) AS cnt FROM orders;")
    assert res3["rows"][0]["cnt"] == 1
