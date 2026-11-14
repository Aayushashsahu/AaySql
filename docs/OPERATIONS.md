# Operations Runbook

How to run AayushSQL 1.0.0, read its state, and recover it. Commands assume a
database called `app.aaydb`; substitute your own path.

**Three rules that never change:**

1. Never delete or truncate a `.wal` file to make an error go away. It may hold the
   only copy of a committed transaction.
2. Never open one database file from two owners (two servers, a server and an
   embedded `Engine`, or two engines). The lease prevents it; do not work around it
   by deleting the lock file.
3. Never copy a `.aaydb` file while a server is writing it. Use `aaysql-backup`,
   which takes exclusive ownership first.

---

## 1. Starting a server

```bash
aaysql-server --db app.aaydb --port 8765
# or: python -m aaysql.server --db app.aaydb
```

The server validates configuration, takes the database lock, opens the database
(recovering any pending WAL), binds, and starts accepting. On success it logs
`AayushSQL v1.0.0 listening on 127.0.0.1:8765 (protocol 1, db=app.aaydb, tls=off,
auth=off, max_clients=32)`.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--db` | `aaysql.aaydb` | Database path. |
| `--memory` | off | Run against an in-memory database. |
| `--host` | `127.0.0.1` | Bind address. Non-loopback requires token + TLS. |
| `--port` | `8765` | TCP port. `0` picks a free port. |
| `--max-clients` | 32 | Concurrent connections (semaphore). |
| `--max-frame-size` | 1048576 | Largest accepted frame, 1 KiB–64 MiB. |
| `--idle-timeout` | 300 | Seconds without activity before the connection closes. |
| `--read-timeout` | 60 | Seconds to wait for bytes of a request. |
| `--write-timeout` | 30 | Seconds allowed for sending a response. |
| `--shutdown-timeout` | 10 | Grace period for in-flight work at shutdown. |
| `--tls-cert`, `--tls-key` | none | Enable TLS (both required). |
| `--tls-client-ca` | none | Require and verify client certificates. |
| `--log-level` | INFO | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

Authentication token: **environment only**.

```bash
export AAYUSQL_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"
```

An empty `AAYUSQL_AUTH_TOKEN` is a startup error rather than "no authentication".

## 2. Shutting down

`Ctrl+C` (SIGINT) or SIGTERM requests a graceful shutdown: the accept loop stops,
open connections are closed, an uncommitted transaction is rolled back, in-flight
work gets up to `--shutdown-timeout`, and the engine is closed. Exit status is `0`
when the drain succeeded, `1` when it timed out (check the log for
`shutdown finished without draining in-flight work`).

The database lock is released when the process exits, including on a crash.

## 3. Files on disk

| File | Meaning |
| --- | --- |
| `app.aaydb` | Root manifest (UTF-8 JSON, format 4) describing the committed generation: table segments, index definitions, and `checkpoint_lsn`. |
| `app.aaydb.seg/` | Immutable, content-addressed table segments (`seg_<digest>.seg`). Never edit by hand; unreferenced files are collected at checkpoint. |
| `app.aaydb.wal` | Delta write-ahead log (format-4 records with `changes`). Present only while a commit is durable but not yet checkpointed. An empty WAL is removed on a clean close. |
| `app.aaydb.server.lock` | OS-held lock; content is the owner PID for diagnostics only. Never delete it to "fix" a busy database. |
| `.<db>.manifest.*.tmp`, `.<db>.seg/.*.tmp` | Manifest and segment staging files. Leftovers from a crash are removed on the next successful load or checkpoint. |
| `app.aaydb.pre-restore-<id>` | The database that a `restore --force` replaced, with its segments copied into `<previous>.seg/`. Delete it once you are satisfied with the restore. |
| `app.aaydb.v3.bak` | Temporary copy kept during a v3→v4 migration. Removed after verification; seeing it means an interrupted migration that a later `--migrate` finishes. |

## 4. Logs

Server logs go to stderr, one line per event, always with the session ID:

```
2026-11-11 10:12:03,411 INFO aaysql.server: connection accepted from 127.0.0.1 (session 3f2a…)
2026-11-11 10:12:03,412 INFO aaysql.server: handshake session=3f2a… client=aaysql-python/1.0.0 protocol=1 tls=off
2026-11-11 10:12:03,415 INFO aaysql.server: request=9c1e… session=3f2a… operation=execute rows=4 duration=0.412ms
2026-11-11 10:12:05,001 INFO aaysql.server: request=ab77… session=3f2a… code=SQL_PARSE_ERROR duration=0.080ms
```

What to look for:

- `code=RECOVERY_REQUIRED` or `database service marked unavailable` — see §7.
- `rejecting connection … maximum of N clients reached` — raise `--max-clients` or
  reduce concurrency; clients receive `TOO_MANY_CONNECTIONS`.
- `rolled back the uncommitted transaction of disconnected session …` — normal when
  a client owning a transaction disappears.
- `shutdown finished without draining in-flight work` — a statement outlived the
  shutdown deadline; the engine was not touched, so reopen and inspect.
- `authentication failed … attempts=2/3` — someone is guessing the token.

Tokens are never logged. Neither are tracebacks sent to clients: an unexpected
failure is logged server-side with a traceback and the client receives
`INTERNAL_ERROR: internal server error`.

## 5. WAL recovery

Recovery is automatic on open.

**Format v4 (current).** The root manifest generation is loaded, then committed
delta transactions in the WAL with `commit_lsn > manifest.checkpoint_lsn` are
applied in order. The delta list is validated before any change is applied, so a
structurally invalid transaction is rejected whole. Recovery then publishes a new
manifest generation, appends a `CHECKPOINT` record, and truncates the WAL.

**Format v3 and earlier.** The snapshot is loaded, WAL records newer than its
`wal_lsn` are validated, the newest committed image is installed, and a checkpoint
rewrites the snapshot atomically before truncating the WAL.

What recovery handles without intervention:

- Torn or checksum-corrupt **trailing** record: the tail is dropped and repaired,
  then the committed prefix is checkpointed.
- A crash before `COMMIT`: the transaction is discarded.
- A crash after the `COMMIT` record was fsynced: the transaction is replayed.
- A crash during checkpoint publication: the previous complete manifest and
  generations remain loadable; the WAL still holds the committed delta.
- Repeated recovery: reopening again is a no-op, and replay is idempotent.

What it refuses (and why that is correct):

- A corrupt record **before** later valid data. Loading fails with `StorageError`
  instead of silently skipping forward. Restore from a backup; do not edit the WAL.
- A duplicate `BEGIN`, non-monotonic LSN, mismatched `write_lsn`, or a `CHECKPOINT`
  pointing at an unknown commit. The log is structurally invalid.
- A WAL written by a different database identity (`db_id`), or a delta that
  addresses a missing table or an out-of-range row position. Loading fails closed.
- A missing or modified segment referenced by the manifest. Loading fails; restore
  from a verified backup rather than adding or editing segment files.

## 6. Ambiguous COMMIT

If `fsync` fails after the `COMMIT` bytes were written, the outcome is genuinely
unknown. AayushSQL does not guess:

- The engine raises `AmbiguousCommitError` and marks itself unusable.
- The server answers `RECOVERY_REQUIRED` and marks the whole database unavailable to
  every session — not just the one that committed.
- The WAL is preserved untouched; memory is not treated as authoritative.

**Procedure:** stop the server, restart it (recovery decides from the log), then read
the rows to determine whether the transaction landed. Do not retry the mutation
blindly, and do not delete the WAL.

## 7. `RECOVERY_REQUIRED`

Every statement returns `RECOVERY_REQUIRED` after an ambiguous commit or a poisoned
WAL. This is a deliberate stop, not a crash.

1. Note the error text in the log (it says whether the durability state is
   ambiguous or the WAL is damaged).
2. Stop the server cleanly if possible; otherwise terminate it.
3. Confirm no other process is using the database (the lock is released when the
   owning process exits).
4. Restart the server. Recovery replays or discards the pending transaction.
5. Verify the affected rows.
6. If the restart also fails with `StorageError`, the log or snapshot is damaged:
   restore the most recent backup into a new path, verify it, then point the server
   at it. Keep the damaged files for inspection.

## 8. Backup and restore

Offline only: stop the server first.

```bash
aaysql-backup backup app.aaydb backups/app-2026-11-11
aaysql-backup verify backups/app-2026-11-11
aaysql-backup restore backups/app-2026-11-11 app-restored.aaydb
```

Behavior worth knowing:

- Backup takes exclusive ownership. If any owner exists — server, embedded
  `Engine`, CLI process or another backup — you get
  `database '…' is in use (another engine or server owns it); stop the existing
  owner first`.
- Before reading, backup opens the database once so any pending WAL is applied. The
  backup therefore contains the last durably committed state, not a torn snapshot.
- An existing backup directory is never replaced without `--force`.
- A format-v4 backup records a file inventory: the root manifest plus every
  referenced segment, each with its size and SHA-256. Verification re-hashes the
  inventory and opens a staged copy. A missing or modified segment fails the backup.
- Restore verifies the manifest and the SHA-256 of every inventory file, checks that
  the format is not newer than this release, and opens the candidate copy before
  touching the target.
- Restore refuses an existing target without `--force`, refuses a target owned by a
  server, and refuses a target that still has a non-empty WAL (recover it first).
- A v4 restore installs segments first and replaces the root manifest atomically, so
  the target is always either the previous generation or the restored one.
- With `--force` the previous database is moved to
  `<target>.pre-restore-<uuid>` with its segments copied into `<previous>.seg/`, so
  the old generation stays independently openable.
- The source database and WAL are only ever read.

Suggested routine: back up daily while the server is stopped, verify immediately,
copy the backup directory off the machine, and test a restore into a scratch path
monthly.

## 9. Disk space

- **Format v4.** A commit appends only the rows it changed to the WAL. A checkpoint
  writes a new segment for each table whose content changed and reuses the existing
  segment bytes for unchanged tables, then publishes a small manifest. You need free
  space for the changed table's segment plus the WAL while it is pending, not a full
  copy of the database per commit. Old segments are collected at checkpoint, so the
  steady state is roughly the live data plus at most one pending WAL threshold
  (default 1 MiB).
- **Format v3.** A snapshot is rewritten in full on every commit. You need free space
  of roughly the database size during a commit (temporary file) plus the WAL image,
  which is a full copy of the database per un-checkpointed transaction.
- A long explicit transaction does **not** accumulate WAL records; only `COMMIT`
  writes. But a failed checkpoint leaves the WAL in place until the next open.
- If a commit fails with `StorageError` mentioning the database file or `ENOSPC`,
  free space and retry the statement; the previous state remains durable. If only the
  checkpoint failed, the commit is already in the WAL and recovery applies it.
- A `RECOVERY_REQUIRED` state does not grow the WAL; further statements are rejected
  before touching storage.

## 10. Database ownership and locking

**One owner per file-backed database.** A `.aaydb` file is owned by exactly one of:
an embedded `Engine`, a server, an offline backup, or a restore. Opening a database
can recover the WAL and rewrite the snapshot, so a second owner must never be
allowed to open it concurrently.

Ownership is an OS advisory lock on `app.aaydb.server.lock`, taken with
`fcntl.flock` on POSIX and `msvcrt.locking` on Windows. A process-local registry
additionally makes a second `Engine` in the *same* process fail deterministically.
Paths are canonicalised (relative vs absolute, `..`, symlinked parent directories,
Windows case), so two spellings of the same file are the same database.

Every file-backed entry point acquires this lease before loading the database or
recovering the WAL:

| Entry point | Who owns the lease | Released by |
| --- | --- | --- |
| `Engine("app.aaydb")` | the engine | `engine.close()` or process exit |
| `aaysql-server` / `AayushSQLServer` | the server | `server.shutdown()` or process exit |
| `aaysql-backup backup` | the backup operation | end of the command, success or failure |
| `aaysql-backup restore` | the restore operation | end of the command, success or failure |

The server hands its lease to the engine it creates (the engine borrows it) rather
than locking twice. A server constructed with an injected persistent `Engine`
borrows that engine's lease and never acquires a second one.

The canonical ownership lock is always `<database>.server.lock`; it is not
configurable. `ServerConfig.lock_path` is kept for compatibility from v0.8.0, but
it is **supplemental**: the server acquires the canonical lock first and the
custom lock in addition, never instead. Both are held for the server's lifetime and
released in reverse order after in-flight work is drained and the created engine is
closed. This means a server started with a custom `lock_path` still blocks — and is
still blocked by — an embedded `Engine`, a backup, a restore, or another server on
the same database. A custom `lock_path` equal to the canonical path is a no-op, and
configuring one on an in-memory server (`db_path` unset) is rejected with
`ServerConfigError` rather than silently ignored.

A conflict raises `DatabaseLockedError` with the stable code `DATABASE_LOCKED`:

```text
DATABASE_LOCKED
database 'app.aaydb' is already in use by another engine or process
(lock file /srv/app.aaydb.server.lock); stop the existing owner before opening
this database
```

The lock is not a PID-file heuristic: the OS drops it when the owning process dies
(even `SIGKILL`), so a crashed owner never blocks the next start. The lock file may
remain on disk after a clean shutdown, and it may contain a stale PID — that text
is diagnostic only. **A lock file's existence does not mean the database is in use,
and deleting it does not release the lock.**

To recover from `DATABASE_LOCKED`:

1. Find the owner (`ps`, `lsof app.aaydb.server.lock`, or the PID in the lock file)
   and stop it cleanly.
2. Confirm the process is gone. Do not delete the lock file.
3. Reopen the database; the OS lock is already free and recovery runs normally.

## 11. Single-writer limitations

- Exactly one server process per database, and exactly one active transaction across
  all its sessions.
- Other sessions get `TRANSACTION_BUSY` (retryable) while another session holds a
  transaction. Long transactions block reads from other sessions too.
- No MVCC, no snapshot isolation, no parallel query execution.
- Every statement is serialized through one lock, so throughput does not grow with
  client count.

## 12. Troubleshooting

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `DATABASE_LOCKED` / "already in use" | A server, embedded `Engine`, backup or restore owns the database. | Stop the owner (`§10`). Do not delete the lock file. |
| `RECOVERY_REQUIRED` | Ambiguous commit or damaged WAL. | §6 and §7. |
| `StorageError: Corrupted database file …` | Snapshot is not valid JSON or fails validation. | Restore a backup; keep the file for diagnosis. |
| `StorageError: Corrupted WAL record …` | Mid-log corruption. | Restore a backup; do not truncate the WAL manually. |
| `TOO_MANY_CONNECTIONS` | `--max-clients` reached. | Close idle clients or raise the limit. |
| `TRANSACTION_BUSY` | Another session owns the transaction. | Retry after it commits/rolls back. |
| `CONNECTION_TIMEOUT` | Client idle or stalled mid-frame. | Reconnect; check client timeouts. |
| Client raises `ConnectionLostError` | Server closed the connection. | The outcome is unknown — reconnect and inspect before retrying. |
| Every `CREATE TABLE` says "already exists" | You opened a newer/other database file. | Check `--db` resolves to the file you expect. |
| `Cannot compare BOOL with INT` | Mixed-type predicate such as `flag = 1`. | Use `flag = TRUE`. |

Reading the database with a text editor is safe only for inspection. Editing it by
hand is unsupported: validation may reject the file, and a valid-looking but wrong
edit will be trusted.
