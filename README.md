# AayushSQL

A custom SQL-compatible relational database engine written from scratch in Python, with one mandatory syntactic requirement:

```sql
AAYUSH SELECT * FROM users;
```

Every top-level SQL statement must begin with `AAYUSH`. Ordinary SQL without the prefix is rejected.

**Dumb idea. Serious implementation.**

## Current Version

**v0.8.0** — Client/server architecture: a dedicated server process owns one `Engine`, multiple clients connect over TCP with a versioned length-prefixed JSON protocol, each connection gets a server-generated session, and explicit transactions are owned by exactly one session at a time. Execution against the engine is serialized; a second client's statements receive a retryable `TRANSACTION_BUSY` instead of seeing uncommitted data. Token authentication, TLS with client-side certificate verification, a loopback-by-default bind, a process-level database lock, and a Python client (`AayushSQLClient`) ship with it. Full contract in [`SERVER_ARCHITECTURE.md`](SERVER_ARCHITECTURE.md).

**Multiple connections, one writer.** v0.8.0 adds accessibility, not concurrency: there is no MVCC, no snapshot isolation, and no parallel query execution.

## Why the `AAYUSH` Prefix Exists

The `AAYUSH` prefix is the intentional joke of the language: SQL keywords (`SELECT`, `INSERT`, `UPDATE`, `DELETE`, `CREATE TABLE`, `WHERE`, `JOIN`, `GROUP BY`, `HAVING`, `ORDER BY`, `LIMIT`, etc.) keep their standard meanings, while `AAYUSH` acts as a mandatory top-level statement prefix.

```text
SELECT * FROM users;
      ↓
AAYUSH SELECT * FROM users;
```

Everything behind the parser — relational joins, column resolution, grouping, aggregation, constraints, atomic mutations, WAL durability, and persistent storage — follows real database engineering principles.

## Implemented vs. Planned Features

### Implemented (v0.8.0)
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
  - The cost-based planner matches an index to a `=`, `>`, `>=`, `<`, `<=` conjunct per source and then decides between `INDEX SCAN` and `TABLE SCAN` by estimated cost. Pushed predicates are re-checked on every candidate and candidate rows keep table order, so indexed and unindexed results are identical.
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
- Query clauses: column selection, `SELECT *`, `AS` aliases, `WHERE`, comparison operators (`=`, `!=`, `<>`, `<`, `<=`, `>`, `>=`), boolean logic (`AND`, `OR`, `NOT`), parentheses, `ORDER BY ... [ASC|DESC]` (columns, aliases, or aggregates), `LIMIT`.
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
- Automated GitHub Actions CI across Python 3.10–3.13.

### Planned (Future Roadmap)
- **v0.8** — Client/server: network protocol, server binary, authentication, localhost-by-default binding.
- **v0.9** — SQL language expansion: `IS NULL`, `LIKE`, `IN`, `BETWEEN`, `DISTINCT`, arithmetic expressions, `OFFSET`, `CASE`.

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

Requires **Python 3.10+** with no external runtime dependencies (`pytest` is declared under `[project.optional-dependencies] dev`).

### Option 1: Editable install
```bash
pip install -e ".[dev]"
aaysql --help
```

### Option 2: Run directly from the repository root

PowerShell:
```powershell
$env:PYTHONPATH="src"
python -m aaysql.cli
```

Bash / macOS / Linux:
```bash
PYTHONPATH=src python -m aaysql.cli
```

## CLI Usage

```text
python -m aaysql.cli [--db PATH | --memory] [--version] [script]
```

- **Default persistent mode** (`./aaysql.aaydb`): `python -m aaysql.cli`
- **Custom database file**: `python -m aaysql.cli --db university.aaydb`
- **In-memory mode**: `python -m aaysql.cli --memory` or `python -m aaysql.cli --db :memory:`
- **Run a script file**: `python -m aaysql.cli --memory examples/demo.aay`

### Script Fail-Fast & Exit-Code Behavior

- Script execution exits with `0` when all statements succeed.
- On the first `AayushSQLError`, the CLI prints `ERROR: ...` to `stderr`, halts before subsequent statements, preserves any earlier persisted mutations, and exits with `1`.
- Interactive REPL mode prints `ERROR: ...` and remains open for further commands.

## Server and Python Client

The embedded engine and the CLI still work exactly as before. The server is an
optional front end for external applications.

### Start the server

```bash
python -m aaysql.server --db university.aaydb
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
python -m aaysql.server --db university.aaydb                       # loopback, no TLS needed
python -m aaysql.server --db university.aaydb --host 0.0.0.0 \
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
- Only one server process may own a database file; the `.server.lock` enforces
  it. Stop the server before using the CLI or embedded `Engine` on that file.
- The `.aaydb` format is unchanged (version 3).

## Testing & Continuous Integration

Run the full test suite from the project root:

```bash
python -m pytest -v
```

GitHub Actions CI (`.github/workflows/tests.yml`) runs `python -m pytest -v` on pushes and pull requests targeting `main` across Python `3.10`, `3.11`, `3.12`, and `3.13`.

## Known Limitations

- Join order is never rewritten: joins execute in the order written. Reordering an `INNER` chain would change the documented unordered-result order, so it is deliberately not implemented; `WHERE`/`ON` conjuncts on a single input are pushed down instead.
- Statistics are recomputed lazily by a full table pass when a table's revision changes; there is no background refresh or persisted histogram. Distinct counts beyond 4,096 values are estimates.
- Only equality conjuncts between the two join inputs enable a hash join. There is no hash aggregate, index-nested-loop join, sort-merge join, or parallel execution.
- Single-column, non-unique indexes only; index matching does not use composite indexes, and `UPDATE`/`DELETE` row selection is still a table scan.
- `EXPLAIN` supports `SELECT` statements only. Plain `EXPLAIN` does not execute the query (it does read rows to compute statistics); `EXPLAIN ANALYZE` does execute it, read-only.
- Duplicate output labels in one `SELECT` list are rejected with `ExecutionError` (result rows are keyed by label), so distinct aliases are required.
- `RIGHT JOIN`, `FULL JOIN`, `CROSS JOIN`, and qualified wildcards (`u.*`) are not supported.
- `GROUP BY` supports simple column references (qualified or unqualified), not arbitrary expressions; nested aggregate functions (`SUM(COUNT(*))`) are not supported.
- No subqueries, `UNION`, `IS NULL` syntax, arithmetic expressions, or `ALTER TABLE`.
- Single-writer execution: no nested transactions, savepoints, isolation levels, MVCC, snapshot isolation, or concurrent writes. The v0.8.0 server accepts many connections but serializes every statement behind one execution lock, and only one session may hold an explicit transaction at a time.
- WAL durability guarantees target single-process crash recovery; directory `fsync` is executed where supported (POSIX). Windows cannot open directories through the CRT, so directory-entry durability relies on NTFS metadata behaviour; this fallback is selected by platform, not by swallowing errors. Genuine directory `fsync` failures (`EACCES`, `EIO`, `ENOSPC`, ...) are surfaced as `StorageError`. Hardware/firmware write-cache failures or OS filesystem corruption remain outside process-crash guarantees (see [`WAL.md`](WAL.md)).
- Table and column names are case-sensitive at execution time.

