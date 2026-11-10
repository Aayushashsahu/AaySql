# AayushSQL Language Specification (v0.2.1)

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
- **Identifiers**: `[A-Za-z_][A-Za-z0-9_]*`. Statement/column/table identifiers are case-sensitive in storage and lookup; SQL keywords are matched case-insensitively.
- **Statement terminator**: Optional trailing `;` on single statements; `;` separates statements in scripts and REPL buffers.

## 3. Data Types

Only the following column types (case-insensitive) are accepted by `CREATE TABLE` and database loading; unsupported type names are rejected with `ParserError` / `ExecutionError` / `StorageError`.

| Canonical Type | Accepted Aliases | Python / JSON Representation |
|---|---|---|
| `INT` | `INTEGER` | `int` (booleans rejected) |
| `TEXT` | `VARCHAR` | `str` |
| `FLOAT` | `DOUBLE` | `float` (integers coerced to `float`) |
| `BOOL` | `BOOLEAN` | `bool` (`0` and `1` coerced to `False`/`True`) |

## 4. Supported Statements

### `CREATE TABLE`
```sql
AAYUSH CREATE TABLE table_name (
    col1 INT PRIMARY KEY,
    col2 TEXT NOT NULL UNIQUE,
    col3 FLOAT DEFAULT 0.0,
    col4 TEXT DEFAULT 'Pune',
    col5 TEXT DEFAULT NULL
);
```
Column rules and constraints:
- At least one column; duplicate column names (case-insensitive) are rejected.
- `PRIMARY KEY` (at most one column per table; unique and non-null).
- `NOT NULL`.
- `UNIQUE` (multiple `NULL` values are permitted).
- `DEFAULT <literal>`: checked against the column's data type and nullability (`DEFAULT NULL` is rejected if `NOT NULL` or `PRIMARY KEY` is set; duplicate `DEFAULT` clauses on one column are rejected).

### `DROP TABLE`
```sql
AAYUSH DROP TABLE table_name;
```

### `INSERT INTO`
```sql
AAYUSH INSERT INTO table_name VALUES (1, 'Aayush', 19);

AAYUSH INSERT INTO table_name (id, name) VALUES
    (2, 'Alex'),
    (3, 'Mika');
```
Multi-row inserts are validated atomically: if any row fails type or constraint validation, no rows from that statement are inserted.

### `SELECT`
```sql
AAYUSH SELECT * FROM table_name;

AAYUSH SELECT col1, col2 AS alias_name
FROM table_name
WHERE col1 >= 10 AND NOT (col2 = 'skip')
ORDER BY col1 DESC, col2 ASC
LIMIT 5;
```
Supported expression operators in `WHERE`:
- Comparisons: `=`, `!=`, `<>`, `<`, `<=`, `>`, `>=` (comparisons against `NULL` evaluate to false; incompatible ordering comparisons such as `INT > TEXT` raise `ExecutionError`).
- Boolean operators: `NOT`, `AND`, `OR` (with parenthesized grouping).

### `UPDATE`
```sql
AAYUSH UPDATE table_name
SET col1 = 20, col2 = 'Updated'
WHERE id = 1;
```
Every assignment in `SET` **must** use an explicit `=` (`col = value`). Missing, duplicate, or misplaced `=` operators raise `ParserError`. Constraint violations across updated rows are checked atomically before applying changes.

### `DELETE`
```sql
AAYUSH DELETE FROM table_name WHERE id = 2;
AAYUSH DELETE FROM table_name;
```

## 5. Unsupported / Planned Syntax

The following standard SQL features are **not yet implemented** in v0.2.1:
- `JOIN` (`INNER JOIN`, `LEFT JOIN`, etc.)
- `GROUP BY`, `HAVING`, aggregate functions (`COUNT`, `SUM`, `AVG`, `MIN`, `MAX`)
- `CREATE INDEX`, `EXPLAIN`
- `BEGIN`, `COMMIT`, `ROLLBACK`
- `ALTER TABLE`, `IF EXISTS` / `IF NOT EXISTS`, `IS NULL` / `IS NOT NULL`, `LIKE`, `IN`, `BETWEEN`, `TRUE`/`FALSE` literals
