# AayushSQL Language Specification (v0.5.0)

## 1. Overview

AayushSQL preserves conventional SQL syntax and semantics with one mandatory rule: **every top-level statement must begin with the `AAYUSH` keyword** (case-insensitive).

- Valid: `AAYUSH SELECT * FROM users;`
- Invalid (missing prefix): `SELECT * FROM users;` -> `ParserError`
- Invalid (duplicate prefix): `AAYUSH AAYUSH SELECT * FROM users;` -> `ParserError`

## 2. Lexical Elements

- **Comments**: Line comments starting with `--` until end-of-line.
- **Strings**: Single-quoted (`'hello'`) or double-quoted (`"hello"`). Doubled quotes escape embedded quotes (`'it''s'`).
- **Numbers**: Integer (`42`, `-7`) and floating-point (`3.14`, `-0.5`) literals.
- **Null literal**: `NULL` (case-insensitive).
- **Identifiers**: `[A-Za-z_][A-Za-z0-9_]*`. Table/column/alias identifiers are case-sensitive in storage and resolution; SQL keywords and aggregate function names are matched case-insensitively.
- **Qualified identifiers**: `table_or_alias.column_name`. Qualified wildcards (`u.*`) are not supported.
- **Statement terminator**: Optional trailing `;` on single statements; `;` separates statements in scripts and REPL buffers.

## 3. Data Types

Only the following column types (case-insensitive) are accepted by `CREATE TABLE` and database loading:

| Canonical Type | Accepted Aliases | Python / JSON Representation |
|---|---|---|
| `INT` | `INTEGER` | `int` (booleans rejected) |
| `TEXT` | `VARCHAR` | `str` |
| `FLOAT` | `DOUBLE` | `float` (integers coerced to `float`) |
| `BOOL` | `BOOLEAN` | `bool` (`0` and `1` coerced to `False`/`True`) |

## 4. Supported Statements

### `CREATE TABLE` / `DROP TABLE`
```sql
AAYUSH CREATE TABLE table_name (
    col1 INT PRIMARY KEY,
    col2 TEXT NOT NULL UNIQUE,
    col3 FLOAT DEFAULT 0.0,
    col4 TEXT DEFAULT NULL
);

AAYUSH DROP TABLE table_name;
```
Column constraints: `PRIMARY KEY` (at most one per table, unique and non-null), `NOT NULL`, `UNIQUE`, `DEFAULT <literal>` (validated against column type and nullability).

### `CREATE INDEX` / `DROP INDEX`
```sql
AAYUSH CREATE INDEX idx_users_age ON users (age);
AAYUSH DROP INDEX idx_users_age;
```
- Single-column, non-unique indexes only. Multi-column and unique indexes are rejected.
- Index names must be unique in the database and match `[A-Za-z_][A-Za-z0-9_]*`.
- The indexed table and column must already exist.
- Index definitions are stored in the `.aaydb` file (format version 2); the B+ tree is rebuilt from table rows when the database is opened.
- Indexes are maintained by `INSERT`, `UPDATE`, and `DELETE`, and are removed with their table via `DROP TABLE`.
- `NULL` column values are not indexed. Because comparisons against `NULL` never evaluate to `TRUE`, indexed and unindexed results stay identical.

### `EXPLAIN` / `EXPLAIN ANALYZE`
```sql
AAYUSH EXPLAIN SELECT * FROM users WHERE age = 19;
AAYUSH EXPLAIN ANALYZE SELECT * FROM users WHERE age = 19;
```

`EXPLAIN` prints the plan the executor actually uses:

```text
QUERY PLAN

INDEX SCAN: idx_users_age
  TABLE: users
  CONDITION: age = 19
```

Without an applicable index:

```text
QUERY PLAN

TABLE SCAN: users
  FILTER: age > 18
```

- Index-assisted filtering applies to single-table predicates of the form `column <op> literal` or `literal <op> column` with `<op>` in `=`, `>`, `>=`, `<`, `<=` against an existing single-column index. `OR`, `NOT`, `!=` / `<>`, `NULL` literals, type-mismatched literals, and joined queries use a table scan.
- The full original `WHERE` clause is always re-applied after index retrieval, and candidate rows are returned in table order, so results (including unordered `LIMIT`) are identical with or without an index.
- `EXPLAIN ANALYZE` appends measured counters: rows scanned from the access path, rows filtered, rows returned, index used, and execution time. Estimates are never fabricated.
- `EXPLAIN` currently supports `SELECT` statements only. It executes the statement to produce real plans and metrics, so it is not a zero-cost command.

### `INSERT INTO`
```sql
AAYUSH INSERT INTO table_name VALUES (1, 'Aayush', 19);
AAYUSH INSERT INTO table_name (id, name) VALUES (2, 'Alex'), (3, 'Mika');
```
Multi-row inserts are validated atomically before modifying table rows.

### `SELECT` (Single-table & Relational Queries)
```sql
AAYUSH SELECT
    u.name AS customer,
    COUNT(o.id) AS order_count,
    SUM(o.price) AS total_spent
FROM users AS u
LEFT JOIN orders AS o ON u.id = o.user_id
WHERE u.city = 'Pune'
GROUP BY u.id, u.name
HAVING COUNT(o.id) >= 1
ORDER BY total_spent DESC, u.name ASC
LIMIT 10;
```

#### Output Labels

Every `SELECT` expression must produce a distinct output label (its `AS` alias, otherwise the column or aggregate name). Two expressions sharing one label — `SELECT u.id AS identifier, o.id AS identifier` or `SELECT u.id, u.id` — are rejected with `ExecutionError`, because result rows are keyed by output label and a duplicate would overwrite a selected value. Use a distinct alias per expression.

#### NULL Semantics (Three-Valued Logic)

`NULL` means *unknown*, not *false*. Comparisons involving `NULL` evaluate to `UNKNOWN`, and `UNKNOWN` propagates through logical operators:

| Expression | Result |
|---|---|
| `5 = NULL`, `5 <> NULL`, `5 > NULL` | `UNKNOWN` |
| `NOT (5 = NULL)` | `UNKNOWN` |
| `TRUE AND UNKNOWN` | `UNKNOWN` |
| `FALSE AND UNKNOWN` | `FALSE` |
| `TRUE OR UNKNOWN` | `TRUE` |
| `FALSE OR UNKNOWN` | `UNKNOWN` |

`WHERE`, `JOIN ON`, `HAVING`, and the `WHERE` clause of `UPDATE` / `DELETE` admit a row only when the predicate is `TRUE`. `UNKNOWN` never qualifies a row, so `NOT (salary = NULL)` matches nothing and `department <> 'Sales'` excludes rows whose `department` is `NULL`. To test for `NULL` you currently need `IS NULL` / `IS NOT NULL`, which are not implemented yet (see §5).

#### Table Sources & Joins
- Table aliases: `FROM users AS u` or `FROM users u` (likewise on joined tables).
- Supported joins: `INNER JOIN ... ON <expr>`, `LEFT JOIN ... ON <expr>`, and `JOIN ... ON <expr>` (synonym for `INNER JOIN`). `RIGHT`, `FULL`, and `CROSS` joins are rejected with `ParserError`.
- Column resolution:
  - Qualified references (`u.id`, `users.id`) must reference an active table/alias in scope.
  - Unqualified references (`id`) are resolved across active sources and rejected with `ExecutionError` if unknown or ambiguous.
  - Duplicate table names or aliases in the same `FROM`/`JOIN` chain are rejected unless disambiguated with distinct aliases.
  - When `SELECT *` is used with joins, output column headers are qualified (`<source>.<col>`) to avoid collisions.

#### Aggregate Functions
- `COUNT(*)`: counts all input rows in the group (returns `0` on empty input).
- `COUNT(expr)`: counts non-`NULL` values (returns `0` on empty input or all-`NULL`).
- `SUM(expr)`, `AVG(expr)`: sum / average of non-`NULL` numeric values (returns `NULL` on empty input or all-`NULL`; rejects non-numeric columns/values).
- `MIN(expr)`, `MAX(expr)`: minimum / maximum of non-`NULL` values (returns `NULL` on empty input or all-`NULL`).
- Nested aggregates and aggregates in `WHERE` or `JOIN ON` are rejected with `ExecutionError`.

#### `GROUP BY` & `HAVING`
- `GROUP BY col1, col2, ...` groups rows by one or more simple column references (qualified or unqualified). `NULL` values in a grouping column group together.
- When `GROUP BY` or aggregates are present, every non-aggregated column in `SELECT`, `HAVING`, and non-alias `ORDER BY` expressions must appear in `GROUP BY`.
- `WHERE` filters rows before grouping; `HAVING` filters groups after aggregation.

#### `ORDER BY` & `LIMIT`
- `ORDER BY` accepts column references (qualified or unqualified), explicit `SELECT` aliases, and aggregate calls (`ORDER BY COUNT(*) DESC`).
- Multiple sort keys with `ASC` (default) / `DESC` are supported; `NULL` values sort last in `ASC` and first in `DESC`.

### `BEGIN` / `COMMIT` / `ROLLBACK`
```sql
AAYUSH BEGIN;
AAYUSH INSERT INTO accounts VALUES (1, 'Aayush', 1000);
AAYUSH UPDATE accounts SET balance = 900 WHERE id = 1;
AAYUSH COMMIT;
```

- `BEGIN` starts an explicit transaction. Mutations are visible to later statements from the same engine but are **not** written to the `.aaydb` file until `COMMIT`.
- `ROLLBACK` discards every change since `BEGIN`, including DDL (`CREATE TABLE`, `DROP TABLE`, `CREATE INDEX`, `DROP INDEX`).
- Each statement inside a transaction is atomic: a statement that fails validation or a constraint leaves no partial changes, and the transaction remains usable.
- `COMMIT` is the only durability point. It persists all changes together through the existing atomic file replacement. If persistence fails, the transaction is rolled back to its pre-`BEGIN` state and `StorageError` is raised; success is never reported for an unpersisted commit.
- Nested `BEGIN`, and `COMMIT` / `ROLLBACK` without an active transaction, raise `ExecutionError`.
- Closing the engine or exiting the CLI with an active transaction rolls it back. The CLI prints `WARNING: rolling back uncommitted transaction on exit` to `stderr`.
- Autocommit behavior outside explicit transactions is unchanged: each mutating statement persists immediately.
- The model is single-process and single-writer. There are no savepoints, isolation levels, or concurrent writers.

### `UPDATE` & `DELETE`
```sql
AAYUSH UPDATE table_name SET col1 = 20, col2 = 'Updated' WHERE id = 1;
AAYUSH DELETE FROM table_name WHERE id = 2;
```
Every assignment in `UPDATE ... SET` requires an explicit `=`.

## 5. Unsupported / Planned Syntax

The following SQL features are **not yet implemented** in v0.5.0:
- `RIGHT JOIN`, `FULL JOIN`, `CROSS JOIN`, natural joins, or comma joins
- Qualified wildcards (`u.*`)
- Subqueries, `UNION`, `DISTINCT`, `CASE`, `IS NULL` / `IS NOT NULL`, `LIKE`, `IN`, `BETWEEN`, arithmetic expressions
- Nested aggregate functions (`SUM(COUNT(*))`) or arbitrary expressions in `GROUP BY`
- `ALTER TABLE`, savepoints, nested transactions, isolation levels, and concurrent writers
- Unique, partial, expression, or multi-column indexes; index plans for joins
- Duplicate output labels within one `SELECT` list (rejected with `ExecutionError` rather than silently merged)
