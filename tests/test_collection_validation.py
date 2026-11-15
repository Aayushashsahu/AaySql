"""Tests for collection validation modes and JSON Schema validator."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from aaysql.engine import Engine
from aaysql.errors import ConstraintError, ExecutionError


def test_flexible_mode_default(tmp_path):
    engine = Engine(tmp_path / "test.db")
    engine.execute("AAYUSH CREATE COLLECTION logs;")

    # Any valid JSON is accepted
    engine.execute("AAYUSH INSERT INTO logs (body) VALUES ('{\"msg\": \"hello\"}');")
    engine.execute("AAYUSH INSERT INTO logs (body) VALUES ('[1, 2, 3]');")
    engine.execute("AAYUSH INSERT INTO logs (body) VALUES ('\"just a string\"');")
    engine.execute("AAYUSH INSERT INTO logs (body) VALUES ('42');")

    res = engine.execute("AAYUSH SELECT COUNT(*) AS cnt FROM logs;")
    assert res["rows"][0]["cnt"] == 4


def test_strict_mode_rejects_invalid_insert(tmp_path):
    engine = Engine(tmp_path / "test.db")
    schema = json.dumps({
        "type": "object",
        "required": ["name", "age"],
        "properties": {
            "name": {"type": "string", "minLength": 2},
            "age": {"type": "integer", "minimum": 0, "maximum": 150},
        },
        "additionalProperties": False,
    })

    engine.execute(f"AAYUSH CREATE COLLECTION users VALIDATION MODE = 'STRICT' SCHEMA = '{schema}';")

    # Valid insert succeeds
    res = engine.execute("AAYUSH INSERT INTO users (body) VALUES ('{\"name\": \"Alice\", \"age\": 30}');")
    assert res["status"] == "ok"

    # Missing required field "age"
    with pytest.raises(ConstraintError, match="required property 'age'"):
        engine.execute("AAYUSH INSERT INTO users (body) VALUES ('{\"name\": \"Bob\"}');")

    # Name too short
    with pytest.raises(ConstraintError, match="name"):
        engine.execute("AAYUSH INSERT INTO users (body) VALUES ('{\"name\": \"A\", \"age\": 25}');")

    # Age negative
    with pytest.raises(ConstraintError, match="age"):
        engine.execute("AAYUSH INSERT INTO users (body) VALUES ('{\"name\": \"Charlie\", \"age\": -5}');")

    # Additional property disallowed
    with pytest.raises(ConstraintError, match="additional property 'extra'"):
        engine.execute("AAYUSH INSERT INTO users (body) VALUES ('{\"name\": \"Dave\", \"age\": 40, \"extra\": true}');")

    # Ensure table only has 1 valid row
    cnt = engine.execute("AAYUSH SELECT COUNT(*) AS cnt FROM users;")
    assert cnt["rows"][0]["cnt"] == 1


def test_strict_mode_rejects_invalid_update(tmp_path):
    engine = Engine(tmp_path / "test.db")
    schema = json.dumps({
        "type": "object",
        "properties": {
            "status": {"enum": ["active", "suspended", "deleted"]},
        },
    })
    engine.execute(f"AAYUSH CREATE COLLECTION accounts VALIDATION MODE = 'STRICT' SCHEMA = '{schema}';")
    engine.execute("AAYUSH INSERT INTO accounts (body) VALUES ('{\"status\": \"active\"}');")

    # Valid update
    engine.execute("AAYUSH UPDATE accounts SET body = '{\"status\": \"suspended\"}';")

    # Invalid update raises ConstraintError and does not change data
    with pytest.raises(ConstraintError, match="status"):
        engine.execute("AAYUSH UPDATE accounts SET body = '{\"status\": \"unknown\"}';")

    row = engine.execute("AAYUSH SELECT body.status FROM accounts;")
    assert row["rows"][0]["body.status"] == "suspended"


def test_warn_mode_increments_warning_counters(tmp_path):
    engine = Engine(tmp_path / "test.db")
    schema = json.dumps({
        "type": "object",
        "required": ["score"],
        "properties": {
            "score": {"type": "number", "minimum": 0},
        },
    })
    engine.execute(f"AAYUSH CREATE COLLECTION metrics VALIDATION MODE = 'WARN' SCHEMA = '{schema}';")

    # Valid insert
    engine.execute("AAYUSH INSERT INTO metrics (body) VALUES ('{\"score\": 10}');")
    table = engine.db.table("metrics")
    assert table.warn_count == 0
    assert table.last_warn is None

    # Invalid insert is allowed in WARN mode, but increments warn_count and sets last_warn
    engine.execute("AAYUSH INSERT INTO metrics (body) VALUES ('{\"score\": -5}');")
    assert table.warn_count == 1
    assert table.last_warn is not None
    assert "score" in table.last_warn["violations"][0]["path"]

    # Another invalid insert
    engine.execute("AAYUSH INSERT INTO metrics (body) VALUES ('{\"no_score\": 1}');")
    assert table.warn_count == 2
    assert "required property 'score'" in table.last_warn["violations"][0]["reason"]

    # Table contains all 3 rows
    cnt = engine.execute("AAYUSH SELECT COUNT(*) AS cnt FROM metrics;")
    assert cnt["rows"][0]["cnt"] == 3


def test_unsupported_schema_keywords_fail_closed(tmp_path):
    engine = Engine(tmp_path / "test.db")

    # "$ref" or "dependencies" are unsupported keywords
    bad_schema = json.dumps({"type": "object", "$ref": "#/definitions/User"})
    with pytest.raises(ExecutionError, match="[Uu]nsupported schema keyword"):
        engine.execute(f"AAYUSH CREATE COLLECTION users VALIDATION MODE = 'STRICT' SCHEMA = '{bad_schema}';")

    bad_schema2 = json.dumps({"type": "object", "oneOf": [{"type": "string"}]})
    with pytest.raises(ExecutionError, match="[Uu]nsupported schema keyword"):
        engine.execute(f"AAYUSH CREATE COLLECTION users VALIDATION MODE = 'STRICT' SCHEMA = '{bad_schema2}';")


def test_schema_pattern_and_items_validation(tmp_path):
    engine = Engine(tmp_path / "test.db")
    schema = json.dumps({
        "type": "object",
        "properties": {
            "email": {"type": "string", "pattern": r"^[^@]+@[^@]+\.[^@]+$"},
            "tags": {
                "type": "array",
                "items": {"type": "string", "maxLength": 10},
            },
        },
    })
    engine.execute(f"AAYUSH CREATE COLLECTION posts VALIDATION MODE = 'STRICT' SCHEMA = '{schema}';")

    # Valid
    engine.execute(
        "AAYUSH INSERT INTO posts (body) VALUES ('{\"email\": \"dev@example.com\", \"tags\": [\"db\", \"sql\"]}');"
    )

    # Invalid email pattern
    with pytest.raises(ConstraintError, match="pattern"):
        engine.execute(
            "AAYUSH INSERT INTO posts (body) VALUES ('{\"email\": \"not-an-email\", \"tags\": [\"db\"]}');"
        )

    # Invalid item in array (tag too long)
    with pytest.raises(ConstraintError, match="maxLength"):
        engine.execute(
            "AAYUSH INSERT INTO posts (body) VALUES ('{\"email\": \"dev@example.com\", \"tags\": [\"this_tag_is_way_too_long\"]}');"
        )
