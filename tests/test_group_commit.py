"""Group commit: one WAL fsync publishes many independent transactions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from aaysql import Engine

SRC_DIR = str(Path(__file__).parents[1] / "src")

CRASH_CHILD = """
import os
import sys
import threading

from aaysql import Engine

path = sys.argv[1]
engine = Engine(path)
engine.execute("AAYUSH CREATE TABLE g (id INT PRIMARY KEY)")
os.environ["_AAYSQL_TEST_CRASH_POINT"] = "after_commit_fsync"
barrier = threading.Barrier(5)

def worker(worker_id):
    session = engine.connect()
    barrier.wait(timeout=20)
    for row in range(4):
        session.execute(f"AAYUSH INSERT INTO g VALUES ({worker_id * 10 + row})")

threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(timeout=20)
"""


def _run_insert_workers(engine: Engine, workers: int, per_worker: int):
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def worker(worker_id: int):
        try:
            session = engine.connect()
            barrier.wait(timeout=20)
            for row in range(per_worker):
                session.execute(
                    f"AAYUSH INSERT INTO g VALUES ({worker_id * 100 + row})"
                )
            session.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    assert all(not thread.is_alive() for thread in threads)


def test_group_commit_batches_independent_transactions_in_memory():
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE g (id INT PRIMARY KEY)")
    _run_insert_workers(engine, workers=8, per_worker=8)
    metrics = engine.mvcc.metrics()
    assert metrics["commits"] == 64
    assert metrics["batches"] < metrics["commits"]
    assert engine.execute("AAYUSH SELECT COUNT(*) AS n FROM g")["rows"] == [{"n": 64}]


def test_group_commit_uses_one_real_fsync_per_batch(tmp_path):
    path = tmp_path / "group.aaydb"
    engine = Engine(path)
    engine.execute("AAYUSH CREATE TABLE g (id INT PRIMARY KEY)")
    _run_insert_workers(engine, workers=8, per_worker=5)
    metrics = engine.mvcc.metrics()
    assert metrics["commits"] == 40
    assert metrics["fsyncs"] == metrics["batches"]
    assert metrics["fsyncs"] < metrics["commits"]
    engine.close()

    reopened = Engine(path)
    assert reopened.execute("AAYUSH SELECT COUNT(*) AS n FROM g")["rows"] == [{"n": 40}]
    reopened.close()


def test_group_batch_checkpoint_covers_all_members_on_both_formats(tmp_path):
    """A batch checkpoint must be the cumulative root, not the last workspace."""
    for version in (3, 4):
        path = tmp_path / f"checkpoint-v{version}.aaydb"
        if version == 3:
            path.write_text(
                json.dumps(
                    {
                        "format": "aaysql",
                        "format_version": 3,
                        "engine_version": "1.0.0",
                        "wal_lsn": 0,
                        "tables": {},
                        "indexes": {},
                    }
                )
            )
        engine = Engine(path)
        engine.storage.checkpoint_bytes = 1
        engine.execute("AAYUSH CREATE TABLE g (id INT PRIMARY KEY)")
        _run_insert_workers(engine, workers=4, per_worker=6)
        assert engine.execute("AAYUSH SELECT COUNT(*) AS n FROM g")["rows"] == [
            {"n": 24}
        ]
        engine.close()

        reopened = Engine(path)
        assert reopened.execute("AAYUSH SELECT COUNT(*) AS n FROM g")["rows"] == [
            {"n": 24}
        ]
        reopened.close()


def test_subprocess_crash_after_group_fsync_recovers_only_complete_batches(tmp_path):
    path = tmp_path / "crash.aaydb"
    env = os.environ.copy()
    env["PYTHONPATH"] = SRC_DIR + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env.pop("_AAYSQL_TEST_CRASH_POINT", None)
    completed = subprocess.run(
        [sys.executable, "-c", CRASH_CHILD, str(path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert completed.returncode == 99, completed.stderr

    reopened = Engine(path)
    rows = reopened.execute("AAYUSH SELECT id FROM g ORDER BY id")["rows"]
    ids = [row["id"] for row in rows]
    assert ids == sorted(set(ids))
    assert 1 <= len(ids) <= 20
    reopened.close()


def test_writer_admission_is_released_before_the_fsync_barrier(tmp_path):
    path = tmp_path / "admission.aaydb"
    engine = Engine(path)
    engine.execute("AAYUSH CREATE TABLE g (id INT PRIMARY KEY)")
    storage = engine.storage
    assert storage is not None
    real_save_batch = storage.save_batch
    entered = threading.Event()
    release = threading.Event()

    def blocked_save_batch(entries):
        entered.set()
        assert release.wait(timeout=20)
        return real_save_batch(entries)

    storage.save_batch = blocked_save_batch
    first_error: list[BaseException] = []

    def first_writer():
        try:
            engine.connect().execute("AAYUSH INSERT INTO g VALUES (1)")
        except BaseException as exc:  # noqa: BLE001
            first_error.append(exc)

    first = threading.Thread(target=first_writer)
    first.start()
    assert entered.wait(timeout=20), "first commit never reached the fsync barrier"

    # The first writer is inside save_batch (the fsync stand-in). A second writer
    # must still get through writer admission and enqueue rather than block on a
    # lock held across durability.
    second_error: list[BaseException] = []

    def second_writer():
        try:
            engine.connect().execute("AAYUSH INSERT INTO g VALUES (2)")
        except BaseException as exc:  # noqa: BLE001
            second_error.append(exc)

    second = threading.Thread(target=second_writer)
    second.start()
    deadline = time.monotonic() + 10
    while engine.mvcc.metrics()["admissions"] < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert engine.mvcc.metrics()["admissions"] >= 2
    assert first.is_alive(), "first commit unexpectedly returned before fsync completed"

    release.set()
    first.join(timeout=20)
    second.join(timeout=20)
    assert first_error == []
    assert second_error == []
    assert engine.execute("AAYUSH SELECT COUNT(*) AS n FROM g")["rows"] == [{"n": 2}]
    engine.close()
