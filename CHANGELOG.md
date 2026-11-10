# Changelog

All notable changes to AayushSQL are documented in this file.

## [0.2.0] — 2026-11-10

### Added
- **Persistent JSON storage (`JsonFileStorage`)**: Saves database schemas, column constraints, and row data to versioned `.aaydb` JSON files (`format="aaysql"`, `format_version=1`).
- **Atomic writes**: Serializes changes to a temporary file in the target directory, flushes and calls `os.fsync()`, and swaps via `os.replace()` to prevent partially written database files.
- **Corruption & schema validation**: Validates loaded database files for JSON syntax, format envelope, version compatibility, column metadata, cell data types, and `PRIMARY KEY` / `UNIQUE` / `NOT NULL` constraints, raising `StorageError` on malformed data.
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
