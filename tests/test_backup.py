"""Offline backup and restore tests.

Covers consistency (WAL recovery before the snapshot), manifest and checksum
validation, exclusive ownership, destination/target overwrite protection, and
query equivalence between the original and restored database.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest

from aaysql import Engine
from aaysql.backup import (
    BACKUP_FORMAT_VERSION,
    BackupError,
    backup_database,
    main,
    read_manifest,
    restore_database,
    verify_backup,
)
from aaysql.server import LOCK_SUFFIX, ServerLock
from aaysql.storage import FORMAT_VERSION
from aaysql.wal import WalManager

SCHEMA = """
AAYUSH CREATE TABLE users (
    id INT PRIMARY KEY,
    name TEXT NOT NULL,
    score INT,
    active BOOL
);
AAYUSH CREATE INDEX idx_users_name ON users (name);
"""

ROWS = """
AAYUSH INSERT INTO users VALUES (1, 'ada', 10, TRUE);
AAYUSH INSERT INTO users VALUES (2, 'grace', NULL, FALSE);
AAYUSH INSERT INTO users VALUES (3, 'alan', 30, TRUE);
"""


def _make_db(path: Path, *, rows: bool = True) -> Engine:
    engine = Engine(path)
    for statement in SCHEMA.strip().split(";"):
        if statement.strip():
            engine.execute(statement.strip() + ";")
    if rows:
        for statement in ROWS.strip().split(";"):
            if statement.strip():
                engine.execute(statement.strip() + ";")
    return engine


def _snapshot(engine: Engine) -> list[dict]:
    return engine.execute("AAYUSH SELECT * FROM users ORDER BY id")["rows"]


# --- backup ---------------------------------------------------------------


def test_backup_of_clean_database_preserves_rows_and_indexes(tmp_path: Path):
    db = tmp_path / "app.aaydb"
    engine = _make_db(db)
    before = _snapshot(engine)
    engine.close()

    dest = tmp_path / "backup"
    manifest = backup_database(db, dest)

    assert manifest["backup_format_version"] == BACKUP_FORMAT_VERSION
    assert manifest["database_format_version"] == FORMAT_VERSION
    assert manifest["tables"] == [{"name": "users", "rows": 3}]
    assert manifest["indexes"] == ["idx_users_name"]
    assert (dest / "database.aaydb").is_file()
    assert read_manifest(dest)["database_sha256"] == manifest["database_sha256"]

    restored = Engine(dest / "database.aaydb")
    assert _snapshot(restored) == before
    assert restored.execute("AAYUSH SELECT name FROM users WHERE id = 2")["rows"] == [
        {"name": "grace"}
    ]
    restored.close()


def test_backup_refuses_existing_destination_without_force(tmp_path: Path):
    db = tmp_path / "app.aaydb"
    _make_db(db).close()
    dest = tmp_path / "backup"
    backup_database(db, dest)

    with pytest.raises(BackupError, match="already exists"):
        backup_database(db, dest)

    # Force replaces the backup directory rather than nesting or merging.
    manifest = backup_database(db, dest, force=True)
    assert manifest["database_sha256"] == read_manifest(dest)["database_sha256"]


def test_backup_recovers_pending_wal_before_snapshot(tmp_path: Path):
    db = tmp_path / "app.aaydb"
    engine = _make_db(db)
    before = _snapshot(engine)
    engine.close()

    # Simulate a committed transaction whose checkpoint never completed.
    payload = json.loads(db.read_text(encoding="utf-8"))
    payload["tables"]["users"]["rows"].append(
        {"id": 4, "name": "edsger", "score": 40, "active": True}
    )
    manager = WalManager(db)
    manager.last_lsn = payload["wal_lsn"]
    manager.append_commit_transaction(payload)
    assert Path(f"{db}.wal").stat().st_size > 0

    dest = tmp_path / "backup"
    backup_database(db, dest)

    restored = Engine(dest / "database.aaydb")
    rows = _snapshot(restored)
    assert len(rows) == len(before) + 1
    assert rows[-1]["name"] == "edsger"
    restored.close()


def test_backup_refuses_database_owned_by_running_server(tmp_path: Path):
    db = tmp_path / "app.aaydb"
    _make_db(db).close()
    lock = ServerLock(Path(f"{db}{LOCK_SUFFIX}"))
    lock.acquire()
    try:
        with pytest.raises(BackupError, match="in use"):
            backup_database(db, tmp_path / "backup")
    finally:
        lock.release()


def test_backup_never_touches_the_source(tmp_path: Path):
    db = tmp_path / "app.aaydb"
    _make_db(db).close()
    original = db.read_bytes()
    backup_database(db, tmp_path / "backup")
    assert db.read_bytes() == original


def test_interrupted_backup_leaves_no_staging_directory(tmp_path: Path, monkeypatch):
    db = tmp_path / "app.aaydb"
    _make_db(db).close()

    def fail(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr("aaysql.backup.os.replace", fail)
    with pytest.raises(BackupError, match="cannot install backup"):
        backup_database(db, tmp_path / "backup")
    leftovers = [p for p in tmp_path.iterdir() if ".tmp-" in p.name]
    assert leftovers == []
    assert not (tmp_path / "backup").exists()


def test_backup_of_missing_database_is_refused(tmp_path: Path):
    with pytest.raises(BackupError, match="does not exist"):
        backup_database(tmp_path / "nope.aaydb", tmp_path / "backup")


# --- verification ---------------------------------------------------------


def test_verify_backup_accepts_valid_backup(tmp_path: Path):
    db = tmp_path / "app.aaydb"
    _make_db(db).close()
    dest = tmp_path / "backup"
    backup_database(db, dest)
    manifest = verify_backup(dest)
    assert manifest["tables"] == [{"name": "users", "rows": 3}]


def test_verify_rejects_checksum_mismatch(tmp_path: Path):
    db = tmp_path / "app.aaydb"
    _make_db(db).close()
    dest = tmp_path / "backup"
    backup_database(db, dest)
    target = dest / "database.aaydb"
    data = bytearray(target.read_bytes())
    data[-2] = ord("}") if data[-2] != ord("}") else ord(" ")
    target.write_bytes(bytes(data))
    with pytest.raises(BackupError, match="checksum mismatch"):
        verify_backup(dest)


def test_verify_rejects_invalid_manifest(tmp_path: Path):
    dest = tmp_path / "backup"
    dest.mkdir()
    (dest / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(BackupError, match="invalid backup manifest"):
        verify_backup(dest)

    (dest / "manifest.json").write_text(
        json.dumps({"backup_format": "something-else"}), encoding="utf-8"
    )
    with pytest.raises(BackupError, match="not an AayushSQL backup"):
        verify_backup(dest)


def test_verify_rejects_future_format(tmp_path: Path):
    dest = tmp_path / "backup"
    dest.mkdir()
    (dest / "manifest.json").write_text(
        json.dumps(
            {
                "backup_format": "aaysql-backup",
                "backup_format_version": 1,
                "database_format_version": FORMAT_VERSION + 1,
                "database_file": "database.aaydb",
                "database_sha256": "0" * 64,
                "database_size": 0,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(BackupError, match="newer database format"):
        verify_backup(dest)


def test_verify_rejects_missing_backup_file(tmp_path: Path):
    db = tmp_path / "app.aaydb"
    _make_db(db).close()
    dest = tmp_path / "backup"
    backup_database(db, dest)
    (dest / "database.aaydb").unlink()
    with pytest.raises(BackupError, match="missing"):
        verify_backup(dest)


# --- restore --------------------------------------------------------------


def test_restore_to_new_path_is_query_equivalent(tmp_path: Path):
    db = tmp_path / "app.aaydb"
    engine = _make_db(db)
    before = _snapshot(engine)
    engine.close()
    dest = tmp_path / "backup"
    backup_database(db, dest)

    restored_path = tmp_path / "restored.aaydb"
    report = restore_database(dest, restored_path)

    assert report["restored"] == str(restored_path)
    assert report["previous_database"] is None
    restored = Engine(restored_path)
    assert _snapshot(restored) == before
    assert restored.execute("AAYUSH SELECT COUNT(*) FROM users")["rows"] == [{"COUNT(*)": 3}]
    # The index was restored as a definition and is usable.
    assert restored.execute("AAYUSH SELECT id FROM users WHERE name = 'alan'")["rows"] == [
        {"id": 3}
    ]
    restored.close()


def test_restore_refuses_existing_target_without_force(tmp_path: Path):
    db = tmp_path / "app.aaydb"
    _make_db(db).close()
    dest = tmp_path / "backup"
    backup_database(db, dest)

    with pytest.raises(BackupError, match="already exists"):
        restore_database(dest, db)


def test_restore_over_existing_target_preserves_previous_copy(tmp_path: Path):
    db = tmp_path / "app.aaydb"
    engine = _make_db(db)
    before = _snapshot(engine)
    engine.close()
    dest = tmp_path / "backup"
    backup_database(db, dest)

    engine = Engine(db)
    engine.execute("AAYUSH DELETE FROM users WHERE id = 1")
    assert engine.execute("AAYUSH SELECT COUNT(*) FROM users")["rows"] == [{"COUNT(*)": 2}]
    engine.close()

    report = restore_database(dest, db, force=True)
    previous = Path(report["previous_database"])
    assert previous.is_file()
    assert Engine(previous).execute("AAYUSH SELECT COUNT(*) FROM users")["rows"] == [
        {"COUNT(*)": 2}
    ]

    restored = Engine(db)
    assert _snapshot(restored) == before
    restored.close()


def test_restore_refuses_target_owned_by_running_server(tmp_path: Path):
    db = tmp_path / "app.aaydb"
    _make_db(db).close()
    dest = tmp_path / "backup"
    backup_database(db, dest)

    target = tmp_path / "target.aaydb"
    lock = ServerLock(Path(f"{target}{LOCK_SUFFIX}"))
    lock.acquire()
    try:
        with pytest.raises(BackupError, match="in use"):
            restore_database(dest, target)
        assert not target.exists()
    finally:
        lock.release()


def test_restore_refuses_target_with_unrecovered_wal(tmp_path: Path):
    db = tmp_path / "app.aaydb"
    _make_db(db).close()
    dest = tmp_path / "backup"
    backup_database(db, dest)

    target = tmp_path / "target.aaydb"
    target.write_bytes(db.read_bytes())
    Path(f"{target}.wal").write_bytes(b'{"unrecovered":true}\n')
    with pytest.raises(BackupError, match="unrecovered WAL"):
        restore_database(dest, target, force=True)
    # The refused restore must not have replaced the target.
    assert Path(f"{target}.wal").read_bytes() == b'{"unrecovered":true}\n'


def test_restored_database_accepts_new_writes_and_reopens(tmp_path: Path):
    db = tmp_path / "app.aaydb"
    _make_db(db).close()
    dest = tmp_path / "backup"
    backup_database(db, dest)
    target = tmp_path / "restored.aaydb"
    restore_database(dest, target)

    engine = Engine(target)
    engine.execute("AAYUSH INSERT INTO users VALUES (99, 'new', 1, TRUE)")
    engine.close()
    reopened = Engine(target)
    assert reopened.execute("AAYUSH SELECT name FROM users WHERE id = 99")["rows"] == [
        {"name": "new"}
    ]
    reopened.close()


def test_corrupted_backup_is_not_restored(tmp_path: Path):
    db = tmp_path / "app.aaydb"
    _make_db(db).close()
    dest = tmp_path / "backup"
    backup_database(db, dest)
    (dest / "database.aaydb").write_bytes(b"{}\n")

    target = tmp_path / "target.aaydb"
    with pytest.raises(BackupError):
        restore_database(dest, target)
    assert not target.exists()


# --- CLI ------------------------------------------------------------------


def test_cli_backup_restore_verify_roundtrip(tmp_path: Path, capsys):
    db = tmp_path / "app.aaydb"
    _make_db(db).close()
    dest = tmp_path / "backup"
    assert main(["backup", str(db), str(dest)]) == 0
    assert "backup written" in capsys.readouterr().out

    assert main(["verify", str(dest)]) == 0
    assert "backup OK" in capsys.readouterr().out

    target = tmp_path / "target.aaydb"
    assert main(["restore", str(dest), str(target)]) == 0
    assert "restored" in capsys.readouterr().out

    assert main(["restore", str(dest), str(target)]) == 1
    assert "already exists" in capsys.readouterr().err
