# Architecture

AayushSQL v0.2.1 separates front-end language processing, schema/execution validation, and persistent storage into focused modules under `src/aaysql/`.

```text
               AayushSQL CLI (cli.py)
                        |
                  Engine (engine.py)
                 /      |          \
        Lexer (lexer.py)|      JsonFileStorage (storage.py)
               |        |              |
        Parser (parser.py)        .aaydb (JSON)
               |        |
             AST (ast.py)
                        |
                Executor (executor.py)
                        |
             Database / Table (storage.py)
```

## Module Responsibilities

- **`token.py` & `lexer.py`**: Tokenizes raw SQL text into typed tokens (`PREFIX` for `AAYUSH`, identifiers, numbers, strings, operators, punctuation) while stripping whitespace and `--` line comments.
- **`ast.py` & `parser.py`**: Enforces the mandatory single `AAYUSH` prefix, validates supported column types and explicit `=` in `UPDATE` assignments, and parses statements into immutable AST dataclasses (`CreateTable`, `DropTable`, `Insert`, `Select`, `Update`, `Delete`).
- **`storage.py`**:
  - Centralizes data-type and schema validation (`TYPE_MAP`, `SUPPORTED_TYPES`, `validate_type_name`, `validate_column_def`, `validate_table_schema`, `coerce_value`).
  - Defines `Table` and `Database` in-memory models.
  - Defines `JsonFileStorage`, which serializes and deserializes versioned `.aaydb` JSON files (`format="aaysql"`, `format_version=1`).
  - Applies the same schema, default, cell-type, and constraint (`PRIMARY KEY` / `UNIQUE` / `NOT NULL`) validation during startup loading, raising `StorageError` on corruption.
  - Writes atomically using a temporary file in the target directory, `flush()` + `os.fsync()`, and `os.replace()`.
- **`executor.py`**: Executes AST statements against `Database`. Multi-row `INSERT` and `UPDATE` statements validate constraints atomically before mutating in-memory rows. Incompatible runtime comparisons or sorts are caught at the comparison boundary and raised as `ExecutionError` chained from `TypeError`; unexpected internal errors are not swallowed.
- **`engine.py`**: Coordinates lexing, parsing, execution, and persistence. Supports both in-memory (`Engine()` / `Engine(":memory:")`) and persistent (`Engine(db_path)`) modes. Saves only on successful mutating statements, skips disk writes on `SELECT`, and restores a pre-mutation snapshot if `save()` fails.
- **`cli.py` & `formatting.py`**: Provides `argparse`-based CLI (`--db`, `--memory`, `--version`, optional script file), quote/comment-aware statement splitting, ASCII table formatting, fail-fast script execution (`stop_on_error=True`, returning exit code `1` on failure), and error-resilient interactive REPL execution.
- **`errors.py`**: Explicit exception hierarchy rooted at `AayushSQLError`:
  - `LexerError`
  - `ParserError`
  - `ExecutionError` (`ConstraintError` subclass)
  - `StorageError`

## CI Pipeline

`.github/workflows/tests.yml` runs `python -m pytest -v` on pushes and pull requests to `main` across Python `3.10`, `3.11`, `3.12`, and `3.13` with read-only `contents` permissions.

## Evolution Path

Future milestones (v0.3–v0.7) can introduce a query planner (`planner.py`), B-tree indexes (`index.py`), transaction logs (`transaction.py`), and page-based storage without altering the `Lexer` / `Parser` / `Engine` boundary.
