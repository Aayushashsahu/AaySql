# Changelog

All notable changes to AayushSQL are documented in this file.

## [0.7.0] — 2026-11-11

### Added
- **Cost-based query optimizer (`src/aaysql/planner.py`)** — the planner is now a two-stage optimizer instead of a single-table access-path picker:
  - **Logical planning** binds sources and decides *what* to compute: join order and join types stay exactly as written, `WHERE` and `ON` clauses are split along top-level `AND` only (`OR` is never split), and each conjunct is assigned to the earliest stage where it is provably equivalent.
  - **Physical planning** decides *how*: per-source `INDEX SCAN` vs `TABLE SCAN`, per-join `HASH JOIN` vs `NESTED LOOP JOIN`, plus `FILTER`, `AGGREGATE`, `PROJECTION`, `SORT`, and `LIMIT` wrappers. Every node carries `estimated_rows`, `estimated_cost`, and a `WHY` rationale.
  - The executor runs the selected physical plan; `EXPLAIN` no longer describes a path the executor might not take.
- **Hash join (`src/aaysql/execution.py`)** — hand-written, for `INNER` and `LEFT` joins:
  - multi-row buckets, so duplicate keys produce every match;
  - composite keys built from a conjunction of equalities;
  - `NULL` key columns never match, matching SQL `UNKNOWN` semantics;
  - the complete `ON` predicate is still evaluated on every candidate, so residual predicates are exact;
  - non-equi conditions, `OR` conditions, and unsafe key extractions fall back to nested loop;
  - output order is left-input order then right-input order — identical to the nested-loop implementation, so unordered results and `LIMIT` are unchanged.
- **Table statistics (`src/aaysql/statistics.py`)** — exact row and `NULL` counts, distinct counts exact up to 4,096 values and bitmap-estimated beyond (fixed memory), and min/max when values are comparable. Statistics are derived from rows, never from indexes. Cached entries are validated against the live `Table` object and a new `Table.revision` counter that is bumped by `INSERT`/`UPDATE`/`DELETE`; snapshots, rollbacks, and reloads replace the whole `Database` (and its cache), so stale statistics cannot survive a mutation.
- **Safe predicate pushdown** — only top-level `AND` conjuncts, only onto their own source, and:
  - right-side `WHERE` predicates of a `LEFT JOIN` stay above the join;
  - right-only `ON` conjuncts are pushed into the right input (a row that fails them can never match);
  - left-only `ON` conjuncts are never pushed below a `LEFT JOIN`;
  - multi-relation predicates always stay at the post-join `FILTER`.
- **Cost model** — documented relative constants for sequential rows, index seek/candidates/table-order restoration (so a very wide range can lose to a sequential scan), hash build/probe/matches, and nested-loop comparisons, calibrated against `benchmarks/benchmark_optimizer.py`. Inputs below `HASH_MIN_INPUT_ROWS` keep the nested-loop reference.
- **`EXPLAIN` / `EXPLAIN ANALYZE`** — plain `EXPLAIN` no longer executes the statement; it reports the plan and labelled estimates (`~rows`, plus an explicit note that estimates are not measurements). `EXPLAIN ANALYZE` reports real per-operator counters: rows in/out, rows rejected, index candidates, hash build/probe/bucket matches, nested-loop comparisons, aggregate/projection/sort/limit counts, and per-operator timing.
- **Tests (`tests/test_optimizer.py`, 65 tests)** — equivalence of every optimization against the forceable nested-loop/table-scan reference: duplicates, all-`NULL` keys, composite keys, residual predicates, three-table chains, with and without indexes, empty/one-row/skewed/many-to-many/size-ratio datasets, `GROUP BY`/`HAVING`/`ORDER BY`/`LIMIT`, pushdown rules, statistics exactness, invalidation through `INSERT`/`UPDATE`/`DELETE`/`ROLLBACK`/reopen, bounded distinct estimation, cost-model choices in both directions, `EXPLAIN` not executing, and metrics accuracy.
- **Benchmarks (`benchmarks/benchmark_optimizer.py`)** — stdlib-only, deterministic, warm-up plus median/min/max spread, with the reference path measured where feasible and larger runs explicitly labelled as extrapolation. Results in `BENCHMARKS.md`.
- **Planner hooks**: `plan_select(..., force_join_algorithm=..., force_access_path=...)` keeps the v0.6.x path forceable for tests and benchmarks. There is no new SQL syntax.

### Changed
- `SELECT` execution moved from `query.py` to `src/aaysql/execution.py`; `query.py` keeps source binding, three-valued logic, and expression evaluation. `query.execute_select()` remains importable and delegates.
- Plan rendering now uses operator names (`JOIN: HASH (INNER)`, `JOIN: NESTED LOOP (INNER)`, `AGGREGATE: GROUP BY ...`) while keeping the v0.4.0 plan lines (`TABLE SCAN:`, `INDEX SCAN:`, `TABLE:`, `CONDITION:`, `FILTER:`) that existing output contracts rely on.
- `ROWS SCANNED` in `EXPLAIN ANALYZE` now counts rows read from every base input, including join inputs; `ROWS FILTERED` counts rows actually rejected by filters.

### Notes
- The `.aaydb` format remains **version 3**; no migration is required from v0.6.1, and no storage/durability behaviour changed.
- Join reordering is deliberately not implemented: it would change the documented unordered result order. See the README limitations.
- Version metadata updated to `0.7.0`.

## [0.6.1] — 2026-11-11

### Fixed
- **Trailing WAL repair before recovery CHECKPOINT (`src/aaysql/wal.py`, `src/aaysql/storage.py`)**:
  - `parse_wal()` now reports `valid_bytes_end`, the exact byte offset after the last fully validated newline-terminated record (computed from UTF-8 bytes, not characters).
  - Recovery truncates only the invalid trailing bytes, flushes and `fsync()`s the WAL, keeps every earlier valid record and LSN, and only then appends the recovery `CHECKPOINT`.
  - Previously, recovery could append `CHECKPOINT` behind a corrupt tail; a crash before truncation turned that tail into mid-log corruption, so the next open raised `StorageError`. A regression test reproduces the full seven-step sequence and fails without the repair.
- **Directory fsync error classification (`src/aaysql/wal.py`)**:
  - `fsync_dir()` no longer swallows every `OSError`. Unsupported directory fsync (`EINVAL` / `ENOTSUP` / `EOPNOTSUPP`) and Windows (where the CRT cannot open directories) use the documented fallback; `EACCES`, `EIO`, `ENOSPC`, and other genuine failures raise `StorageError` with the directory path and errno.
  - File descriptors are closed on every path. A checkpoint that fails after `os.replace()` keeps the committed state and the WAL, and is never reported as a failed commit.
- **Partial WAL append handling (`src/aaysql/wal.py`, `src/aaysql/engine.py`)**:
  - Appends now use `os.open(..., O_APPEND)` with an `os.write()` write-all loop, so short writes cannot be mistaken for complete records.
  - An explicit append state machine (`begin_appended` -> `write_appended` -> `commit_attempted` -> `commit_durable`) distinguishes failures before the COMMIT record from failures while making it durable.
  - Any append that may have left torn bytes poisons the WAL manager. A poisoned manager refuses further appends and blocks engine statements until the database is reopened and recovery repairs the log; LSNs only advance after durable success.
  - A failed COMMIT `fsync` (or a failure fsyncing the directory entry of a newly created WAL) raises `AmbiguousCommitError`, preserves the WAL, keeps the in-memory state, and never claims a definite rollback. A new process decides the outcome from the actual COMMIT record.

### Added
- **Tests (`tests/test_wal_hardening.py`)**: 25 tests covering corrupt-tail byte boundaries, tail repair before checkpoint, the seven-step crash regression, crash after tail repair, repeated recovery, mid-log corruption (in-process and subprocess), directory fsync classification (supported, unsupported platform, `EINVAL`, `EACCES`, `EIO`, `ENOSPC`, post-`os.replace()` failure), short writes, partial-append poisoning, ambiguous COMMIT, poisoned-manager refusal, checkpoint append failure, and five combined real-subprocess `os._exit()` scenarios.
- **Documentation (`WAL.md`, `README.md`, `ARCHITECTURE.md`)**: Documented trailing-tail repair, valid-byte-boundary tracking, WAL poisoning and recovery-required states, partial append semantics, ambiguous COMMIT behavior, and POSIX/Windows directory fsync guarantees.

### Notes
- The `.aaydb` format remains **version 3**; no migration is required from v0.6.0.
- Durability claims remain limited to process-crash recovery under documented OS `fsync` behavior; arbitrary hardware power loss and firmware write-cache failures are outside the tested envelope.
- Version metadata updated to `0.6.1`.

## [0.6.0] — 2026-11-10

### Added
- **Write-Ahead Log (`src/aaysql/wal.py`)**:
  - Adjacent `<db_path>.wal` using newline-delimited canonical JSON records (`begin`, `write`, `commit`, `checkpoint`) protected by canonical SHA-256 checksums, strict monotonic LSNs, and unique UUID4 transaction IDs.
  - WAL `os.fsync()` commit durability (with POSIX directory-entry `fsync_dir()`) precedes `.aaydb` snapshot writes across both explicit `BEGIN` / `COMMIT` transactions and single-statement autocommit mutations (`INSERT`, `UPDATE`, `DELETE`, `CREATE/DROP TABLE`, `CREATE/DROP INDEX`).
  - Distinguished commit error semantics (`AmbiguousCommitError` in `src/aaysql/errors.py`): pre-commit errors restore memory cleanly; post-commit checkpoint errors keep the committed state and preserve the WAL without reporting failure; `COMMIT` `fsync` errors mark the engine `needs_recovery=True` and require reopen recovery.
- **Database format version 3 & migration (`src/aaysql/storage.py`)**:
  - Added `wal_lsn` to `.aaydb` format version `3` tracking the highest committed WAL LSN applied to the snapshot.
  - Preserved read compatibility with format versions `1` and `2` (`wal_lsn = 0`) without overwriting legacy files on read, upgrading safely to v3 on write or recovery checkpoint.
- **Crash-safe checkpointing & startup recovery (`src/aaysql/storage.py`)**:
  - Safe checkpoint ordering: atomic `.tmp` write + `os.fsync()` -> `os.replace()` -> `fsync_dir()` -> WAL `checkpoint` record -> WAL truncation -> stale `.tmp` cleanup.
  - Startup recovery validates WAL integrity, ignores trailing torn or checksum-corrupt records, stops immediately at mid-log corruption without skipping forward, discards incomplete transactions, and idempotently replays the newest committed image with `commit_lsn > snapshot.wal_lsn`.
- **Tests (`tests/test_wal_recovery.py`)**: Added 24 tests including real child subprocess `os._exit()` crash tests across all critical pre-commit, post-`COMMIT` `fsync`, snapshot write, `os.replace()`, checkpoint, and WAL truncation stages, plus corruption, idempotency, error-semantics, and v1/v2->v3 migration coverage.
- **Documentation (`WAL.md`, `ARCHITECTURE.md`, `LANGUAGE_SPEC.md`, `README.md`)**: Documented WAL protocol, LSN/txn invariants, durability semantics, checkpoint ordering, recovery, format v3, and platform limitations.
- **Version metadata** updated to `0.6.0`.

## [0.5.0] — 2026-11-10

### Added
- **Explicit transactions (`src/aaysql/transaction.py`, `src/aaysql/engine.py`)**: `AAYUSH BEGIN`, `AAYUSH COMMIT`, and `AAYUSH ROLLBACK` for multi-statement transactions.
  - `BEGIN` captures a deep snapshot of tables and index trees. Mutations inside the transaction are visible to later statements in the same engine but are not written to disk.
  - `COMMIT` performs the single durable save through the existing atomic file replacement. If the save fails, the pre-`BEGIN` snapshot is restored, `StorageError` is raised, and the transaction is never reported as committed.
  - `ROLLBACK` reinstalls the `BEGIN` snapshot, discarding DML and DDL, including `CREATE INDEX` and `DROP TABLE`.
  - Each statement inside a transaction is atomic: a failed statement leaves no partial rows or index entries, and the transaction stays usable.
  - Nested `BEGIN`, and `COMMIT` / `ROLLBACK` without an active transaction, raise `ExecutionError`.
  - Closing an engine with an active transaction rolls it back. The CLI prints `WARNING: rolling back uncommitted transaction on exit` to `stderr`, so uncommitted work is never silently persisted.
  - Autocommit behavior outside explicit transactions is unchanged.
- **Tests (`tests/test_transactions.py`)**: 14 tests covering commit/rollback persistence, multiple statements, DML and DDL rollback, index changes, constraint failures inside transactions, failed commits, nested/missing transaction errors, in-transaction visibility versus a second reader, engine close, in-memory transactions, and CLI exit/script behavior.
- **Documentation**: `LANGUAGE_SPEC.md`, `ARCHITECTURE.md`, and `README.md` now describe transaction semantics, the commit durability point, and the single-writer model.
- **Version metadata** updated to `0.5.0`; the `.aaydb` format stays version 2.

### Notes
- The model is single-process and single-writer: no savepoints, isolation levels, nested transactions, or concurrent writers.
- `COMMIT` is the only durability point and rewrites the full database file.

## [0.4.0] — 2026-11-10

### Added
- **B+ tree (`src/aaysql/btree.py`)**: A from-scratch ordered tree with internal separator nodes, linked leaf nodes for range traversal, node splitting on insert, redistribution and merging on delete, duplicate key support via physical `(key, sequence)` entries, empty-tree handling, and `validate()` for structural invariants.
- **Secondary indexes (`src/aaysql/index.py`)**: Single-column, non-unique indexes binding a name and table column to a B+ tree, with lookup, range scan, incremental add/remove, rebuild-from-rows, and index name validation. `NULL` values are not indexed, and index scans return rows in table order.
- **Index DDL (`src/aaysql/parser.py`, `src/aaysql/executor.py`)**: `CREATE INDEX idx ON table (column)` and `DROP INDEX idx`, with duplicate-name, unknown-table, unknown-column, and multi-column rejection. `DROP TABLE` removes that table's indexes.
- **Query planner (`src/aaysql/planner.py`)**: Chooses `INDEX SCAN` or `TABLE SCAN` for single-table `SELECT` statements using `=`, `>`, `>=`, `<`, `<=` predicates on indexed columns (either operand order), and falls back to a table scan for `OR`, `NOT`, `!=` / `<>`, `NULL` literals, type-mismatched literals, and joined queries. The complete `WHERE` clause is always re-applied after candidate retrieval.
- **`EXPLAIN` / `EXPLAIN ANALYZE`**: Renders the plan actually executed (`INDEX SCAN` with index, table, and condition; `TABLE SCAN` with filter) and, with `ANALYZE`, measured rows scanned, rows filtered, rows returned, index used, join algorithm, and execution time.
- **Storage format version 2 (`src/aaysql/storage.py`)**: Index definitions are persisted in `.aaydb` files and B+ trees are rebuilt and validated on load. Format version 1 files remain readable and upgrade on the next write; unknown versions and corrupt index metadata are rejected without modifying the file.
- **Benchmarks (`benchmarks/benchmark_index.py`, `BENCHMARKS.md`)**: Reproducible measurements for table scan versus index equality/range access at 1,000, 10,000, and 100,000 rows, index build cost, mutation overhead, and storage overhead, with no timing assertions in CI.
- **Tests**: `tests/test_btree.py` (ordering, duplicates, splits, merges, ranges, randomized equivalence, invariant checks) and `tests/test_indexing.py` (DDL, maintenance, failed-mutation consistency, indexed/unindexed result equivalence, EXPLAIN, persistence, and v1→v2 migration).
- **Version metadata** updated to `0.4.0`; `tests/test_cli.py` now asserts the version from the package instead of a hard-coded string.

### Notes
- Indexes are acceleration structures only: table rows stay authoritative and every index can be rebuilt from them.
- Index-assisted filtering covers single-table predicates only; joins remain nested-loop scans.
- `EXPLAIN` executes the statement to report real plans and metrics, so it is not a zero-cost command.

## [0.3.0] — 2026-11-10

### Added
- **Relational JOINs (`src/aaysql/ast.py`, `src/aaysql/parser.py`, `src/aaysql/query.py`)**:
  - Implemented `INNER JOIN ... ON ...`, `LEFT JOIN ... ON ...`, and multi-table chained joins via nested-loop execution.
  - Added table alias support (`FROM users AS u` and `FROM users u`) and `JoinClause` AST nodes.
  - Added qualified column references (`users.id`, `u.id`, `orders.product`) with ambiguity detection for shared unqualified column names and collision-safe `<ref>.<col>` headers for joined `SELECT *`.
- **Aggregate Functions**:
  - Implemented `COUNT(*)`, `COUNT(column)`, `SUM(column)`, `AVG(column)`, `MIN(column)`, and `MAX(column)` via `AggregateCall` AST nodes.
  - Enforces SQL `NULL` semantics (`COUNT` counts non-`NULL`; `SUM`/`AVG`/`MIN`/`MAX` ignore `NULL` and return `NULL` on empty/all-`NULL`; `COUNT` returns `0` on empty input) and rejects non-numeric `SUM`/`AVG` inputs and nested aggregates.
- **`GROUP BY` and `HAVING`**:
  - Implemented single- and multi-column `GROUP BY` (including `NULL` grouping keys) and post-aggregation `HAVING` filtering.
  - Enforces SQL grouping rules: rejects ungrouped, non-aggregated columns in `SELECT`, `HAVING`, and `ORDER BY`.
  - Extended `ORDER BY` to support qualified columns, explicit `SELECT` aliases, and aggregate expressions.
- **Modular Query Pipeline (`src/aaysql/query.py`)**:
  - Separated relational query resolution and execution stages (`FROM` -> `JOIN` -> `WHERE` -> `GROUP BY` -> Aggregates -> `HAVING` -> Projection -> `ORDER BY` -> `LIMIT`) and shared expression evaluation with `Executor` DML operations.
- **Tests & Documentation**:
  - Added `tests/test_relational_queries.py` covering JOINs, table aliases, qualified/ambiguous columns, aggregates, `GROUP BY`, `HAVING`, combined queries, and persistence compatibility.
  - Added `tests/test_null_semantics.py` covering complete `AND`/`OR`/`NOT` truth tables, `NULL` comparisons in `WHERE`, `JOIN ON`, `HAVING`, `UPDATE`, and `DELETE`, and duplicate output label rejection.
  - Updated `README.md`, `ARCHITECTURE.md`, `LANGUAGE_SPEC.md`, and version metadata (`0.3.0`) while keeping `.aaydb` format version `1` compatible.

### Fixed
- **Three-valued `NULL` logic (`src/aaysql/query.py`, `src/aaysql/executor.py`)**: Comparisons against `NULL` previously returned ordinary `FALSE`, which made `NOT` invert an unknown into a match. They now evaluate to `UNKNOWN`, which propagates through `AND` / `OR` / `NOT` per SQL truth tables, and `WHERE`, `JOIN ON`, `HAVING`, `UPDATE`, and `DELETE` predicates admit a row only on `TRUE`.
- **Duplicate `SELECT` output labels (`src/aaysql/query.py`)**: Two selected expressions sharing an output label (for example `u.id AS identifier, o.id AS identifier`, or `u.id, u.id`) previously overwrote one value in the label-keyed result row. The query is now rejected with `ExecutionError` before any rows are scanned, so no data is silently discarded.
- **Single expression evaluator (`src/aaysql/query.py`)**: Row-level and group-level evaluation (`eval_row_expr` and `_eval_group_expr`) were duplicated logic trees; both now delegate to one `eval_expr()`, so aggregate and non-aggregate paths cannot diverge on logic semantics.

## [0.2.1] — 2026-11-10

### Fixed
- **Mandatory `=` in `UPDATE` assignments (`src/aaysql/parser.py`)**: Replaced optional `=` consumption with `parse_assignment()`, requiring `=` in every `SET` assignment and rejecting missing, duplicate, or misplaced `=` operators or missing assignment values with `ParserError` including position info.
- **Complete `CREATE TABLE` schema & default validation (`src/aaysql/parser.py`, `src/aaysql/storage.py`)**:
  - Centralized type and schema validation (`TYPE_MAP`, `SUPPORTED_TYPES`, `validate_type_name`, `validate_column_def`, `validate_table_schema`) shared consistently across `CREATE TABLE` parsing, `Database.create_table()`, and `JsonFileStorage.load()`.
  - Rejects unsupported data types, duplicate `DEFAULT` clauses, type-mismatched `DEFAULT` values, and `DEFAULT NULL` on `NOT NULL` / `PRIMARY KEY` columns.
- **Fail-fast script exit codes (`src/aaysql/cli.py`)**: Updated `run_buffer(..., stop_on_error=...)` and `main()` so script execution stops on the first `AayushSQLError`, preserves previously persisted statements, prints errors to `stderr`, and exits with code `1` (while keeping interactive REPL sessions resilient after query errors).
- **Normalized runtime comparison errors (`src/aaysql/executor.py`)**: Replaced eager comparison dictionary evaluation with `_compare()`, preventing `TypeError` on `=`/`!=` with mixed types and converting incompatible ordering comparisons (`<`, `<=`, `>`, `>=`) and `ORDER BY` sort `TypeError`s into `ExecutionError` chained with `from exc` without swallowing unexpected internal bugs.

### Added
- **GitHub Actions CI (`.github/workflows/tests.yml`)**: Runs `pytest -v` on pushes and pull requests targeting `main` across Python `3.10`, `3.11`, `3.12`, and `3.13`.
- **Regression test suite (`tests/test_hardening.py`, `tests/test_cli.py`)**: Added coverage for `UPDATE` grammar edge cases, supported/unsupported types and `DEFAULT` validation, script fail-fast exit codes, REPL error recovery, `--version`, and normalized `ExecutionError`s.
- **Packaging**: Declared `[project.optional-dependencies] dev = ["pytest>=7"]` in `pyproject.toml` and bumped version metadata to `0.2.1`.

## [0.2.0] — 2026-11-10

### Added
- **Persistent JSON storage (`JsonFileStorage`)**: Saves database schemas, column constraints, and row data to versioned `.aaydb` JSON files (`format="aaysql"`, `format_version=1`).
- **Atomic writes**: Serializes changes to a temporary file in the target directory, flushes and calls `os.fsync()`, and swaps via `os.replace()` to prevent partially written database files.
- **Corruption & schema validation**: Validates loaded database files for JSON syntax, format envelope, version compatibility, column metadata, cell data types, and `PRIMARY KEY` / `UNIQUE` / `NOT NULL` constraints on load, raising `StorageError` on malformed data.
- **Engine persistence integration (`Engine(db_path=...)`)**:
  - Automatically loads existing database files on startup and persists after every successful mutating statement.
  - Skips disk writes for read-only `SELECT` queries and failed statements.
  - Restores in-memory state if disk persistence fails.
  - Supports in-memory mode via `Engine()` or `Engine(":memory:")`, plus `close()` and context manager (`with Engine(...) as db:`).
- **CLI extensions (`aaysql.cli`)**:
  - Added `--db PATH` (default `aaysql.aaydb`, or `:memory:`) and `--memory` flags, plus `--version`.
  - Improved multi-line REPL statement splitting to respect string literals and `--` comments.
  - Clean startup/error reporting and reliable shutdown handling.
- **Documentation & tests**:
  - Added `LANGUAGE_SPEC.md` and `CHANGELOG.md`; updated `README.md` and `ARCHITECTURE.md`.
  - Added `tests/test_persistence.py` and `tests/test_cli.py` covering multi-session persistence, constraints after reload, corruption detection, atomic rollback, and CLI flows.

### Fixed
- **Multi-row `INSERT` atomicity**: Failed constraint checks on later rows in a batch no longer leave earlier rows inserted in the table.
- **Multi-row `UPDATE` constraint validation**: `PRIMARY KEY` and `UNIQUE` collisions between multiple rows updated in the same statement are now detected before mutating any row.
- **Duplicate column detection in `INSERT`**: Specifying the same column twice in an `INSERT` column list now raises `ExecutionError`.
- **`ORDER BY` with multiple `NULL` values**: Sorting columns containing multiple `NULL` rows no longer raises `TypeError`.
- **CLI banner formatting**: Removed stray literal `\n` from the ASCII art banner.

## [0.1.0] — Initial Release

### Added
- Custom lexer and recursive-descent parser requiring the mandatory `AAYUSH` prefix.
- In-memory execution engine supporting `CREATE TABLE`, `DROP TABLE`, `INSERT`, `SELECT`, `UPDATE`, and `DELETE`.
- Support for `WHERE`, comparison operators (`=`, `!=`, `<>`, `<`, `<=`, `>`, `>=`), `AND`/`OR`/`NOT`, `ORDER BY`, `LIMIT`, and `AS` aliases.
- Column types (`INT`, `TEXT`, `FLOAT`, `BOOL`) and constraints (`PRIMARY KEY`, `NOT NULL`, `UNIQUE`, `DEFAULT`).
- Interactive REPL and script runner CLI with ASCII table formatting.
