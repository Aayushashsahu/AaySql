# Changelog

All notable changes to AayushSQL are documented in this file.

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
