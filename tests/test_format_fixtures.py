"""Database format compatibility fixtures.

Every fixture under ``tests/fixtures`` is permanent and read-only: each test copies
it to a temporary directory first, so a failing migration can never destroy the
only copy of a fixture.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest

from aaysql import Engine
from aaysql.backup import backup_database
from aaysql.errors import StorageError
from aaysql.storage import FORMAT_VERSION, JsonFileStorage

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED_ROWS = [
    {"id": 1, "name": "ada", "score": 10},
    {"id": 2, "name": "grace", "score": None},
    {"id": 3, "name": "alan", "score": 30},
]


def _copy(name: str, tmp_path: Path) -> Path:
    target = tmp_path / name
    shutil.copy(FIXTURES / name, target)
    return target


@pytest.mark.parametrize(
    ("fixture", "declared_version"),
    [("format_v1.aaydb", 1), ("format_v2.aaydb", 2), ("format_v3.aaydb", 3)],
)
def test_fixture_opens_and_reads_rows(tmp_path: Path, fixture: str, declared_version: int):
    db = _copy(fixture, tmp_path)
    payload = json.loads(db.read_text(encoding="utf-8"))
    assert payload["format_version"] == declared_version

    engine = Engine(db)
    try:
        assert engine.execute("AAYUSH SELECT * FROM users ORDER BY id")["rows"] == EXPECTED_ROWS
        assert engine.execute("AAYUSH SELECT name FROM users WHERE score IS NULL")["rows"] == [
            {"name": "grace"}
        ]
    finally:
        engine.close()


def test_reading_a_legacy_fixture_does_not_modify_it(tmp_path: Path):
    for fixture in ("format_v1.aaydb", "format_v2.aaydb", "format_v3.aaydb"):
        db = _copy(fixture, tmp_path)
        before = db.read_bytes()
        Engine(db).close()
        assert db.read_bytes() == before, f"{fixture} was rewritten by a read-only open"


def test_v1_migrates_to_current_format_on_write(tmp_path: Path):
    db = _copy("format_v1.aaydb", tmp_path)
    engine = Engine(db)
    engine.execute("AAYUSH INSERT INTO users VALUES (4, 'edsger', 40)")
    engine.close()

    payload = json.loads(db.read_text(encoding="utf-8"))
    assert payload["format_version"] == FORMAT_VERSION
    assert payload["wal_lsn"] >= 1
    reopened = Engine(db)
    assert reopened.execute("AAYUSH SELECT COUNT(*) FROM users")["rows"] == [{"COUNT(*)": 4}]
    reopened.close()


def test_v2_migration_preserves_index_definitions(tmp_path: Path):
    db = _copy("format_v2.aaydb", tmp_path)
    engine = Engine(db)
    assert "idx_users_name" in engine.db.indexes
    engine.execute("AAYUSH INSERT INTO users VALUES (4, 'edsger', 40)")
    engine.close()

    payload = json.loads(db.read_text(encoding="utf-8"))
    assert payload["format_version"] == FORMAT_VERSION
    assert payload["indexes"] == {"idx_users_name": {"table": "users", "column": "name"}}
    reopened = Engine(db)
    assert reopened.execute(
        "AAYUSH SELECT id FROM users WHERE name = 'edsger'"
    )["rows"] == [{"id": 4}]
    reopened.close()


def test_v3_fixture_indexes_are_rebuilt_from_rows(tmp_path: Path):
    db = _copy("format_v3.aaydb", tmp_path)
    engine = Engine(db)
    assert set(engine.db.indexes) == {"idx_users_name", "idx_users_score"}
    rows = engine.execute("AAYUSH SELECT id FROM users WHERE score >= 10 ORDER BY id")["rows"]
    assert rows == [{"id": 1}, {"id": 3}]
    engine.close()


@pytest.mark.parametrize("fixture", ["corrupt_not_json.aaydb", "corrupt_schema.aaydb"])
def test_corrupt_fixtures_are_rejected_and_left_untouched(tmp_path: Path, fixture: str):
    db = _copy(fixture, tmp_path)
    before = db.read_bytes()
    with pytest.raises(StorageError):
        Engine(db)
    assert db.read_bytes() == before


def test_fixture_without_tables_mapping_is_rejected(tmp_path: Path):
    db = tmp_path / "no_tables.aaydb"
    db.write_text(
        json.dumps({"format": "aaysql", "format_version": 3, "wal_lsn": 0}), encoding="utf-8"
    )
    with pytest.raises(StorageError, match="tables"):
        Engine(db)


def test_backup_of_migrated_legacy_database(tmp_path: Path):
    db = _copy("format_v1.aaydb", tmp_path)
    engine = Engine(db)
    engine.execute("AAYUSH CREATE INDEX idx_users_score ON users (score)")
    engine.execute("AAYUSH INSERT INTO users VALUES (4, 'edsger', NULL)")
    engine.close()

    destination = tmp_path / "legacy-backup"
    manifest = backup_database(db, destination)
    assert manifest["database_format_version"] == FORMAT_VERSION
    assert manifest["tables"] == [{"name": "users", "rows": 4}]
    assert manifest["indexes"] == ["idx_users_score"]

    restored = Engine(destination / "database.aaydb")
    assert restored.execute("AAYUSH SELECT COUNT(*) FROM users")["rows"] == [{"COUNT(*)": 4}]
    restored.close()


def test_storage_load_reports_directory_paths(tmp_path: Path):
    directory = tmp_path / "dir.aaydb"
    directory.mkdir()
    with pytest.raises(StorageError, match="directory"):
        JsonFileStorage(directory).load()
