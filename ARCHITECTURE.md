# Architecture

AayushSQL v0.6.0 separates the language front end, query planning, relational execution, index maintenance, DML/constraint execution, Write-Ahead Logging (WAL), and persistent snapshot storage into focused modules under `src/aaysql/`.

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
                  \                 /
              Query pipeline (query.py)
```

## Query Execution Pipeline (`src/aaysql/query.py`)

`SELECT` statements follow a structured multi-stage relational pipeline. Planning runs before execution and only chooses the access path for the driving table:

```text
Planning (index scan or table scan)  [planner.py]
    -> Source Binding (FROM + aliases)
    -> JOIN Stage (nested-loop INNER / LEFT JOIN)
    -> WHERE Stage (row-level filtering, TRUE only)
    -> GROUP BY Stage (grouping + NULL key handling)
    -> Aggregate Stage (COUNT, SUM, AVG, MIN, MAX)
    -> HAVING Stage (group-level filtering, TRUE only)
    -> Projection Stage (columns, aliases, qualified names)
    -> ORDER BY Stage (columns, aliases, aggregates)
    -> LIMIT Stage
```

Three-valued logic is centralized in `query.py` (`sql_and`, `sql_or`, `sql_not`, `is_sql_true`): `NULL` comparisons produce `UNKNOWN`, `UNKNOWN` propagates through `AND`/`OR`/`NOT`, and row filters admit only `TRUE`. A single recursive `eval_expr()` serves row-level and group-level evaluation so the two paths cannot diverge.

## Query Planning (`src/aaysql/planner.py`)

- `plan_select()` returns a `QueryPlan` describing the access path actually executed: `INDEX SCAN` or `TABLE SCAN`.
- Index eligibility is deliberately narrow: single-table `SELECT`, a top-level `AND` conjunct of the form `column <op> literal` (or `literal <op> column`) with `<op>` in `=`, `>`, `>=`, `<`, `<=`, a matching single-column index, and a literal whose type is compatible with the column. `OR`, `NOT`, `!=`, and `NULL` literals fall back to a table scan.
- The index only produces candidate rows. The complete original `WHERE` predicate is always re-applied, so results are identical with and without an index.
- Candidate rows are returned in table order, so an unordered `SELECT ... LIMIT` returns the same rows either way.
- Indexes are never pushed across joins; joined queries always report `TABLE SCAN` plus `JOIN: NESTED LOOP`.
- `EXPLAIN` renders the real plan; `EXPLAIN ANALYZE` appends measured counters (rows scanned from the access path, rows filtered, rows returned, execution time). No estimated or fabricated statistics are reported.

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
- Every persistent mutation writes `begin`, `write`, and `commit` records with canonical SHA-256 checksums to `<db_path>.wal` and calls `os.fsync()` before checkpointing.
- Checkpointing writes a temporary `.aaydb` snapshot, calls `os.fsync()`, atomically swaps via `os.replace()`, calls `fsync_dir()` where supported, appends a `checkpoint` record, and only then truncates the WAL.
- On startup `load()`, `parse_wal()` validates records, ignores torn/checksum-corrupt trailing records, rejects mid-log corruption without skipping forward, and idempotently replays the newest committed image where `commit_lsn > snapshot.wal_lsn`. See [`WAL.md`](WAL.md) for the complete protocol specification.

## Module Responsibilities

- **`token.py` & `lexer.py`**: Tokenizes SQL into typed tokens (`PREFIX` for `AAYUSH`, identifiers, numbers, strings, operators, punctuation).
- **`ast.py` & `parser.py`**: Enforce the single `AAYUSH` prefix and parse DDL/DML, `CREATE INDEX`, `DROP INDEX`, `EXPLAIN [ANALYZE]`, and relational queries into immutable AST nodes.
- **`query.py`**: Source binding, identifier resolution, `JOIN`, `WHERE`, grouping, aggregates, `HAVING`, projection, `ORDER BY`, and `LIMIT`; owns three-valued logic and duplicate output-label rejection.
- **`planner.py`**: Chooses and renders the access path; fetches candidate rows for index scans.
- **`index.py` / `btree.py`**: Secondary index maintenance, lookups, range scans, and B+ tree invariants.
- **`executor.py`**: Executes DDL/DML and `EXPLAIN`, maintains indexes on mutations, and delegates `SELECT` to the query pipeline.
- **`wal.py`**: WAL record schema, SHA-256 checksums, strict validation, append-only `fsync` writes, checkpoint/truncation helpers, and internal crash-test hooks.
- **`storage.py`**: Schema/type/constraint validation, `Table`/`Database` models, v1/v2->v3 migration, crash-safe checkpointing, and startup WAL recovery.
- **`engine.py` & `transaction.py`**: Coordinate lexing, parsing, execution, and persistence. Autocommit and explicit `COMMIT` persist through WAL + checkpoint; ambiguous fsync errors enforce reopen recovery.
- **`cli.py` & `formatting.py`**: `--db`, `--memory`, `--version`, fail-fast scripts, error-resilient REPL, ASCII tables, plan text output.
- **`errors.py`**: Exception hierarchy rooted at `AayushSQLError` (`LexerError`, `ParserError`, `ExecutionError`, `ConstraintError`, `StorageError`, `AmbiguousCommitError`).

