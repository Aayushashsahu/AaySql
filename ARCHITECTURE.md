# Architecture

AayushSQL v0.8.0 separates the language front end, logical/physical query planning, statistics, relational execution, index maintenance, DML/constraint execution, Write-Ahead Logging (WAL), persistent snapshot storage, and the client/server layer into focused modules under `src/aaysql/`. The embedded engine is unchanged: the server is a front end over one `Engine` instance.

```text
                  AayushSQL CLI (cli.py)
                           |
                     Engine (engine.py)
                   /        |             \
       Lexer (lexer.py)     |        JsonFileStorage (storage.py)
                  |         |            /              \
        Parser (parser.py)  |     WalManager (wal.py)   .aaydb (JSON, format v3)
                  |         |          |                     ^
              AST (ast.py)  |    <db>.wal (NDJSON)           |
                            |                        Index catalog (index.py)
                 Executor (executor.py)                      |
                  /                 \                    B+ tree (btree.py)
     Planner (planner.py)      Database / Table (storage.py)
            |        \                /
   Statistics    Execution pipeline (execution.py)
 (statistics.py)          /
              Expression helpers (query.py)
```

## Query Planning & Execution (`src/aaysql/planner.py`, `src/aaysql/execution.py`)

Planning is split into an explicitly separate logical and physical stage:

```text
Logical planning            [planner.py: build_logical_plan]
  source binding (FROM + aliases)
  join order/type as written, ON conjuncts split on top-level AND
  WHERE conjuncts assigned to the earliest provably equivalent stage
  (pushed into a scan, or kept as a post-join FILTER)
        |
Physical planning           [planner.py: physical_plan]
  per-source access path: TABLE SCAN vs INDEX SCAN (cost model)
  per-join algorithm: NESTED LOOP vs HASH (cost model)
  wrappers: AGGREGATE -> PROJECTION -> SORT -> LIMIT
  every node carries estimated rows/cost and a WHY note
        |
Plan execution             [execution.py]
  scans -> joins -> post-join FILTER -> GROUP BY/HAVING -> projection -> ORDER BY -> LIMIT
```

- **Logical plan**: a `LogicalPlan` records bound sources, `JoinSpec`s whose order and type are exactly as written, the assigned conjuncts per source (`pushed`) and the conjuncts that must stay after the join (`post_filters`). Predicates are flattened only along top-level `AND`; `OR` is never split. A conjunct is pushed into a scan only when it references exactly that source and that source is not the nullable side of a `LEFT JOIN`. For `LEFT JOIN`s, right-only `ON` conjuncts are pushed into the right input (a row that fails them can never match), while left-only `ON` conjuncts and right-side `WHERE` conjuncts stay above the join.
- **Physical plan**: `PlanNode` operators are `TABLE SCAN`, `INDEX SCAN`, `JOIN` (`HASH`/`NESTED LOOP`), `FILTER`, `AGGREGATE`, `PROJECTION`, `SORT`, `LIMIT`. Each node stores `estimated_rows`/`estimated_cost` from statistics plus a `why` rationale. The executor runs exactly this tree: the chosen access path and join algorithm are the ones that execute, not a description of a different code path.
- **Statistics** (`statistics.py`) come from table rows, never from indexes: exact row and NULL counts, distinct counts exact up to 4,096 values and bitmap-estimated beyond, and min/max only when values are comparable. `Table.revision` is bumped by every row mutation; the cache entry is validated against the live table object and revision, and snapshots/rollbacks replace the whole `Database`, so a stale estimate cannot survive a mutation.
- **Cost model**: costs are abstract units with documented relative constants (`COST_SEQ_ROW`, `COST_INDEX_ROW`, `COST_INDEX_SORT`, `COST_HASH_BUILD`, `COST_HASH_PROBE`, `COST_NL_COMPARE`). The index cost includes restoring table order, so a very wide range can lose to a sequential scan; the hash cost includes build, probe, and estimated matches. Constants were calibrated against `benchmarks/benchmark_optimizer.py`. Small inputs keep the nested-loop reference (`HASH_MIN_INPUT_ROWS`).
- **Hash join**: buckets are lists, so duplicate keys produce every match; keys are tuples of equality-conjunct columns, so composite keys work; a `NULL` in any key column never matches (SQL `UNKNOWN`); the full `ON` predicate is still evaluated on every candidate, so residual predicates are exact. Output order is left-input order, then right-input order, identical to nested loop.
- **Row order is unchanged from v0.6.x.** The hash join is an algorithmic substitution, not a reordering: same rows, same order. Join reordering is deliberately not implemented (see Known Limitations).
- `EXPLAIN` renders the plan and never executes the statement; `EXPLAIN ANALYZE` executes it and appends measured per-operator counters. Figures prefixed with `~` are estimates and the plan text says so.
- `plan_select(..., force_join_algorithm=..., force_access_path=...)` keeps the v0.6.x nested-loop/table-scan path forceable for equivalence tests and benchmarks. There is no SQL syntax for it.

Three-valued logic is centralized in `query.py` (`sql_and`, `sql_or`, `sql_not`, `is_sql_true`): `NULL` comparisons produce `UNKNOWN`, `UNKNOWN` propagates through `AND`/`OR`/`NOT`, and row filters admit only `TRUE`. A single recursive `eval_expr()` serves row-level and group-level evaluation so the two paths cannot diverge.

## Transactions (`src/aaysql/transaction.py`, `src/aaysql/engine.py`)

- `TransactionManager` tracks the single optional transaction of an engine instance. `BEGIN` captures a deep snapshot of the database (rows and index trees); statements mutate memory only; `COMMIT` delegates to durable WAL commit + checkpoint; `ROLLBACK` reinstalls the snapshot.
- Each mutating statement inside a transaction also snapshots and restores on failure, so a failed statement never leaves partial rows or index entries.
- Pre-commit persistence errors restore the pre-`BEGIN` snapshot and raise `StorageError`. Once WAL `COMMIT` `os.fsync()` succeeds, the commit is durable even if later checkpointing fails; ambiguous `fsync` errors raise `AmbiguousCommitError` and block further writes until reopened for recovery.
- Closing the engine (or exiting the CLI) with an active transaction rolls it back; the CLI warns on `stderr`, so uncommitted work is never silently persisted or discarded.
- Deliberately single-process and single-writer: no nested transactions, savepoints, isolation levels, or concurrent writers.

## Indexing (`src/aaysql/index.py`, `src/aaysql/btree.py`)

- `BPlusTree` is implemented from scratch: internal separator nodes, ordered leaf nodes linked for range scans, node splitting on insert, borrowing/merging on delete, duplicate key support (physical `(key, sequence)` entries), empty-tree handling, and `validate()` invariant checking.
- `Index` is a single-column index binding a name, table, and column to a `BPlusTree`. `NULL` values are not indexed because comparisons against `NULL` never evaluate to `TRUE`. Each index also tracks its rows' table positions so index scans preserve table row order.
- Table rows remain authoritative. Indexes can always be rebuilt from rows, which is what happens when a database file is opened.
- `INSERT`, `UPDATE`, and `DELETE` maintain indexes; `DROP TABLE` removes that table's indexes. Failed statements validate before mutating, and a failed pre-commit persistence save restores a snapshot whose index trees are rebuilt from the restored rows.

## Storage & WAL Recovery (`src/aaysql/storage.py`, `src/aaysql/wal.py`)

- `.aaydb` files are JSON with `format="aaysql"`. Format **version 3** adds `"wal_lsn"` tracking the highest committed WAL LSN applied to the snapshot. Format **versions 1 and 2** remain readable (treated as `wal_lsn = 0`) and upgrade to version 3 on write or recovery checkpoint.
- Every persistent mutation writes `begin`, `write`, and `commit` records with canonical SHA-256 checksums to `<db_path>.wal` and calls `os.fsync()` before checkpointing. Appends are unbuffered `O_APPEND` writes completed by an `os.write()` write-all loop, so a short write is never treated as a complete record.
- `WalManager` runs an explicit append state machine (`begin_appended` -> `write_appended` -> `commit_attempted` -> `commit_durable`). Any append that may have left torn or uncertain bytes sets `poisoned`, which blocks further appends and engine statements until the database is reopened and recovery repairs the log. LSNs advance only after durable success.
- Checkpointing writes a temporary `.aaydb` snapshot, calls `os.fsync()`, atomically swaps via `os.replace()`, calls `fsync_dir()` where supported, appends a `checkpoint` record, and only then truncates the WAL. `fsync_dir()` distinguishes unsupported directory fsync (`EINVAL`/`ENOTSUP`/`EOPNOTSUPP`, plus Windows' deliberate platform fallback) from genuine failures (`EACCES`/`EIO`/`ENOSPC`), which raise `StorageError` instead of claiming durability.
- On startup `load()`, `parse_wal()` validates records, tracks `valid_bytes_end` for the last valid record, ignores torn/checksum-corrupt trailing records, rejects mid-log corruption without skipping forward, and idempotently replays the newest committed image where `commit_lsn > snapshot.wal_lsn`. Before appending a recovery `CHECKPOINT`, it truncates only the invalid trailing bytes and fsyncs, so a repaired tail cannot become mid-log corruption after a crash. See [`WAL.md`](WAL.md) for the complete protocol specification.

## Client/Server Layer (`src/aaysql/server.py`, `src/aaysql/protocol.py`, `src/aaysql/client.py`)

- **One engine per server.** `AayushSQLServer` opens the `Engine` before binding
  the socket, so WAL recovery completes before any request can arrive, and it
  never creates an engine per connection.
- **Connection threads, serialized execution.** `socketserver.ThreadingTCPServer`
  runs one thread per connection, but every statement is executed under the
  single `TransactionCoordinator` lock, released before the response is written.
- **Session-owned transactions.** The coordinator tracks the one session allowed
  to use the engine's single `TransactionManager`. Non-owners are rejected with a
  retryable `TRANSACTION_BUSY` before the engine is touched, so uncommitted rows
  are never observable and statements are never absorbed into another session's
  transaction. Ownership is re-derived from `engine.in_transaction` after each
  statement — the engine stays authoritative.
- **Disconnect rollback.** Session cleanup rolls back an active transaction and
  releases ownership. An ambiguous `COMMIT` instead marks the whole service
  `RECOVERY_REQUIRED`, keeps the ownership tombstone, and preserves the WAL for
  reopen recovery; no rollback is ever claimed.
- **Process lock.** `<db>.server.lock` is held with `fcntl.flock` (POSIX) or
  `msvcrt.locking` (Windows) from startup until shutdown, so two server processes
  cannot own one database and a crash releases the lock automatically.
- **Protocol.** 4-byte big-endian length + UTF-8 JSON frames, independently
  versioned (`PROTOCOL_VERSION = 1`), with `hello`/`ping`/`execute`, per-session
  request-ID validation, allocation-safe frame limits, stable error codes, and no
  tracebacks on the wire.
- **Transport security.** Loopback bind by default; non-loopback binds require
  both `AAYUSQL_AUTH_TOKEN` (compared with `hmac.compare_digest`, never logged or
  persisted) and TLS via the standard `ssl` module. The client verifies
  certificates and hostnames by default and never retries a request whose outcome
  may be unknown.

The complete contract, framing details, error codes and limitations are in
[`SERVER_ARCHITECTURE.md`](SERVER_ARCHITECTURE.md).

## Module Responsibilities

- **`token.py` & `lexer.py`**: Tokenizes SQL into typed tokens (`PREFIX` for `AAYUSH`, identifiers, numbers, strings, operators, punctuation).
- **`ast.py` & `parser.py`**: Enforce the single `AAYUSH` prefix and parse DDL/DML, `CREATE INDEX`, `DROP INDEX`, `EXPLAIN [ANALYZE]`, and relational queries into immutable AST nodes.
- **`query.py`**: Source binding, identifier resolution, three-valued logic, expression evaluation, grouping/aggregate helpers, and duplicate output-label rejection.
- **`planner.py`**: Logical planning (predicate placement), physical planning (access paths, join algorithms), the cost model, and plan/metric rendering. Imports no executor code.
- **`statistics.py`**: Bounded-memory per-table statistics and the revision-validated cache used by the cost model.
- **`execution.py`**: Runs the physical plan tree: scans, hash/nested-loop joins, post-join filters, aggregation, projection, sort, limit, and per-operator metrics.
- **`index.py` / `btree.py`**: Secondary index maintenance, lookups, range scans, and B+ tree invariants.
- **`executor.py`**: Executes DDL/DML and `EXPLAIN`, maintains indexes on mutations, and delegates `SELECT` to the query pipeline.
- **`wal.py`**: WAL record schema, SHA-256 checksums, strict validation, byte-accurate tail parsing and repair, the append state machine with write-all/poisoning, directory fsync error classification, checkpoint/truncation helpers, and internal crash-test hooks.
- **`storage.py`**: Schema/type/constraint validation, `Table`/`Database` models, v1/v2->v3 migration, crash-safe checkpointing, and startup WAL recovery.
- **`engine.py` & `transaction.py`**: Coordinate lexing, parsing, execution, and persistence. Autocommit and explicit `COMMIT` persist through WAL + checkpoint; ambiguous fsync errors and poisoned WALs enforce reopen recovery.
- **`server.py`**: One `Engine` per server, session model, transaction coordinator, TCP server, framed request dispatch, authentication, TLS configuration, process lock, bounded graceful shutdown, `python -m aaysql.server` CLI.
- **`protocol.py`**: Frame encoding/decoding and incremental reading, envelope and request validation, protocol version, capabilities, and the stable protocol error codes.
- **`client.py`**: `AayushSQLClient` (handshake, correlation, timeouts, TLS, token), the client exception hierarchy, and the no-automatic-retry contract for ambiguous requests.
- **`cli.py` & `formatting.py`**: `--db`, `--memory`, `--version`, fail-fast scripts, error-resilient REPL, ASCII tables, plan text output.
- **`errors.py`**: Exception hierarchy rooted at `AayushSQLError` (`LexerError`, `ParserError`, `ExecutionError`, `ConstraintError`, `StorageError`, `AmbiguousCommitError`).

