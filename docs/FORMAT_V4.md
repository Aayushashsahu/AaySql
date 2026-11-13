# AayushSQL database format v4

**Status:** implemented on `feat/delta-wal-segmented-checkpoints`, package version
`1.0.0` (no release or tag change). Format v3 remains readable and v3 databases
keep working unchanged until they are explicitly migrated.

This document is the normative description of how format v4 stores data, commits
transactions, checkpoints, recovers and migrates. It is written to be read
alongside `WAL.md` (v3 WAL protocol, still used for v3 files) and
`docs/OPERATIONS.md`.

---

## 1. Why the format changed

Format v3 appends a **complete database image** to the WAL and rewrites the whole
snapshot on every commit. A one-row insert into a 5,000-row database writes the
whole database twice. Format v4 writes:

- a **delta WAL record** containing only the rows and schema objects changed by
  the transaction, and
- **immutable per-table segments** for the tables that changed, referenced by a
  small root manifest.

Commit cost is therefore proportional to the transaction, not the database. The
`Database.clone()` snapshot used for statement atomicity and rollback is
deliberately **unchanged** in this milestone: statement/transaction rollback is
still O(database). That cost is a separate milestone (undo logging).

## 2. File layout

The path the user opens is still the database path. It now holds the root
manifest instead of the full snapshot:

```text
app.aaydb                  root manifest (JSON, authoritative pointer)
app.aaydb.wal              delta WAL (v4 records; may be absent or empty)
app.aaydb.seg/             immutable content-addressed segment files
  seg_<24 hex>.seg
app.aaydb.server.lock      canonical ownership lock (Phase 1, unchanged)
```

`app.aaydb.seg/` is a directory next to the database file. It is created at the
first checkpoint. Segment files are never modified in place; a new generation is
a new file name.

The manifest is a single small JSON document, which means the existing
single-path deployment, lock placement (`<db>.server.lock`), backup manifest
layout, CLI argument and `DATABASE_LOCKED` behaviour all keep working without a
new user-facing path. The earlier research sketch used a separate
`<db>.aaydb4/` directory; that would have changed the path users open, the lock
name and every CLI invocation, so it was not adopted. The requirement that the
root manifest be the authoritative pointer is satisfied by making the existing
database path *be* the manifest.

### 2.1 Identity of a v4 database

A v4 database is identified by `db_id`, a random 128-bit hex string created when
the database is created (or migrated) and recorded in the manifest and in every
WAL record. A WAL whose `db` field does not match the manifest is rejected: it
belongs to a different database that happened to reuse the path.

## 3. Root manifest schema

```json
{
  "format": "aaysql",
  "format_version": 4,
  "engine_version": "1.0.0",
  "db_id": "9f2c...e1",
  "generation": 7,
  "checkpoint_lsn": 42,
  "tables": {
    "users": {
      "segment": "seg_3a91c07f5d2e4b6a8c1d0e2f.seg",
      "generation": 3,
      "length": 2048,
      "sha256": "…64 hex…"
    }
  },
  "indexes": {
    "users_email": {"table": "users", "column": "email"}
  }
}
```

- `format_version` is `4`. Readers of other versions fail with a typed error;
  a version newer than the running engine is reported as unsupported (a v1.0.0
  binary therefore refuses a v4 database instead of misreading it).
- `generation` is a monotonically increasing checkpoint counter.
- `checkpoint_lsn` is the WAL LSN of the last transaction included in the
  referenced segments. Every committed transaction with `commit_lsn <=
  checkpoint_lsn` is fully represented by the segments.
- `tables` maps table name to its current immutable segment and its integrity
  metadata. `generation` is per table and increases when that table is rewritten.
- `indexes` stores index definitions only. Index trees are rebuilt from rows on
  load; no tree structure is persisted.

The manifest is untrusted input: table names are data, not paths, and segment
names must match `^seg_[0-9a-f]{24}\.seg$`. Anything containing a path separator
or `..` is rejected before the filesystem is touched.

## 4. Segment format

A segment is a single canonical JSON document:

```json
{
  "format": "aaysql-segment",
  "format_version": 4,
  "table": "users",
  "columns": [{"name": "id", "type_name": "INT", "primary_key": true, …}],
  "rows": [[1, "a@example.com"], [2, "b@example.com"]]
}
```

Rows are arrays in column order (compact and deterministic). Serialization uses
sorted keys, no insignificant whitespace and no NaN/Infinity. The `sha256` in the
manifest is the SHA-256 of the exact bytes on disk, and the file name embeds the
first 24 hex characters of that digest (`seg_<hash prefix>.seg`). Consequences:

- segment names are **content-addressed**, so the same table state always has the
  same file name;
- an unchanged table is therefore reused by construction — a checkpoint that
  encounters an existing file with the same digest returns that reference without
  writing a new file, so the other table's bytes are never rewritten;
- a name collision with different bytes is a corruption, not a silent overwrite;
- GC can delete any segment file the active manifest does not reference.

Segments store **logical** data only: rows, column definitions, index definitions
(in the manifest). B+ tree pages, object pointers and statistics are never
persisted; they are rebuilt from rows.

## 5. Delta WAL

The WAL keeps the v3 framing and durability machinery: newline-delimited canonical
JSON records, SHA-256 per record, strictly increasing LSNs, an explicit
`BEGIN`/`WRITE`/`COMMIT` transaction sequence with a commit marker, `CHECKPOINT`
records, single-writer append state machine, poisoned-WAL handling, ambiguous
commit classification and torn-tail repair. Only the `WRITE` payload changes.

Every v4 record carries `"v": 4` and `"db": "<db_id>"`.

| Record | Fields |
|---|---|
| `begin` | `lsn`, `txn`, `type`, `v`, `db`, `checksum` |
| `write` | `lsn`, `txn`, `type`, `v`, `db`, `changes`, `checksum` |
| `commit` | `lsn`, `txn`, `type`, `v`, `db`, `write_lsn`, `checksum` |
| `checkpoint` | `lsn`, `txn`, `type`, `v`, `db`, `commit_lsn`, `checksum` |

`changes` is an ordered list of storage-level mutations produced by the
transaction:

```json
{"t": "insert",       "table": "users", "rows": [{"id": 3, "email": "c@x"}]}
{"t": "update",       "table": "users", "index": 1, "row": {"id": 2, "email": "b@y"}}
{"t": "delete",       "table": "users", "index": 0}
{"t": "create_table", "table": "logs",  "columns": [ … ]}
{"t": "drop_table",   "table": "logs"}
{"t": "create_index", "index": "users_email", "table": "users", "column": "email"}
{"t": "drop_index",   "index": "users_email"}
```

`update` and `delete` address a row by its **position in the table's row list at
the moment the operation ran**, and operations are replayed in the order they
were recorded. That reproduces row order, duplicate rows in tables without a
primary key, and the exact list semantics of `INSERT`/`UPDATE`/`DELETE`, without
inventing a visible row id or a hidden column. `delete` operations from one
statement are recorded in descending position order so replay removes exactly the
same rows. `update` carries the complete post-update row (row-level granularity,
not byte-level); `insert` carries the inserted rows.

No full-database comparison is used to build a delta: mutations are recorded at
the storage boundary as they happen (see `delta.py`), so a transaction touching
one row of one table never serializes another table, and never serializes the
database.

### 5.1 Transaction framing and the commit point

One transaction is one `BEGIN`, one `WRITE` (all changes), one `COMMIT`. A
multi-statement transaction produces exactly one `WRITE`. The WAL `fsync` of the
`COMMIT` record is the **logical commit point**:

- failure before the `COMMIT` bytes are flushed: the transaction is rolled back
  in memory and the WAL is poisoned (it may contain a partial transaction);
- failure while flushing the `COMMIT` record: `AmbiguousCommitError`; the outcome
  is decided by recovery, never by a guess, and the transaction is never retried
  automatically;
- success of the `COMMIT` `fsync` followed by a checkpoint failure: **the
  transaction is committed**. The failure is recorded as a pending checkpoint,
  the WAL is left intact, and the next open checkpoints it. It is never reported
  as a rollback.

A transaction with no mutations appends nothing. An autocommit mutation is its
own one-statement transaction.

## 6. Checkpoint protocol

Triggers:

1. WAL size threshold — default 1 MiB of WAL bytes (`Engine` / `SegmentedStorage`
   constructor parameter `checkpoint_bytes`), tested deterministically;
2. an explicit `Engine.checkpoint()` / `storage.checkpoint(db)`;
3. clean `Engine.close()` (an explicit checkpoint of dirty tables);
4. recovery, after replaying committed transactions.

There is no background thread and no group commit in this phase.

A checkpoint is:

1. `checkpoint_lsn = db.wal_lsn` (the last committed LSN applied in memory);
2. for each table, serialize a segment payload and compute its digest. If a file
   named `seg_<digest prefix>.seg` already exists with exactly those bytes, reuse
   it without writing; otherwise write the payload to a fsynced temporary file,
   `os.replace` it to the content-addressed name, and fsync the segment directory;
3. tables whose bytes are unchanged keep their existing manifest entry — their
   files are not rewritten or renamed;
4. build the new manifest (generation + 1, new `checkpoint_lsn`, table map,
   index definitions) and write it to a temporary file in the database's
   directory, fsync it;
5. `os.replace(temp_manifest, db_path)` — the single atomic publication point —
   then fsync the parent directory where the platform supports it;
6. only now advance WAL checkpoint state: append a `CHECKPOINT` record carrying
   `commit_lsn`, then truncate the WAL and fsync it;
7. only now garbage-collect segment files that the new manifest does not
   reference, plus abandoned `*.tmp*` files.

Until step 5 the previous manifest and every segment it references are untouched,
so a crash at any earlier point leaves the previous generation complete and
authoritative. After step 5 the new generation is complete on disk by
construction (segments were fsynced before the manifest referenced them). The
old generation's files may still exist; they are simply unreferenced and are
removed by GC, which is therefore never required for correctness.

On Windows, `os.replace` over an existing file is atomic (rename over an open
handle is refused, which surfaces as an error rather than a torn file). Directory
fsync is skipped on Windows by design (`wal.fsync_dir`), so directory-entry
durability relies on NTFS metadata behaviour; this is the same documented limit as
v3. A checkpoint that fails at any step is reported through
`pending_checkpoint`/`last_checkpoint_error` and retried on the next checkpoint
or open; committed transactions remain in the WAL.

### 6.1 WAL rotation

v4 uses a single WAL file, truncated (not renamed) after a durable manifest
publication. A crash between publication and truncation leaves records whose
`commit_lsn <= checkpoint_lsn`; recovery ignores exactly those, so replay is
idempotent. Rotation is therefore "publish, then truncate"; there is no window in
which uncheckpointed committed changes can be discarded.

### 6.2 Garbage collection rules

- never delete a segment referenced by the active manifest;
- run only after the new manifest is durable, so the previous generation is
  already unreachable by any recoverable state;
- best-effort: a failure is logged through the return value and never fails the
  checkpoint, because correctness does not depend on it;
- safe to retry, and safe to run concurrently with nothing else (the database is
  exclusively owned);
- v4 therefore has bounded segment growth: at most one file per distinct table
  state per checkpoint, and orphans are removed on the next checkpoint.

## 7. Recovery state machine

`Engine(path)` on a v4 database:

1. refuse if the path is a directory or unreadable;
2. read and validate the root manifest; load every referenced segment, verifying
   name, existence, length and SHA-256, and rebuild index trees from rows;
3. a missing manifest becomes a brand-new database with a fresh `db_id`; a WAL
   that does not carry that identity is rejected rather than adopted;
4. parse the WAL: version/identity, exact field set, checksum, strictly
   increasing LSNs, `BEGIN`→`WRITE`→`COMMIT` matching, `CHECKPOINT` references;
5. a malformed record **before** the end of the log is a hard `StorageError`
   (fail closed, never skip forward); a malformed or short final record is a torn
   tail: repair only those trailing bytes, then continue;
6. select committed transactions with `commit_lsn > checkpoint_lsn`;
7. apply their changes in LSN order, validating each change structurally and
   its row values against the schema; rebuild the affected index trees;
8. validate the resulting tables (types, primary key/unique duplicates);
9. checkpoint (rewrite only the affected segments), publish, truncate the WAL;
   a checkpoint failure aborts the open with the WAL and the previous manifest
   preserved, so the next open retries the same state — no committed transaction
   is ever dropped;
10. reopening produces the same in-memory database: recovery is idempotent.

An ambiguous commit (fsync unknown) is decided by this procedure: if the
`COMMIT` record is present and valid, the transaction happened; if it is absent
or torn, it did not.

## 8. Migration from v3

v3 databases are **not** rewritten on open. A v3 file is opened and written as v3
by the v3 code path; new databases are created as v4. Migration is explicit:

```python
from aaysql.storage import migrate_to_v4
migrate_to_v4("app.aaydb")
```

or `aaysql --migrate app.aaydb` on the command line. The operation:

1. acquires the canonical Phase 1 lease (`<db>.server.lock`) and fails loudly if
   another engine, server or backup owns the database;
2. loads the v3 database through the **v3** loader, including v3 WAL recovery and
   its v3 checkpoint, so the committed v3 state is exact;
3. copies the v3 snapshot to `<db>.v3.bak` and fsyncs it (recoverability);
4. writes v4 segments for every table under a fresh `db_id`, fsyncs them;
5. builds the v4 manifest, fsyncs it, and atomically replaces the database path;
6. reopens the published v4 database to verify it loads;
7. on success removes `<db>.v3.bak` and the (empty) v3 WAL; on verification
   failure restores `<db>.v3.bak` over the path and raises.

A crash before step 5 leaves the v3 snapshot authoritative (plus an unused
segment directory and `<db>.v3.bak`); a crash after step 5 leaves a complete v4
generation. Migration never interprets v3 whole-image WAL records as v4 deltas —
the two formats are distinguished by the snapshot/manifest `format_version`
before any WAL is read. Repeating migration on a v4 database is a no-op.

## 9. Backup and restore

Backups remain offline and take the canonical lease. Backup manifest
`backup_format_version` 2 adds a complete **file inventory**:

```json
{
  "backup_format": "aaysql-backup",
  "backup_format_version": 2,
  "database_format_version": 4,
  "database_file": "database.aaydb",
  "database_sha256": "…",
  "database_size": 1234,
  "files": [
    {"path": "database.aaydb", "size": 1234, "sha256": "…"},
    {"path": "database.aaydb.seg/seg_….seg", "size": 2048, "sha256": "…"}
  ],
  "segments": ["seg_….seg"],
  "tables": [{"name": "users", "rows": 2}],
  "indexes": ["users_email"],
  "wal_lsn": 42,
  "consistency": "offline snapshot after WAL recovery"
}
```

Every file in the inventory is verified on `verify_backup` and before restore.
Paths must be relative, contain no `..` and stay inside the backup directory. v1
backup artifacts remain readable and restorable; new backups always use v2.

Restore stages the entire backup directory into a temporary directory, verifies
it, and opens the staged copy. Installation keeps the **root manifest as the
single publication point**: segments are copied into the target's segment
directory first (content-addressed, so existing identical files are reused and a
name whose bytes differ is rejected), fsynced, and only then is the root manifest
atomically replaced. The previous root is kept as `<target>.pre-restore-<id>`
until the restored database has been reopened successfully; on failure it is
restored. A failed restore therefore never leaves a target that mixes two
generations: it is either the old root or the new one.

## 10. Compatibility boundaries

- **v4 is not readable by the v1.0.0 package.** A v1.0.0 engine reports
  `Unsupported database format version: 4` and does not modify the file.
- v3 (and v1/v2) databases remain readable by this build and continue to be
  written as v3 until explicitly migrated.
- Protocol v1, SQL semantics, transaction semantics, backup CLI arguments and
  the `<db>.server.lock` ownership contract are unchanged. The package version
  stays `1.0.0` for this development phase; the tag `v1.0.0` is untouched.
- The format version written by `Database.to_dict()` (the v3 snapshot) stays `3`.

## 11. What is still O(database)

- `Database.clone()` for statement atomicity and rollback (deliberately kept).
- Loading a database: all segments are materialized and index trees rebuilt.
- Migration and backup of a database.
- Recovery CPU is proportional to the checkpointed data plus the WAL replayed;
  the WAL itself is now proportional to the committed changes.
