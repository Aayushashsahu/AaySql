# MVCC Read Views and Group Commit (Phase 4)

This document defines the concurrency contract added in Phase 4. The durable
rules did not change: a transaction is committed only after its WAL `COMMIT`
record is durable. What changed is how committed state is published in memory.

## 1. Guarantees

- A reader sees a **complete committed database version**. It never sees a
  partially applied statement or another session's uncommitted workspace.
- Readers do not wait for writers and writers do not wait for readers. A read
  pins an immutable published root; a writer mutates a private copy-on-write
  root.
- One writer is admitted at a time to generate a commit. Admission is released
  **before** the WAL fsync, so another writer can execute and enqueue while the
  disk is busy.
- Commits are published in durable order. Many independent transactions may
  share one WAL `COMMIT` fsync.
- Conflicts are first-updater-wins. A transaction that changed a table whose
  revision moved after its snapshot is rejected with `WriteConflictError`
  (`WRITE_CONFLICT` over the protocol) and is never partially published.
- Snapshot release is explicit and deterministic. Retained snapshots are counted
  by the store; a released snapshot no longer roots its version.

## 2. Isolation levels

| SQL | Default? | Read behavior |
| --- | --- | --- |
| `AAYUSH BEGIN` | Yes | `READ COMMITTED`: each statement reads the latest committed root. Once the transaction writes, it keeps that transaction's snapshot to keep its own reads coherent. |
| `AAYUSH BEGIN SNAPSHOT` | No | `SNAPSHOT`: every statement reads the root pinned at `BEGIN` until `COMMIT`/`ROLLBACK`. |
| `AAYUSH BEGIN ISOLATION LEVEL SNAPSHOT` | No | Same as `BEGIN SNAPSHOT`. |
| `AAYUSH BEGIN ISOLATION LEVEL READ COMMITTED` | No | Explicit form of the default. |

There is no `SERIALIZABLE` level. Autocommit statements are independent
transactions and use the latest committed root. Autocommit write statements are
internally retried if they lose a revision race, so callers do not see a
transient `WRITE_CONFLICT` for a single statement.

## 3. Published versions and snapshots

`MvccStore` owns the current `Version`:

```text
Version(number, db, table_revisions, catalog_revision)
```

- `db` is the published root. Published tables are treated as immutable.
- `table_revisions` is a logical counter per table, used for conflict detection.
- `catalog_revision` advances on table/index DDL.

`engine.connect(isolation=...)` returns an independent `DatabaseSession`. It owns
its transaction manager, recorder, undo journal, and executor. A snapshot is a
constant-time capture of the current root and revision maps. Session statements
never mutate the pinned root.

The legacy `Engine.execute` API remains the single-process mutable path. Before
the first session is opened, its state is cloned into a published root. Use
`Engine.connect()` for concurrency, and do not mix direct `Engine.execute`
mutations with live sessions.

## 4. Writer path and copy-on-write

A write statement:

1. captures (or reuses) a snapshot;
2. shallow-copies the catalog, sharing all table objects with the published root;
3. on first write to a table, detaches that table: copies its rows and rebuilds
   that table's indexes from the private rows;
4. runs the existing `Executor`, recorder, and undo journal against the private
   root;
5. submits the recorded delta changes and the base revisions to the commit
   queue.

Reader snapshots are O(1) in database size. A writer's cost is proportional to
the table it changes, not the whole database; unchanged tables and their index
trees are shared. The engine makes no claim of O(1) writes, and checkpointing
and recovery remain table/database sized.

DDL modifies only the private catalog, so it becomes visible to other sessions
only when the commit is published. A snapshot taken before a `CREATE TABLE`
cannot see the new table.

## 5. Conflict detection

At admission the store compares each changed table's base revision with the
tentative revision, which includes proposals queued behind an in-flight fsync.
If they differ, the transaction is rejected before it enters the queue.

- `UPDATE` / `DELETE` / index/table DDL require an unchanged table revision.
- Insert-only transactions may append to the same table as another insert-only
  transaction without a table-revision conflict. Their UNIQUE and PRIMARY KEY
  values are reserved against published rows and every earlier queued insert;
  a collision raises `ConstraintError`.
- Any DDL requires an unchanged catalog revision, so two sessions cannot
  simultaneously create/drop the same schema object.
- Conflict failures abort the transaction. The caller may retry the whole
  transaction. Reusing the old transaction is not supported.

This is deliberately conservative table-level conflict detection for writes;
there is no row-version chain, no lock manager, and no multi-writer execution.
It is sufficient for first-updater-wins and prevents lost updates.

## 6. Group commit

```text
writer A ──admission──> append proposal ──release admission──┐
writer B ──admission──> append proposal ──release admission──┤
writer C ──admission──> append proposal ──release admission──┤
                                                             v
                                         one flusher drains the queue
                                         one WAL batch append + one fsync
                                         publish all roots in order
```

- `_admission` protects conflict validation and queue insertion only.
- `_flush` ensures one thread owns the WAL append/fsync at a time.
- The flusher waits a 5 ms coalescing window and drains every queued proposal.
- `Storage.save_batch()` appends ordinary per-transaction
  `BEGIN`/`WRITE`/`COMMIT` record triples and calls `os.fsync()` once after the
  last `COMMIT`.
- A batch is published only after the fsync succeeds. Each session's future is
  completed after its root is published. A later proposal is rebased onto the
  newer root by replaying its deltas; table conflicts guarantee it cannot
  overwrite rows written by an earlier proposal.
- If the batch fsync fails, every transaction in it is failed with the same
  classified exception (`AmbiguousCommitError` after COMMIT bytes were written,
  `StorageError` before). The store becomes recovery-required; no transaction is
  reported as rolled back.
- The final image in a batch participates in the normal v4 checkpoint threshold.
  A checkpoint failure is a pending checkpoint, not a commit failure.

The batch affects **durability, not visibility ordering**: rows from batch
members become visible together only after the shared fsync.

## 7. Reclamation

`MvccStore.capture()` registers a snapshot and `release()` unregisters it. A
published root remains reachable only from the current version and active
snapshots; when the last snapshot is released the older root is collectible by
normal Python reference counting. Sessions release their snapshot on `COMMIT`,
`ROLLBACK`, error paths, disconnect cleanup, and `close()`.

`engine.mvcc.snapshot_stats()` reports `retained`, `captured`, `released`, and
the current version. `engine.mvcc.metrics()` reports fsyncs, batches, commits,
admissions, queue depth, and retained snapshots.

## 8. Server behavior

Each accepted TCP connection gets its own `DatabaseSession`. Readers run without
the old global execution lock. A session's `BEGIN` is its own; it is no longer a
process-wide owner. Disconnect rolls back only that session's transaction.
`WRITE_CONFLICT` is a stable retryable protocol code; `TRANSACTION_BUSY` is
retained for compatibility but is no longer produced by independent sessions.

## 9. Tests and benchmark

- `tests/test_mvcc.py`: committed-version visibility, READ COMMITTED refresh,
  rollback invisibility, DDL visibility, index/table agreement, conflicts,
  insert-only merging, snapshot accounting, concurrent pinned readers.
- `tests/test_isolation.py`: repeatable reads and phantoms under `SNAPSHOT`.
- `tests/test_group_commit.py`: in-memory and real v4 batching, one fsync per
  batch, writer admission during the fsync barrier, subprocess crash after a
  group fsync.
- `tests/test_concurrent_server.py`: concurrent server readers, independent
  transactions, `WRITE_CONFLICT` mapping.
- `benchmarks/benchmark_concurrency.py`: serial versus grouped commits and
  reader latency during an open writer.
