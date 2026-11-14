"""Format v3 to v4 migration: roundtrip, interruptions, and failure recovery."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest

from aaysql import Engine, segments
from aaysql.errors import StorageError
from aaysql.storage import (
    ENGINE_VERSION,
    FORMAT_VERSION,
    SEGMENTED_FORMAT_VERSION,
    migrate_to_v4,
    peek_format_version,
)
from aaysql.wal import build_record, serialize_record

SRC_DIR = str(Path(__file__).parents[1] / "src")
CRASH_EXIT_CODE = 99

SETUP = [
    "AAYUSH CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL, score FLOAT)",
    "AAYUSH CREATE TABLE audit (id INT PRIMARY KEY, note TEXT)",
    "AAYUSH CREATE INDEX idx_users_name ON users (name)",
    "AAYUSH INSERT INTO users VALUES (1, 'Aayush', 9.5), (2, 'Alex', 7.25)",
    "AAYUSH INSERT INTO audit VALUES (1, 'created')",
]


def _seed_v3(db_path: Path, statements: list[str] | None = None) -> None:
    db_path.write_text(
        json.dumps(
            {
                "format": "aaysql",
                "format_version": FORMAT_VERSION,
                "engine_version": ENGINE_VERSION,
                "wal_lsn": 0,
                "tables": {},
                "indexes": {},
            }
        ),
        encoding="utf-8",
    )
    with Engine(db_path) as engine:
        for statement in statements if statements is not None else SETUP:
            engine.execute(statement)


def _run_migration_crash(db_path: Path, crash_point: str) -> subprocess.CompletedProcess:
    script = """
import os
import sys

os.environ["_AAYSQL_TEST_CRASH_POINT"] = sys.argv[2]
from aaysql.storage import migrate_to_v4

migrate_to_v4(sys.argv[1])
raise AssertionError("expected the migration to terminate")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = SRC_DIR + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.pop("_AAYSQL_TEST_CRASH_POINT", None)
    return subprocess.run(
        [sys.executable, "-c", script, str(db_path), crash_point],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _users(db_path: Path) -> list[dict]:
    with Engine(db_path) as engine:
        return engine.execute(
            "AAYUSH SELECT id, name, score FROM users ORDER BY id ASC"
        )["rows"]


# --- roundtrip -------------------------------------------------------------


def test_migration_preserves_tables_rows_types_and_indexes(tmp_path: Path):
    db_path = tmp_path / "legacy.aaydb"
    _seed_v3(db_path)
    assert peek_format_version(db_path) == 3

    report = migrate_to_v4(db_path)

    assert report["migrated"] is True
    assert peek_format_version(db_path) == SEGMENTED_FORMAT_VERSION
    manifest = segments.read_manifest(db_path)
    assert set(manifest["tables"]) == {"users", "audit"}
    assert manifest["indexes"] == {
        "idx_users_name": {"table": "users", "column": "name"}
    }
    assert not Path(f"{db_path}.v3.bak").exists()
    assert _users(db_path) == [
        {"id": 1, "name": "Aayush", "score": 9.5},
        {"id": 2, "name": "Alex", "score": 7.25},
    ]
    with Engine(db_path) as engine:
        plan = engine.execute(
            "AAYUSH EXPLAIN SELECT id FROM users WHERE name = 'Aayush'"
        )["message"]
        assert "INDEX SCAN: idx_users_name" in plan


def test_migration_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "twice.aaydb"
    _seed_v3(db_path)
    assert migrate_to_v4(db_path)["migrated"] is True
    generation = segments.read_manifest(db_path)["generation"]

    report = migrate_to_v4(db_path)

    assert report["migrated"] is False
    assert "already format v4" in report["message"]
    assert segments.read_manifest(db_path)["generation"] == generation
    assert _users(db_path)[0]["name"] == "Aayush"


def test_migration_leaves_the_new_database_writable(tmp_path: Path):
    db_path = tmp_path / "write.aaydb"
    _seed_v3(db_path)
    migrate_to_v4(db_path)
    with Engine(db_path) as engine:
        engine.execute("AAYUSH INSERT INTO users VALUES (3, 'Sam', 5.0)")
        engine.execute("AAYUSH DELETE FROM audit WHERE id = 1")
    with Engine(db_path) as engine:
        assert [row["id"] for row in _rows(engine, "users")] == [1, 2, 3]
        assert _rows(engine, "audit") == []


def _rows(engine: Engine, table: str) -> list[dict]:
    return engine.execute(f"AAYUSH SELECT * FROM {table} ORDER BY id ASC")["rows"]


def test_migration_of_a_missing_database_is_rejected(tmp_path: Path):
    with pytest.raises(StorageError, match="does not exist"):
        migrate_to_v4(tmp_path / "absent.aaydb")


# --- interrupted migration -------------------------------------------------


def test_crash_after_migration_backup_leaves_v3_usable(tmp_path: Path):
    db_path = tmp_path / "crash_backup.aaydb"
    _seed_v3(db_path)

    result = _run_migration_crash(db_path, "after_migration_backup")

    assert result.returncode == CRASH_EXIT_CODE, result.stderr
    assert peek_format_version(db_path) == 3
    backup = Path(f"{db_path}.v3.bak")
    assert backup.exists()
    assert _users(db_path)[0]["name"] == "Aayush"

    report = migrate_to_v4(db_path)
    assert report["migrated"] is True
    assert not backup.exists()
    assert _users(db_path)[1]["name"] == "Alex"


def test_crash_after_migration_manifest_is_finished_on_next_call(tmp_path: Path):
    db_path = tmp_path / "crash_manifest.aaydb"
    _seed_v3(db_path)

    result = _run_migration_crash(db_path, "after_migration_manifest")

    assert result.returncode == CRASH_EXIT_CODE, result.stderr
    assert peek_format_version(db_path) == SEGMENTED_FORMAT_VERSION
    backup = Path(f"{db_path}.v3.bak")
    assert backup.exists()
    assert _users(db_path)[0]["score"] == 9.5

    report = migrate_to_v4(db_path)
    assert report["migrated"] is False
    assert not backup.exists()


def test_failed_verification_restores_the_v3_file(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "verify_fail.aaydb"
    _seed_v3(db_path)

    def boom(before, after):
        raise StorageError("Migration verification failed: injected")

    monkeypatch.setattr("aaysql.storage._verify_migration", boom)

    with pytest.raises(StorageError, match="injected"):
        migrate_to_v4(db_path)

    assert peek_format_version(db_path) == 3
    assert _users(db_path)[0]["name"] == "Aayush"
    assert Path(f"{db_path}.v3.bak").exists()


def test_malformed_v3_wal_aborts_without_touching_the_file(tmp_path: Path):
    db_path = tmp_path / "bad_wal.aaydb"
    _seed_v3(db_path)
    wal_path = Path(f"{db_path}.wal")
    record = build_record(1, "txn-bad", "begin")
    record["checksum"] = "0" * 64
    wal_path.write_bytes(serialize_record(record) + b"\n")
    original = db_path.read_bytes()

    with pytest.raises(StorageError, match="Corrupted WAL"):
        migrate_to_v4(db_path)

    assert peek_format_version(db_path) == 3
    assert db_path.read_bytes() == original
    assert not Path(f"{db_path}.v3.bak").exists()
    assert not segments.segment_dir(db_path).exists()


# --- command line ----------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    script = "import sys\nfrom aaysql.cli import main\nraise SystemExit(main())"
    env = os.environ.copy()
    env["PYTHONPATH"] = SRC_DIR + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_cli_migrate_converts_and_reports(tmp_path: Path):
    db_path = tmp_path / "cli.aaydb"
    _seed_v3(db_path)

    result = _run_cli("--migrate", str(db_path))

    assert result.returncode == 0, result.stderr
    assert "Migrated" in result.stdout
    assert peek_format_version(db_path) == SEGMENTED_FORMAT_VERSION
    assert _users(db_path)[0]["id"] == 1


def test_cli_migrate_reports_errors_with_status_one(tmp_path: Path):
    result = _run_cli("--migrate", str(tmp_path / "nope.aaydb"))
    assert result.returncode == 1
    assert "ERROR:" in result.stderr
