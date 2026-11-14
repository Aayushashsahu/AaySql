"""Snapshot-consistent readers, one serialized writer, and group commit.

The durable rules of the engine do not change here: a transaction becomes
visible only after its WAL COMMIT record is durable. This module adds the
in-memory publication layer around that rule.

* A :class:`Version` is an immutable-in-practice database root. Readers pin one
  root for the duration of a statement (READ COMMITTED) or a transaction
  (SNAPSHOT). Nothing a writer does can mutate a published table, so a reader
  never sees half of an INSERT or an uncommitted UPDATE.
* A writer works on a shallow copy-on-write root. The first mutation of a table
  detaches that table (and only that table) from the published root.
* Commits enter a queue. One flusher appends all queued transactions to the WAL
  and performs a single COMMIT fsync, then publishes them in order. Writer
  admission is released before the fsync, so other sessions can keep executing
  and enqueue while the disk is busy.
* Conflicts are first-updater-wins. A transaction commits only when every table
  it changed still has the revision it started from; otherwise it receives
  :class:`WriteConflictError`.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .ast import (
    Begin,
    Commit,
    CreateIndex,
    CreateTable,
    Delete,
    DropIndex,
    DropTable,
    Insert,
    Rollback,
    Update,
)
from .delta import MutationRecorder, apply_changes
from .errors import (
    AmbiguousCommitError,
    ConstraintError,
    ExecutionError,
    StorageError,
    WriteConflictError,
)
from .executor import Executor
from .lexer import Lexer
from .parser import Parser
from .storage import Database
from .transaction import TransactionManager
from .undo import UndoJournal

MUTATION_STATEMENTS = (
    CreateTable,
    DropTable,
    Insert,
    Update,
    Delete,
    CreateIndex,
    DropIndex,
)

# A short coalescing window lets commits from other admitted writers join the
# batch. It is a latency/throughput trade-off, not a correctness mechanism.
COMMIT_GROUP_WINDOW_SECONDS = 0.005


class IsolationLevel(str, Enum):
    """The two supported isolation levels."""

    READ_COMMITTED = "READ COMMITTED"
    SNAPSHOT = "SNAPSHOT"

    @classmethod
    def parse(cls, value: Any) -> IsolationLevel:
        if value is None:
            return cls.READ_COMMITTED
        if isinstance(value, cls):
            return value
        text = str(value).strip().upper().replace("_", " ")
        if text in {"READ COMMITTED", "READCOMMITTED", "RC"}:
            return cls.READ_COMMITTED
        if text in {"SNAPSHOT", "SNAPSHOT ISOLATION", "REPEATABLE READ"}:
            return cls.SNAPSHOT
        raise ExecutionError(f"Unsupported isolation level: {value!r}")


@dataclass
class Version:
    """One published database root and the revisions it publishes."""

    number: int
    db: Database
    table_revisions: dict[str, int]
    catalog_revision: int


@dataclass
class Snapshot:
    """A pinned read view. Releasing it is deterministic and idempotent."""

    snapshot_id: str
    version: int
    db: Database
    table_revisions: dict[str, int]
    catalog_revision: int
    _released: bool = field(default=False, repr=False)


@dataclass
class CommitProposal:
    db: Database
    changes: list[dict[str, Any]]
    dirty_tables: set[str]
    insert_only: set[str]
    ddl: bool
    base_version: int
    future: Future
    order: int


class MvccStore:
    """Owns published roots, snapshot accounting, and the group-commit queue."""

    def __init__(
        self,
        db: Database,
        storage: Any | None = None,
        *,
        group_window: float = COMMIT_GROUP_WINDOW_SECONDS,
    ) -> None:
        self._storage = storage
        self._lock = threading.RLock()
        self._admission = threading.Lock()
        self._flush = threading.Lock()
        self._published = threading.Lock()
        self._group_window = max(0.0, float(group_window))
        self._version = Version(
            number=0, db=db, table_revisions={name: 0 for name in db.tables}, catalog_revision=0
        )
        self._tentative_revisions = dict(self._version.table_revisions)
        self._tentative_catalog = 0
        self._next_order = 0
        self._queue: deque[CommitProposal] = deque()
        self._pending_unique: dict[tuple[str, str], set[tuple[type, Any]]] = {}
        self._snapshots: dict[str, Snapshot] = {}
        self._failed: BaseException | None = None
        self._publish_hook: Any | None = None
        self.fsyncs = 0
        self.batches = 0
        self.commits = 0
        self.snapshots_captured = 0
        self.snapshots_released = 0
        self.admissions = 0
        self.commit_wait_timeouts = 0

    # --- publication state -------------------------------------------------

    @property
    def current(self) -> Database:
        with self._lock:
            return self._version.db

    @property
    def version(self) -> int:
        with self._lock:
            return self._version.number

    @property
    def failed(self) -> BaseException | None:
        with self._lock:
            return self._failed

    def set_publish_hook(self, hook: Any) -> None:
        self._publish_hook = hook

    def _assert_available(self) -> None:
        if self._failed is not None:
            raise StorageError(
                "database durability state is ambiguous after a failed group commit; "
                "reopen the database to recover"
            ) from self._failed

    # --- snapshots ---------------------------------------------------------

    def capture(self) -> Snapshot:
        with self._lock:
            self._assert_available()
            snapshot = Snapshot(
                snapshot_id=uuid.uuid4().hex,
                version=self._version.number,
                db=self._version.db,
                table_revisions=dict(self._version.table_revisions),
                catalog_revision=self._version.catalog_revision,
            )
            self._snapshots[snapshot.snapshot_id] = snapshot
            self.snapshots_captured += 1
            return snapshot

    def release(self, snapshot: Snapshot | None) -> None:
        if snapshot is None or snapshot._released:
            return
        with self._lock:
            snapshot._released = True
            self._snapshots.pop(snapshot.snapshot_id, None)
            self.snapshots_released += 1

    @property
    def retained_snapshots(self) -> int:
        with self._lock:
            return len(self._snapshots)

    def snapshot_stats(self) -> dict[str, int]:
        with self._lock:
            retained = len(self._snapshots)
        return {
            "retained": retained,
            "captured": self.snapshots_captured,
            "released": self.snapshots_released,
            "current_version": self._version.number,
        }

    def metrics(self) -> dict[str, int]:
        with self._lock:
            return {
                "fsyncs": self.fsyncs,
                "batches": self.batches,
                "commits": self.commits,
                "admissions": self.admissions,
                "queue_depth": len(self._queue),
                "commit_wait_timeouts": self.commit_wait_timeouts,
                "retained_snapshots": len(self._snapshots),
                "version": self._version.number,
            }

    # --- legacy engine bridge ---------------------------------------------

    def sync_legacy(self, db: Database) -> None:
        """Publish a legacy in-place engine mutation as a new immutable root.

        Legacy :class:`~aaysql.engine.Engine` calls mutate one Database object
        directly. Before a session captures a snapshot, that object is cloned so
        the published root can never be changed underneath the session.
        """
        with self._lock:
            copied = db.clone()
            revisions = {name: revisions + 1 for name, revisions in self._version.table_revisions.items()}
            for name in copied.tables:
                revisions.setdefault(name, 1)
            self._version = Version(
                number=self._version.number + 1,
                db=copied,
                table_revisions=revisions,
                catalog_revision=self._version.catalog_revision + 1,
            )
            self._tentative_revisions = dict(revisions)
            self._tentative_catalog = self._version.catalog_revision

    # --- commit path -------------------------------------------------------

    def submit(
        self,
        db: Database,
        changes: list[dict[str, Any]],
        *,
        base_snapshot: Snapshot,
        dirty_tables: set[str],
        insert_tables: set[str] | None = None,
        ddl: bool,
    ) -> dict[str, Any]:
        """Validate, enqueue, flush, and wait for one transaction's publication."""
        if not changes and not dirty_tables:
            return {"status": "ok", "message": "Transaction committed", "commit_lsn": db.wal_lsn}

        with self._admission:
            self.admissions += 1
            self._assert_available()
            insert_only = set(insert_tables or ())
            for name in sorted(dirty_tables - insert_only):
                expected = base_snapshot.table_revisions.get(name)
                actual = self._tentative_revisions.get(name)
                if expected != actual:
                    raise WriteConflictError(
                        f"Write conflict on table '{name}': the table changed after "
                        "this transaction's snapshot"
                    )
            for name in sorted(insert_only):
                if name in base_snapshot.table_revisions and name not in self._tentative_revisions:
                    raise WriteConflictError(
                        f"Write conflict on table '{name}': the table was dropped"
                    )
            if ddl and base_snapshot.catalog_revision != self._tentative_catalog:
                raise WriteConflictError(
                    "Write conflict on the schema catalog: another transaction "
                    "changed tables or indexes after this transaction's snapshot"
                )
            self._reserve_insert_keys(db, changes, insert_only)
            order = self._next_order
            self._next_order += 1
            future: Future = Future()
            proposal = CommitProposal(
                db=db,
                changes=list(changes),
                dirty_tables=set(dirty_tables),
                insert_only=insert_only,
                ddl=ddl,
                base_version=base_snapshot.version,
                future=future,
                order=order,
            )
            # Reserve the revisions now. This is what makes first-updater-wins
            # deterministic even while an earlier batch is still fsyncing.
            for name in dirty_tables:
                if name in db.tables:
                    self._tentative_revisions[name] = (
                        self._tentative_revisions.get(name, 0) + 1
                    )
                else:
                    self._tentative_revisions.pop(name, None)
            if ddl:
                self._tentative_catalog += 1
            with self._lock:
                self._queue.append(proposal)

        try:
            while not future.done():
                self._drive()
                if not future.done():
                    try:
                        future.result(timeout=0.05)
                    except FutureTimeoutError:
                        # Python 3.10 keeps concurrent.futures.TimeoutError
                        # distinct from the built-in TimeoutError.
                        with self._lock:
                            self.commit_wait_timeouts += 1
                        continue
            return future.result()
        except BaseException:
            if not future.done():
                future.cancel()
            raise

    def _reserve_insert_keys(
        self, db: Database, changes: list[dict[str, Any]], insert_only: set[str]
    ) -> None:
        """Reject concurrent inserts that would collide on a UNIQUE column.

        Inserts do not conflict with other inserts on table revision, so their
        key uniqueness must be checked against both the published rows and rows
        already reserved by earlier queued batches.
        """
        staged: dict[tuple[str, str], set[tuple[type, Any]]] = {}
        for change in changes:
            if change.get("t") != "insert":
                continue
            name = change.get("table")
            if name not in insert_only or name not in db.tables:
                continue
            published = self._version.db.tables.get(name)
            local = db.table(name)
            columns = [c for c in local.columns if c.primary_key or c.unique]
            if not columns:
                continue
            for row in change.get("rows", []):
                for column in columns:
                    value = row.get(column.name)
                    if value is None:
                        continue
                    key = (type(value), value)
                    slot = (name, column.name)
                    pending = self._pending_unique.get(slot, set())
                    if key in pending or key in staged.get(slot, set()):
                        kind = "PRIMARY KEY" if column.primary_key else "UNIQUE"
                        raise ConstraintError(
                            f"{kind} violation on '{column.name}'"
                        )
                    for existing in published.rows if published is not None else ():
                        current = existing.get(column.name)
                        if current is not None and (type(current), current) == key:
                            kind = "PRIMARY KEY" if column.primary_key else "UNIQUE"
                            raise ConstraintError(
                                f"{kind} violation on '{column.name}'"
                            )
                    staged.setdefault(slot, set()).add(key)
        for slot, keys in staged.items():
            self._pending_unique.setdefault(slot, set()).update(keys)

    def _drive(self) -> None:
        if not self._flush.acquire(blocking=False):
            return
        try:
            with self._lock:
                if not self._queue:
                    return
            if self._group_window:
                time.sleep(self._group_window)
            with self._lock:
                batch = list(self._queue)
                self._queue.clear()
            if not batch:
                return
            try:
                plans = self._plan_batch(batch)
                if self._storage is not None:
                    # v3 WAL records are whole images, so each image must be the
                    # cumulative batch root, not a member's older private
                    # workspace. v4 ignores the image and uses each delta.
                    self._storage.save_batch(
                        [(merged, proposal.changes) for proposal, merged, _, _ in plans]
                    )
                    with self._lock:
                        self.fsyncs += 1
                        self.batches += 1
                else:
                    with self._lock:
                        self.batches += 1
                final = self._publish_plans(plans)
                if self._storage is not None:
                    # Only after publication: a checkpoint must never reference a
                    # root older than the WAL commit point for this batch.
                    self._storage.checkpoint_batch(final.db, final.db.wal_lsn)
            except AmbiguousCommitError as exc:
                self._fail(batch, exc)
                raise
            except BaseException as exc:
                self._fail(batch, exc)
                raise
        finally:
            self._flush.release()

    def _fail(self, batch: list[CommitProposal], exc: BaseException) -> None:
        with self._lock:
            self._failed = exc
            for proposal in batch:
                if not proposal.future.done():
                    proposal.future.set_exception(exc)

    def _plan_batch(
        self, batch: list[CommitProposal]
    ) -> list[tuple[CommitProposal, Database, dict[str, int], int]]:
        """Compute cumulative roots for a batch before its WAL is written."""
        merged_results: list[tuple[CommitProposal, Database, dict[str, int], int]] = []
        with self._published:
            with self._lock:
                current = self._version
            for proposal in batch:
                if proposal.base_version == current.number:
                    merged = proposal.db
                else:
                    merged = self._merge(proposal, current.db)
                revisions = dict(current.table_revisions)
                for name in proposal.dirty_tables:
                    if name in merged.tables:
                        revisions[name] = revisions.get(name, 0) + 1
                    else:
                        revisions.pop(name, None)
                catalog = current.catalog_revision + (1 if proposal.ddl else 0)
                current = Version(
                    number=current.number + 1,
                    db=merged,
                    table_revisions=revisions,
                    catalog_revision=catalog,
                )
                merged_results.append(
                    (proposal, merged, revisions, catalog)
                )
        return merged_results

    def _publish_plans(
        self, plans: list[tuple[CommitProposal, Database, dict[str, int], int]]
    ) -> Version:
        with self._published:
            current = self._version
            for _proposal, _merged, revisions, catalog in plans:
                current = Version(
                    number=current.number + 1,
                    db=_merged,
                    table_revisions=revisions,
                    catalog_revision=catalog,
                )

            with self._lock:
                self._version = current
                # Proposals that could not be placed in this batch stay queued;
                # their reservations already use the new committed baseline.
                self.commits += len(plans)
                self._publish_hook and self._publish_hook(current.db)

            for proposal, merged, revisions, catalog in plans:
                for change in proposal.changes:
                    if change.get("t") != "insert":
                        continue
                    name = change.get("table")
                    table = merged.tables.get(name)
                    if table is None:
                        continue
                    for column in table.columns:
                        if not (column.primary_key or column.unique):
                            continue
                        pending = self._pending_unique.get((name, column.name))
                        if pending is None:
                            continue
                        for row in change.get("rows", []):
                            value = row.get(column.name)
                            if value is not None:
                                pending.discard((type(value), value))
                if not proposal.future.done():
                    proposal.future.set_result(
                        {
                            "status": "ok",
                            "message": "Transaction committed",
                            "commit_lsn": merged.wal_lsn,
                            "version": current.number,
                        }
                    )
        return current

    def _merge(self, proposal: CommitProposal, base: Database) -> Database:
        """Rebase a queued proposal onto a newer published root.

        Conflicts guarantee that no other committed transaction touched the
        tables this proposal writes, so replaying its deltas onto the newer root
        is deterministic and cannot overwrite another writer's rows.
        """
        merged = base.shallow_clone()
        for name in proposal.dirty_tables:
            if name in merged.tables:
                merged.detach_table(name)
            else:
                merged.tables.pop(name, None)
                merged.drop_indexes_on(name)
        apply_changes(merged, proposal.changes)
        merged.wal_lsn = proposal.db.wal_lsn
        return merged


@dataclass
class _Workspace:
    db: Database
    base: Snapshot
    dirty_tables: set[str] = field(default_factory=set)
    insert_tables: set[str] = field(default_factory=set)
    private_tables: set[str] = field(default_factory=set)
    ddl: bool = False


class DatabaseSession:
    """One isolated session with its own transaction and read view."""

    def __init__(
        self,
        store: MvccStore,
        *,
        isolation: IsolationLevel | str | None = None,
        lexer: Lexer | None = None,
    ) -> None:
        self.store = store
        self.isolation = IsolationLevel.parse(isolation)
        self._lexer = lexer or Lexer()
        self._recorder = MutationRecorder()
        self._journal = UndoJournal()
        self._transactions = TransactionManager()
        self._snapshot: Snapshot | None = None
        self._workspace: _Workspace | None = None
        self._closed = False

    @property
    def in_transaction(self) -> bool:
        return self._transactions.in_transaction

    @property
    def db(self) -> Database:
        return self._workspace.db if self._workspace is not None else self.store.current

    def _ensure_usable(self) -> None:
        if self._closed:
            raise StorageError("Cannot execute statement on a closed session")
        if self.store.failed is not None:
            raise StorageError(
                "database durability state is ambiguous after a WAL failure; "
                "reopen the database to recover"
            ) from self.store.failed

    def execute(self, sql: str) -> dict[str, Any]:
        self._ensure_usable()
        tokens = self._lexer.tokenize(sql)
        stmt = Parser(tokens).parse()
        if isinstance(stmt, Begin):
            return self.begin(stmt.isolation)
        if isinstance(stmt, Commit):
            return self.commit()
        if isinstance(stmt, Rollback):
            return self.rollback()
        if isinstance(stmt, MUTATION_STATEMENTS):
            if self._transactions.in_transaction:
                return self._execute_in_transaction(stmt)
            return self._execute_autocommit(stmt)
        return self._execute_read(stmt)

    def _executor(self, db: Database) -> Executor:
        return Executor(db, recorder=self._recorder, journal=self._journal)

    def _execute_read(self, stmt: Any) -> dict[str, Any]:
        if self._workspace is not None:
            return self._executor(self._workspace.db).execute(stmt)
        refresh = self._snapshot is None or (
            self.isolation is IsolationLevel.READ_COMMITTED
        )
        own_snapshot = False
        if refresh:
            if self._snapshot is not None:
                self.store.release(self._snapshot)
            self._snapshot = self.store.capture()
            own_snapshot = not self._transactions.in_transaction
        snapshot = self._snapshot
        assert snapshot is not None
        try:
            return self._executor(snapshot.db).execute(stmt)
        finally:
            if own_snapshot:
                self.store.release(snapshot)
                self._snapshot = None

    def begin(self, isolation: str | None = None) -> dict[str, Any]:
        self._ensure_usable()
        if isolation is not None:
            self.isolation = IsolationLevel.parse(isolation)
        if self._transactions.in_transaction:
            raise ExecutionError(
                "Nested transactions are not supported; COMMIT or ROLLBACK the active transaction first"
            )
        self._transactions.begin(self._journal.mark())
        self._recorder.clear()
        self._recorder.start()
        if self._snapshot is None:
            self._snapshot = self.store.capture()
        return {"status": "ok", "message": "Transaction started"}

    def _begin_workspace(self) -> _Workspace:
        if self._workspace is None:
            if self._snapshot is None:
                self._snapshot = self.store.capture()
            self._workspace = _Workspace(db=self._snapshot.db.shallow_clone(), base=self._snapshot)
        return self._workspace

    def _prepare_mutation(self, stmt: Any) -> None:
        workspace = self._begin_workspace()
        table = getattr(stmt, "table", None)
        if isinstance(stmt, DropIndex):
            index = workspace.db.indexes.get(stmt.name)
            table = index.table if index is not None else None
        if isinstance(stmt, CreateTable):
            table = stmt.name
        if isinstance(stmt, DropTable):
            table = stmt.name
        if isinstance(stmt, CreateIndex):
            table = stmt.table
        if isinstance(stmt, (DropIndex, CreateTable, DropTable, CreateIndex)):
            workspace.ddl = True
        if table is None:
            return
        workspace.dirty_tables.add(table)
        if isinstance(stmt, Insert):
            workspace.insert_tables.add(table)
        else:
            workspace.insert_tables.discard(table)
        if isinstance(stmt, (CreateTable, DropTable)):
            return
        if table not in workspace.private_tables and table in workspace.db.tables:
            workspace.db.detach_table(table)
            workspace.private_tables.add(table)

    def _execute_autocommit(self, stmt: Any) -> dict[str, Any]:
        # Autocommit statements are internally retried on a stale table
        # revision. The retry is invisible to the caller and never reuses a
        # workspace whose constraint checks ran against an older snapshot.
        last_conflict: WriteConflictError | None = None
        for attempt in range(100):
            try:
                return self._autocommit_once(stmt)
            except WriteConflictError as exc:
                last_conflict = exc
                if attempt >= 99:
                    break
                time.sleep(0.001)
        assert last_conflict is not None
        raise last_conflict

    def _autocommit_once(self, stmt: Any) -> dict[str, Any]:
        self._recorder.clear()
        self._recorder.start()
        snapshot = self.store.capture()
        self._snapshot = snapshot
        self._workspace = None
        try:
            self._prepare_mutation(stmt)
            workspace = self._workspace
            assert workspace is not None
            mark = self._journal.mark()
            try:
                result = self._executor(workspace.db).execute(stmt)
            except Exception:
                self._journal.rollback_to(mark, workspace.db)
                raise
            changes = self._recorder.take()
            self._commit_workspace(workspace, changes)
            return result
        finally:
            self.store.release(snapshot)
            self._snapshot = None
            self._workspace = None
            self._recorder.clear()
            self._journal.clear()

    def _execute_in_transaction(self, stmt: Any) -> dict[str, Any]:
        mark = self._journal.mark()
        recorder_mark = self._recorder.mark()
        if self._workspace is None and self.isolation is IsolationLevel.READ_COMMITTED:
            # Every statement in READ COMMITTED starts from the latest committed
            # root until this transaction performs its first write.
            if self._snapshot is not None:
                self.store.release(self._snapshot)
                self._snapshot = None
            self._snapshot = self.store.capture()
        self._prepare_mutation(stmt)
        workspace = self._workspace
        assert workspace is not None
        try:
            result = self._executor(workspace.db).execute(stmt)
        except Exception:
            self._journal.rollback_to(mark, workspace.db)
            self._recorder.rollback_to(recorder_mark)
            raise
        self._transactions.record_statement()
        return result

    def _commit_workspace(
        self, workspace: _Workspace, changes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        try:
            result = self.store.submit(
                workspace.db,
                changes,
                base_snapshot=workspace.base,
                dirty_tables=workspace.dirty_tables,
                insert_tables=workspace.insert_tables,
                ddl=workspace.ddl,
            )
        except AmbiguousCommitError:
            self._journal.clear()
            self._workspace = None
            raise
        return result

    def commit(self) -> dict[str, Any]:
        self._ensure_usable()
        transaction = self._transactions.commit()
        marker = transaction.undo_marker
        workspace = self._workspace
        try:
            if workspace is None:
                return {"status": "ok", "message": "Transaction committed"}
            changes = self._recorder.take()
            result = self._commit_workspace(workspace, changes)
        except AmbiguousCommitError:
            self._journal.clear()
            self._workspace = None
            raise
        except Exception:
            self._recorder.take()
            if workspace is not None:
                self._journal.rollback_to(marker, workspace.db)
            raise
        finally:
            self.store.release(self._snapshot)
            self._snapshot = None
            self._workspace = None
        self._journal.discard_to(marker)
        return result

    def rollback(self) -> dict[str, Any]:
        self._ensure_usable()
        transaction = self._transactions.rollback()
        if self._workspace is not None:
            self._journal.rollback_to(transaction.undo_marker, self._workspace.db)
        self._recorder.clear()
        self.store.release(self._snapshot)
        self._snapshot = None
        self._workspace = None
        return {"status": "ok", "message": "Transaction rolled back"}

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._transactions.in_transaction:
                self.rollback()
        finally:
            self.store.release(self._snapshot)
            self._snapshot = None
            self._workspace = None
            self._closed = True
