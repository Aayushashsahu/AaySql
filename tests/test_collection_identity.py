"""Tests for collection identity (_id, UUIDv7), immutability, revision (_rev), and OCC."""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from aaysql.engine import Engine
from aaysql.errors import ConstraintError, ExecutionError
from aaysql.uuidv7 import generate_uuidv7, is_uuidv7

UUIDV7_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def test_uuidv7_generator_format_and_monotonicity():
    prev = generate_uuidv7()
    assert is_uuidv7(prev)
    assert UUIDV7_REGEX.match(prev) is not None

    for _ in range(500):
        curr = generate_uuidv7()
        assert is_uuidv7(curr)
        assert UUIDV7_REGEX.match(curr) is not None
        assert curr > prev, f"UUIDv7 sequence must be strictly increasing: {curr} <= {prev}"
        prev = curr


def test_id_auto_generation_when_omitted(tmp_path):
    engine = Engine(tmp_path / "test.db")
    engine.execute("AAYUSH CREATE COLLECTION notes;")

    res = engine.execute("AAYUSH INSERT INTO notes (body) VALUES ('{\"title\": \"first\"}');")
    assert res["status"] == "ok"
    assert "inserted_id" in res
    assert "inserted_ids" in res
    assert len(res["inserted_ids"]) == 1
    assert is_uuidv7(res["inserted_id"])

    res2 = engine.execute(
        "AAYUSH INSERT INTO notes (body) VALUES ('{\"title\": \"second\"}'), ('{\"title\": \"third\"}');"
    )
    assert len(res2["inserted_ids"]) == 2
    id2, id3 = res2["inserted_ids"]
    assert is_uuidv7(id2)
    assert is_uuidv7(id3)
    assert id3 > id2

    # Check rows in table
    rows = engine.execute("AAYUSH SELECT _id, _rev, body.title FROM notes;")["rows"]
    assert len(rows) == 3
    assert rows[0]["body.title"] == "first"
    assert rows[0]["_rev"] == 1


def test_explicit_id_respected_and_null_rejected(tmp_path):
    engine = Engine(tmp_path / "test.db")
    engine.execute("AAYUSH CREATE COLLECTION users;")

    # Explicit custom _id
    res = engine.execute("AAYUSH INSERT INTO users (_id, _rev, body) VALUES ('custom-id-123', 1, '{\"name\": \"Alice\"}');")
    assert res["status"] == "ok"
    assert res["inserted_id"] == "custom-id-123"

    row = engine.execute("AAYUSH SELECT * FROM users WHERE _id = 'custom-id-123';")["rows"][0]
    assert row["_id"] == "custom-id-123"

    # Explicit NULL for _id must be rejected
    with pytest.raises(ConstraintError):
        engine.execute("AAYUSH INSERT INTO users (_id, _rev, body) VALUES (NULL, 1, '{\"name\": \"Bob\"}');")


def test_id_immutability_on_update(tmp_path):
    engine = Engine(tmp_path / "test.db")
    engine.execute("AAYUSH CREATE COLLECTION tasks;")
    engine.execute("AAYUSH INSERT INTO tasks (body) VALUES ('{\"task\": \"clean\"}');")

    with pytest.raises(ExecutionError, match="immutable"):
        engine.execute("AAYUSH UPDATE tasks SET _id = 'new-id';")


def test_rev_auto_increment_and_occ(tmp_path):
    engine = Engine(tmp_path / "test.db")
    engine.execute("AAYUSH CREATE COLLECTION docs;")

    ins = engine.execute("AAYUSH INSERT INTO docs (body) VALUES ('{\"version\": 1}');")
    doc_id = ins["inserted_id"]

    # Initial revision is 1
    row = engine.execute(f"AAYUSH SELECT _rev, body.version FROM docs WHERE _id = '{doc_id}';")["rows"][0]
    assert row["_rev"] == 1
    assert row["body.version"] == 1

    # Update body increments _rev to 2
    engine.execute(f"AAYUSH UPDATE docs SET body = '{{\"version\": 2}}' WHERE _id = '{doc_id}';")
    row = engine.execute(f"AAYUSH SELECT _rev, body.version FROM docs WHERE _id = '{doc_id}';")["rows"][0]
    assert row["_rev"] == 2
    assert row["body.version"] == 2

    # OCC (Optimistic Concurrency Control):
    # Stale update with _rev = 1 updates 0 rows
    stale_res = engine.execute(
        f"AAYUSH UPDATE docs SET body = '{{\"version\": 999}}' WHERE _id = '{doc_id}' AND _rev = 1;"
    )
    assert stale_res["rows_affected"] == 0

    row = engine.execute(f"AAYUSH SELECT _rev, body.version FROM docs WHERE _id = '{doc_id}';")["rows"][0]
    assert row["_rev"] == 2
    assert row["body.version"] == 2

    # Concurrent successful update with _rev = 2 succeeds and bumps to 3
    valid_res = engine.execute(
        f"AAYUSH UPDATE docs SET body = '{{\"version\": 3}}' WHERE _id = '{doc_id}' AND _rev = 2;"
    )
    assert valid_res["rows_affected"] == 1

    row = engine.execute(f"AAYUSH SELECT _rev, body.version FROM docs WHERE _id = '{doc_id}';")["rows"][0]
    assert row["_rev"] == 3
    assert row["body.version"] == 3
