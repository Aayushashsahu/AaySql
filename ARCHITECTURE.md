# Architecture

AayushSQL v0.4.0 separates the language front end, query planning, relational execution, index maintenance, DML/constraint execution, and persistent storage into focused modules under `src/aaysql/`.

```text
                  AayushSQL CLI (cli.py)
                           |
                     Engine (engine.py)
                   /        |             \
       Lexer (lexer.py)     |        JsonFileStorage (storage.py)
                  |         |                |
        Parser (parser.py)  |          .aaydb (JSON, format v2)
                  |         |                ^
              AST (ast.py)  |                |
                            |         Index catalog (index.py)
                 Executor (executor.py)      |
                  /                 \     B+ tree (btree.py)
     Planner (planner.py)      Database / Table (storage.py)
                  \                 /
              Query pipeline (query.py)
```

## Query Execution Pipeline (`src/aaysql/query.py`)

`SELECT` statements follow a structured multi-stage relational pipeline. Planning runs before execution and only chooses the access path for the driving table:

```text
Planning (index scan or table scan)  [planner.py]
    -> Source Binding (FROM + aliases)
    -> JOIN Stage (nested-loop INNER / LEFT JOIN)
    -> WHERE Stage (row-level filtering, TRUE only)
    -> GROUP BY Stage (grouping + NULL key handling)
    -> Aggregate Stage (COUNT, SUM, AVG, MIN, MAX)
    -> HAVING Stage (group-level filtering, TRUE only)
    -> Projection Stage (columns, aliases, qualified names)
    -> ORDER BY Stage (columns, aliases, aggregates)
    -> LIMIT Stage
```

Three-valued logic is centralized in `query.py` (`sql_and`, `sql_or`, `sql_not`, `is_sql_true`): `NULL` comparisons produce `UNKNOWN`, `UNKNOWN` propagates through `AND`/`OR`/`NOT`, and row filters admit only `TRUE`. A single recursive `eval_expr()` serves row-level and group-level evaluation so the two paths cannot diverge.

## Query Planning (`src/aaysql/planner.py`)

- `plan_select()` returns a `QueryPlan` describing the access path actually executed: `INDEX SCAN` or `TABLE SCAN`.
- Index eligibility is deliberately narrow: single-table `SELECT`, a top-level `AND` conjunct of the form `column <op> literal` (or `literal <op> column`) with `<op>` in `=`, `>`, `>=`, `<`, `<=`, a matching single-column index, and a literal whose type is compatible with the column. `OR`, `NOT`, `!=`, and `NULL` literals fall back to a table scan.
- The index only produces candidate rows. The complete original `WHERE` predicate is always re-applied, so results are identical with and without an index.
- Candidate rows are returned in table order, so an unordered `SELECT ... LIMIT` returns the same rows either way.
- Indexes are never pushed across joins; joined queries always report `TABLE SCAN` plus `JOIN: NESTED LOOP`.
- `EXPLAIN` renders the real plan; `EXPLAIN ANALYZE` appends measured counters (rows scanned from the access path, rows filtered, rows returned, execution time). No estimated or fabricated statistics are reported.

## Indexing (`src/aaysql/index.py`, `src/aaysql/btree.py`)

- `BPlusTree` is implemented from scratch: internal separator nodes, ordered leaf nodes linked for range scans, node splitting on insert, borrowing/merging on delete, duplicate key support (physical `(key, sequence)` entries), empty-tree handling, and `validate()` invariant checking.
- `Index` is a single-column index binding a name, table, and column to a `BPlusTree`. `NULL` values are not indexed because comparisons against `NULL` never evaluate to `TRUE`. Each index also tracks its rows' table positions so index scans preserve table row order.
- Table rows remain authoritative. Indexes can always be rebuilt from rows, which is what happens when a database file is opened.
- `INSERT`, `UPDATE`, and `DELETE` maintain indexes; `DROP TABLE` removes that table's indexes. Failed statements validate before mutating, and a failed persistence save restores a snapshot whose index trees are rebuilt from the restored rows.

## Storage (`src/aaysql/storage.py`)

- `.aaydb` files are JSON with `format="aaysql"`. Format **version 2** adds an `indexes` mapping of index definitions (`{"name": {"table": ..., "column": ...}}`). Trees are never serialized; they are rebuilt and validated on load.
- Format **version 1** files (no indexes) remain readable and upgrade to version 2 on the next write. Unknown versions are rejected without modifying the file.
- Load validates table schemas, row types, constraints, and index definitions (name syntax, referenced table and column); corrupted index metadata raises `StorageError` and leaves the original file untouched.
- Writes stay atomic: serialize to a temporary file in the target directory, `fsync`, then `os.replace`.

## Module Responsibilities

- **`token.py` & `lexer.py`**: Tokenizes SQL into typed tokens (`PREFIX` for `AAYUSH`, identifiers, numbers, strings, operators, punctuation).
- **`ast.py` & `parser.py`**: Enforce the single `AAYUSH` prefix and parse DDL/DML, `CREATE INDEX`, `DROP INDEX`, `EXPLAIN [ANALYZE]`, and relational queries into immutable AST nodes.
- **`query.py`**: Source binding, identifier resolution, `JOIN`, `WHERE`, grouping, aggregates, `HAVING`, projection, `ORDER BY`, and `LIMIT`; owns three-valued logic and duplicate output-label rejection.
- **`planner.py`**: Chooses and renders the access path; fetches candidate rows for index scans.
- **`index.py` / `btree.py`**: Secondary index maintenance, lookups, range scans, and B+ tree invariants.
- **`executor.py`**: Executes DDL/DML and `EXPLAIN`, maintains indexes on mutations, and delegates `SELECT` to the query pipeline.
- **`storage.py`**: Schema/type/constraint validation, `Table`/`Database` models, index catalog persistence and migration, atomic JSON storage.
- **`engine.py`**: Coordinates lexing, parsing, execution, and persistence (`save` only on successful mutating statements, rollback on save failure).
- **`cli.py` & `formatting.py`**: `--db`, `--memory`, `--version`, fail-fast scripts, error-resilient REPL, ASCII tables, plan text output.
- **`errors.py`**: Exception hierarchy rooted at `AayushSQLError` (`LexerError`, `ParserError`, `ExecutionError`, `ConstraintError`, `StorageError`).
