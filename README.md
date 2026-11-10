# AayushSQL

A custom SQL-compatible relational database engine written from scratch in Python, with one mandatory syntactic requirement:

```sql
AAYUSH SELECT * FROM users;
```

Every top-level SQL statement must begin with `AAYUSH`. Ordinary SQL without the prefix is rejected.

**Dumb idea. Serious implementation.**

## Current Version

**v0.5.0** — Transactions: `AAYUSH BEGIN` / `COMMIT` / `ROLLBACK` with deferred persistence, atomic multi-statement commits, per-statement rollback on failure, index-aware snapshots, automatic rollback on engine/CLI exit, and a CLI warning for uncommitted work — on top of the v0.4.0 indexing engine.

## Why the `AAYUSH` Prefix Exists

The `AAYUSH` prefix is the intentional joke of the language: SQL keywords (`SELECT`, `INSERT`, `UPDATE`, `DELETE`, `CREATE TABLE`, `WHERE`, `JOIN`, `GROUP BY`, `HAVING`, `ORDER BY`, `LIMIT`, etc.) keep their standard meanings, while `AAYUSH` acts as a mandatory top-level statement prefix.

```text
SELECT * FROM users;
      ↓
AAYUSH SELECT * FROM users;
```

Everything behind the parser — relational joins, column resolution, grouping, aggregation, constraints, atomic mutations, and persistent storage — follows real database engineering principles.

## Implemented vs. Planned Features

### Implemented (v0.3.0)
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
  - Query planner reuses an index for single-table `=`, `>`, `>=`, `<`, `<=` predicates and falls back to a table scan otherwise. The full `WHERE` clause is always re-applied and candidate rows keep table order, so indexed and unindexed results are identical.
  - `EXPLAIN SELECT ...` reports the real access path (`INDEX SCAN` / `TABLE SCAN`); `EXPLAIN ANALYZE` adds measured rows scanned, rows filtered, rows returned, index used, and execution time.
  - `.aaydb` format version 2 stores index definitions; version 1 files load unchanged and upgrade on the next write.
  - Reproducible benchmarks in `benchmarks/benchmark_index.py`, with recorded measurements in `BENCHMARKS.md`.
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
  - Each statement stays atomic inside a transaction, and a failed `COMMIT` restores the pre-`BEGIN` state and raises `StorageError` instead of reporting success.
  - Closing the engine or exiting the CLI with an active transaction rolls it back and prints a warning to `stderr`. Autocommit behavior outside transactions is unchanged.
- Persistent storage:
  - Versioned JSON database files (`.aaydb`, format version `1`) with atomic replacement (`os.replace`) and startup corruption/schema validation.
  - Read-only `SELECT` queries (including joins and aggregations) never modify persisted files.
  - Configurable database file path (`--db`) and in-memory mode (`--memory` / `--db :memory:`).
- Fail-fast script runner (non-zero exit code on error) and error-resilient interactive REPL.
- Automated GitHub Actions CI across Python 3.10–3.13.

### Planned (Future Roadmap)
- **v0.6** — Reliability: write-ahead logging (WAL), checkpoints, crash recovery.
- **v0.7** — Query optimizer: hash joins, cardinality statistics, filter pushdown, profiling.
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

## Testing & Continuous Integration

Run the full test suite from the project root:

```bash
python -m pytest -v
```

GitHub Actions CI (`.github/workflows/tests.yml`) runs `python -m pytest -v` on pushes and pull requests targeting `main` across Python `3.10`, `3.11`, `3.12`, and `3.13`.

## Known Limitations

- Nested-loop join execution only; index plans cover single-table predicates, not joins or hash joins.
- Single-column, non-unique indexes only; no optimizer statistics, and `EXPLAIN` executes the query it explains.
- `EXPLAIN` supports `SELECT` statements only and reports metrics for the driving access path; nested-loop inner scans are not counted in `ROWS SCANNED`.
- Duplicate output labels in one `SELECT` list are rejected with `ExecutionError` (result rows are keyed by label), so distinct aliases are required.
- `RIGHT JOIN`, `FULL JOIN`, `CROSS JOIN`, and qualified wildcards (`u.*`) are not supported.
- `GROUP BY` supports simple column references (qualified or unqualified), not arbitrary expressions; nested aggregate functions (`SUM(COUNT(*))`) are not supported.
- No subqueries, `UNION`, `IS NULL` syntax, arithmetic expressions, `ALTER TABLE`, or multi-statement transactions (`BEGIN`/`COMMIT`/`ROLLBACK`) yet.
- Single-process, single-writer execution with full-database JSON rewrite on mutations.
- Transactions are single-process and single-writer: no nested transactions, savepoints, isolation levels, or concurrent connections; `COMMIT` persists by rewriting the whole `.aaydb` file.
- Table and column names are case-sensitive at execution time.
