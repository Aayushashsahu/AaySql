# AayushSQL Write-Ahead Log (WAL) & Crash Recovery Design (v0.6.0)

This document specifies the Write-Ahead Log (WAL) protocol, durability semantics, checkpoint ordering, format v3 migration, and recovery guarantees implemented in AayushSQL `v0.6.0` (`src/aaysql/wal.py`, `src/aaysql/storage.py`, `src/aaysql/engine.py`).

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
- **Directory entry `fsync`:** POSIX systems (Linux, macOS) support `os.open(dir, O_RDONLY)` + `os.fsync(fd)` to flush directory entries when creating `.wal` or atomically replacing `.aaydb`. On Windows, opening a directory via `os.open()` is unsupported (`PermissionError`), so `fsync_dir()` degrades cleanly without failing commits; therefore directory-entry durability on Windows relies on OS/NTFS metadata behavior.
- **Scope boundary (process crashes vs. hardware/power loss):** Real subprocess `os._exit()` tests prove survival against abrupt process termination when OS page-cache/file `os.fsync()` semantics behave as documented by the host OS. AayushSQL cannot independently guarantee survival across storage-device firmware write-cache lying, unbuffered hardware/power-loss corruption of in-place file sectors, or filesystem bugs.
- **Single-process writer:** Multi-process concurrent writers against the same `.aaydb` / `.wal` pair are not supported.

---

## 10. Running WAL & Crash-Recovery Tests

Run all WAL, migration, error-semantics, and real-subprocess crash tests:

```bash
python -m pytest -v tests/test_wal_recovery.py
```

Run the entire test suite (including all regression tests):

```bash
python -m pytest -v
```
