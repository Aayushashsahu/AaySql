# AayushSQL

A custom SQL-compatible relational database engine written from scratch in Python, with one mandatory syntactic requirement:

```sql
AAYUSH SELECT * FROM users;
```

Every top-level SQL statement must begin with `AAYUSH`. Ordinary SQL without the prefix is rejected.

**Dumb idea. Serious implementation.**

## Current Version

**v1.0.0 — First Stable Release** (`.aaydb` format v3, network protocol v1).

AayushSQL v1.0.0 establishes a frozen, documented compatibility contract, security boundary, offline backup and restore (`aaysql-backup`), cross-platform CI (Ubuntu/Windows/macOS across Python 3.10–3.13), and wheel/source packaging on top of the full v0.1.0–v0.9.0 engine: relational queries, expressions (`IS NULL`, `LIKE`, `IN`, `BETWEEN`, arithmetic, `CASE`, `DISTINCT`, `OFFSET`), B+ tree indexes, cost-based optimizer and hash joins, transactions, WAL crash recovery, and TCP server with token auth and TLS.

**Multiple connections, one writer.** The server adds accessibility, not concurrency: there is no MVCC, no snapshot isolation, and no parallel query execution.

## Why the `AAYUSH` Prefix Exists

The `AAYUSH` prefix is the intentional joke of the language: SQL keywords (`SELECT`, `INSERT`, `UPDATE`, `DELETE`, `CREATE TABLE`, `WHERE`, `JOIN`, `GROUP BY`, `HAVING`, `ORDER BY`, `LIMIT`, etc.) keep their standard meanings, while `AAYUSH` acts as a mandatory top-level statement prefix.

```text
SELECT * FROM users;
      ↓
AAYUSH SELECT * FROM users;
```

Everything behind the parser — relational joins, column resolution, grouping, aggregation, constraints, atomic mutations, WAL durability, and persistent storage — follows real database engineering principles.

## Implemented vs. Planned Features

### Implemented (v1.0.0)
- **SQL expression language** (`src/aaysql/expressions.py`, `src/aaysql/parser.py`):
  - One precedence-climbing parser for the whole expression grammar (`OR` < `AND` < `NOT` < comparisons/predicates < `+ -` < `* / %` < unary sign < primary), with parentheses overriding it. Numbers are lexed unsigned so subtraction and unary negation are unambiguous (`1-2` and `1 - 2` both mean minus).
  - One canonical evaluator used by every execution path (reference/embedded and plan-driven), with a shared AST traversal (`iter_expr_children`/`walk_expr`) that all validation, aggregate detection, grouped-expression checks, predicate placement, and EXPLAIN rendering go through — a new node type cannot silently bypass a rule.
  - `IS NULL` / `IS NOT NULL`: never `UNKNOWN`, and never implemented as an equality comparison.
  - `LIKE` / `NOT LIKE`: `%` and `_`, case-sensitive, compiled from a fully escaped and anchored pattern (no user-supplied regular expressions), with a bounded pattern cache.
  - `IN` / `NOT IN`: scalar membership with three-valued logic, including the `NULL`-in-list `UNKNOWN` case; empty lists and subqueries are rejected.
  - `BETWEEN` / `NOT BETWEEN`: inclusive both ends, `UNKNOWN`-preserving, and index-assisted as a range scan when bounds are literals.
  - Arithmetic `+ - * / %` and unary `+`/`-`: integer results stay integer, `/` is float, `%` follows Python floored semantics, `NULL` propagates, `BOOL`/`TEXT` operands, division/modulo by zero, and non-finite results are `ExecutionError`.
  - `CASE`: searched and simple forms, evaluated in order with only `TRUE` matching, nested `CASE`, `CASE` inside aggregates, and `CASE` in `UPDATE` still passing column coercion and constraints.
  - `SELECT DISTINCT`: whole-projected-row duplicate elimination after projection and before ordering, first-occurrence ordering preserved, `NULL`s equal, type-tagged keys, and `ORDER BY` required to reference the projected row.
  - `OFFSET` (with or without `LIMIT`): applied after ordering and before limiting, with non-negative-integer validation.
  - `TRUE` / `FALSE` literals and signed literals in `INSERT`/`DEFAULT`.
- **Optimizer integration**: new expressions participate in predicate pushdown, grouped validation, hash-join eligibility (only `column = column` keys; everything else is a residual predicate), and `EXPLAIN`/`EXPLAIN ANALYZE` rendering (`DISTINCT`, `OFFSET`, computed projections, `CASE`/`LIKE`/`IN`/`BETWEEN` text). Selectivity is refined only where statistics exist; `LIKE`/`CASE`/arithmetic keep the honest default.
- **Client/server**: the new syntax works through `AayushSQLClient` unchanged — the protocol stays version `1` because SQL is still a string, and integration tests compare every feature against the embedded engine over real sockets.
- **Tests**: `tests/test_expressions.py` (parser, precedence, truth tables), `tests/test_language_queries.py` (planning, EXPLAIN, index equivalence, joins, persistence), `tests/test_expression_fuzz.py` (deterministic randomized differential testing against an independent reference interpreter), `tests/test_client_server_language.py` (real TCP).
- **Benchmarks**: `benchmarks/benchmark_language.py` with recorded results in `BENCHMARKS.md`.
- **Server** (`src/aaysql/server.py`, entry point `python -m aaysql.server`): owns exactly one `Engine`, one session per connection, all engine access serialized through a single coordinator lock, session-owned transactions with immediate retryable `TRANSACTION_BUSY` for other sessions, automatic rollback when a transaction owner disconnects, and `RECOVERY_REQUIRED` for every session after an ambiguous commit.
- **Versioned network protocol** (`src/aaysql/protocol.py`, protocol version `1`): 4-byte big-endian length + UTF-8 JSON frames, 1 MiB default frame limit validated before allocation, `hello`/`ping`/`execute` operations, request IDs with duplicate detection, and stable error codes (`INVALID_REQUEST`, `UNSUPPORTED_PROTOCOL_VERSION`, `AUTHENTICATION_FAILED`, `SQL_PARSE_ERROR`, `SQL_EXECUTION_ERROR`, `CONSTRAINT_ERROR`, `TRANSACTION_BUSY`, `TRANSACTION_ERROR`, `REQUEST_TOO_LARGE`, `DATABASE_UNAVAILABLE`, `RECOVERY_REQUIRED`, `CONNECTION_TIMEOUT`, `INTERNAL_ERROR`, `TOO_MANY_CONNECTIONS`, `HELLO_REQUIRED`, `UNKNOWN_OPERATION`). Tracebacks are never sent to clients.
- **Python client** (`src/aaysql/client.py`): `AayushSQLClient` with `connect`/`execute`/`ping`/`close`, context manager, optional token and TLS, configurable timeouts, a documented exception hierarchy, and **no automatic retries** — an interrupted request reports `outcome_unknown`.
- **Security**: loopback bind by default; non-loopback binds require both `AAYUSQL_AUTH_TOKEN` and TLS; tokens compared with `hmac.compare_digest` and never logged or stored; TLS certificate and hostname verification on by default; optional client-certificate verification.
- **Operational**: exclusive per-database process lock released by the OS on crash, connection/ frame/ timeout limits, bounded graceful shutdown that rolls back uncommitted work, and integration tests over real sockets.
- Mandatory `AAYUSH` top-level statement prefix (rejects missing or duplicate `AAYUSH`).
- DDL & DML: `CREATE TABLE`, `DROP TABLE`, `INSERT INTO ... VALUES ...`, `SELECT ... FROM ...`, `UPDATE ... SET ...`, `DELETE FROM ...`.
- Relational joins & source binding:
  - `INNER JOIN ... ON ...` and `LEFT JOIN ... ON ...` across two or more tables.
  - Table aliases (`FROM users AS u` and `FROM users u`).
  - Qualified column references (`users.id`, `u.id`, `orders.product`) with ambiguity detection for unqualified shared column names.
- Indexing & query planning (v0.4.0):
  - From-scratch B+ tree (`src/aaysql/btree.py`): ordered linked leaves with range traversal, internal separator nodes, node splitting, borrowing/merging on delete, duplicate keys, and structural invariant validation.
  - `CREATE INDEX idx ON table (column)` / `DROP INDEX idx` for single-column, non-unique indexes. Definitions persist in `.aaydb`; trees are rebuilt from table rows on open.
  - Indexes stay synchronized with `INSERT`, `UPDATE`, and `DELETE`, are removed by `DROP TABLE`, and never replace table rows as the source of truth.
  - The cost-based planner matches an index to a `=`, `>`, `>=`, `<`, `<=` conjunct per source (and, since v0.9.0, to `column BETWEEN literal AND literal` as one inclusive range) and then decides between `INDEX SCAN` and `TABLE SCAN` by estimated cost. Pushed predicates are re-checked on every candidate and candidate rows keep table order, so indexed and unindexed results are identical.
  - Reproducible benchmarks in `benchmarks/benchmark_index.py` and `benchmarks/benchmark_optimizer.py`, with recorded measurements in `BENCHMARKS.md`.
- Query optimizer (v0.7.0):
  - Structured planning: a logical plan (bound sources, join order/type, predicate placement) is separate from the physical plan (`TABLE SCAN`, `INDEX SCAN`, `NESTED LOOP JOIN`, `HASH JOIN`, `FILTER`, `AGGREGATE`, `PROJECTION`, `SORT`, `LIMIT`). The executor runs the physical plan that was selected.
  - Hand-written hash join for equi-joins: multi-row buckets (duplicate keys produce every match), composite keys from conjunctions of equalities, `NULL` keys never match, and the full `ON` predicate is still evaluated on every candidate so residual predicates are exact. Non-equi, `OR`, and unsafe key extractions fall back to nested loop.
  - Deterministic ordering: every join algorithm emits rows in left-input order and then right-input order, exactly like the nested-loop reference, so unordered queries (and `LIMIT`) keep their previous results.
  - In-memory statistics (`src/aaysql/statistics.py`): exact row and `NULL` counts, distinct counts exact up to 4,096 values and bitmap-estimated beyond (bounded memory), and min/max when comparable. Derived from rows, never from indexes, and invalidated by every mutation, rollback, and reopen.
  - Safe predicate pushdown: only top-level `AND` conjuncts are pushed, `OR` is never split, predicates are never detached from their relations, right-side `WHERE` predicates of a `LEFT JOIN` stay after the join, right-only `ON` conjuncts are pushed into the right input, and left-only `ON` conjuncts are never pushed below a `LEFT JOIN`.
  - Cost model: documented relative constants for sequential rows, index seek/candidates/order restoration, hash build/probe/matches, and nested-loop comparisons, with a stated rationale (`WHY:`) for each hardware-free choice in `EXPLAIN`. Small joins keep the nested-loop reference.
  - `EXPLAIN` reports estimates (`~rows`) and never executes the statement or mutates state; `EXPLAIN ANALYZE` reports measured per-operator counters (rows in/out, rejected, index candidates, hash build/probe/bucket matches, nested-loop comparisons, aggregate/sort/limit counts, per-operator timing).
  - Equivalence suite (`tests/test_optimizer.py`) comparing every optimization against the forceable nested-loop/table-scan reference across duplicates, `NULL` keys, composite keys, residuals, joins with/without indexes, skew, empty and one-row inputs, and size ratios.
- Aggregation & grouping:
  - `COUNT(*)`, `COUNT(column)`, `SUM(column)`, `AVG(column)`, `MIN(column)`, `MAX(column)`.
  - `GROUP BY` across one or more columns (including `NULL` grouping keys) with SQL grouping correctness validation.
  - `HAVING` group-level filtering after aggregation.
- Query clauses: expression selection, `SELECT *`, `SELECT DISTINCT`, `AS` aliases, `WHERE`, comparison operators (`=`, `!=`, `<>`, `<`, `<=`, `>`, `>=`), predicates (`IS [NOT] NULL`, `[NOT] LIKE`, `[NOT] IN`, `[NOT] BETWEEN`), arithmetic (`+ - * / %`, unary `+`/`-`), `CASE WHEN ... THEN ... [ELSE ...] END`, boolean logic (`AND`, `OR`, `NOT`), parentheses, `ORDER BY ... [ASC|DESC]` (columns, aliases, aggregates, or expressions), `LIMIT n`, and `OFFSET m`.
- SQL `NULL` semantics with full three-valued logic: comparisons against `NULL` yield `UNKNOWN`, `UNKNOWN` propagates through `AND`/`OR`/`NOT`, and `WHERE` / `JOIN ON` / `HAVING` / `UPDATE` / `DELETE` predicates accept a row only on `TRUE`.
- Data types & constraints: `INT`/`INTEGER`, `TEXT`/`VARCHAR`, `FLOAT`/`DOUBLE`, `BOOL`/`BOOLEAN`; `PRIMARY KEY`, `NOT NULL`, `UNIQUE`, `DEFAULT`.
- Transactions (v0.5.0):
  - `AAYUSH BEGIN`, `AAYUSH COMMIT`, `AAYUSH ROLLBACK` for multi-statement transactions.
  - Mutations inside a transaction are visible to later statements but stay in memory until the single `COMMIT` save; `ROLLBACK` restores the pre-`BEGIN` snapshot, including DDL and index changes.
  - Each statement stays atomic inside a transaction.
  - Closing the engine or exiting the CLI with an active transaction rolls it back and prints a warning to `stderr`.
- Write-Ahead Log & Crash Recovery (v0.6.0, hardened in v0.6.1 — see [`WAL.md`](WAL.md)):
  - Adjacent newline-delimited JSON WAL (`<db>.wal`) with canonical SHA-256 checksums, monotonic LSNs, unique per-transaction IDs, and `BEGIN` / `WRITE` / `COMMIT` / `CHECKPOINT` records.
  - WAL `os.fsync()` commit durability precedes snapshot checkpointing for both explicit transactions and autocommit mutations (`INSERT`, `UPDATE`, `DELETE`, `CREATE/DROP TABLE`, `CREATE/DROP INDEX`).
  - Distinguished error semantics: pre-commit errors roll back cleanly, post-`COMMIT` checkpoint failures preserve the committed state and WAL without misreporting failure, and `COMMIT` `fsync` errors raise `AmbiguousCommitError` and require reopen recovery.
  - Crash-safe checkpoint ordering (`.tmp` write + `fsync` -> `os.replace` -> dir `fsync` -> `CHECKPOINT` record -> WAL truncation) and idempotent startup recovery.
  - Trailing torn/corrupt WAL records are safely ignored; mid-log corruption stops immediately without skipping forward.
  - Trailing-tail repair (v0.6.1): recovery tracks the exact byte boundary of the last valid record, truncates only the invalid tail, `fsync`s it, and only then appends `CHECKPOINT` — a corrupt tail can never become mid-log corruption after a crash.
  - Partial append hardening (v0.6.1): unbuffered `O_APPEND` writes with a write-all loop, an explicit append state machine, and WAL poisoning. A WAL that may contain torn bytes refuses further appends, and the engine blocks statements until reopen recovery.
  - Ambiguous `COMMIT` semantics (v0.6.1): a failed `COMMIT` `fsync` preserves the WAL, keeps memory unchanged, and raises `AmbiguousCommitError` instead of claiming a rollback; recovery decides from the actual record.
  - Directory `fsync` errors are classified (v0.6.1): unsupported operations (and Windows) fall back deliberately, while `EACCES`/`EIO`/`ENOSPC` propagate as `StorageError` and never silently claim durability.
  - `.aaydb` format version `3` (`wal_lsn`) with safe read-only compatibility and on-write migration for format v1 and v2 databases.
- Persistent storage:
  - Read-only `SELECT` queries (including joins and aggregations) never modify persisted files.
  - Configurable database file path (`--db`) and in-memory mode (`--memory` / `--db :memory:`).
- Fail-fast script runner (non-zero exit code on error) and error-resilient interactive REPL.
- Automated cross-platform GitHub Actions CI across Ubuntu, Windows, and macOS on Python 3.10–3.13, plus packaging and clean-venv install validation.

### Planned (Post-1.0 Roadmap)
- Candidate future work beyond v1.0.0: subqueries/`UNION`, expression or multi-column indexes, aggregate `DISTINCT`, window functions, and concurrent MVCC writers (the v1.0.0 model is strictly single-writer).

## Example SQL Expressions

### `NULL` filtering

```sql
AAYUSH SELECT * FROM users WHERE email IS NOT NULL;
```

### Pattern matching

```sql
AAYUSH SELECT * FROM users WHERE name LIKE 'A%';
AAYUSH SELECT * FROM users WHERE name NOT LIKE '%son';
```

### Membership

```sql
AAYUSH SELECT * FROM employees WHERE department IN ('Engineering', 'Research');
AAYUSH SELECT * FROM employees WHERE department NOT IN ('Sales', 'HR');
```

### Range

```sql
AAYUSH SELECT * FROM employees WHERE salary BETWEEN 30000 AND 60000;
```

### Numeric expressions

```sql
AAYUSH SELECT name, salary * 1.10 AS revised_salary FROM employees;
AAYUSH SELECT price * quantity AS total FROM order_items;
AAYUSH UPDATE accounts SET balance = balance - 100 WHERE id = 1;
```

### `CASE` classification

```sql
AAYUSH SELECT
    name,
    CASE
        WHEN score >= 90 THEN 'A'
        WHEN score >= 75 THEN 'B'
        WHEN score >= 60 THEN 'C'
        ELSE 'F'
    END AS grade
FROM students;
```

### `DISTINCT` and `OFFSET`

```sql
AAYUSH SELECT DISTINCT department
FROM employees
ORDER BY department
LIMIT 10 OFFSET 20;
```

### Expressions compose with grouping and joins

```sql
AAYUSH SELECT
    department,
    COUNT(*) AS headcount,
    SUM(CASE WHEN salary BETWEEN 50000 AND 90000 THEN salary ELSE 0 END) AS banded,
    AVG(salary) * 1.1 AS projected
FROM employees
WHERE name IS NOT NULL AND name NOT LIKE '%test%'
GROUP BY department
HAVING COUNT(*) > 1
ORDER BY headcount DESC, department
LIMIT 5 OFFSET 0;
```

## Example Relational Queries

### Multi-table `LEFT JOIN` with `GROUP BY`, `HAVING`, `ORDER BY`, and `LIMIT`

```sql
AAYUSH SELECT
    u.name AS customer,
    COUNT(o.id) AS order_count,
    SUM(o.price) AS total_spent
FROM users AS u
LEFT JOIN orders AS o ON u.id = o.user_id
GROUP BY u.id, u.name
HAVING COUNT(o.id) >= 1
ORDER BY total_spent DESC
LIMIT 5;
```

### `GROUP BY` with `WHERE`, `HAVING`, and aggregates

```sql
AAYUSH SELECT
    department,
    COUNT(*) AS employee_count,
    AVG(salary) AS average_salary
FROM employees
WHERE salary > 40000
GROUP BY department
HAVING AVG(salary) > 50000
ORDER BY average_salary DESC;
```

## Installation and Execution

Requires **Python 3.10–3.13** with **zero external runtime dependencies** (`pytest`, `build`, and `ruff` are declared under `[project.optional-dependencies] dev`).

### Option 1: Install from wheel (recommended)
```bash
pip install aaysql-1.0.0-py3-none-any.whl
aaysql --version          # aaysql 1.0.0
aaysql-server --help
aaysql-backup --version
```

### Option 2: Editable install from source
```bash
pip install -e ".[dev]"
aaysql --help
```

### Option 3: Run directly from the repository root

PowerShell:
```powershell
$env:PYTHONPATH="src"
python -m aaysql.cli
```

Bash / macOS / Linux:
```bash
PYTHONPATH=src python -m aaysql.cli
```

## Embedded Usage & CLI

```text
aaysql [--db PATH | --memory] [--version] [script]
```

- **Default persistent mode** (`./aaysql.aaydb`): `aaysql`
- **Custom database file**: `aaysql --db university.aaydb`
- **In-memory mode**: `aaysql --memory` or `aaysql --db :memory:`
- **Run a script file**: `aaysql --memory examples/demo.aay`

```python
from aaysql import Engine

with Engine("university.aaydb") as db:
    db.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL)")
    db.execute("AAYUSH INSERT INTO users VALUES (1, 'Aayush')")
    print(db.execute("AAYUSH SELECT * FROM users")["rows"])
```

### Script Fail-Fast & Exit-Code Behavior

- Script execution exits with `0` when all statements succeed.
- On the first `AayushSQLError`, the CLI prints `ERROR: ...` to `stderr`, halts before subsequent statements, preserves any earlier persisted mutations, and exits with `1`.
- Interactive REPL mode prints `ERROR: ...` and remains open for further commands.

## Server and Python Client

The embedded engine and the CLI still work exactly as before. The server is an
optional front end for external applications.

**One owner per database file.** A `.aaydb` file may be opened by exactly one
owner at a time: an embedded `Engine`, a server, `aaysql-backup`, or a restore.
Starting a server while an `Engine` holds the file (or the reverse) fails with
`DatabaseLockedError` / `DATABASE_LOCKED`; stop the existing owner first. Multiple
processes must never manipulate the same database file directly.

### Start the server

```bash
aaysql-server --db university.aaydb
# or: python -m aaysql.server --db university.aaydb
```

Defaults: `127.0.0.1:8765`, in-file database, 32 connections, 1 MiB frames.
Useful flags: `--host`, `--port`, `--memory`, `--max-clients`, `--max-frame-size`,
`--idle-timeout`, `--read-timeout`, `--write-timeout`, `--shutdown-timeout`,
`--tls-cert`, `--tls-key`, `--tls-client-ca`, `--log-level`.

### Connect from Python

```python
from aaysql.client import AayushSQLClient

with AayushSQLClient("127.0.0.1", 8765) as db:
    db.execute(
        "AAYUSH CREATE TABLE students "
        "(id INT PRIMARY KEY, name TEXT NOT NULL)"
    )

    db.execute(
        "AAYUSH INSERT INTO students VALUES "
        "(1, 'Aayush')"
    )

    result = db.execute(
        "AAYUSH SELECT * FROM students"
    )

    print(result["rows"])  # [{'id': 1, 'name': 'Aayush'}]
```

`execute()` returns the same result structure as `Engine.execute()`.

### Explicit transaction

```python
with AayushSQLClient("127.0.0.1", 8765) as db:
    db.execute("AAYUSH BEGIN")

    try:
        db.execute(
            "AAYUSH INSERT INTO students VALUES (2, 'Alex')"
        )
        db.execute("AAYUSH COMMIT")
    except Exception:
        # Only attempt rollback when the server confirms
        # the transaction remains active and the outcome
        # is not ambiguous.
        raise
```

### Multiple clients

A connection is not a transaction. Client A may hold an open transaction while
Client B stays connected; B's statements are rejected with
`TRANSACTION_BUSY` (`retryable: true`) until A commits or rolls back. B never
sees A's uncommitted rows, and B's statements are never folded into A's
transaction. If A disconnects mid-transaction, the server rolls it back
automatically.

```python
from aaysql.client import AayushSQLClient, ServerError

with AayushSQLClient("127.0.0.1", 8765) as db:
    try:
        db.execute("AAYUSH SELECT * FROM students")
    except ServerError as exc:
        if exc.code == "TRANSACTION_BUSY":
            ...  # someone else owns the transaction; back off and retry
```

### Authentication and TLS

```bash
# Token from the environment; never passed on the command line.
export AAYUSQL_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
aaysql-server --db university.aaydb                                 # loopback, no TLS needed
aaysql-server --db university.aaydb --host 0.0.0.0 \
    --tls-cert server-cert.pem --tls-key server-key.pem             # remote: token + TLS required
```

```python
with AayushSQLClient("db.example.com", 8765, token=token,
                     tls=True, ca_certs="ca.pem",
                     server_hostname="db.example.com") as db:
    ...
```

Binding to a non-loopback address without a token **and** TLS is refused at
startup. Certificate verification is never disabled. Certificate creation for
local development is documented in [`SERVER_ARCHITECTURE.md`](SERVER_ARCHITECTURE.md).

### Ambiguous requests are never retried

If the connection breaks or a request times out after the statement was sent,
the server may already have committed it. The client raises `ConnectionLostError`
or `RequestTimeoutError` with `outcome_unknown = True` and closes the socket
instead of replaying the statement. Reconnect and inspect the database to
determine the real state; never replay mutations or `COMMIT` automatically.

### Current concurrency limits

- Statements execute **serially** against the single engine; there are no
  concurrent writes, no MVCC, no snapshot isolation, and no parallel queries.
- Only one session may own a transaction at a time; other sessions are blocked
  with `TRANSACTION_BUSY` rather than being served stale reads.
- Only one owner may hold a database file: one server, one embedded `Engine`,
  one backup, or one restore. The canonical `<database>.server.lock` lease
  enforces it across processes and within a process, for every entry point. A
  configured `ServerConfig.lock_path` is an additional coordination lock, never
  a replacement for canonical ownership. Stop the owner before using the CLI, an
  embedded `Engine`, or `aaysql-backup` on that file. See
  [docs/OPERATIONS.md](docs/OPERATIONS.md#10-database-ownership-and-locking).
- The `.aaydb` format is version 3; network protocol is version 1.

## Offline Backup and Restore

`aaysql-backup` provides exclusive **offline** backups (stop any running server first):

```bash
aaysql-backup backup university.aaydb backups/university-2026-11-11
aaysql-backup verify backups/university-2026-11-11
aaysql-backup restore backups/university-2026-11-11 university-restored.aaydb
```

- Backups recover any pending WAL before reading, verify and record SHA-256 checksums in `manifest.json`, and stage atomically.
- Restoring validates the candidate copy before swapping and refuses to overwrite an existing database (or a target with unrecovered WAL data) without `--force`. See [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Testing & Continuous Integration

Run the full test suite from the project root:

```bash
python -m pytest -v
```

GitHub Actions CI (`.github/workflows/tests.yml`) runs across **Ubuntu, Windows, and macOS** on Python **3.10, 3.11, 3.12, and 3.13**, plus a wheel/sdist packaging and clean-venv install job.

## Documentation

- [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) — End-to-end tutorial (SQL, client/server, backup/restore)
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — Operational runbook, recovery, `RECOVERY_REQUIRED`, locking, troubleshooting
- [`COMPATIBILITY.md`](COMPATIBILITY.md) — Formal SQL, format v3, protocol v1, and Python API compatibility contract
- [`LANGUAGE_SPEC.md`](LANGUAGE_SPEC.md) — Full lexical rules and SQL grammar
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — Internal engine architecture
- [`WAL.md`](WAL.md) — Write-ahead log protocol, checksums, checkpointing, and crash recovery
- [`SERVER_ARCHITECTURE.md`](SERVER_ARCHITECTURE.md) — TCP server, coordinator lock, and session ownership
- [`SECURITY.md`](SECURITY.md) & [`THREAT_MODEL.md`](THREAT_MODEL.md) — Security boundary and threat model
- [`AUDIT.md`](AUDIT.md) — v1.0.0 stabilization audit findings and fixes
- [`RELEASE_BENCHMARKS.md`](RELEASE_BENCHMARKS.md) & [`BENCHMARKS.md`](BENCHMARKS.md) — Release and subsystem benchmark measurements
- [`CHANGELOG.md`](CHANGELOG.md) — Release history

## Known Limitations

- Join order is never rewritten: joins execute in the order written. Reordering an `INNER` chain would change the documented unordered-result order, so it is deliberately not implemented; `WHERE`/`ON` conjuncts on a single input are pushed down instead.
- Statistics are recomputed lazily by a full table pass when a table's revision changes; there is no background refresh or persisted histogram. Distinct counts beyond 4,096 values are estimates.
- Only equality conjuncts between the two join inputs enable a hash join. There is no hash aggregate, index-nested-loop join, sort-merge join, or parallel execution.
- Single-column, non-unique indexes only; index matching does not use composite indexes, and `UPDATE`/`DELETE` row selection is still a table scan.
- `EXPLAIN` supports `SELECT` statements only. Plain `EXPLAIN` does not execute the query (it does read rows to compute statistics); `EXPLAIN ANALYZE` does execute it, read-only.
- Duplicate output labels in one `SELECT` list are rejected with `ExecutionError` (result rows are keyed by label), so distinct aliases are required.
- `RIGHT JOIN`, `FULL JOIN`, `CROSS JOIN`, and qualified wildcards (`u.*`) are not supported.
- `GROUP BY` supports simple column references (qualified or unqualified), not arbitrary expressions; nested aggregate functions (`SUM(COUNT(*))`) are not supported.
- No subqueries (including `IN (SELECT ...)`), `UNION`, `EXISTS`, `WITH`, window functions, `CAST`, `COALESCE`, string concatenation (`||`), `LIKE ... ESCAPE`, or `ALTER TABLE`. `IS TRUE` / `IS FALSE` / `IS DISTINCT FROM` are not implemented — only `IS NULL` and `IS NOT NULL`.
- `LIKE` is case-sensitive and has no collation or escape clause. Patterns never execute as regular expressions.
- Only comparisons against literals and `BETWEEN` between two literals can use an index. `IS NULL`, `LIKE`, `IN`, `NOT BETWEEN`, arithmetic, and `CASE` predicates always use a table scan; the planner does not rewrite an arbitrary expression into an indexed comparison.
- Statistics refine selectivity only for single-column comparison, `IS NULL`, `IN`, and `BETWEEN` with literal bounds. `LIKE`, `CASE`, and arithmetic predicates use the documented conservative default; there are no histograms or pattern statistics.
- Arithmetic accepts only `INT` and `FLOAT` (never `BOOL`/`TEXT`); division and modulo by zero raise `ExecutionError`, and non-finite results are refused rather than stored.
- `DISTINCT` orders through the projected row only: with `DISTINCT`, `ORDER BY` expressions must be selected (alias, selected expression, or a selected column for `DISTINCT *`).
- Single-writer execution: no nested transactions, savepoints, isolation levels, MVCC, snapshot isolation, or concurrent writes. The server accepts many connections but serializes every statement behind one execution lock, and only one session may hold an explicit transaction at a time.
- Backup/restore is offline only (`aaysql-backup`); live/online backup is not supported.
- WAL durability guarantees target single-process crash recovery; directory `fsync` is executed where supported (POSIX). Windows cannot open directories through the CRT, so directory-entry durability relies on NTFS metadata behaviour; this fallback is selected by platform, not by swallowing errors. Genuine directory `fsync` failures (`EACCES`, `EIO`, `ENOSPC`, ...) are surfaced as `StorageError`. Hardware/firmware write-cache failures or OS filesystem corruption remain outside process-crash guarantees (see [`WAL.md`](WAL.md)).
- Table and column names are case-sensitive at execution time.


