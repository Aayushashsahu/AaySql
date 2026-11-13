"""Format v4 segmented checkpoints: reuse, garbage collection, and safety."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest

from aaysql import Engine, segments
from aaysql.delta import apply_changes
from aaysql.errors import StorageError
from aaysql.storage import SegmentedStorage


def _manifest(db_path: Path) -> dict:
    return segments.read_manifest(db_path)


def _segment_names(db_path: Path) -> set[str]:
    directory = segments.segment_dir(db_path)
    if not directory.is_dir():
        return set()
    return {entry.name for entry in directory.iterdir() if entry.is_file()}


def _referenced(manifest: dict) -> set[str]:
    return {ref["segment"] for ref in manifest["tables"].values()}


def _seed_two_tables(db_path: Path) -> None:
    with Engine(db_path) as engine:
        engine.execute("AAYUSH CREATE TABLE hot (id INT PRIMARY KEY, v TEXT)")
        engine.execute("AAYUSH CREATE TABLE cold (id INT PRIMARY KEY, v TEXT)")
        engine.execute("AAYUSH INSERT INTO hot VALUES (1, 'a')")
        engine.execute("AAYUSH INSERT INTO cold VALUES (1, 'x'), (2, 'y')")


# --- reuse and garbage collection ------------------------------------------


def test_unchanged_tables_reuse_their_segment_bytes(tmp_path: Path):
    db_path = tmp_path / "reuse.aaydb"
    _seed_two_tables(db_path)
    before = _manifest(db_path)
    cold_ref = before["tables"]["cold"]
    cold_path = segments.segment_dir(db_path) / cold_ref["segment"]
    cold_mtime = cold_path.stat().st_mtime_ns

    with Engine(db_path) as engine:
        engine.execute("AAYUSH INSERT INTO hot VALUES (2, 'b')")
        engine.checkpoint()

    after = _manifest(db_path)
    assert after["tables"]["cold"] == cold_ref
    assert cold_path.exists()
    # The unchanged segment was not rewritten.
    assert cold_path.stat().st_mtime_ns == cold_mtime
    assert after["tables"]["hot"]["segment"] != before["tables"]["hot"]["segment"]


def test_checkpoint_collects_unreferenced_segments(tmp_path: Path):
    db_path = tmp_path / "gc.aaydb"
    _seed_two_tables(db_path)
    with Engine(db_path) as engine:
        for value in range(6):
            engine.execute(f"AAYUSH UPDATE hot SET v = 'v{value}' WHERE id = 1")
            engine.checkpoint()

    manifest = _manifest(db_path)
    on_disk = _segment_names(db_path)
    assert on_disk == _referenced(manifest)
    assert len(on_disk) == 2


def test_garbage_collection_ignores_directories_and_symlinks(tmp_path: Path):
    db_path = tmp_path / "gc_safe.aaydb"
    _seed_two_tables(db_path)
    directory = segments.segment_dir(db_path)
    stray_dir = directory / "keep_me"
    stray_dir.mkdir()
    with Engine(db_path) as engine:
        engine.execute("AAYUSH INSERT INTO hot VALUES (2, 'b')")
        engine.checkpoint()
    assert stray_dir.is_dir()


def test_explicit_checkpoint_advances_generation_and_truncates_wal(tmp_path: Path):
    db_path = tmp_path / "explicit.aaydb"
    with Engine(db_path) as engine:
        engine.execute("AAYUSH CREATE TABLE t (id INT)")
    with Engine(db_path) as engine:
        engine.execute("AAYUSH INSERT INTO t VALUES (1)")
        wal_path = Path(f"{db_path}.wal")
        assert wal_path.stat().st_size > 0
        generation = _manifest(db_path)["generation"]
        engine.checkpoint()

        manifest = _manifest(db_path)
        assert manifest["generation"] == generation + 1
        assert manifest["checkpoint_lsn"] == engine.db.wal_lsn
        assert not wal_path.exists() or wal_path.stat().st_size == 0

    with Engine(db_path) as engine:
        assert engine.execute("AAYUSH SELECT id FROM t")["rows"] == [{"id": 1}]


def test_wal_threshold_triggers_an_automatic_checkpoint(tmp_path: Path):
    db_path = tmp_path / "threshold.aaydb"
    with Engine(db_path) as engine:
        engine.execute("AAYUSH CREATE TABLE t (id INT, v TEXT)")

    storage = SegmentedStorage(db_path, checkpoint_bytes=200)
    db = storage.load()
    for value in range(6):
        changes = [{"t": "insert", "table": "t", "rows": [{"id": value, "v": "x"}]}]
        apply_changes(db, changes)
        storage.save(db, changes)
        wal_path = Path(f"{db_path}.wal")
        if not wal_path.exists() or wal_path.stat().st_size == 0:
            break
    else:  # pragma: no cover - threshold must be crossed
        pytest.fail("WAL never crossed the checkpoint threshold")

    manifest = segments.read_manifest(db_path)
    assert manifest["checkpoint_lsn"] == db.wal_lsn
    assert _segment_names(db_path) == _referenced(manifest)


# --- missing or corrupt segments -------------------------------------------


def test_missing_segment_fails_closed(tmp_path: Path):
    db_path = tmp_path / "missing.aaydb"
    _seed_two_tables(db_path)
    segment = segments.segment_dir(db_path) / _manifest(db_path)["tables"]["cold"]["segment"]
    segment.unlink()

    with pytest.raises(StorageError, match="Missing segment"):
        Engine(db_path)


def test_corrupt_segment_fails_closed(tmp_path: Path):
    db_path = tmp_path / "corrupt.aaydb"
    _seed_two_tables(db_path)
    segment = segments.segment_dir(db_path) / _manifest(db_path)["tables"]["cold"]["segment"]
    data = bytearray(segment.read_bytes())
    data[-2] = ord(" ") if data[-2] != ord(" ") else ord("\t")
    segment.write_bytes(bytes(data))

    with pytest.raises(StorageError, match="checksum mismatch|Corrupted segment"):
        Engine(db_path)


def test_segment_with_wrong_length_fails_closed(tmp_path: Path):
    db_path = tmp_path / "length.aaydb"
    _seed_two_tables(db_path)
    segment = segments.segment_dir(db_path) / _manifest(db_path)["tables"]["cold"]["segment"]
    segment.write_bytes(segment.read_bytes() + b"\n")

    with pytest.raises(StorageError, match="bytes, manifest says"):
        Engine(db_path)


def test_segment_name_must_be_content_addressed(tmp_path: Path):
    db_path = tmp_path / "name.aaydb"
    _seed_two_tables(db_path)
    manifest = _manifest(db_path)
    # A well-formed name derived from a different digest is not the file that
    # the manifest checksum points at, so it must not resolve.
    manifest["tables"]["cold"]["segment"] = f"seg_{'0' * 24}.seg"
    db_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(StorageError, match="Missing segment"):
        Engine(db_path)


# --- untrusted manifests ----------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../escape.seg",
        "..\\escape.seg",
        "seg_0123456789abcdef01234567.seg/../../escape.seg",
        "/etc/passwd",
        "seg_0123456789abcdef01234567.txt",
        "",
    ],
)
def test_manifest_path_traversal_is_rejected(tmp_path: Path, name: str):
    db_path = tmp_path / "traversal.aaydb"
    _seed_two_tables(db_path)
    manifest = _manifest(db_path)
    manifest["tables"]["cold"]["segment"] = name
    db_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(StorageError, match="Invalid segment file name"):
        Engine(db_path)


def test_manifest_checksum_length_must_be_sha256(tmp_path: Path):
    db_path = tmp_path / "digest.aaydb"
    _seed_two_tables(db_path)
    manifest = _manifest(db_path)
    manifest["tables"]["cold"]["sha256"] = "abc"
    db_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(StorageError, match="Invalid segment checksum"):
        Engine(db_path)


def test_manifest_format_version_is_checked(tmp_path: Path):
    db_path = tmp_path / "version.aaydb"
    _seed_two_tables(db_path)
    manifest = _manifest(db_path)
    manifest["format_version"] = 99
    db_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(StorageError, match="Unsupported database format version"):
        Engine(db_path)


def test_manifest_references_an_existing_table_for_indexes(tmp_path: Path):
    db_path = tmp_path / "index_ref.aaydb"
    _seed_two_tables(db_path)
    manifest = _manifest(db_path)
    manifest["indexes"]["idx_ghost"] = {"table": "ghost", "column": "id"}
    db_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(StorageError, match="references unknown table"):
        Engine(db_path)


def test_dropping_a_table_removes_its_segment(tmp_path: Path):
    db_path = tmp_path / "drop.aaydb"
    _seed_two_tables(db_path)
    cold = _manifest(db_path)["tables"]["cold"]["segment"]
    with Engine(db_path) as engine:
        engine.execute("AAYUSH DROP TABLE cold")
    manifest = _manifest(db_path)
    assert "cold" not in manifest["tables"]
    assert not (segments.segment_dir(db_path) / cold).exists()
    assert _segment_names(db_path) == _referenced(manifest)
