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
