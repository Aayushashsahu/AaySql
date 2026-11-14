"""Format v4 backup and restore: inventory, verification, and replacement."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest

from aaysql import Engine, segments
from aaysql.backup import (
    BackupError,
    backup_database,
    read_manifest,
    restore_database,
    verify_backup,
)

SRC_DIR = str(Path(__file__).parents[1] / "src")


def _seed(db_path: Path, *, extra: bool = False) -> None:
    with Engine(db_path) as engine:
        engine.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, name TEXT)")
        engine.execute("AAYUSH CREATE INDEX idx_users_name ON users (name)")
        engine.execute("AAYUSH INSERT INTO users VALUES (1, 'Aayush'), (2, 'Alex')")
        if extra:
            engine.execute("AAYUSH INSERT INTO users VALUES (3, 'Sam')")


def _rows(db_path: Path) -> list[dict]:
    with Engine(db_path) as engine:
        return engine.execute(
            "AAYUSH SELECT id, name FROM users ORDER BY id ASC"
        )["rows"]


def _backup(db_path: Path, destination: Path) -> dict:
    return backup_database(db_path, destination)


# --- backup inventory ------------------------------------------------------


def test_v4_backup_lists_database_and_every_segment(tmp_path: Path):
    db_path = tmp_path / "backup.aaydb"
    _seed(db_path)
    destination = tmp_path / "backup_dir"

    manifest = _backup(db_path, destination)

    assert manifest["backup_format_version"] == 2
    assert manifest["database_format_version"] == 4
    paths = [entry["path"] for entry in manifest["files"]]
    assert paths[0] == "database.aaydb"
    assert all(path.startswith("database.aaydb.seg/") for path in paths[1:])
    assert len(paths) == 1 + len(manifest["segments"])
    assert (destination / "database.aaydb").is_file()
    for segment_name in manifest["segments"]:
        assert (destination / "database.aaydb.seg" / segment_name).is_file()


def test_verify_backup_accepts_and_restores_a_v4_snapshot(tmp_path: Path):
    db_path = tmp_path / "roundtrip.aaydb"
    _seed(db_path)
    destination = tmp_path / "roundtrip_backup"
    _backup(db_path, destination)

    manifest = verify_backup(destination)
    assert manifest["tables"][0]["name"] == "users"

    target = tmp_path / "restored.aaydb"
    report = restore_database(destination, target)

    assert report["previous_database"] is None
    assert _rows(target) == [
        {"id": 1, "name": "Aayush"},
        {"id": 2, "name": "Alex"},
    ]
    with Engine(target) as engine:
        plan = engine.execute(
            "AAYUSH EXPLAIN SELECT id FROM users WHERE name = 'Alex'"
        )["message"]
        assert "INDEX SCAN: idx_users_name" in plan
    restored_manifest = segments.read_manifest(target)
    assert restored_manifest["db_id"] == json.loads(
        (destination / "database.aaydb").read_text(encoding="utf-8")
    )["db_id"]


def test_backup_replaces_an_existing_directory_only_with_force(tmp_path: Path):
    db_path = tmp_path / "force.aaydb"
    _seed(db_path)
    destination = tmp_path / "force_backup"
    _backup(db_path, destination)

    with pytest.raises(BackupError, match="already exists"):
        _backup(db_path, destination)

    with Engine(db_path) as engine:
        engine.execute("AAYUSH INSERT INTO users VALUES (3, 'Sam')")
    manifest = backup_database(db_path, destination, force=True)
    assert manifest["tables"][0]["rows"] == 3


# --- corruption in the backup ----------------------------------------------


def test_missing_segment_in_backup_fails_verification(tmp_path: Path):
    db_path = tmp_path / "missing_seg.aaydb"
    _seed(db_path)
    destination = tmp_path / "missing_seg_backup"
    manifest = _backup(db_path, destination)
    (destination / "database.aaydb.seg" / manifest["segments"][0]).unlink()

    with pytest.raises(BackupError, match="backup is missing"):
        verify_backup(destination)

    target = tmp_path / "should_not_exist.aaydb"
    with pytest.raises(BackupError, match="backup is missing"):
        restore_database(destination, target)
    assert not target.exists()


def test_corrupt_segment_in_backup_fails_verification(tmp_path: Path):
    db_path = tmp_path / "corrupt_seg.aaydb"
    _seed(db_path)
    destination = tmp_path / "corrupt_seg_backup"
    manifest = _backup(db_path, destination)
    segment = destination / "database.aaydb.seg" / manifest["segments"][0]
    data = bytearray(segment.read_bytes())
    data[0] = ord("{") if data[0] != ord("{") else ord(" ")
    segment.write_bytes(bytes(data))

    with pytest.raises(BackupError, match="checksum mismatch"):
        verify_backup(destination)


def test_manifest_without_inventory_is_rejected(tmp_path: Path):
    db_path = tmp_path / "no_inventory.aaydb"
    _seed(db_path)
    destination = tmp_path / "no_inventory_backup"
    _backup(db_path, destination)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["files"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError, match="missing its file inventory"):
        read_manifest(destination)


def test_inventory_must_list_the_database_file(tmp_path: Path):
    db_path = tmp_path / "no_db_entry.aaydb"
    _seed(db_path)
    destination = tmp_path / "no_db_entry_backup"
    _backup(db_path, destination)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = [
        entry for entry in manifest["files"] if entry["path"] != "database.aaydb"
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError, match="does not list the database file"):
        read_manifest(destination)


def test_inventory_paths_cannot_escape_the_backup(tmp_path: Path):
    db_path = tmp_path / "escape.aaydb"
    _seed(db_path)
    destination = tmp_path / "escape_backup"
    _backup(db_path, destination)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][1]["path"] = "../outside.seg"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError, match="escapes the backup directory"):
        read_manifest(destination)


# --- restoring over an existing database -----------------------------------


def test_restore_preserves_the_previous_generation(tmp_path: Path):
    source = tmp_path / "source.aaydb"
    _seed(source)
    destination = tmp_path / "previous_backup"
    _backup(source, destination)

    target = tmp_path / "target.aaydb"
    with Engine(target) as engine:
        engine.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, name TEXT)")
        engine.execute("AAYUSH INSERT INTO users VALUES (9, 'Old')")

    report = restore_database(destination, target, force=True)

    previous = Path(report["previous_database"])
    assert previous.exists()
    assert segments.segment_dir(previous).is_dir()
    assert _rows(target) == [
        {"id": 1, "name": "Aayush"},
        {"id": 2, "name": "Alex"},
    ]
    assert _rows(previous) == [{"id": 9, "name": "Old"}]


def test_restore_requires_force_to_replace_an_existing_database(tmp_path: Path):
    source = tmp_path / "source2.aaydb"
    _seed(source)
    destination = tmp_path / "force_restore_backup"
    _backup(source, destination)
    target = tmp_path / "existing.aaydb"
    _seed(target, extra=True)

    with pytest.raises(BackupError, match="already exists"):
        restore_database(destination, target)
    assert _rows(target) == [
        {"id": 1, "name": "Aayush"},
        {"id": 2, "name": "Alex"},
        {"id": 3, "name": "Sam"},
    ]


def test_restore_refuses_a_target_with_an_unrecovered_wal(tmp_path: Path):
    source = tmp_path / "source3.aaydb"
    _seed(source)
    destination = tmp_path / "wal_restore_backup"
    _backup(source, destination)
    target = tmp_path / "pending.aaydb"
    _seed(target)
    Path(f"{target}.wal").write_bytes(b'{"lsn":1,"txn":"x","type":"beg')

    with pytest.raises(BackupError, match="unrecovered WAL data"):
        restore_database(destination, target, force=True)


def test_backup_fails_while_another_owner_holds_the_database(tmp_path: Path):
    db_path = tmp_path / "locked.aaydb"
    _seed(db_path)
    engine = Engine(db_path)
    try:
        with pytest.raises(BackupError, match="is in use"):
            _backup(db_path, tmp_path / "locked_backup")
    finally:
        engine.close()


def test_restore_fails_while_another_owner_holds_the_target(tmp_path: Path):
    source = tmp_path / "source4.aaydb"
    _seed(source)
    destination = tmp_path / "owned_backup"
    _backup(source, destination)
    target = tmp_path / "owned.aaydb"
    _seed(target)
    engine = Engine(target)
    try:
        with pytest.raises(BackupError, match="is in use"):
            restore_database(destination, target, force=True)
    finally:
        engine.close()


# --- older backups and the command line ------------------------------------


def test_v3_backup_and_restore_still_work(tmp_path: Path):
    from aaysql.storage import ENGINE_VERSION, FORMAT_VERSION, peek_format_version

    db_path = tmp_path / "legacy.aaydb"
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
    _seed(db_path)
    assert peek_format_version(db_path) == 3
    destination = tmp_path / "legacy_backup"

    manifest = _backup(db_path, destination)

    assert manifest["database_format_version"] == 3
    assert "files" not in manifest
    verify_backup(destination)
    target = tmp_path / "legacy_restored.aaydb"
    restore_database(destination, target)
    assert peek_format_version(target) == 3
    assert _rows(target)[1]["name"] == "Alex"


def _run_backup_cli(*args: str) -> subprocess.CompletedProcess:
    script = "import sys\nfrom aaysql.backup import main\nraise SystemExit(main())"
    env = os.environ.copy()
    env["PYTHONPATH"] = SRC_DIR + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_backup_cli_roundtrip_for_v4(tmp_path: Path):
    db_path = tmp_path / "cli.aaydb"
    _seed(db_path)
    destination = tmp_path / "cli_backup"

    created = _run_backup_cli("backup", str(db_path), str(destination))
    assert created.returncode == 0, created.stderr
    assert "backup written" in created.stdout

    verified = _run_backup_cli("verify", str(destination))
    assert verified.returncode == 0, verified.stderr
    assert "backup OK: format 4" in verified.stdout

    target = tmp_path / "cli_restored.aaydb"
    restored = _run_backup_cli("restore", str(destination), str(target))
    assert restored.returncode == 0, restored.stderr
    assert _rows(target)[0]["name"] == "Aayush"
