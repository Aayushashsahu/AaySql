"""Format v4 storage benchmark: delta commits vs whole-image checkpoints.

Compares, on the same machine and the same logical dataset:

* v4 single-row autocommit commit (delta WAL only) -- median latency and WAL bytes
* v4 explicit checkpoint -- median latency and segment+manifest bytes written
* v4 recovery on open -- replay one committed delta and checkpoint
* v3 single-row autocommit commit (whole-image WAL + full snapshot rewrite)

The dataset is constructed directly through the storage objects so the
measurement is storage cost, not SQL parsing. Zero-write reuse of an unchanged
table is also measured. Nothing here is a timing gate.

Run with::

    python benchmarks/benchmark_v4.py
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from aaysql import Engine, __version__, segments
from aaysql.delta import apply_changes
from aaysql.storage import (
    ENGINE_VERSION,
    FORMAT_VERSION,
    SEGMENTED_FORMAT_VERSION,
    ColumnDef,
    Database,
    SegmentedStorage,
)

SIZES = (100, 1_000, 10_000, 100_000)
SAMPLES = 5
NO_CHECKPOINT = 1 << 40


def _new_database(rows: int) -> Database:
    db = Database()
    db.create_table(
        "t",
        [
            ColumnDef(name="id", type_name="INT", primary_key=True),
            ColumnDef(name="payload", type_name="TEXT"),
        ],
    )
    db.tables["t"].rows.extend(
        {"id": index, "payload": f"x{index % 977}"} for index in range(1, rows + 1)
    )
    return db


def _seed_v4(path: Path, rows: int) -> None:
    db = _new_database(rows)
    storage = SegmentedStorage(path)
    storage.db_id = uuid.uuid4().hex
    storage.generation = 0
    storage._publish(db, checkpoint_lsn=0)


def _seed_v3(path: Path, rows: int) -> None:
    db = _new_database(rows)
    payload = db.to_dict()
    payload.update(
        {
            "format": "aaysql",
            "format_version": FORMAT_VERSION,
            "engine_version": ENGINE_VERSION,
            "wal_lsn": 0,
        }
    )
    path.write_text(json.dumps(payload), encoding="utf-8")


def _median(values: list[float]) -> float:
    return statistics.median(values)


def _segment_sizes(path: Path) -> dict[str, int]:
    directory = Path(f"{path}.seg")
    if not directory.is_dir():
        return {}
    return {
        entry.name: entry.stat().st_size
        for entry in directory.iterdir()
        if entry.is_file()
    }


def _remove_database(path: Path) -> None:
    for stale in path.parent.glob(f"{path.name}*"):
        if stale.is_file():
            stale.unlink()
        elif stale.is_dir():
            for child in stale.iterdir():
                child.unlink()
            stale.rmdir()


def _measure_v4(root: Path, rows: int) -> dict:
    commit_ms: list[float] = []
    commit_bytes: list[int] = []
    checkpoint_ms: list[float] = []
    checkpoint_bytes: list[int] = []
    recovery_ms: list[float] = []
    recovery_bytes: list[int] = []

    for sample in range(SAMPLES):
        path = root / f"v4-{rows}-{sample}.aaydb"
        _seed_v4(path, rows)

        engine = Engine(path)
        before_wal = Path(f"{path}.wal")
        before_segments = _segment_sizes(path)
        t0 = time.perf_counter()
        engine.execute(f"AAYUSH INSERT INTO t VALUES ({rows + 1}, 'one')")
        commit_ms.append((time.perf_counter() - t0) * 1000.0)
        commit_bytes.append(before_wal.stat().st_size if before_wal.exists() else 0)

        t0 = time.perf_counter()
        engine.checkpoint()
        checkpoint_ms.append((time.perf_counter() - t0) * 1000.0)
        after_segments = _segment_sizes(path)
        written = sum(
            size for name, size in after_segments.items() if name not in before_segments
        ) + path.stat().st_size
        checkpoint_bytes.append(written)
        engine.close()

        # Leave one committed delta in the WAL, then time a real reopen.
        storage = SegmentedStorage(path, checkpoint_bytes=NO_CHECKPOINT)
        db = storage.load()
        changes = [
            {"t": "insert", "table": "t", "rows": [{"id": rows + 2, "payload": "r"}]}
        ]
        apply_changes(db, changes)
        storage.save(db, changes)
        wal_path = Path(f"{path}.wal")
        recovery_bytes.append(wal_path.stat().st_size if wal_path.exists() else 0)
        t0 = time.perf_counter()
        Engine(path).close()
        recovery_ms.append((time.perf_counter() - t0) * 1000.0)

        _remove_database(path)

    return {
        "rows": rows,
        "commit_ms": _median(commit_ms),
        "commit_bytes": int(_median(commit_bytes)),
        "checkpoint_ms": _median(checkpoint_ms),
        "checkpoint_bytes": int(_median(checkpoint_bytes)),
        "recovery_ms": _median(recovery_ms),
        "recovery_bytes": int(_median(recovery_bytes)),
    }


def _measure_v3(root: Path, rows: int) -> dict:
    commit_ms: list[float] = []
    snapshot_bytes: list[int] = []
    for sample in range(SAMPLES):
        path = root / f"v3-{rows}-{sample}.aaydb"
        _seed_v3(path, rows)
        engine = Engine(path)
        t0 = time.perf_counter()
        engine.execute(f"AAYUSH INSERT INTO t VALUES ({rows + 1}, 'one')")
        commit_ms.append((time.perf_counter() - t0) * 1000.0)
        snapshot_bytes.append(path.stat().st_size)
        engine.close()
        _remove_database(path)
    return {
        "rows": rows,
        "commit_ms": _median(commit_ms),
        "snapshot_bytes": int(_median(snapshot_bytes)),
    }


def _unchanged_table_writes(root: Path) -> dict:
    """Prove that checkpointing one table does not rewrite an untouched one."""
    path = root / "reuse.aaydb"
    db = Database()
    columns = [
        ColumnDef(name="id", type_name="INT", primary_key=True),
        ColumnDef(name="v", type_name="TEXT"),
    ]
    db.create_table("hot", columns)
    db.create_table("cold", columns)
    db.tables["hot"].rows.append({"id": 1, "v": "a"})
    db.tables["cold"].rows.extend({"id": i, "v": f"c{i}"} for i in range(1, 101))
    storage = SegmentedStorage(path)
    storage.db_id = uuid.uuid4().hex
    storage.generation = 0
    storage._publish(db, checkpoint_lsn=0)

    cold_name = segments.read_manifest(path)["tables"]["cold"]["segment"]
    cold_before = Path(f"{path}.seg/{cold_name}")
    cold_mtime = cold_before.stat().st_mtime_ns
    before = _segment_sizes(path)

    engine = Engine(path)
    engine.execute("AAYUSH INSERT INTO hot VALUES (2, 'b')")
    engine.checkpoint()
    engine.close()

    after = _segment_sizes(path)
    cold_name_after = segments.read_manifest(path)["tables"]["cold"]["segment"]
    cold_after = Path(f"{path}.seg/{cold_name_after}")
    cold_unchanged = (
        cold_name_after == cold_name
        and cold_after.stat().st_mtime_ns == cold_mtime
    )
    return {
        "cold_new_files": 0 if cold_unchanged else 1,
        "cold_mtime_unchanged": cold_unchanged,
        "cold_bytes_written": (
            0 if cold_unchanged else after.get(cold_name_after, 0)
        ),
        "hot_new_files": sum(1 for name in after if name not in before),
    }


def main() -> None:
    print(
        f"AayushSQL {__version__} | format v{SEGMENTED_FORMAT_VERSION} "
        f"(v{FORMAT_VERSION} baseline) | {platform.platform()} | "
        f"Python {platform.python_version()} | {os.cpu_count()} cores"
    )
    print(
        "Dataset: one table, INT PRIMARY KEY + TEXT payload; single-row autocommit "
        f"INSERT; median of {SAMPLES} runs."
    )
    print()
    print(
        "| Rows | v4 commit ms | v4 WAL bytes | v4 checkpoint ms | "
        "v4 checkpoint bytes | v4 reopen ms | v4 replayed WAL bytes | "
        "v3 commit+checkpoint ms | v3 snapshot bytes |"
    )
    print("| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    with tempfile.TemporaryDirectory(prefix="aaysql-v4-bench-") as tmp:
        root = Path(tmp)
        for rows in SIZES:
            v4 = _measure_v4(root, rows)
            v3 = _measure_v3(root, rows)
            print(
                f"| {rows} | {v4['commit_ms']:.2f} | {v4['commit_bytes']} | "
                f"{v4['checkpoint_ms']:.2f} | {v4['checkpoint_bytes']} | "
                f"{v4['recovery_ms']:.2f} | {v4['recovery_bytes']} | "
                f"{v3['commit_ms']:.2f} | {v3['snapshot_bytes']} |"
            )
        reuse = _unchanged_table_writes(root)

    print()
    print("Unchanged-table checkpoint:")
    print(
        f"- untouched table rewritten: {bool(reuse['cold_new_files'])} "
        f"(bytes written: {reuse['cold_bytes_written']}); "
        f"segment file and mtime unchanged: {reuse['cold_mtime_unchanged']}; "
        f"new segment files in total (changed table only): {reuse['hot_new_files']}"
    )
    print()
    print(
        "Notes: v4 commit = WAL append + fsync only (a checkpoint is not triggered "
        "below 1 MiB of WAL). v4 checkpoint = segment write + fsync + manifest "
        "replace; checkpoint bytes include the manifest and only newly written "
        "segment files. v3 commit+checkpoint = whole-image WAL write + full snapshot "
        "rewrite + fsync. Reopen also rebuilds index trees; cloning/loading remains "
        "O(database), so no O(1) claim is made."
    )


if __name__ == "__main__":
    main()
