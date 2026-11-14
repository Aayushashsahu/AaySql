# Changelog

All notable changes to AayushSQL are documented in this file.

## [Unreleased] — format v4 & Phase 5 (Native JSON & Document Model)

### Added
- **Native JSON & Document Model (Phase 5, `src/aaysql/json_value.py`, `docs/JSON_MODEL.md`)**: First-class `JSON` and `DOCUMENT` column data types with strict schema validation. Enforces strict limits: max 1 MiB document size, max 64 nesting depth, max 1 KiB key length, max 32 path segments, signed 64-bit integer range. Rejects duplicate keys, non-finite floats (`NaN`, `Infinity`), and null bytes `\u0000`.
- **Sentinels and Three-State Truth Table (`src/aaysql/json_value.py`, `src/aaysql/expressions.py`)**: Distinct `JSON_NULL` (`_JsonNull`) and `ABSENT` (`_Absent`) internal query sentinels. Complete three-state truth table: `IS NULL` (true for SQL NULL, JSON NULL, ABSENT), `IS MISSING` (true only for ABSENT), `IS JSON NULL` (true only for JSON NULL), and their negated counterparts.
- **Path Navigation & Indexing (`src/aaysql/parser.py`, `src/aaysql/expressions.py`)**: Dot navigation (`profile.address.city`) and bracket indexing (`tags[0]`, `tags['key']`) in projection and predicates. Path ambiguity resolution handles table qualifier vs column vs property without schema knowledge at parse time.
- **Built-in Document Functions**: `JSON_EXTRACT(doc, path)`, `JSON_VALUE(doc, path)`, `JSON_EXISTS(doc, path)`, `JSON_SET(doc, path, val)`, and `JSON_REMOVE(doc, path)`.
- **Collections (`CREATE COLLECTION <name>`)**: Sugar compiling directly to relational tables with `_id TEXT PRIMARY KEY, _rev INT, body JSON`.
- **Point Mutation & Transactional Integrity**: Supports `UPDATE ... SET col.path = val`, `JSON_SET`, `JSON_REMOVE`, and whole document replacement. In-memory undo journal records before-images using `deep_copy_json`, providing O(1) begin, zero database cloning, failed statement atomicity, and clean rollback.
- **MVCC & Storage Integration**: Format v4 segments and delta WAL store canonical serializations. MVCC snapshot readers remain isolated via copy-on-write boundaries preventing Python object aliasing or in-place mutation bleed-through.
- **Tests & Benchmarks**: 38 dedicated tests across `test_json_values.py`, `test_json_queries.py`, `test_json_mutations.py`, `test_json_storage.py`, `test_json_mvcc.py`, and `benchmarks/benchmark_json.py`.
- **Delta WAL (`src/aaysql/wal.py`, `src/aaysql/delta.py`)**: commits append only the changed rows and schema objects (`insert` / `update` / `delete` / DDL) instead of a whole-database image. Framing, checksums, LSNs, the `COMMIT` fsync commit point, torn-tail repair, mid-log corruption failure, and ambiguous-commit handling are preserved. Every record carries the database identity; a foreign `db_id` is rejected.
- **Segmented checkpoints (`src/aaysql/segments.py`, `src/aaysql/storage.py`)**: the database path is now a root manifest referencing immutable, content-addressed per-table segments in `<db>.seg/`. Checkpoints write only changed tables, reuse unchanged segment bytes, publish the manifest atomically, then append `CHECKPOINT` and truncate the WAL. Unreferenced segments are garbage-collected. Missing, truncated, modified, or path-traversing segment references fail closed.
- **Format v4 and explicit migration**: new databases are v4; formats v1–v3 remain readable and stay on the v3 backend until `migrate_to_v4()` / `aaysql --migrate PATH` runs under the canonical `DatabaseLease` (backup to `<db>.v3.bak`, segments, publish, verify, cleanup; restore on failure). The operation is idempotent and finishes an interrupted migration.
- **Backup format v2**: v4 backups record a full file inventory (manifest plus every segment) with per-file size and SHA-256; verification and restore validate it. Restore installs segments first and replaces the root manifest atomically, preserving the previous generation as `<target>.pre-restore-<id>` with its segments.
- **`Engine.checkpoint()`**, a WAL-size checkpoint threshold (1 MiB default), and recovery checkpoints, all without a background thread or group commit.
- **Documentation**: [`docs/FORMAT_V4.md`](docs/FORMAT_V4.md) plus updates to [`COMPATIBILITY.md`](COMPATIBILITY.md), [`WAL.md`](WAL.md), [`docs/OPERATIONS.md`](docs/OPERATIONS.md), [`README.md`](README.md), and [`RELEASE_BENCHMARKS.md`](RELEASE_BENCHMARKS.md).
- **Tests and benchmark**: `test_delta_v4.py`, `test_checkpoints_v4.py`, `test_migration_v4.py`, `test_backup_v4.py`, `test_crash_v4.py` (including real subprocess crash points), and `benchmarks/benchmark_v4.py` (100 / 1k / 10k / 100k rows, commit vs checkpoint vs recovery, bytes written, unchanged tables).

### Changed
- **MVCC read views and group commit (`src/aaysql/mvcc.py`)**: `Engine.connect(isolation=...)` now returns an independent session. A published `Database` root is immutable-in-practice; readers pin it in O(1), writers use a per-session copy-on-write workspace, and a commit publishes a new root only after durability. `READ COMMITTED` is the default; `BEGIN SNAPSHOT` / `BEGIN ISOLATION LEVEL SNAPSHOT` pins a repeatable read view. First-updater-wins conflicts raise `WriteConflictError` (protocol `WRITE_CONFLICT`), while insert-only transactions may append concurrently with UNIQUE/PRIMARY KEY reservation. Snapshot capture/release is counted and deterministic. The server assigns one session per connection and no longer blocks readers with `TRANSACTION_BUSY`.
- **WAL group commit (`WalManager.append_commit_batch`, `append_delta_batch`, `JsonFileStorage.save_batch`, `SegmentedStorage.save_batch`)**: independent transactions are appended as normal per-transaction record triples and share one `os.fsync()` after the final `COMMIT`. Writer admission is released before that fsync, so new writers can execute and enqueue while the disk is busy. Failure classification and torn-record poisoning are unchanged.
- **Python 3.10 group-commit completion wait.** The commit wait now catches `concurrent.futures.TimeoutError` explicitly. Python 3.10 keeps that class distinct from the built-in `TimeoutError`, so a writer waiting behind another writer's fsync could previously receive `TimeoutError` instead of waiting for publication.
- **In-memory undo journal for transactions (`src/aaysql/undo.py`, `src/aaysql/executor.py`, `src/aaysql/engine.py`, `src/aaysql/transaction.py`)**: `BEGIN` and per-statement markers are O(1); transactions no longer clone the database. Mutations append inverse entries (insert start/rows, update before-images in place, deleted row objects at their original positions, DDL objects and index state), and rollback applies only those entries. Indexes are restored with exact inverse operations, and `Table.revision` is bumped so cached statistics cannot survive a rollback. Journal memory is proportional to the mutations made, not to the database size.
- **Commit classification keeps its meaning.** Pre-commit failures undo the journal and raise `StorageError`; a successful WAL `COMMIT` fsync followed by a checkpoint failure remains committed with a pending checkpoint; an ambiguous fsync clears the journal without applying it, leaves memory untouched, and keeps `needs_recovery` set.
- **Tests and benchmark**: `tests/test_undo_journal.py` and `tests/test_undo_engine.py` cover journal inverses, duplicate-row order, injected partial failures, autocommit, v3/v4, durability classification, and a monkeypatched `Database.clone` proof. `benchmarks/benchmark_undo.py` reports clone-free transaction, statement-rollback, memory, and persistent-autocommit results.
- **Phase 4 tests and benchmark**: `tests/test_mvcc.py`, `tests/test_isolation.py`, `tests/test_group_commit.py` (including a real subprocess crash after a group fsync), and `tests/test_concurrent_server.py`. `benchmarks/benchmark_concurrency.py` measures serial versus grouped commits and reader latency during an open writer; results are recorded in [`RELEASE_BENCHMARKS.md`](RELEASE_BENCHMARKS.md). The contract is documented in [`docs/MVCC.md`](docs/MVCC.md).

### Notes
- `.aaydb` format v4 is **not** readable by the v1.0.0 package. Package version remains `1.0.0` and tag `v1.0.0` is unchanged. `Database.clone()`, load, backup, and restore remain O(database); rollback cost is O(mutations), while UPDATE/DELETE row location, index builds, v3 snapshot commits, and v4 checkpoints stay proportional to table or database size. No O(1) database claim is made.
- **Concurrency limits (Phase 4).** Multiple readers are concurrent. One commit generator is admitted at a time; there are no row-level version chains, no SERIALIZABLE isolation, no concurrent write execution, and no parallel query execution. A writer detaches the changed table (rows and indexes) from the published root, so writer cost is O(changed table), not O(database); reader snapshot capture is O(1). Conflicts are table-level and callers retry the whole transaction.

## [1.0.0] — 2026-11-11

### Added
- **Official offline backup and restore (`src/aaysql/backup.py`, `aaysql-backup` CLI)**:
  - Exclusive DB ownership via the server lock, automatic pending WAL recovery before reading, `manifest.json` with format versions and SHA-256 checksum, atomic staged writes, `verify`, and safe `restore` validating loadability and preserving the replaced database until validation passes (`<target>.pre-restore-<id>`).
- **Formal compatibility & security documentation**:
  - Added [`COMPATIBILITY.md`](COMPATIBILITY.md) (SQL, format v3, network protocol v1, and Python API compatibility contract), [`AUDIT.md`](AUDIT.md) (module-by-module v1.0.0 stabilization audit), [`SECURITY.md`](SECURITY.md), [`THREAT_MODEL.md`](THREAT_MODEL.md), [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md), [`docs/OPERATIONS.md`](docs/OPERATIONS.md), and [`RELEASE_BENCHMARKS.md`](RELEASE_BENCHMARKS.md).
- **Permanent format fixtures (`tests/fixtures/`, `tests/test_format_fixtures.py`)**:
  - Read-only fixtures for format v1, v2, v3, valid indexed databases, `NULL` values, and corrupt JSON/schema files, verifying non-destructive read-only opens and v1/v2 -> v3 migration on write.
- **Packaging & cross-platform CI**:
  - Added `MANIFEST.in`, console scripts (`aaysql`, `aaysql-server`, `aaysql-backup`), Python 3.10–3.13 classifiers, and GitHub Actions testing across Ubuntu, Windows, and macOS plus wheel/sdist artifact and clean-venv install validation.
- **Additional stress & documentation tests**:
  - `tests/test_stability.py`, `tests/test_backup.py`, `tests/test_server_stress.py` (1–32 concurrent TCP client mixed workloads & timeouts), `tests/test_docs_examples.py`, and `benchmarks/benchmark_release.py`.

### Fixed
- **SQL boolean comparison guard (`src/aaysql/expressions.py`)**: Mixed `BOOL` vs numeric comparisons (`flag = 1`) now raise `ExecutionError` instead of silently evaluating Python `True == 1`.
- **Client framing & error diagnostics (`src/aaysql/protocol.py`, `src/aaysql/client.py`)**: Client-side oversized requests now report `REQUEST_TOO_LARGE` instead of `RESPONSE_TOO_LARGE`, and `ProtocolError` includes the stable error code.
- **Server environment config (`src/aaysql/server.py`)**: `ServerConfig.from_environment(auth_token=...)` pops explicit `auth_token` cleanly without `TypeError` when `AAYUSQL_AUTH_TOKEN` is also set.
- **Windows backup fsync (`src/aaysql/backup.py`)**: Opened staged backup files with `O_RDWR` for `os.fsync()` to satisfy Windows CRT constraints.

### Notes
- `.aaydb` format remains **version 3** and network protocol remains **version 1**.
- Single-writer architecture: no MVCC, parallel queries, or live online backup.

## [0.9.0] — 2026-11-11

### Added
- **Unified expression engine (`src/aaysql/expressions.py`)** — every expression node is walked by one shared traversal and evaluated by one canonical evaluator:
  - `iter_expr_children` / `walk_expr` are the single description of expression shape. Identifier validation, aggregate detection and placement, grouped-expression rules, planner predicate-source analysis, and EXPLAIN rendering all use them, so a new node type cannot silently bypass a rule. An unknown node raises `ExecutionError` instead of being ignored, and a test asserts every expression class is covered.
  - `eval_expr` serves row-level and group-level evaluation (aggregates resolve through a callback) and is used by both the reference DML path and the plan-driven `SELECT` path — there is no second evaluator.
- **Expression parsing (`src/aaysql/parser.py`, `src/aaysql/lexer.py`)** — one precedence-climbing parser: `OR` < `AND` < `NOT` < comparisons/`IS`/`IN`/`BETWEEN`/`LIKE` < `+ -` < `* / %` < unary `+`/`-` < primary. Parentheses override it. Numbers are now lexed unsigned, so `1-2` is unambiguously subtraction; a sign is folded into an adjacent numeric literal, so `-5` and `DEFAULT -4` still work. `*` remains a wildcard only in the `SELECT` list position. New tokens: `+ - / %`, plus `TRUE`/`FALSE` literals.
- **`IS NULL` / `IS NOT NULL`** — never `UNKNOWN`, and deliberately not implemented as an equality comparison (`5 = NULL` stays `UNKNOWN`, `5 IS NULL` is `FALSE`). Always a table scan, because NULLs are not indexed.
- **`LIKE` / `NOT LIKE`** — `%` (any run) and `_` (exactly one character), case-sensitive. Patterns are compiled into an anchored regex with every non-wildcard character escaped and a 256-entry bounded cache, so user patterns are never executed as regular expressions. Both operands must be `TEXT`; `NULL` on either side yields `UNKNOWN`, and `NOT LIKE` preserves it.
- **`IN` / `NOT IN`** — three-valued membership: TRUE on a match, `UNKNOWN` when nothing matches but a candidate is `NULL`, FALSE otherwise, and `UNKNOWN` when the left operand is `NULL`. Empty lists and subqueries inside `IN` are `ParserError`s. List members may be any scalar expression.
- **`BETWEEN` / `NOT BETWEEN`** — inclusive at both ends, defined as `x >= low AND x <= high`, preserving `UNKNOWN`. `column BETWEEN literal AND literal` can use a single inclusive index range scan; the original predicate remains authoritative and both paths are tested for identical rows.
- **Arithmetic** — `+ - * / %` and unary `+`/`-`: `INT` operands stay `INT` for `+ - * %`, `/` always produces a float, modulo follows floored Python semantics, `NULL` propagates, and `BOOL`/`TEXT` operands, division/modulo by zero, and non-finite results raise `ExecutionError`. `UPDATE` remains statement-atomic when an expression raises.
- **`CASE`** — searched and simple forms, first `TRUE` branch wins (`UNKNOWN` never matches), missing `ELSE` yields `NULL`, simple `CASE` uses SQL `=` so a `NULL` base never matches `WHEN NULL`, nesting is supported, and `CASE` inside aggregates and `UPDATE` assignments composes with existing coercion and constraints.
- **`SELECT DISTINCT`** — duplicate elimination over the complete projected row, after projection and before ordering; first occurrence kept, `NULL`s equal, type-tagged keys (`TRUE` is not `1`). `ORDER BY` must reference the projected row (unique alias, selected expression, or a selected column for `DISTINCT *`), otherwise the query is rejected.
- **`OFFSET`** — `LIMIT n OFFSET m` and `OFFSET m` without `LIMIT`; non-negative integers only, `OFFSET` before `LIMIT` rejected. Applied after `ORDER BY` and before `LIMIT`, never before filtering, joining, grouping, or sorting.
- **`ORDER BY` expressions** — sort keys are now arbitrary expressions (`ORDER BY price * quantity DESC`), in addition to columns, aliases, and aggregates.
- **Planner/optimizer integration** — new expressions participate in safe predicate pushdown and grouped validation; hash keys still come only from `column = column`, with new expression conjuncts evaluated as residual predicates; `BETWEEN` gets the range-scan access path; `EXPLAIN` renders `DISTINCT`, `OFFSET`, computed projections, and the full `CASE`/`LIKE`/`IN`/`BETWEEN` text; `EXPLAIN ANALYZE` reports `duplicates_removed`, `rows_discarded`, and post-`LIMIT` rows. Selectivity is refined only where statistics exist (`IS NULL`, `IN`, `BETWEEN`, comparisons); `LIKE`, `CASE`, and arithmetic keep the conservative default.
- **Tests** — `tests/test_expressions.py` (parser precedence, AST shape, truth tables, context coverage), `tests/test_language_queries.py` (planning, EXPLAIN, index equivalence, LEFT JOIN placement, hash/nested-loop equivalence, persistence, transactions), `tests/test_expression_fuzz.py` (deterministic randomized differential testing against an independent reference interpreter and LIKE matcher), and `tests/test_client_server_language.py` (real TCP sockets, embedded-vs-client equivalence, error codes, transaction isolation).
- **Benchmarks** — `benchmarks/benchmark_language.py` (arithmetic projection/filtering, LIKE on small vs large text, IN, BETWEEN with and without an index, DISTINCT low/high cardinality, CASE, OFFSET, combined filter/group/sort, and join regression), recorded in `BENCHMARKS.md`.

### Changed
- Version metadata is `0.9.0` (`pyproject.toml`, `aaysql.__version__`, `storage.ENGINE_VERSION`).
- Computed `SELECT` projections are no longer elided from `EXPLAIN`; a single-table query is only elided when every selected item is a plain column reference.
- `DEFAULT` accepts signed literals and `TRUE`/`FALSE`, matching the expression parser.

### Notes
- The `.aaydb` format remains **version 3** and the network protocol remains **version 1**; neither storage nor WAL behaviour changed.
- Index support remains deliberately narrow: `IS NULL`, `LIKE`, `IN`, `NOT BETWEEN`, arithmetic, and `CASE` predicates use a table scan, and the planner never rewrites an arbitrary expression into an indexed comparison.
- No optimizer or storage claims are attached to this release: the language expansion does not make existing queries faster, and no measured speedup is claimed.

## [0.8.0] — 2026-11-11

### Added
- **Database server (`src/aaysql/server.py`)** — `python -m aaysql.server --db university.aaydb` serves one `Engine` over TCP:
  - one engine per server, opened (with normal WAL recovery) before the listening socket is bound;
  - one thread per connection, but **all** engine access serialized through a single `TransactionCoordinator` lock that is released before responses are written;
  - server-generated session IDs, per-session request counters, and idle/read/write timeouts;
  - connection cap (`--max-clients`, default 32) with `TOO_MANY_CONNECTIONS` rejection, 1 MiB frame cap (`--max-frame-size`), and bounded graceful shutdown (`--shutdown-timeout`);
  - exclusive `<db>.server.lock` via `fcntl.flock` / `msvcrt.locking`, released by the OS if the process dies;
  - SIGINT/SIGTERM shutdown that stops accepting, unwinds sessions (rolling back uncommitted work), then closes the engine and releases the lock.
- **Session-owned transactions** — the engine has one `TransactionManager`, so the coordinator decides which session may use it:
  - `BEGIN` from another session while a transaction is open is rejected with a retryable `TRANSACTION_BUSY` **before** the engine is called;
  - non-owners cannot execute `SELECT`, `EXPLAIN`, DML or DDL while a transaction is open, so uncommitted rows and schema changes are never observable;
  - ownership is re-derived from the authoritative `engine.in_transaction` after each statement, never parsed from SQL text;
  - a transaction owner that disconnects is rolled back automatically and ownership is released;
  - an ambiguous `COMMIT` (`AmbiguousCommitError`), `needs_recovery`, or poisoned WAL marks the whole service unavailable: every session receives `RECOVERY_REQUIRED`, the ownership tombstone is kept, and the WAL is preserved for reopen recovery — a rollback is never claimed.
- **Versioned network protocol (`src/aaysql/protocol.py`)** — `PROTOCOL_VERSION = 1`, independent of the package version:
  - 4-byte big-endian length + UTF-8 JSON frames, with incremental reads across partial headers/payloads and multiple frames per `recv`;
  - frame lengths validated before allocation; malformed JSON, invalid UTF-8, truncated frames and oversized frames produce structured errors;
  - `hello`, `ping`, `execute` operations with request-ID validation and per-session duplicate detection;
  - stable error codes: `INVALID_REQUEST`, `UNSUPPORTED_PROTOCOL_VERSION`, `AUTHENTICATION_FAILED`, `SQL_PARSE_ERROR`, `SQL_EXECUTION_ERROR`, `CONSTRAINT_ERROR`, `TRANSACTION_BUSY`, `TRANSACTION_ERROR`, `REQUEST_TOO_LARGE`, `DATABASE_UNAVAILABLE`, `RECOVERY_REQUIRED`, `CONNECTION_TIMEOUT`, `INTERNAL_ERROR`, plus `UNKNOWN_OPERATION`, `TOO_MANY_CONNECTIONS` and `HELLO_REQUIRED`;
  - unknown exceptions are logged server-side with a traceback and returned to clients as a generic `INTERNAL_ERROR` — tracebacks are never sent.
- **Python client (`src/aaysql/client.py`)** — `AayushSQLClient` with `connect`/`execute`/`ping`/`close`, context-manager support, optional token and TLS (CA file, hostname, client certificate), configurable connect/read timeouts and frame limit, and an exception hierarchy (`AayushSQLClientError`, `ConnectionError`, `TLSHandshakeError`, `AuthenticationError`, `ProtocolError`, `ServerError`, `RequestTimeoutError`, `ConnectionLostError`). `execute()` returns the same result structure as `Engine.execute()`.
- **No automatic retries** — `ConnectionLostError` and `RequestTimeoutError` set `outcome_unknown = True`; the client closes the socket and never replays a mutation or `COMMIT`, because the server may already have committed it.
- **Authentication and TLS** — optional bearer token from `AAYUSQL_AUTH_TOKEN`, compared with `hmac.compare_digest`, never logged, never in argv, never written to `.aaydb`; repeated failures close the connection. Binding to a non-loopback host is refused unless a token **and** TLS certificate/key are configured. TLS uses the standard `ssl` module with a TLS 1.2 minimum and optional `CERT_REQUIRED` client certificates; the client verifies certificates and hostnames by default.
- **Tests (`tests/test_client_server.py`, 57 tests)** — real TCP sockets and ephemeral ports: handshake and protocol errors, malformed JSON / invalid UTF-8 / partial frames / oversized frames, request-ID validation, unknown operations, persistent connections and pipelining, client-side timeout, authentication and TLS (certificate generation skipped when `openssl` is unavailable), server lifecycle (new/existing database, interrupted-WAL recovery, second server refused, lock release after clean exit and after `os._exit`, graceful shutdown, shutdown during a request), and transaction scenarios A–G (two autocommit clients, session-owned transaction with `TRANSACTION_BUSY`, rollback visibility, disconnect rollback, competing `BEGIN`, injected ambiguous `COMMIT` with recovery on restart, concurrent correlated requests).
- **Benchmarks (`benchmarks/benchmark_server.py`)** — stdlib-only, real sockets, warm-up plus median/p95: connection handshake, ping RTT, point and scan `SELECT`, autocommit `INSERT` (includes WAL `fsync`), explicit transaction, connection churn, a ~0.46 MB result transfer, and 4 concurrent clients under serialized execution. Results in `BENCHMARKS.md`.
- **`Engine.wal_poisoned`** — small public read-only property so the server can detect a poisoned WAL without reaching into private state.
- **Docs**: new [`SERVER_ARCHITECTURE.md`](SERVER_ARCHITECTURE.md) (ownership model, protocol, security, shutdown, limitations); README server/client examples; architecture cross-reference.

### Changed
- Version metadata is `0.8.0` (`pyproject.toml`, `aaysql.__version__`, `storage.ENGINE_VERSION`).
- Transaction-control failures (for example `COMMIT` with no active transaction) now map to `TRANSACTION_ERROR` instead of `SQL_EXECUTION_ERROR` on the wire; other execution errors are unchanged.

### Notes
- The `.aaydb` format remains **version 3**; no migration and no storage/WAL behaviour change. Server and embedded use share the same engine and the same on-disk format.
- Concurrency is still single-writer: many connections, serialized statements, at most one open explicit transaction. No MVCC, snapshot isolation, concurrent writes, or parallel query execution.
- While a server owns a database, using the CLI or an embedded `Engine` on that file from another process is unsupported (the lock only prevents another *server*).
- TLS tests generate ephemeral self-signed certificates with `openssl` and skip when it is unavailable; no keys or certificates are committed.

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
