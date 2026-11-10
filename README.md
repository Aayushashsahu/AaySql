# AayushSQL

A custom SQL-compatible relational database engine written from scratch in Python, with one mandatory syntactic requirement:

```sql
AAYUSH SELECT * FROM users;
```

Every top-level SQL statement must begin with `AAYUSH`. Ordinary SQL without the prefix is rejected.

**Dumb idea. Serious implementation.**

## Current Version

**v0.3.0** — Relational Query Engine adding `INNER JOIN`, `LEFT JOIN`, table aliases, qualified column references, aggregate functions (`COUNT`, `SUM`, `AVG`, `MIN`, `MAX`), `GROUP BY`, and `HAVING` on top of the hardened v0.2.1 persistent engine.

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
- Aggregation & grouping:
  - `COUNT(*)`, `COUNT(column)`, `SUM(column)`, `AVG(column)`, `MIN(column)`, `MAX(column)`.
  - `GROUP BY` across one or more columns (including `NULL` grouping keys) with SQL grouping correctness validation.
  - `HAVING` group-level filtering after aggregation.
- Query clauses: column selection, `SELECT *`, `AS` aliases, `WHERE`, comparison operators (`=`, `!=`, `<>`, `<`, `<=`, `>`, `>=`), boolean logic (`AND`, `OR`, `NOT`), parentheses, `ORDER BY ... [ASC|DESC]` (columns, aliases, or aggregates), `LIMIT`.
- Data types & constraints: `INT`/`INTEGER`, `TEXT`/`VARCHAR`, `FLOAT`/`DOUBLE`, `BOOL`/`BOOLEAN`; `PRIMARY KEY`, `NOT NULL`, `UNIQUE`, `DEFAULT`.
- Persistent storage:
  - Versioned JSON database files (`.aaydb`, format version `1`) with atomic replacement (`os.replace`) and startup corruption/schema validation.
  - Read-only `SELECT` queries (including joins and aggregations) never modify persisted files.
  - Configurable database file path (`--db`) and in-memory mode (`--memory` / `--db :memory:`).
- Fail-fast script runner (non-zero exit code on error) and error-resilient interactive REPL.
- Automated GitHub Actions CI across Python 3.10–3.13.

### Planned (Future Roadmap)
- **v0.4** — Indexing: B-tree / B+ tree indexes, index-assisted execution, `EXPLAIN`, query plan inspection.
- **v0.5** — Transactions: `BEGIN`, `COMMIT`, `ROLLBACK`, multi-statement atomicity.
- **v0.6** — Reliability: write-ahead logging (WAL), crash recovery, page-based storage.
- **v0.7** — Server capabilities: client/server network protocol and concurrent connections.

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

- Nested-loop join execution only (no B-tree indexes or hash joins yet).
- `RIGHT JOIN`, `FULL JOIN`, `CROSS JOIN`, and qualified wildcards (`u.*`) are not supported.
- `GROUP BY` supports simple column references (qualified or unqualified), not arbitrary expressions; nested aggregate functions (`SUM(COUNT(*))`) are not supported.
- No subqueries, `UNION`, `IS NULL` syntax, arithmetic expressions, `ALTER TABLE`, or multi-statement transactions (`BEGIN`/`COMMIT`/`ROLLBACK`) yet.
- Single-process, single-writer execution with full-database JSON rewrite on mutations.
- Table and column names are case-sensitive at execution time.
