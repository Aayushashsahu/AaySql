# AayushSQL

A custom SQL-compatible relational database engine written from scratch in Python, with one mandatory syntactic requirement:

```sql
AAYUSH SELECT * FROM users;
```

Every top-level SQL statement must begin with `AAYUSH`. Ordinary SQL without the prefix is rejected.

**Dumb idea. Serious implementation.**

## Current Version

**v0.2.1** — Engine hardening, strict parser/schema validation, fail-fast script exit codes, normalized runtime errors, and GitHub Actions CI, built on top of v0.2.0 persistent storage.

## Why the `AAYUSH` Prefix Exists

The `AAYUSH` prefix is the intentional joke of the language: SQL keywords (`SELECT`, `INSERT`, `UPDATE`, `DELETE`, `CREATE TABLE`, `WHERE`, `ORDER BY`, `LIMIT`, etc.) keep their standard meanings, while `AAYUSH` acts as a mandatory top-level statement prefix.

```text
SELECT * FROM users;
      ↓
AAYUSH SELECT * FROM users;
```

Everything behind the parser — constraints, validation, atomic mutations, and persistent storage — follows real database engineering principles.

## Implemented vs. Planned Features

### Implemented (v0.2.1)
- Mandatory `AAYUSH` top-level statement prefix (rejects missing or duplicate `AAYUSH`).
- DDL & DML: `CREATE TABLE`, `DROP TABLE`, `INSERT INTO ... VALUES ...`, `SELECT ... FROM ...`, `UPDATE ... SET ...`, `DELETE FROM ...`.
- Strict `UPDATE` assignment grammar requiring `=` in every `SET col = value` assignment.
- Query clauses: column selection, `SELECT *`, `AS` aliases, `WHERE`, comparison operators (`=`, `!=`, `<>`, `<`, `<=`, `>`, `>=`), boolean logic (`AND`, `OR`, `NOT`), parentheses, `ORDER BY ... [ASC|DESC]`, `LIMIT`.
- Data types (strictly validated on `CREATE TABLE` and database load): `INT` / `INTEGER`, `TEXT` / `VARCHAR`, `FLOAT` / `DOUBLE`, `BOOL` / `BOOLEAN`.
- Constraints: `PRIMARY KEY` (single-column, unique and non-null), `NOT NULL`, `UNIQUE`, `DEFAULT` (validated against column type and nullability).
- Persistent storage:
  - Versioned JSON database files (`.aaydb`) with schemas, constraints, and rows.
  - Automatic persistence after every successful mutation.
  - Temporary-file writes with `fsync` and atomic replacement (`os.replace`).
  - No disk writes for read-only `SELECT` queries or failed statements.
  - Corruption/malformed-file detection raising explicit `StorageError`s.
  - Configurable database file path (`--db`) and temporary in-memory mode (`--memory` / `--db :memory:`).
- Interactive REPL (resilient to query errors) and fail-fast script runner (returns non-zero exit code on failure).
- Normalized runtime error hierarchy (`AayushSQLError`) with exception chaining for invalid runtime comparisons.
- Automated GitHub Actions CI across Python 3.10–3.13.

### Planned (Future Roadmap)
- **v0.3** — Relational capabilities: `INNER JOIN`, `LEFT JOIN`, `GROUP BY`, `HAVING`, aggregate functions.
- **v0.4** — Indexing: B-tree / B+ tree indexes, `EXPLAIN`, query plan inspection.
- **v0.5** — Transactions: `BEGIN`, `COMMIT`, `ROLLBACK`, multi-statement atomicity.
- **v0.6** — Reliability: write-ahead logging (WAL), crash recovery, page-based storage.
- **v0.7** — Server capabilities: client/server network protocol and concurrent connections.

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

- **Default persistent mode** (uses `./aaysql.aaydb` in the current working directory):
  ```bash
  python -m aaysql.cli
  ```
- **Custom database file**:
  ```bash
  python -m aaysql.cli --db university.aaydb
  ```
- **In-memory mode** (temporary session, nothing written to disk):
  ```bash
  python -m aaysql.cli --memory
  # or:
  python -m aaysql.cli --db :memory:
  ```
- **Run a script file**:
  ```bash
  python -m aaysql.cli --memory examples/demo.aay
  python -m aaysql.cli --db university.aaydb examples/demo.aay
  ```

### Script Fail-Fast & Exit-Code Behavior

- When executing a script file, statements run sequentially in auto-persist mode.
- If all statements succeed, the CLI exits with status `0`.
- On the first `AayushSQLError` (lexer, parser, execution, constraint, or storage error), the CLI prints `ERROR: ...` to `stderr`, halts before subsequent statements, preserves any earlier persisted mutations, and exits with status `1`.
- In interactive REPL mode, query errors print `ERROR: ...` and keep the session open for further commands.

### Multi-session Persistence Example

**Session 1:**
```powershell
$env:PYTHONPATH="src"
python -m aaysql.cli --db university.aaydb
```
```sql
AAYUSH CREATE TABLE users (
    id INT PRIMARY KEY,
    name TEXT NOT NULL,
    age INT
);
AAYUSH INSERT INTO users VALUES (1, 'Aayush', 19);
AAYUSH INSERT INTO users VALUES (2, 'Alex', 20);
exit
```

**Session 2:**
```powershell
$env:PYTHONPATH="src"
python -m aaysql.cli --db university.aaydb
```
```sql
AAYUSH SELECT * FROM users;
```
```text
+----+--------+-----+
| id | name   | age |
+----+--------+-----+
| 1  | Aayush | 19  |
| 2  | Alex   | 20  |
+----+--------+-----+
(2 rows)
```

## Database File Behavior

- If the target `--db` file does not exist yet, AayushSQL starts with an empty database and creates the file on the first successful mutating statement (`CREATE TABLE`, `DROP TABLE`, `INSERT`, `UPDATE`, `DELETE`).
- Read-only `SELECT` queries and failed statements never modify the database file.
- Writes are serialized to a temporary file in the same directory, flushed and `fsync`ed, and swapped in via `os.replace`.
- If an existing database file is empty, unreadable, invalid JSON, or fails schema/type/constraint validation, AayushSQL refuses to load it with a `StorageError` rather than silently overwriting user data.

## Testing & Continuous Integration

Run the full test suite from the project root:

```bash
python -m pytest -v
```

GitHub Actions CI (`.github/workflows/tests.yml`) runs `python -m pytest -v` on pushes and pull requests targeting `main` across Python `3.10`, `3.11`, `3.12`, and `3.13`.

## Known Limitations

- Single-process, single-writer execution (no file locking or concurrent multi-process access).
- Full-database JSON rewrite on each mutation (no WAL or page-based storage yet).
- Single-column `PRIMARY KEY` only; no foreign keys, `ALTER TABLE`, `JOIN`, or `GROUP BY` yet.
- Table and column names are case-sensitive at execution time.
- Expressions in `WHERE` and `SELECT` support literals, column identifiers, comparisons, and boolean logic (`AND`/`OR`/`NOT`), not arithmetic expressions or `IS NULL` syntax yet.
