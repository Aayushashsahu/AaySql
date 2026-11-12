"""Ownership invariant: one legitimate owner per file-backed database.

Covers embedded Engine ownership, server exclusion in both directions,
transaction safety, backup/restore interaction, and real cross-process
contention (including crash release).
"""

import hashlib
import os
import ssl
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest

from aaysql import Engine
from aaysql.backup import BackupError, backup_database, restore_database
from aaysql.client import AayushSQLClient
from aaysql.errors import DatabaseLockedError, StorageError
from aaysql.locking import LOCK_SUFFIX, DatabaseLease
from aaysql.server import (
    AayushSQLServer,
    ServerConfig,
    ServerConfigError,
    ServerLock,
)

SRC = str(Path(__file__).parents[1] / "src")


# --- helpers --------------------------------------------------------------


def _open_with_rows(path: Path) -> None:
    with Engine(path) as engine:
        engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
        engine.execute("AAYUSH INSERT INTO t VALUES (1, 'one')")


def _digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _server(path: Path) -> AayushSQLServer:
    return AayushSQLServer(ServerConfig(db_path=path, port=0))


def _run(code: str, *, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _try_open_script(db: Path) -> str:
    return (
        "import sys\n"
        f"sys.path.insert(0, {SRC!r})\n"
        "from aaysql import Engine\n"
        "from aaysql.errors import DatabaseLockedError\n"
        f"p = {str(db)!r}\n"
        "try:\n"
        "    e = Engine(p)\n"
        "except DatabaseLockedError as exc:\n"
        "    print('CODE', exc.code, flush=True)\n"
        "    sys.exit(3)\n"
        "e.close()\n"
        "print('OPEN', flush=True)\n"
    )


# --- 7.1 embedded engine ownership ---------------------------------------


def test_first_file_backed_engine_opens(tmp_path: Path):
    engine = Engine(tmp_path / "one.aaydb")
    assert engine.is_persistent
    assert engine.lease is not None and engine.lease.active
    assert engine.owns_lease is True
    engine.close()


def test_second_engine_on_same_database_is_refused(tmp_path: Path):
    path = tmp_path / "owned.aaydb"
    engine = Engine(path)
    try:
        with pytest.raises(DatabaseLockedError) as excinfo:
            Engine(path)
        assert excinfo.value.code == "DATABASE_LOCKED"
        assert "in use" in str(excinfo.value)
        assert str(path) in str(excinfo.value)
    finally:
        engine.close()


def test_closing_the_owner_allows_a_new_engine(tmp_path: Path):
    path = tmp_path / "handover.aaydb"
    first = Engine(path)
    first.execute("AAYUSH CREATE TABLE t (id INT)")
    first.close()

    second = Engine(path)
    try:
        assert second.execute("AAYUSH SELECT COUNT(*) AS n FROM t")["rows"] == [{"n": 0}]
    finally:
        second.close()


def test_repeated_close_is_safe(tmp_path: Path):
    path = tmp_path / "twice.aaydb"
    engine = Engine(path)
    engine.close()
    engine.close()
    assert engine.lease is None
    with Engine(path):
        pass


def test_initialization_failure_releases_the_lease(tmp_path: Path, monkeypatch):
    path = tmp_path / "broken.aaydb"

    def explode(self):
        raise StorageError("simulated corrupt snapshot")

    monkeypatch.setattr("aaysql.engine.JsonFileStorage.load", explode)
    with pytest.raises(StorageError, match="simulated corrupt"):
        Engine(path)
    monkeypatch.undo()

    # The failed constructor must not leave an OS lock or registry entry.
    with Engine(path):
        pass


def test_in_memory_engine_takes_no_filesystem_lock(tmp_path: Path):
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    old = os.getcwd()
    try:
        os.chdir(workdir)
        first = Engine(":memory:")
        second = Engine(":memory:")
        assert first.lease is None and second.lease is None
        first.close()
        second.close()
        assert list(workdir.iterdir()) == []
    finally:
        os.chdir(old)


def test_distinct_database_paths_are_independent(tmp_path: Path):
    first = Engine(tmp_path / "a.aaydb")
    second = Engine(tmp_path / "b.aaydb")
    try:
        first.execute("AAYUSH CREATE TABLE only_a (id INT)")
        second.execute("AAYUSH CREATE TABLE only_b (id INT)")
        first.execute("AAYUSH INSERT INTO only_a VALUES (1)")
        second.execute("AAYUSH INSERT INTO only_b VALUES (2)")
        assert first.execute("AAYUSH SELECT * FROM only_a")["rows"] == [{"id": 1}]
        assert second.execute("AAYUSH SELECT * FROM only_b")["rows"] == [{"id": 2}]
    finally:
        first.close()
        second.close()


def test_relative_and_absolute_paths_are_the_same_database(tmp_path: Path):
    old = os.getcwd()
    try:
        os.chdir(tmp_path)
        first = Engine("relative.aaydb")
        try:
            with pytest.raises(DatabaseLockedError):
                Engine(tmp_path / "relative.aaydb")
        finally:
            first.close()
        absolute = Engine(tmp_path / "relative.aaydb")
        try:
            with pytest.raises(DatabaseLockedError):
                Engine("relative.aaydb")
        finally:
            absolute.close()
    finally:
        os.chdir(old)


def test_same_process_contention_across_threads_is_consistent(tmp_path: Path):
    path = tmp_path / "threads.aaydb"
    engine = Engine(path)
    results: list[object] = []

    def contender() -> None:
        try:
            Engine(path).close()
        except BaseException as exc:  # noqa: BLE001 - recorded for assertion
            results.append(exc)

    threads = [threading.Thread(target=contender) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    try:
        assert len(results) == 4
        assert all(isinstance(exc, DatabaseLockedError) for exc in results)
    finally:
        engine.close()


def test_stale_lock_file_does_not_block_a_new_owner(tmp_path: Path):
    path = tmp_path / "stale.aaydb"
    lock_file = Path(f"{path}{LOCK_SUFFIX}")
    lock_file.write_text("999999\n", encoding="ascii")
    with Engine(path):
        pass
    # The file may remain; only the OS lock decides ownership.
    assert lock_file.exists()


def test_lock_path_only_lease_cannot_be_borrowed(tmp_path: Path):
    path = tmp_path / "noborrow.aaydb"
    lease = DatabaseLease.for_lock_path(Path(f"{path}{LOCK_SUFFIX}"))
    lease.acquire()
    try:
        with pytest.raises(StorageError, match="lock-path-only"):
            lease.validate_borrow(path)
    finally:
        lease.release()
    # The compatibility wrapper still works and does block a real engine.
    with ServerLock(Path(f"{path}{LOCK_SUFFIX}")), pytest.raises(DatabaseLockedError):
        Engine(path)
    with Engine(path):
        pass


def test_borrowed_lease_must_match_the_database(tmp_path: Path):
    first = tmp_path / "first.aaydb"
    second = tmp_path / "second.aaydb"
    with Engine(first) as engine:
        assert engine.lease is not None
        with pytest.raises(StorageError, match="belongs to"):
            Engine(second, _lease=engine.lease)


def test_engine_does_not_release_a_borrowed_lease(tmp_path: Path):
    path = tmp_path / "borrow.aaydb"
    lease = DatabaseLease.for_database(path)
    lease.acquire()
    try:
        engine = Engine(path, _lease=lease)
        engine.close()
        engine.close()
        assert lease.active
        assert engine.owns_lease is False
    finally:
        lease.release()
    with Engine(path):
        pass


def test_released_lease_cannot_be_borrowed(tmp_path: Path):
    path = tmp_path / "released.aaydb"
    lease = DatabaseLease.for_database(path)
    lease.acquire()
    lease.release()
    with pytest.raises(StorageError, match="released"):
        Engine(path, _lease=lease)


# --- 7.2 server -> embedded exclusion ------------------------------------


def test_embedded_engine_is_refused_while_a_server_owns_the_database(tmp_path: Path):
    path = tmp_path / "server-own.aaydb"
    _open_with_rows(path)
    server = _server(path)
    server.start()
    try:
        with AayushSQLClient(*server.address) as client:
            client.execute("AAYUSH INSERT INTO t VALUES (2, 'two')")

        db_before = _digest(path)
        wal_before = _digest(Path(f"{path}.wal"))
        with pytest.raises(DatabaseLockedError) as excinfo:
            Engine(path)
        assert excinfo.value.code == "DATABASE_LOCKED"
        # Rejected access must not recover, checkpoint or modify anything.
        assert _digest(path) == db_before
        assert _digest(Path(f"{path}.wal")) == wal_before

        with AayushSQLClient(*server.address) as client:
            assert client.execute("AAYUSH SELECT COUNT(*) AS n FROM t")["rows"] == [{"n": 2}]
            client.execute("AAYUSH INSERT INTO t VALUES (3, 'three')")
    finally:
        server.shutdown()


# --- 7.3 embedded -> server exclusion ------------------------------------


def test_server_startup_is_refused_while_an_engine_owns_the_database(tmp_path: Path):
    path = tmp_path / "engine-own.aaydb"
    engine = Engine(path)
    engine.execute("AAYUSH CREATE TABLE t (id INT)")
    server = _server(path)
    try:
        with pytest.raises(DatabaseLockedError):
            server.start()
        # The embedded owner is untouched and still usable.
        engine.execute("AAYUSH INSERT INTO t VALUES (1)")
        assert engine.execute("AAYUSH SELECT COUNT(*) AS n FROM t")["rows"] == [{"n": 1}]
    finally:
        engine.close()

    server.start()
    try:
        with AayushSQLClient(*server.address) as client:
            assert client.execute("AAYUSH SELECT COUNT(*) AS n FROM t")["rows"] == [{"n": 1}]
    finally:
        server.shutdown()


# --- 7.4 server transaction safety ---------------------------------------


def test_rejected_open_does_not_disturb_an_open_server_transaction(tmp_path: Path):
    path = tmp_path / "txn.aaydb"
    server = _server(path)
    server.start()
    try:
        with AayushSQLClient(*server.address) as client:
            client.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")
            client.execute("AAYUSH BEGIN")
            client.execute("AAYUSH INSERT INTO t VALUES (1)")
            client.execute("AAYUSH INSERT INTO t VALUES (2)")

            for _ in range(3):
                with pytest.raises(DatabaseLockedError):
                    Engine(path)

            # The transaction survived the rejected opens and can commit.
            client.execute("AAYUSH COMMIT")
            assert client.execute("AAYUSH SELECT COUNT(*) AS n FROM t")["rows"] == [{"n": 2}]
    finally:
        server.shutdown()

    with Engine(path) as engine:
        assert engine.execute("AAYUSH SELECT COUNT(*) AS n FROM t")["rows"] == [{"n": 2}]


# --- 7.5 backup interaction ----------------------------------------------


def test_backup_refuses_while_a_server_owns_the_database(tmp_path: Path):
    path = tmp_path / "backup-server.aaydb"
    _open_with_rows(path)
    server = _server(path)
    server.start()
    try:
        with pytest.raises(BackupError, match="in use"):
            backup_database(path, tmp_path / "backup")
    finally:
        server.shutdown()


def test_backup_refuses_while_an_engine_owns_the_database(tmp_path: Path):
    path = tmp_path / "backup-engine.aaydb"
    with Engine(path) as engine:
        engine.execute("AAYUSH CREATE TABLE t (id INT)")
        with pytest.raises(BackupError, match="in use"):
            backup_database(path, tmp_path / "backup")


def test_backup_succeeds_after_the_owner_closes(tmp_path: Path):
    path = tmp_path / "backup-free.aaydb"
    _open_with_rows(path)
    manifest = backup_database(path, tmp_path / "backup")
    assert manifest["tables"] == [{"name": "t", "rows": 1}]


def test_wal_recovery_borrows_an_already_acquired_lease(tmp_path: Path):
    from aaysql.backup import _recover_pending_wal

    path = tmp_path / "borrow-recovery.aaydb"
    _open_with_rows(path)
    lease = DatabaseLease.for_database(path)
    lease.acquire()
    try:
        # Would raise DatabaseLockedError without borrowing.
        _recover_pending_wal(path, lease=lease)
        assert lease.active
    finally:
        lease.release()
    with Engine(path):
        pass


# --- 7.6 restore interaction ---------------------------------------------


def test_restore_refuses_a_target_owned_by_an_engine(tmp_path: Path):
    source = tmp_path / "source.aaydb"
    _open_with_rows(source)
    dest = tmp_path / "backup"
    backup_database(source, dest)

    target = tmp_path / "target.aaydb"
    with Engine(target), pytest.raises(BackupError, match="in use"):
        restore_database(dest, target, force=True)


def test_restore_refuses_a_target_owned_by_a_server(tmp_path: Path):
    source = tmp_path / "src-server.aaydb"
    _open_with_rows(source)
    dest = tmp_path / "backup"
    backup_database(source, dest)

    target = tmp_path / "target-server.aaydb"
    server = _server(target)
    server.start()
    try:
        with pytest.raises(BackupError, match="in use"):
            restore_database(dest, target, force=True)
    finally:
        server.shutdown()


def test_restore_releases_ownership_after_success(tmp_path: Path):
    source = tmp_path / "src-ok.aaydb"
    _open_with_rows(source)
    dest = tmp_path / "backup"
    backup_database(source, dest)

    target = tmp_path / "restored.aaydb"
    restore_database(dest, target)
    with Engine(target) as engine:
        assert engine.execute("AAYUSH SELECT COUNT(*) AS n FROM t")["rows"] == [{"n": 1}]


def test_failed_restore_leaves_the_target_recoverable(tmp_path: Path):
    source = tmp_path / "src-fail.aaydb"
    _open_with_rows(source)
    dest = tmp_path / "backup"
    backup_database(source, dest)

    target = tmp_path / "target-fail.aaydb"
    with Engine(target) as engine:
        engine.execute("AAYUSH CREATE TABLE preserved (id INT)")
        engine.execute("AAYUSH INSERT INTO preserved VALUES (7)")
    before = target.read_bytes()

    # A non-empty WAL belongs to the current target; restore must refuse.
    wal = Path(f"{target}.wal")
    wal.write_bytes(b"not really wal\n")
    with pytest.raises(BackupError, match="unrecovered WAL"):
        restore_database(dest, target, force=True)
    assert target.read_bytes() == before
    wal.unlink()
    with Engine(target) as engine:
        assert engine.execute("AAYUSH SELECT * FROM preserved")["rows"] == [{"id": 7}]


# --- 7.7 cross-process exclusion -----------------------------------------


def test_cross_process_owner_blocks_a_second_process(tmp_path: Path):
    path = tmp_path / "cross.aaydb"
    holder_code = (
        "import sys\n"
        f"sys.path.insert(0, {SRC!r})\n"
        "from aaysql import Engine\n"
        f"e = Engine({str(path)!r})\n"
        "print('READY', flush=True)\n"
        "sys.stdin.readline()\n"
        "e.close()\n"
        "print('CLOSED', flush=True)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "READY"

        blocked = _run(_try_open_script(path))
        assert blocked.returncode == 3, blocked.stderr
        assert "DATABASE_LOCKED" in blocked.stdout

        # The holder is still healthy.
        assert holder.poll() is None
    finally:
        if holder.poll() is None:
            assert holder.stdin is not None
            holder.stdin.write("\n")
            holder.stdin.flush()
            holder.wait(timeout=60)
    assert holder.returncode == 0

    reopened = _run(_try_open_script(path))
    assert reopened.returncode == 0, reopened.stderr
    assert "OPEN" in reopened.stdout


def test_os_releases_the_lease_when_the_owner_crashes(tmp_path: Path):
    path = tmp_path / "crash.aaydb"
    crash_code = (
        "import os, sys\n"
        f"sys.path.insert(0, {SRC!r})\n"
        "from aaysql import Engine\n"
        f"e = Engine({str(path)!r})\n"
        "print('READY', flush=True)\n"
        "os._exit(0)\n"
    )
    crashed = _run(crash_code)
    assert crashed.returncode == 0, crashed.stderr
    assert "READY" in crashed.stdout
    assert Path(f"{path}{LOCK_SUFFIX}").exists()

    reopened = _run(_try_open_script(path))
    assert reopened.returncode == 0, reopened.stderr
    assert "OPEN" in reopened.stdout


def test_server_and_process_ownership_agree(tmp_path: Path):
    path = tmp_path / "server-cross.aaydb"
    server = _server(path)
    server.start()
    try:
        blocked = _run(_try_open_script(path))
        assert blocked.returncode == 3, blocked.stderr
        assert "DATABASE_LOCKED" in blocked.stdout
    finally:
        server.shutdown()
    released = _run(_try_open_script(path))
    assert released.returncode == 0, released.stderr


# --- 7.8 startup failure cleanup and injected engines --------------------


def test_server_bind_failure_releases_the_lease(tmp_path: Path, monkeypatch):
    path = tmp_path / "bind-fail.aaydb"

    def refuse(self, *args, **kwargs):
        raise OSError("simulated bind failure")

    monkeypatch.setattr("aaysql.server._ThreadingTCPServer.server_bind", refuse)
    server = _server(path)
    with pytest.raises(OSError, match="simulated bind failure"):
        server.start()
    monkeypatch.undo()
    with Engine(path):
        pass


def test_server_tls_failure_releases_the_lease(tmp_path: Path):
    path = tmp_path / "tls-fail.aaydb"
    cert = tmp_path / "bad-cert.pem"
    key = tmp_path / "bad-key.pem"
    cert.write_text("not a certificate\n", encoding="ascii")
    key.write_text("not a key\n", encoding="ascii")
    server = AayushSQLServer(
        ServerConfig(db_path=path, port=0, tls_cert=str(cert), tls_key=str(key))
    )
    with pytest.raises(ssl.SSLError):
        server.start()
    with Engine(path):
        pass


def test_server_borrows_an_injected_persistent_engine(tmp_path: Path):
    path = tmp_path / "injected.aaydb"
    engine = Engine(path)
    engine.execute("AAYUSH CREATE TABLE t (id INT)")
    engine.execute("AAYUSH INSERT INTO t VALUES (1)")
    server = AayushSQLServer(ServerConfig(db_path=path, port=0), engine=engine)
    try:
        # Starting must not attempt a second acquisition of the engine's lease.
        server.start()
        assert server.engine is engine
        with AayushSQLClient(*server.address) as client:
            assert client.execute("AAYUSH SELECT * FROM t")["rows"] == [{"id": 1}]
        server.shutdown()
        # Existing lifecycle closes the injected engine; ownership is then free.
        assert engine.lease is None
        with Engine(path):
            pass
    finally:
        try:
            server.shutdown()
        finally:
            engine.close()


def test_server_accepts_an_injected_in_memory_engine(tmp_path: Path):
    memory = Engine(":memory:")
    memory.execute("AAYUSH CREATE TABLE t (id INT)")
    server = AayushSQLServer(ServerConfig(db_path=None, port=0), engine=memory)
    try:
        server.start()
        with AayushSQLClient(*server.address) as client:
            client.execute("AAYUSH INSERT INTO t VALUES (5)")
            assert client.execute("AAYUSH SELECT COUNT(*) AS n FROM t")["rows"] == [{"n": 1}]
    finally:
        server.shutdown()
        memory.close()


def test_backup_failure_releases_the_lease(tmp_path: Path, monkeypatch):
    path = tmp_path / "backup-fail.aaydb"
    _open_with_rows(path)
    from aaysql import backup as backup_module

    def explode(*args, **kwargs):
        raise BackupError("simulated recovery failure")

    monkeypatch.setattr(backup_module, "_recover_pending_wal", explode)
    with pytest.raises(BackupError, match="simulated"):
        backup_database(path, tmp_path / "backup")
    monkeypatch.undo()
    with Engine(path):
        pass


# --- 7.9 canonical ownership versus a custom lock_path --------------------
#
# ServerConfig.lock_path is a v0.8.0 compatibility surface. It is a
# *supplemental* coordination lock: the server still acquires the canonical
# <db>.server.lock, so no configuration can create a second ownership identity.


def _server_with_lock(path: Path, lock: Path | str) -> AayushSQLServer:
    return AayushSQLServer(ServerConfig(db_path=path, port=0, lock_path=Path(lock)))


def _assert_server_usable(server: AayushSQLServer, table: str) -> None:
    """Prove the server still serves traffic after a rejected open."""
    with AayushSQLClient(*server.address) as client:
        client.execute(f"AAYUSH CREATE TABLE {table} (id INT)")
        rows = client.execute(f"AAYUSH SELECT COUNT(*) AS n FROM {table}")["rows"]
        assert rows == [{"n": 0}]


def _live_server_script(db: Path, lock: Path) -> str:
    return (
        "import sys\n"
        f"sys.path.insert(0, {SRC!r})\n"
        "from aaysql.server import AayushSQLServer, ServerConfig\n"
        f"cfg = ServerConfig(db_path={str(db)!r}, port=0, lock_path={str(lock)!r})\n"
        "server = AayushSQLServer(cfg)\n"
        "server.start()\n"
        "print('READY', flush=True)\n"
        "sys.stdin.readline()\n"
        "server.shutdown()\n"
        "print('CLOSED', flush=True)\n"
    )


def _try_server_script(db: Path, lock: Path) -> str:
    return (
        "import sys\n"
        f"sys.path.insert(0, {SRC!r})\n"
        "from aaysql.errors import DatabaseLockedError\n"
        "from aaysql.server import AayushSQLServer, ServerConfig\n"
        f"cfg = ServerConfig(db_path={str(db)!r}, port=0, lock_path={str(lock)!r})\n"
        "try:\n"
        "    server = AayushSQLServer(cfg)\n"
        "    server.start()\n"
        "except DatabaseLockedError as exc:\n"
        "    print('CODE', exc.code, flush=True)\n"
        "    sys.exit(3)\n"
        "server.shutdown()\n"
        "print('STARTED', flush=True)\n"
    )


def test_custom_lock_server_blocks_an_embedded_engine(tmp_path: Path):
    path = tmp_path / "custom.aaydb"
    lock = tmp_path / "custom.lock"
    server = _server_with_lock(path, lock)
    server.start()
    try:
        # Both the canonical ownership lock and the supplemental lock exist.
        assert Path(f"{path}{LOCK_SUFFIX}").exists()
        assert lock.exists()
        assert server.started

        with pytest.raises(DatabaseLockedError) as excinfo:
            Engine(path)
        assert excinfo.value.code == "DATABASE_LOCKED"
        assert str(path) in str(excinfo.value)

        # The server is untouched by the rejected open.
        _assert_server_usable(server, "probe")
    finally:
        server.shutdown()
        server.shutdown()  # repeated shutdown is safe

    with Engine(path) as engine:
        assert engine.owns_lease is True


def test_custom_lock_server_blocks_backup(tmp_path: Path):
    path = tmp_path / "custom-backup.aaydb"
    _open_with_rows(path)
    lock = tmp_path / "custom-backup.lock"
    server = _server_with_lock(path, lock)
    server.start()
    dest = tmp_path / "backup"
    try:
        with pytest.raises(BackupError, match="in use"):
            backup_database(path, dest)
        # A refused backup leaves no destination and no staging artifact.
        assert not dest.exists()
        assert list(tmp_path.glob("backup*")) == []
    finally:
        server.shutdown()

    manifest = backup_database(path, dest)
    assert manifest["tables"] == [{"name": "t", "rows": 1}]


@pytest.mark.parametrize("first_is_custom", [True, False])
def test_default_and_custom_lock_servers_contend(
    tmp_path: Path, first_is_custom: bool
):
    path = tmp_path / "pair.aaydb"
    lock = tmp_path / "pair.lock"
    first = _server_with_lock(path, lock) if first_is_custom else _server(path)
    first.start()
    try:
        second = _server(path) if first_is_custom else _server_with_lock(path, lock)
        with pytest.raises(DatabaseLockedError) as excinfo:
            second.start()
        assert excinfo.value.code == "DATABASE_LOCKED"
        assert second.started is False

        # The failed startup released everything it had acquired.
        _assert_server_usable(first, "still_serving")
    finally:
        first.shutdown()

    with Engine(path):
        pass


def test_custom_lock_server_and_embedded_engine_contend_across_processes(
    tmp_path: Path,
):
    path = tmp_path / "xproc.aaydb"
    lock = tmp_path / "xproc.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", _live_server_script(path, lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "READY"
        blocked = _run(_try_open_script(path))
        assert blocked.returncode == 3, blocked.stderr
        assert "DATABASE_LOCKED" in blocked.stdout
        assert holder.poll() is None
    finally:
        if holder.poll() is None:
            assert holder.stdin is not None
            holder.stdin.write("\n")
            holder.stdin.flush()
            try:
                holder.wait(timeout=60)
            except subprocess.TimeoutExpired:  # pragma: no cover - last resort
                holder.kill()
                holder.wait(timeout=60)
    assert holder.returncode == 0
    assert "CLOSED" in (holder.stdout.read() if holder.stdout else "")

    opened = _run(_try_open_script(path))
    assert opened.returncode == 0, opened.stderr
    assert "OPEN" in opened.stdout


def test_custom_lock_server_is_blocked_by_an_embedded_engine_across_processes(
    tmp_path: Path,
):
    path = tmp_path / "xproc-rev.aaydb"
    lock = tmp_path / "xproc-rev.lock"
    with Engine(path):
        blocked = _run(_try_server_script(path, lock))
        assert blocked.returncode == 3, blocked.stderr
        assert "DATABASE_LOCKED" in blocked.stdout
        # The custom lock was never left behind as a stale reservation.
        assert not lock.exists()

    started = _run(_try_server_script(path, lock))
    assert started.returncode == 0, started.stderr
    assert "STARTED" in started.stdout


def test_only_the_canonical_lease_can_be_borrowed(tmp_path: Path):
    path = tmp_path / "borrow-canonical.aaydb"
    canonical = DatabaseLease.for_database(path)
    assert canonical.is_canonical
    canonical.acquire()
    try:
        canonical.validate_borrow(path)
        with Engine(path, _lease=canonical) as engine:
            engine.execute("AAYUSH CREATE TABLE t (id INT)")
        assert canonical.active
    finally:
        canonical.release()

    with pytest.raises(StorageError, match="already been released"):
        canonical.validate_borrow(path)
    with Engine(path):
        pass


def test_supplemental_lease_cannot_impersonate_canonical_ownership(tmp_path: Path):
    path = tmp_path / "impersonate.aaydb"
    lease = DatabaseLease.for_database(path, lock_path=tmp_path / "elsewhere.lock")
    assert lease.db_path == path
    assert lease.is_canonical is False
    lease.acquire()
    try:
        with pytest.raises(StorageError, match="canonical ownership lock"):
            lease.validate_borrow(path)
        with pytest.raises(StorageError, match="canonical ownership lock"):
            Engine(path, _lease=lease)
    finally:
        lease.release()

    # The unrelated lock never blocked real ownership.
    with Engine(path):
        pass


def test_lock_path_equal_to_canonical_is_not_acquired_twice(tmp_path: Path):
    path = tmp_path / "same.aaydb"
    config = ServerConfig(
        db_path=path, port=0, lock_path=Path(f"{path}{LOCK_SUFFIX}")
    )
    assert config.supplemental_lock_path is None
    server = AayushSQLServer(config)
    server.start()
    try:
        with pytest.raises(DatabaseLockedError):
            Engine(path)
        _assert_server_usable(server, "same_lock")
    finally:
        server.shutdown()
    with Engine(path):
        pass


def test_relative_supplemental_lock_path_is_canonicalised(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = Path("relative.aaydb")
    server = _server_with_lock(path, "relative.lock")
    server.start()
    try:
        assert (tmp_path / "relative.lock").exists()
        with pytest.raises(DatabaseLockedError):
            Engine(tmp_path / "relative.aaydb")
    finally:
        server.shutdown()
    with Engine(tmp_path / "relative.aaydb"):
        pass


def test_two_databases_cannot_share_a_supplemental_lock(tmp_path: Path):
    shared = tmp_path / "shared.lock"
    first_db = tmp_path / "first.aaydb"
    second_db = tmp_path / "second.aaydb"
    first = _server_with_lock(first_db, shared)
    first.start()
    try:
        second = _server_with_lock(second_db, shared)
        with pytest.raises(DatabaseLockedError) as excinfo:
            second.start()
        assert excinfo.value.code == "DATABASE_LOCKED"
        # The failed startup unwound the canonical lock it had already taken.
        with Engine(second_db):
            pass
    finally:
        first.shutdown()


def test_custom_lock_startup_failure_releases_both_leases(tmp_path: Path, monkeypatch):
    path = tmp_path / "both-fail.aaydb"
    lock = tmp_path / "both-fail.lock"

    def refuse(self, *args, **kwargs):
        raise OSError("simulated bind failure")

    monkeypatch.setattr("aaysql.server._ThreadingTCPServer.server_bind", refuse)
    server = _server_with_lock(path, lock)
    with pytest.raises(OSError, match="simulated bind failure"):
        server.start()
    monkeypatch.undo()

    # No stale reservation and no held lock: both entry points work again.
    with Engine(path):
        pass
    retry = _server_with_lock(path, lock)
    retry.start()
    retry.shutdown()


def test_supplemental_acquisition_failure_releases_canonical_lease(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "supp-fail.aaydb"
    lock = tmp_path / "supp-fail.lock"
    real_for_database = DatabaseLease.for_database

    def refuse_custom(cls, db_path, *, lock_path=None):
        if lock_path is not None:
            raise StorageError("simulated supplemental lock failure")
        return real_for_database(db_path)

    monkeypatch.setattr(
        "aaysql.server.DatabaseLease",
        type(
            "PatchedLease",
            (DatabaseLease,),
            {"for_database": classmethod(refuse_custom)},
        ),
    )
    server = _server_with_lock(path, lock)
    with pytest.raises(StorageError, match="simulated supplemental"):
        server.start()
    monkeypatch.undo()

    with Engine(path):
        pass
    assert not lock.exists()


def test_lock_path_without_a_database_is_rejected():
    with pytest.raises(ServerConfigError, match="lock_path requires a file-backed"):
        AayushSQLServer(ServerConfig(lock_path=Path("irrelevant.lock")))


def test_custom_lock_resolves_symlinked_parents(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows privileges
        pytest.skip("directory symlinks are not available on this host")

    path = real / "sym.aaydb"
    server = _server_with_lock(path, link / "custom.lock")
    server.start()
    try:
        with pytest.raises(DatabaseLockedError):
            Engine(link / "sym.aaydb")
    finally:
        server.shutdown()
