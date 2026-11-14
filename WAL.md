# AayushSQL Write-Ahead Log (WAL) & Crash Recovery Design (v0.6.1, format v4 in §12)

This document specifies the Write-Ahead Log (WAL) protocol, durability semantics, checkpoint ordering, format v3 migration, and recovery guarantees implemented in AayushSQL `v0.6.1` (`src/aaysql/wal.py`, `src/aaysql/storage.py`, `src/aaysql/engine.py`). Section 11 describes the v0.6.1 hardening of tail repair, partial appends, and directory fsync error handling.

---

## 1. WAL Architecture

For any persistent database at `<db_path>` (for example, `university.aaydb`), AayushSQL places an adjacent Write-Ahead Log at:

```text
<db_path>.wal
```

In-memory databases (`Engine()` / `Engine(":memory:")`) do not create or touch WAL files.

Every persistent mutation path passes through `JsonFileStorage.save()` and `WalManager.append_commit_transaction()`:

- **Explicit transactions** (`AAYUSH BEGIN` ... `AAYUSH COMMIT`)
- **Single-statement autocommit mutations** (`INSERT`, `UPDATE`, `DELETE`, `CREATE TABLE`, `DROP TABLE`, `CREATE INDEX`, `DROP INDEX`)

Read-only `SELECT` and `EXPLAIN` queries never write to the WAL or the `.aaydb` snapshot.

---

## 2. WAL Record Schema

The WAL uses append-only, newline-delimited UTF-8 JSON (one complete JSON object terminated by `\n` per record). Non-finite JSON constants (`NaN`, `Infinity`, `-Infinity`) are rejected.

### Common Fields

| Field | Type | Description |
|---|---|---|
| `lsn` | `int` (`> 0`) | Strictly monotonically increasing Log Sequence Number |
| `txn` | `str` | Unique transaction identifier (`txn-<uuid4-hex>`) |
| `type` | `str` | One of `"begin"`, `"write"`, `"commit"`, `"checkpoint"` |
| `checksum` | `str` | 64-char lowercase SHA-256 hex digest |

### Record Types

1. **`begin`**
   ```json
   {"checksum":"...","lsn":1,"txn":"txn-...","type":"begin"}
   ```
   Marks the start of a transaction. A `txn` identifier may begin at most once.

2. **`write`**
   ```json
   {"checksum":"...","image":{...},"lsn":2,"txn":"txn-...","type":"write"}
   ```
   Contains a complete serialized format-v3 database image (`format`, `format_version`, `engine_version`, `wal_lsn`, `tables`, `indexes`) representing the transaction's intended committed state.

3. **`commit`**
   ```json
   {"checksum":"...","lsn":3,"txn":"txn-...","type":"commit","write_lsn":2}
   ```
   References the transaction's `write` record via `write_lsn` and establishes transaction commit once durably flushed and `os.fsync()`ed.

4. **`checkpoint`**
   ```json
   {"checksum":"...","commit_lsn":3,"lsn":4,"txn":"txn-...","type":"checkpoint"}
   ```
   Records that the committed transaction at `commit_lsn` has been atomically persisted to the main `.aaydb` snapshot.

### Canonical Checksum

Checksums are computed with SHA-256 over the canonical JSON encoding of the record excluding `"checksum"`:

```python
json.dumps(
    payload_without_checksum,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
).encode("utf-8")
```

Verification uses constant-time digest comparison (`hmac.compare_digest`).

---

## 3. LSN Assignment & Transaction Association

- On database load, `WalManager.last_lsn` initializes to `max(snapshot.wal_lsn, wal.max_lsn)`.
- Each committed transaction receives strictly ascending consecutive LSNs: `begin_lsn`, `write_lsn`, `commit_lsn`.
- Each transaction uses a fresh 128-bit UUID4 `txn` identifier (`txn-<hex>`), preventing accidental cross-transaction association across crashes or reopens.
- During recovery parsing (`parse_wal`):
  - LSNs must strictly increase (`lsn > prev_lsn`).
  - `BEGIN` rejects duplicate `txn` IDs.
  - `WRITE` requires an active `BEGIN` for the exact `txn` ID.
  - `COMMIT` requires a prior `WRITE` for the exact `txn` ID and an exact match on `write_lsn`.
  - `CHECKPOINT` requires `commit_lsn` to correspond to a committed transaction in the WAL or `<= snapshot.wal_lsn`.

---

## 4. Commit Durability Semantics

A commit executes in two distinct phases: WAL commit, followed by checkpointing.

1. Construct the complete committed database image (with `wal_lsn = commit_lsn`).
2. Append newline-terminated `begin`, `write`, and `commit` records to `<db_path>.wal` and flush.
3. Call `os.fsync()` on the WAL file descriptor (and `fsync_dir` on newly created WAL files where supported by the OS).
4. **Only after `os.fsync()` returns successfully** is the transaction considered durably committed.

### Commit Error Cases

1. **Failure before `COMMIT` fsync (e.g., serialization or write error before `COMMIT`):**
   - `StorageError` is raised.
   - `Engine` restores the pre-transaction / pre-statement snapshot in memory.
2. **Checkpoint failure *after* `COMMIT` fsync succeeds:**
   - The transaction is **already durably committed in the WAL**.
   - `Engine` preserves the committed in-memory state, keeps the WAL intact (does **not** truncate it), marks `pending_checkpoint = True`, and reports commit success rather than returning a misleading commit failure.
   - Recovery completes the checkpoint on next reopen (or on a later successful checkpoint).
3. **`os.fsync()` on the `COMMIT` record itself fails:**
   - Durability is **ambiguous** (the OS may or may not have persisted the flushed `COMMIT` bytes).
   - `AmbiguousCommitError` (a subclass of `StorageError`) is raised.
   - `Engine.needs_recovery` becomes `True`, the WAL is preserved untouched, and further operations in that engine instance are blocked until the database is reopened so recovery can inspect the WAL.
4. **Directory-entry `fsync()` of a newly created WAL fails:**
   - The `COMMIT` bytes are fsynced, but the file's directory entry may not be durable, so the outcome is not a definite rollback.
   - `AmbiguousCommitError` is raised, the WAL is poisoned, and reopening decides the outcome from the actual record.

---

## 5. Crash-Safe Checkpoint Ordering

Checkpointing never removes the WAL before the main `.aaydb` snapshot is durably replaced:

1. Confirm the `COMMIT` record is durable in the WAL (`commit_lsn`).
2. Build the `.aaydb` payload with `format_version = 3` and `wal_lsn = commit_lsn`.
3. Write the payload to a hidden temporary file in the database directory.
4. `flush()` and `os.fsync()` the temporary file.
5. Atomically replace `<db_path>` via `os.replace(tmp_path, db_path)`.
6. `fsync_dir(parent)` where supported.
7. Append and `os.fsync()` a `CHECKPOINT` record in `<db_path>.wal`.
8. Truncate `<db_path>.wal` to `0` bytes, `flush()`, and `os.fsync()` it.
9. `fsync_dir(parent)` where supported, and remove any stale checkpoint `.tmp` files.

On clean `Engine.close()`, if no checkpoint is pending and the WAL is `0` bytes, the empty WAL file is unlinked cleanly.

---

## 6. Recovery Behavior

Recovery runs automatically inside `JsonFileStorage.load()` when `Engine(db_path)` opens a database, before any SQL queries are permitted:

1. Read and validate `<db_path>` if it exists (defaulting to an empty `Database` with `wal_lsn = 0` if missing).
2. If `<db_path>.wal` does not exist, return the loaded database without writing to disk.
3. Parse and validate `<db_path>.wal` via `parse_wal()`.
4. Reconstruct committed transactions (those with valid `BEGIN` -> `WRITE` -> `COMMIT` records). Discard incomplete transactions lacking a valid `COMMIT`.
5. Filter committed transactions to those with `commit_lsn > base_db.wal_lsn`.
6. If applicable committed transactions exist:
   - Select the newest valid committed image (`Database.from_dict(latest.image)`, rebuilding and validating all B+ tree indexes).
   - Run the full atomic checkpoint pipeline (`_checkpoint`) to persist the recovered snapshot with `wal_lsn = latest.commit_lsn`, append `CHECKPOINT`, and truncate the WAL.
7. Otherwise (only uncommitted transactions or already-checkpointed records `<= base_db.wal_lsn`), truncate any leftover WAL bytes and clean stale `.tmp` files without rewriting `.aaydb`.

Before step 6 appends its recovery `CHECKPOINT`, recovery repairs any invalid trailing bytes: `parse_wal()` reports `valid_bytes_end` (the exact UTF-8 byte offset after the last validated newline-terminated record), recovery truncates only bytes after it, flushes and `fsync()`s the WAL, keeps every earlier valid record and its LSN, and only then appends the `CHECKPOINT`. This guarantees that a repaired torn or checksum-corrupt tail can never end up in the middle of the log if the process crashes before truncation.

Recovery is **idempotent**: running recovery twice or reopening after recovery yields the identical database state and prevents duplicate replay via `commit_lsn > snapshot.wal_lsn`.

---

## 7. Incomplete vs. Corrupt Records

- **Trailing incomplete or checksum-corrupt record** (e.g., partial line missing `\n`, invalid UTF-8/JSON on the final line, or checksum mismatch on the final line): safely ignored as a crash-torn tail write. All earlier valid committed transactions remain recoverable.
- **Corrupt record before subsequent bytes/records** (non-trailing corruption): recovery **stops immediately at the first invalid record**, refuses to skip forward to later-looking records, and raises `StorageError` so mid-log corruption is never silently masked.
- **Protocol violations on checksum-valid records** (non-monotonic LSNs, duplicate `BEGIN`, mismatched `txn` IDs, `COMMIT` without matching `WRITE`): rejected immediately with `StorageError`.

---

## 8. `.aaydb` Format v3 and Legacy Migration

Format version `3` adds top-level field `"wal_lsn"` (`int >= 0`), tracking the highest committed WAL LSN applied to the snapshot.

- **Format v1** (no `indexes`, no `wal_lsn`) and **Format v2** (has `indexes`, no `wal_lsn`) remain readable and are treated as having `wal_lsn = 0`.
- Opening a legacy v1 or v2 file without a WAL (e.g., for `SELECT`) **never modifies or overwrites** the file on disk.
- Migration to format v3 occurs safely on the next write (or recovery checkpoint) while preserving all tables, rows, column constraints, defaults, and index definitions.
- Format v3 snapshots lacking a valid non-negative integer `wal_lsn` are rejected with `StorageError`.

---

## 9. Durability Guarantees & Platform Limitations

### What is verified and guaranteed
- **Process crash recovery:** Verified across real child subprocesses killed with `os._exit()` (bypassing Python cleanup, `finally`, `with` blocks, and stdio flushes) at every critical stage: before `COMMIT`, partial `COMMIT`, after `COMMIT` `os.fsync()`, during temporary snapshot write, after snapshot `fsync` before `os.replace()`, after `os.replace()`, after `CHECKPOINT` before truncation, and during WAL truncation.
- Uncommitted transactions and torn trailing WAL writes never become visible after recovery.
- Committed transactions whose `COMMIT` `os.fsync()` returned survive all tested process crashes without loss or duplicate replay.

### Platform & Hardware Limitations
- **Directory entry `fsync`:** POSIX systems (Linux, macOS) support `os.open(dir, O_RDONLY)` + `os.fsync(fd)` to flush directory entries when creating `.wal` or atomically replacing `.aaydb`. On Windows, opening a directory via `os.open()` is unsupported, so `fsync_dir()` deliberately skips directory fsync; directory-entry durability there relies on NTFS metadata behavior. This fallback is chosen by platform, not by swallowing errors: unsupported operations reported as `EINVAL`/`ENOTSUP`/`EOPNOTSUPP` also fall back, while `EACCES`, `EIO`, `ENOSPC`, and other genuine errors raise `StorageError`.
- **Scope boundary (process crashes vs. hardware/power loss):** Real subprocess `os._exit()` tests prove survival against abrupt process termination when OS page-cache/file `os.fsync()` semantics behave as documented by the host OS. AayushSQL cannot independently guarantee survival across storage-device firmware write-cache lying, unbuffered hardware/power-loss corruption of in-place file sectors, or filesystem bugs.
- **Single-process writer:** Multi-process concurrent writers against the same `.aaydb` / `.wal` pair are not supported.

---

## 10. v0.6.1 Recovery Hardening

### 10.1 Trailing-tail repair and byte-boundary tracking

`parse_wal()` returns `WalAnalysis.valid_bytes_end`, the exact byte offset immediately after the last fully validated newline-terminated record. The offset is computed over UTF-8 bytes, not characters, so multi-byte text in a `WRITE` image cannot shift it.

When recovery encounters a torn or checksum-corrupt *trailing* record:

1. Every preceding valid record is preserved, including committed transactions awaiting checkpoint.
2. `WalManager.repair_trailing(valid_bytes_end)` truncates only the invalid bytes, then flushes and `fsync()`s the WAL file and its directory.
3. Recovery only then appends its `CHECKPOINT`; the next LSN is greater than every retained valid LSN.
4. No record is ever appended behind an invalid record and no invalid record is skipped in favour of later data.

Without this repair, recovery could append `CHECKPOINT` behind a corrupt tail and crash before truncation, turning a recoverable trailing fault into mid-log corruption (`StorageError`) on the next open. A regression test reproduces the complete seven-step sequence and fails when the repair is disabled.

### 10.2 Append state machine, short writes, and WAL poisoning

Appends use `os.open(..., O_APPEND)` and an `os.write()` write-all loop. A short write is completed rather than mistaken for a finished record. The manager exposes its last transition:

| State | Meaning |
|---|---|
| `idle` | Last append completed durably |
| `begin_appended` | BEGIN bytes may be on disk; WRITE/COMMIT not durable |
| `write_appended` | BEGIN + WRITE may be on disk; COMMIT not durable |
| `commit_attempted` | COMMIT bytes are being written / fsynced |
| `commit_durable` | COMMIT `fsync()` succeeded |
| `checkpoint_durable` | CHECKPOINT `fsync()` succeeded |
| `damaged` | An append failed before COMMIT durability; the log may contain torn bytes |
| `ambiguous` | COMMIT durability could not be confirmed |

Any `damaged` or `ambiguous` transition poisons the manager (`poisoned = True`): further appends raise `StorageError`, and `Engine` refuses every statement with a reopen-recovery error until a new process opens the database. LSNs advance only after durable success. A new process always parses, validates, and repairs the tail before appending.

### 10.3 Ambiguous commit outcomes

- **Failure before the COMMIT record is durable:** the transaction is not committed. In-memory state is restored to the pre-transaction/pre-statement snapshot, the WAL is preserved, and the engine requires reopen recovery because the log may end in a torn record.
- **COMMIT bytes written but `fsync()` (or the new WAL's directory fsync) failed:** `AmbiguousCommitError` is raised. The engine never claims a definite commit or rollback, keeps its in-memory state, preserves the WAL, and blocks all further work. Startup recovery decides the outcome from whether a complete, checksum-valid COMMIT exists.
- **COMMIT `fsync()` succeeded:** the transaction is durably committed. A later checkpoint failure is not reported as a failed commit; the WAL is retained so recovery can finish, and the committed state is never rolled back.

### 10.4 Directory fsync classification

`fsync_dir()` now distinguishes three cases:

| Case | Behaviour |
|---|---|
| POSIX with directory fsync support | `os.open(O_RDONLY)` + `os.fsync(fd)` |
| Unsupported platform or operation (`Windows`, `EINVAL`, `ENOTSUP`, `EOPNOTSUPP`) | Documented fallback; no error |
| Genuine filesystem failure (`EACCES`, `EIO`, `ENOSPC`, ...) | `StorageError` with path and errno |

Descriptors are closed on every path. A failure after `os.replace()` leaves the WAL untouched, keeps the committed transaction, and surfaces a storage error rather than silently claiming durability.

### 10.5 Coverage and durability scope

v0.6.1 adds `tests/test_wal_hardening.py` (25 tests): byte-boundary parsing, tail repair, the seven-step regression, crash after tail repair, repeated recovery, mid-log corruption, directory fsync classification with mocked `EINVAL`/`EACCES`/`EIO`/`ENOSPC`, post-`os.replace()` failure, short writes, partial-append poisoning, ambiguous COMMIT, poisoned-manager refusal, checkpoint append failure, plus five combined real-subprocess `os._exit()` scenarios (corrupt tail, directory fsync `EIO`, partial COMMIT, multiple commits plus corrupt tail at the checkpoint boundary, and mid-log corruption).

These tests prove recovery from abrupt process termination. They do not claim survival of storage-device firmware write-cache lies, arbitrary power loss that corrupts in-place sectors, or filesystem bugs; see Section 9.

## 11. Running WAL & Crash-Recovery Tests

Run all WAL, migration, error-semantics, hardening, and real-subprocess crash tests:

```bash
python -m pytest -v tests/test_wal_recovery.py tests/test_wal_hardening.py
```

Run the entire test suite (including all regression tests):

```bash
python -m pytest -v
```

## 12. Format v4 Delta WAL

Format v4 replaces the whole-image `WRITE` record with a list of changes. Framing,
checksums, LSN assignment, append hardening, and the `COMMIT`-fsync durability rule
are unchanged from Sections 2–5; only the record payload and checkpoint target differ.

### 12.1 Records

A v4 transaction is still `BEGIN`, one `WRITE`, `COMMIT`, all tagged with `"v":4`
and the database identity `"db": "<32 hex>"`:

```json
{"checksum":"...","changes":[{"t":"insert","table":"t","rows":[{"id":1}]}],"db":"…","lsn":2,"txn":"txn-…","type":"write","v":4}
```

`changes` is an ordered list applied exactly once, in order:

| `t` | Payload |
| --- | --- |
| `insert` | `table`, `rows` (full post-image row objects, appended in order) |
| `update` | `table`, `index` (current row position), `row` (full post-image) |
| `delete` | `table`, `index` (current row position) |
| `create_table` | `table`, `columns` |
| `drop_table` | `table` |
| `create_index` / `drop_index` | `index`, `table`, `column` for create |

Updates and deletes address rows positionally, so replay depends on applying every
prior change in the same order. Deletes are recorded in descending index order so a
single statement's positions remain valid as rows are removed. A `CHECKPOINT` record
then carries `commit_lsn`; it is appended only after the new manifest generation is
durable and immediately before the WAL is truncated.

### 12.2 Recovery

1. Load the root manifest generation (segments are verified by length and SHA-256).
2. Parse the WAL in full before applying anything: torn tail is dropped, mid-log
   corruption fails closed, foreign `db_id` is rejected, and change payloads are
   validated (`validate_change`).
3. Apply committed transactions with `commit_lsn > manifest.checkpoint_lsn` in LSN
   order. Transactions at or below the checkpoint are skipped, which makes replay
   idempotent after a crash between manifest publication and WAL truncation.
4. Publish a new manifest generation, append `CHECKPOINT`, truncate the WAL, and
   collect unreferenced segments.

### 12.3 Crash guarantees

- Crash before `COMMIT` fsync: the transaction is discarded, the manifest and
  segments are untouched.
- Crash after `COMMIT` fsync, before or during checkpoint: the delta is replayed on
  the next open. Segment or manifest publication failing never rolls back the commit.
- Crash after segment rename or manifest replace: the previous or new complete
  generation loads; content-addressed segments are never overwritten with different
  bytes, and temp files are removed on the next load.
- Crash during WAL truncation: the remaining committed prefix is either skipped as
  already checkpointed or replayed; rows are never duplicated.
- Recovery itself crashing after tail repair: the repaired WAL still contains the
  committed delta, so the next open applies it once.

Coverage lives in `tests/test_delta_v4.py`, `tests/test_checkpoints_v4.py`,
`tests/test_migration_v4.py`, `tests/test_backup_v4.py`, and
`tests/test_crash_v4.py`.
