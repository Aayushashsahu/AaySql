"""Tests for collection schema versioning, ALTER COLLECTION, and VALIDATE COLLECTION."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from aaysql.engine import Engine
from aaysql.errors import ConstraintError, ExecutionError


def test_alter_validation_mode(tmp_path):
    engine = Engine(tmp_path / "test.db")
    engine.execute("AAYUSH CREATE COLLECTION orders;")
    table = engine.db.table("orders")
    assert table.validation_mode == "FLEXIBLE"

    res = engine.execute("AAYUSH ALTER COLLECTION orders SET VALIDATION MODE = 'WARN';")
    assert res["status"] == "ok"
    assert table.validation_mode == "WARN"

    res = engine.execute("AAYUSH ALTER COLLECTION orders SET VALIDATION MODE = 'STRICT';")
    assert res["status"] == "ok"
    assert table.validation_mode == "STRICT"

    with pytest.raises(ExecutionError, match="Invalid validation mode"):
        engine.execute("AAYUSH ALTER COLLECTION orders SET VALIDATION MODE = 'INVALID';")


def test_alter_schema_and_version_increment(tmp_path):
    engine = Engine(tmp_path / "test.db")
    engine.execute("AAYUSH CREATE COLLECTION profiles;")
    table = engine.db.table("profiles")
    assert table.schema_version == 1
    assert table.schema is None

    schema_v2 = json.dumps({
        "type": "object",
        "required": ["username"],
        "properties": {"username": {"type": "string"}},
    })
    res = engine.execute(f"AAYUSH ALTER COLLECTION profiles SET SCHEMA = '{schema_v2}';")
    assert res["status"] == "ok"
    assert table.schema_version == 2
    assert table.schema == json.loads(schema_v2)

    schema_v3 = json.dumps({
        "type": "object",
        "required": ["username", "email"],
        "properties": {
            "username": {"type": "string"},
            "email": {"type": "string"},
        },
    })
    engine.execute(f"AAYUSH ALTER COLLECTION profiles SET SCHEMA = '{schema_v3}';")
    assert table.schema_version == 3


def test_validate_collection_command(tmp_path):
    engine = Engine(tmp_path / "test.db")
    engine.execute("AAYUSH CREATE COLLECTION products;")

    # Insert 3 products in FLEXIBLE mode
    engine.execute("AAYUSH INSERT INTO products (body) VALUES ('{\"sku\": \"A1\", \"price\": 10}');")
    engine.execute("AAYUSH INSERT INTO products (body) VALUES ('{\"sku\": \"A2\", \"price\": -5}');")
    engine.execute("AAYUSH INSERT INTO products (body) VALUES ('{\"invalid\": true}');")

    # Before schema, all are valid
    res = engine.execute("AAYUSH VALIDATE COLLECTION products;")
    assert res["status"] == "ok"
    assert res["total_rows"] == 3
    assert res["valid_rows"] == 3
    assert res["invalid_rows"] == 0

    # Add schema requiring sku and positive price
    schema = json.dumps({
        "type": "object",
        "required": ["sku", "price"],
        "properties": {
            "sku": {"type": "string"},
            "price": {"type": "number", "minimum": 0},
        },
    })
    engine.execute(f"AAYUSH ALTER COLLECTION products SET SCHEMA = '{schema}';")

    # Now VALIDATE COLLECTION reports 1 valid, 2 invalid
    res = engine.execute("AAYUSH VALIDATE COLLECTION products;")
    assert res["status"] == "ok"
    assert res["total_rows"] == 3
    assert res["valid_rows"] == 1
    assert res["invalid_rows"] == 2
    assert len(res["violations"]) == 2
    reasons = [v["violations"][0]["reason"] for v in res["violations"]]
    assert any("minimum 0" in r for r in reasons)
    assert any("required property" in r for r in reasons)


def test_alter_collection_undo_rollback(tmp_path):
    engine = Engine(tmp_path / "test.db")
    engine.execute("AAYUSH CREATE COLLECTION items;")
    table = engine.db.table("items")
    assert table.validation_mode == "FLEXIBLE"
    assert table.schema_version == 1

    engine.execute("AAYUSH BEGIN TRANSACTION;")
    engine.execute("AAYUSH ALTER COLLECTION items SET VALIDATION MODE = 'STRICT';")
    schema = json.dumps({"type": "object", "required": ["x"]})
    engine.execute(f"AAYUSH ALTER COLLECTION items SET SCHEMA = '{schema}';")

    assert table.validation_mode == "STRICT"
    assert table.schema_version == 2
    assert table.schema is not None

    engine.execute("AAYUSH ROLLBACK;")

    assert table.validation_mode == "FLEXIBLE"
    assert table.schema_version == 1
    assert table.schema is None


def test_collection_metadata_persistence_and_recovery(tmp_path):
    db_path = tmp_path / "persist.db"
    e1 = Engine(db_path)
    schema = json.dumps({
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string"}},
    })
    e1.execute(f"AAYUSH CREATE COLLECTION customers VALIDATION MODE = 'STRICT' SCHEMA = '{schema}';")
    e1.execute("AAYUSH INSERT INTO customers (body) VALUES ('{\"name\": \"Alice\"}');")
    e1.close()

    # Reopen and verify metadata recovered from WAL / segments
    e2 = Engine(db_path)
    table = e2.db.table("customers")
    assert table.is_collection is True
    assert table.validation_mode == "STRICT"
    assert table.schema_version == 1
    assert table.schema == json.loads(schema)

    # STRICT mode still active after recovery
    with pytest.raises(ConstraintError):
        e2.execute("AAYUSH INSERT INTO customers (body) VALUES ('{\"wrong\": 123}');")

    e2.close()
