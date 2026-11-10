# Architecture

AayushSQL v0.2.0 separates front-end language processing, execution, and persistent storage into focused modules under `src/aaysql/`.

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
- **`ast.py` & `parser.py`**: Enforces the mandatory single `AAYUSH` prefix (rejecting missing or duplicate prefixes) and parses statements into immutable AST dataclasses (`CreateTable`, `DropTable`, `Insert`, `Select`, `Update`, `Delete`).
- **`storage.py`**:
  - Defines `Table` and `Database` in-memory models and shared `coerce_value` type/nullability checking.
  - Defines `JsonFileStorage`, which serializes and deserializes versioned `.aaydb` JSON files (`format="aaysql"`, `format_version=1`).
  - Validates loaded metadata, column definitions, row schemas, cell types, and `PRIMARY KEY` / `UNIQUE` / `NOT NULL` constraints on startup, raising `StorageError` on corruption.
  - Writes atomically using a temporary file in the target directory, `flush()` + `os.fsync()`, and `os.replace()`.
- **`executor.py`**: Executes AST statements against `Database`. Multi-row `INSERT` and `UPDATE` statements validate constraints atomically against the full candidate state before mutating in-memory rows. Expression evaluation avoids `eval()` completely.
- **`engine.py`**: Coordinates lexing, parsing, execution, and persistence. Supports both in-memory (`Engine()` / `Engine(":memory:")`) and persistent (`Engine(db_path)`) modes. Saves only on successful mutating statements, skips disk writes on `SELECT`, and restores a pre-mutation snapshot if `save()` fails.
- **`cli.py` & `formatting.py`**: Provides `argparse`-based CLI (`--db`, `--memory`, optional script file, `--version`), quote/comment-aware statement splitting, ASCII table formatting, and clean startup/shutdown error handling.
- **`errors.py`**: Explicit exception hierarchy rooted at `AayushSQLError` (`LexerError`, `ParserError`, `ExecutionError`, `ConstraintError`, `StorageError`).

## Persistence Lifecycle (v0.2.0)

1. **Startup**: `Engine(db_path)` instantiates `JsonFileStorage(db_path)` and calls `load()`. Missing files initialize an empty `Database`; malformed or schema-invalid files raise `StorageError`.
2. **Execution**:
   - **Read-only (`SELECT`)**: Executed directly against `self.db`; no disk write occurs.
   - **Mutating (`CREATE TABLE`, `DROP TABLE`, `INSERT`, `UPDATE`, `DELETE`)**: Snapshot taken -> `Executor` validates and applies mutation -> `JsonFileStorage.save()` atomically writes the updated state. If execution or `save()` fails, persisted data on disk and in-memory state remain uncorrupted.
3. **Shutdown**: `Engine.close()` marks the engine closed; CLI ensures `close()` runs in a `finally` block.

## Evolution Path

Future milestones (v0.3–v0.7) can introduce a query planner (`planner.py`), B-tree indexes (`index.py`), transaction logs (`transaction.py`), and page-based storage without altering the `Lexer` / `Parser` / `Engine` boundary.
