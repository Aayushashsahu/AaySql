# Architecture

AayushSQL v0.3.0 separates front-end language processing, relational query execution, DML/constraint execution, and persistent storage into focused modules under `src/aaysql/`.

```text
               AayushSQL CLI (cli.py)
                        |
                  Engine (engine.py)
                 /      |          \
        Lexer (lexer.py)|      JsonFileStorage (storage.py)
               |        |              |
        Parser (parser.py)        .aaydb (JSON, v1)
               |        |
             AST (ast.py)
                        |
                Executor (executor.py)
               /                 \
    Query Pipeline (query.py)   Database / Table (storage.py)
```

## Query Execution Pipeline (`src/aaysql/query.py`)

`SELECT` statements follow a structured multi-stage relational pipeline in `execute_select()`:

```text
Source Binding (FROM + aliases)
    -> JOIN Stage (nested-loop INNER / LEFT JOIN)
    -> WHERE Stage (row-level filtering)
    -> GROUP BY Stage (grouping + NULL key handling)
    -> Aggregate Stage (COUNT, SUM, AVG, MIN, MAX)
    -> HAVING Stage (group-level filtering)
    -> Projection Stage (columns, aliases, qualified names)
    -> ORDER BY Stage (columns, aliases, aggregates)
    -> LIMIT Stage
```

## Module Responsibilities

- **`token.py` & `lexer.py`**: Tokenizes SQL text into typed tokens (`PREFIX` for `AAYUSH`, identifiers, numbers, strings, operators, punctuation) while stripping whitespace and `--` comments.
- **`ast.py` & `parser.py`**: Enforces the mandatory single `AAYUSH` prefix and parses DDL/DML and relational queries into immutable AST dataclasses (`Select`, `JoinClause`, `AggregateCall`, `Identifier` with optional `qualifier`, etc.).
- **`query.py`**:
  - Implements `SourceBinding`, `resolve_identifier()` (qualified/unqualified resolution and ambiguity detection), and a single recursive evaluator (`eval_expr()`, exposed as `eval_row_expr()` for row-level use) shared by `JOIN ON`, `WHERE`, `SELECT`, `ORDER BY`, `HAVING`, and DML filters.
  - Implements SQL three-valued logic (`sql_and`, `sql_or`, `sql_not`, `is_sql_true`): `NULL` comparisons yield `UNKNOWN`, `UNKNOWN` propagates through `AND`/`OR`/`NOT`, and row filters admit only `TRUE`.
  - Validates `JOIN ON`, `WHERE`, `GROUP BY`, `HAVING`, `SELECT`, and `ORDER BY` expressions against active source schemas before executing.
  - Enforces SQL grouping rules (rejects ungrouped, non-aggregated columns in `SELECT`, `HAVING`, and `ORDER BY`).
  - Executes `INNER JOIN` and `LEFT JOIN` via nested loops over source-keyed row contexts (`dict[ref, dict[col, val]]`) to avoid column collisions across joined tables.
  - Rejects duplicate `SELECT` output labels (`_validate_output_labels`) before scanning rows, so no selected value is ever overwritten in the label-keyed result rows.
- **`executor.py`**: Executes `CREATE TABLE`, `DROP TABLE`, `INSERT`, `UPDATE`, and `DELETE`, reuses `query.py` expression evaluation, and delegates `SELECT` to `execute_select()`.
- **`storage.py`**: Centralizes column/schema validation (`validate_type_name`, `validate_column_def`, `validate_table_schema`, `coerce_value`), `Table` / `Database` models, and atomic JSON file persistence (`JsonFileStorage`, format version `1`).
- **`engine.py`**: Coordinates lexing, parsing, execution, and persistence (`save` only on successful mutating statements, rollback on save failure).
- **`cli.py` & `formatting.py`**: Provides `--db`, `--memory`, `--version`, fail-fast script execution, error-resilient REPL, and ASCII table formatting.
- **`errors.py`**: Exception hierarchy rooted at `AayushSQLError` (`LexerError`, `ParserError`, `ExecutionError`, `ConstraintError`, `StorageError`).
