# AayushSQL Language Specification (v1.0.0)

## 1. Overview

AayushSQL preserves conventional SQL syntax and semantics with one mandatory rule: **every top-level statement must begin with the `AAYUSH` keyword** (case-insensitive).

- Valid: `AAYUSH SELECT * FROM users;`
- Invalid (missing prefix): `SELECT * FROM users;` -> `ParserError`
- Invalid (duplicate prefix): `AAYUSH AAYUSH SELECT * FROM users;` -> `ParserError`

v0.9.0 adds a unified expression system: `IS NULL`, `LIKE`, `IN`, `BETWEEN`, arithmetic, `CASE`, `DISTINCT`, and `OFFSET`. Every expression is parsed by one precedence-climbing parser and evaluated by one evaluator, so all supported expressions work in every supported context.

## 2. Lexical Elements

- **Comments**: Line comments starting with `--` until end-of-line.
- **Strings**: Single-quoted (`'hello'`) or double-quoted (`"hello"`). Doubled quotes escape embedded quotes (`'it''s'`).
- **Numbers**: Unsigned integer (`42`) and floating-point (`3.14`) literals. A leading sign is an operator, not part of the literal: `-5` is unary minus applied to `5`, `1-2` lexes as `1`, `-`, `2`, and `1 - 2` behaves identically. A sign applied to a numeric literal is folded into the literal when parsed, so `-7` is the same value in `INSERT`/`DEFAULT` as in an expression. Scientific notation (`1e9`) is not supported.
- **Boolean literals**: `TRUE` and `FALSE` (case-insensitive).
- **Null literal**: `NULL` (case-insensitive).
- **Identifiers**: `[A-Za-z_][A-Za-z0-9_]*`. Table/column/alias identifiers are case-sensitive in storage and resolution; SQL keywords and aggregate function names are matched case-insensitively.
- **Qualified identifiers**: `table_or_alias.column_name`. Qualified wildcards (`u.*`) are not supported.
- **Operators**: `=`, `!=`, `<>`, `<`, `<=`, `>`, `>=`, `+`, `-`, `*`, `/`, `%`.
- **Statement terminator**: Optional trailing `;` on single statements; `;` separates statements in scripts and REPL buffers.

## 3. Data Types

Only the following column types (case-insensitive) are accepted by `CREATE TABLE` and database loading:

| Canonical Type | Accepted Aliases | Python / JSON Representation |
|---|---|---|
| `INT` | `INTEGER` | `int` (booleans rejected) |
| `TEXT` | `VARCHAR` | `str` |
| `FLOAT` | `DOUBLE` | `float` (integers coerced to `float`) |
| `BOOL` | `BOOLEAN` | `bool` (`0` and `1` coerced to `False`/`True`) |

Computed expressions are not typed columns: a `SELECT` result may mix types across rows, and `UPDATE` results are still validated and coerced against the target column (see §6.8).

## 4. Operator Precedence

Precedence from lowest to highest. Parentheses override it.

| Precedence | Operators | Associativity |
|---|---|---|
| 1 | `OR` | left |
| 2 | `AND` | left |
| 3 | `NOT` (prefix) | right |
| 4 | `=`, `!=`, `<>`, `<`, `<=`, `>`, `>=`, `IS [NOT] NULL`, `[NOT] IN`, `[NOT] BETWEEN`, `[NOT] LIKE` | non-associative |
| 5 | `+`, `-` | left |
| 6 | `*`, `/`, `%` | left |
| 7 | unary `+`, unary `-` | right |
| 8 | literals, identifiers, aggregates, `CASE`, `( expr )` | — |

Consequences worth stating explicitly:

- `2 + 3 * 4` is `14`; `(2 + 3) * 4` is `20`.
- `NOT a = b` parses as `NOT (a = b)`, because `NOT` binds looser than a comparison.
- `a BETWEEN 1 AND 5 AND b = 2` parses as `(a BETWEEN 1 AND 5) AND (b = 2)`: `BETWEEN` consumes only its own `AND`.
- `a NOT IN (...)`, `a NOT LIKE '...'`, and `a NOT BETWEEN x AND y` are the negated forms of the corresponding predicates; they are not `NOT (a IN ...)` textually, but they have identical three-valued results.
- `a * b` is multiplication; `SELECT *` is a wildcard because the `SELECT` list is parsed as either `*` or a list of expressions.

## 5. Expressions

### 5.1 Column references, literals, parentheses

An expression is a column reference (qualified or unqualified), a literal, an aggregate call, a `CASE`, or a combination of these with operators and parentheses.

### 5.2 Arithmetic

| Operator | Result type | Notes |
|---|---|---|
| `+`, `-`, `*` | `INT` when both operands are integers, `FLOAT` otherwise | |
| `/` | always `FLOAT` | `10 / 4` is `2.5` |
| `%` | `INT` when both operands are integers, `FLOAT` otherwise | Python floored semantics: the sign follows the divisor (`-7 % 3` is `2`, `7 % -3` is `-2`) |
| unary `+`, unary `-` | same type as the operand | `-NULL` is `NULL` |

Rules:

- Only `INT`/`FLOAT` operands are numeric. `BOOL` is never silently treated as a number, and neither are `TEXT` values; either raises `ExecutionError` naming the operator.
- If either operand is `NULL`, the result is `NULL` (no error).
- Division by zero and modulo by zero raise `ExecutionError`.
- A result that is not finite (`-inf`, `inf`, or `NaN`) raises `ExecutionError` instead of producing a value that could reach persisted column data.

### 5.3 `IS NULL` / `IS NOT NULL`

- `x IS NULL` is `TRUE` when `x` is `NULL` and `FALSE` otherwise.
- `x IS NOT NULL` is the reverse.
- These are the only predicates that never return `UNKNOWN`, and they are **not** equality comparisons: `5 = NULL` is `UNKNOWN`, while `5 IS NULL` is `FALSE`.
- `NOT (x IS NULL)` is equivalent to `x IS NOT NULL`.

`NULL` values are not stored in B+ tree indexes, so `IS NULL` always uses a table scan; an index is never chosen for it.

### 5.4 `LIKE` / `NOT LIKE`

`value LIKE pattern` compares text with SQL wildcards:

| Wildcard | Meaning |
|---|---|
| `%` | zero or more characters |
| `_` | exactly one character |

Every other character is literal. `LIKE` is **case-sensitive**; there is no `ILIKE` and no escape character clause (`ESCAPE` is not implemented). Patterns are converted to an anchored regular expression with every non-wildcard character escaped, so a pattern is never executed as a user-supplied regular expression.

- `'A%'` matches `A`, `Alex`, `Aayush`; `'%son'` matches `Jackson`, `Wilson`; `'_at'` matches `cat`, `hat` and not `at`; `'a_c'` matches `abc` and `a-c`.
- Both operands must be `TEXT`; a non-text operand raises `ExecutionError`.
- If either operand is `NULL`, the result is `UNKNOWN`, and `NOT LIKE` preserves `UNKNOWN`.
- `LIKENULL` has no index support and always runs on a table scan.

### 5.5 `IN` / `NOT IN`

`x IN (a, b, c)` is scalar membership using SQL equality. Subqueries inside `IN` are not supported, and an empty list is a `ParserError`.

Rules (three-valued):

1. TRUE if any non-`NULL` candidate equals `x`.
2. `UNKNOWN` if no candidate matches and at least one candidate is `NULL`.
3. FALSE if no candidate matches and no candidate is `NULL`.
4. `UNKNOWN` if `x` itself is `NULL`.

| Expression | Result |
|---|---|
| `2 IN (1, 2, 3)` | TRUE |
| `2 IN (1, 3)` | FALSE |
| `2 IN (1, NULL, 3)` | UNKNOWN |
| `2 IN (2, NULL)` | TRUE |
| `NULL IN (1, 2)` | UNKNOWN |
| `2 NOT IN (1, 3)` | TRUE |
| `2 NOT IN (1, NULL, 3)` | UNKNOWN |

`NOT IN` is the logical negation of `IN`, so it keeps `UNKNOWN`. List members may be arbitrary scalar expressions; they are evaluated per row.

`IN` is not index-assisted in v0.9.0 (see §9).

### 5.6 `BETWEEN` / `NOT BETWEEN`

`x BETWEEN low AND high` is inclusive at both ends and is defined as `x >= low AND x <= high`.

| Expression | Result |
|---|---|
| `5 BETWEEN 1 AND 5` | TRUE |
| `5 BETWEEN 6 AND 10` | FALSE |
| `5 NOT BETWEEN 1 AND 5` | FALSE |
| `NULL BETWEEN 1 AND 5` | UNKNOWN |
| `5 BETWEEN NULL AND 10` | UNKNOWN |
| `5 BETWEEN 10 AND 1` | FALSE |

`NOT BETWEEN` is the logical negation, so it also returns `UNKNOWN` when either bound or the value is `NULL`. Type-incompatible operands raise the same `ExecutionError` an equivalent comparison would.

When the value is a column and both bounds are literals of the column's type, `BETWEEN` may be executed as one contiguous index range scan; the original predicate is still the source of truth and both paths are tested to return identical rows.

### 5.7 `CASE`

Two forms are supported:

```sql
-- searched CASE
CASE WHEN score >= 90 THEN 'A' WHEN score >= 75 THEN 'B' ELSE 'C' END

-- simple CASE
CASE department WHEN 'Engineering' THEN 'Technical' ELSE 'Other' END
```

- Searched `CASE` evaluates `WHEN` conditions in order and takes the first one that is `TRUE`. `FALSE` and `UNKNOWN` do not match.
- Simple `CASE` evaluates its base once, then compares it to each `WHEN` expression with SQL `=`; only `TRUE` matches. A `NULL` base never matches a `WHEN NULL` branch, because a `NULL` comparison is `UNKNOWN`.
- If no branch matches, `ELSE` is used; without `ELSE` the result is `NULL`.
- `CASE` may be nested and may appear anywhere an expression is allowed, including inside arithmetic, aggregates (`SUM(CASE WHEN ... THEN x ELSE 0 END)`) and `UPDATE` assignments.
- `CASE` requires at least one `WHEN` and a closing `END`; missing `THEN`, `END`, or an unclosed expression is a `ParserError`.

### 5.8 `DISTINCT`

`SELECT DISTINCT ...` removes duplicate projected rows.

- Distinctness is based on the complete projected row, so multiple selected expressions form one composite key.
- Duplicate elimination happens after projection, before `ORDER BY`, `OFFSET`, and `LIMIT`.
- The first occurrence of each distinct row is kept, so unordered `DISTINCT` output follows the engine's deterministic row order.
- `NULL` values compare equal to each other for duplicate elimination.
- Values of different SQL types are not conflated (`TRUE` is not the same key as `1`, `1` is not `1.0`).
- `SELECT DISTINCT *` deduplicates whole rows.
- With `DISTINCT`, every `ORDER BY` expression must be a unique `SELECT` alias, exactly one of the selected expressions, or (for `DISTINCT *`) a selected column; otherwise the query is rejected with `ExecutionError`. Ordering therefore always refers to the projected row that survived duplicate elimination.
- Duplicate output labels are still rejected (`SELECT DISTINCT id, id`), because result rows are keyed by label.

### 5.9 `OFFSET`

```sql
AAYUSH SELECT * FROM users ORDER BY id LIMIT 10 OFFSET 20;
AAYUSH SELECT id FROM users ORDER BY id OFFSET 5;
```

- `LIMIT n OFFSET m` and `OFFSET m` without `LIMIT` are supported. `OFFSET` before `LIMIT` is a `ParserError`.
- `LIMIT` and `OFFSET` must be non-negative integers; negative values, floats, and other tokens are rejected with `ParserError`.
- `OFFSET` is applied after `ORDER BY` and before `LIMIT`, and never before filtering, joins, grouping, or sorting.
- An `OFFSET` beyond the result size returns no rows.

## 6. Expression Contexts

Every expression above is accepted in every context that accepts a scalar expression:

| Context | Notes |
|---|---|
| `SELECT` list | projection, including `CASE`, arithmetic, and aggregates |
| `WHERE` | row filter, before grouping |
| `JOIN ... ON` | join condition; also used for hash-key extraction when eligible |
| `GROUP BY` | simple column references only (see §9) |
| `HAVING` | group filter, may mix aggregates and arithmetic |
| `ORDER BY` | any expression, not only columns and aggregates |
| `UPDATE ... SET col = <expr>` | assignment value |
| `UPDATE` / `DELETE` `WHERE` | row selection, identical semantics to `SELECT ... WHERE` |
| `CASE` conditions and results | any expression, including nested `CASE` |

Aggregate restrictions are unchanged: aggregates are not allowed in `WHERE`, `JOIN ON`, or `UPDATE` assignments; nested aggregates (`SUM(COUNT(*))`) are rejected; and in a grouped query every non-aggregated column reference must appear in `GROUP BY` — validation recurses through every expression node, including `CASE` branches and `IN` lists.

## 7. Supported Statements

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
Column constraints: `PRIMARY KEY` (at most one per table, unique and non-null), `NOT NULL`, `UNIQUE`, `DEFAULT <literal>` (validated against column type and nullability). A `DEFAULT` literal may carry a sign (`DEFAULT -4`) and may be `TRUE`/`FALSE`.

### `CREATE INDEX` / `DROP INDEX`
```sql
AAYUSH CREATE INDEX idx_users_age ON users (age);
AAYUSH DROP INDEX idx_users_age;
```
- Single-column, non-unique indexes only. Multi-column and unique indexes are rejected.
- Index names must be unique in the database and match `[A-Za-z_][A-Za-z0-9_]*`.
- The indexed table and column must already exist.
- Index definitions are stored in the `.aaydb` file (format version 3); the B+ tree is rebuilt from table rows when the database is opened.
- Indexes are maintained by `INSERT`, `UPDATE`, and `DELETE`, and are removed with their table via `DROP TABLE`.
- `NULL` column values are not indexed. Because comparisons against `NULL` never evaluate to `TRUE`, indexed and unindexed results stay identical.

### `EXPLAIN` / `EXPLAIN ANALYZE`
```sql
AAYUSH EXPLAIN SELECT * FROM users WHERE age = 19;
AAYUSH EXPLAIN ANALYZE SELECT * FROM users WHERE age = 19;
```

`EXPLAIN` prints the physical plan the executor actually uses:

```text
QUERY PLAN

INDEX SCAN: idx_users_age
  TABLE: users
  CONDITION: age = 19
  FILTER: age = 19
  WHY: index estimated cheaper: ~1.1 cost units vs ~4.0 for a table scan (~1 candidates)
  ESTIMATED ROWS: ~1
```

Without an applicable index, or when the cost model prefers a sequential pass:

```text
QUERY PLAN

TABLE SCAN: users
  FILTER: age > 18
  ESTIMATED ROWS: ~2
```

Computed projections are shown with their expression text, so `CASE` and arithmetic are visible:

```text
PROJECTION: adjusted: CASE WHEN salary > 50000 THEN salary * 1.1 ELSE salary END
```

A `DISTINCT` / `ORDER BY` / `OFFSET` / `LIMIT` query renders one wrapper per operator, innermost first:

```text
QUERY PLAN

LIMIT: 2
  OFFSET: 1
    SORT: department ASC
      DISTINCT: department
        TABLE SCAN: employees
```

- Plans use the operator vocabulary `TABLE SCAN`, `INDEX SCAN`, `JOIN` (`NESTED LOOP` / `HASH`), `FILTER`, `AGGREGATE`, `PROJECTION`, `DISTINCT`, `SORT`, `OFFSET`, `LIMIT`.
- Index-assisted filtering applies to a conjunct of the form `column <op> literal` or `literal <op> column` with `<op>` in `=`, `>`, `>=`, `<`, `<=`, and to `column BETWEEN literal AND literal`, against an existing single-column index. `IS NULL`, `LIKE`, `IN`, arithmetic, `CASE`, `OR`, `NOT`, `!=`/`<>`, `NULL` literals, and type-mismatched literals do not use an index.
- Whether an available index or table scan is used is decided by a cost model over in-memory statistics. `WHY:` states the choice; every figure prefixed with `~` is an estimate.
- Predicates are pushed down only as top-level `AND` conjuncts, never split out of `OR`, and never moved across a `LEFT JOIN` onto its nullable side. Predicate analysis uses the same expression traversal as evaluation, so a new expression type cannot be pushed by accident.
- Equi-joins may use a hash join; only `column = column` conjuncts between the two inputs are eligible hash keys. New expressions in `ON` are evaluated as residual predicates on every candidate, and non-equi-only conditions fall back to nested loop.
- Selectivity estimates are refined only for shapes statistics actually describe (single-column comparison, `IS NULL`, `IN`, `BETWEEN` against literals). `LIKE`, `CASE`, and arithmetic predicates keep the documented conservative default; no fake precision is reported.
- `EXPLAIN ANALYZE` appends measured per-operator counters: rows in/out, rows rejected, index candidates, hash build/probe/bucket matches, nested-loop comparisons, duplicates removed by `DISTINCT`, rows discarded by `OFFSET`, rows returned after `LIMIT`, and per-operator timing.
- `EXPLAIN` supports `SELECT` statements only. Plain `EXPLAIN` never executes the statement and never mutates state; only `EXPLAIN ANALYZE` runs it.

### `INSERT INTO`
```sql
AAYUSH INSERT INTO table_name VALUES (1, 'Aayush', 19);
AAYUSH INSERT INTO table_name (id, name) VALUES (2, 'Alex'), (3, 'Mika');
```
Values are literals (`NULL`, numbers with an optional sign, strings, `TRUE`/`FALSE`); arbitrary expressions are not accepted in `VALUES`. Multi-row inserts are validated atomically before modifying table rows.

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
LIMIT 10 OFFSET 5;
```

#### Output Labels

Every `SELECT` expression must produce a distinct output label (its `AS` alias, otherwise the column name; a computed expression without an alias is labelled `expr_<position>`). Two expressions sharing one label — `SELECT u.id AS identifier, o.id AS identifier` or `SELECT u.id, u.id` — are rejected with `ExecutionError`, because result rows are keyed by output label. Use a distinct alias per expression.

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

`WHERE`, `JOIN ON`, `HAVING`, and the `WHERE` clause of `UPDATE` / `DELETE` admit a row only when the predicate is `TRUE`. `UNKNOWN` never qualifies a row, so `NOT (salary = NULL)` matches nothing and `department <> 'Sales'` excludes rows whose `department` is `NULL`. Use `IS NULL` / `IS NOT NULL` to test for `NULL`.

`IS NULL`, `LIKE`, `IN`, `BETWEEN`, `CASE`, and arithmetic follow the same rule: they only ever produce `TRUE`, `FALSE`, or `UNKNOWN`, and never treat `UNKNOWN` as `FALSE` inside `NOT`.

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
- `SUM(expr)`, `AVG(expr)`: sum / average of non-`NULL` numeric values (returns `NULL` on empty input or all-`NULL`; rejects non-numeric results).
- `MIN(expr)`, `MAX(expr)`: minimum / maximum of non-`NULL` values (returns `NULL` on empty input or all-`NULL`).
- Aggregate arguments may be any expression: `SUM(price * quantity)`, `AVG(salary) * 1.1`, `MAX(CASE WHEN ... THEN x END)`.
- Nested aggregates and aggregates in `WHERE`, `JOIN ON`, or `UPDATE` assignments are rejected with `ExecutionError`.

#### `GROUP BY` & `HAVING`
- `GROUP BY col1, col2, ...` groups rows by one or more simple column references (qualified or unqualified). `NULL` values in a grouping column group together.
- When `GROUP BY` or aggregates are present, every non-aggregated column in `SELECT`, `HAVING`, and non-alias `ORDER BY` expressions must appear in `GROUP BY`. The rule is checked recursively through every expression node.
- `WHERE` filters rows before grouping; `HAVING` filters groups after aggregation.

#### `ORDER BY`, `OFFSET` & `LIMIT`
- `ORDER BY` accepts column references, explicit `SELECT` aliases, aggregate calls, and arbitrary expressions (`ORDER BY price * quantity DESC`).
- Multiple sort keys with `ASC` (default) / `DESC` are supported; `NULL` values sort last in `ASC` and first in `DESC`.
- `LIMIT n` and `OFFSET m` take non-negative integers. Execution order is: `FROM`/`JOIN` -> `WHERE` -> `GROUP BY`/aggregates -> `HAVING` -> projection -> `DISTINCT` -> `ORDER BY` -> `OFFSET` -> `LIMIT`.
- For `DISTINCT` queries, `ORDER BY` keys must refer to the projected row (see §5.8).

### `BEGIN` / `COMMIT` / `ROLLBACK`
```sql
AAYUSH BEGIN;
AAYUSH INSERT INTO accounts VALUES (1, 'Aayush', 1000);
AAYUSH UPDATE accounts SET balance = balance - 100 WHERE id = 1;
AAYUSH COMMIT;

AAYUSH BEGIN SNAPSHOT;
AAYUSH SELECT * FROM accounts;
AAYUSH COMMIT;
```

- `BEGIN` starts an explicit READ COMMITTED transaction. Mutations are visible to later statements from the same session but are **not** written to the `.aaydb` file until `COMMIT`. Each statement reads the latest committed snapshot until the transaction writes, after which it keeps that transaction's snapshot.
- `BEGIN SNAPSHOT` (or `BEGIN ISOLATION LEVEL SNAPSHOT`) pins the read view at `BEGIN`, so repeated reads see the same committed version and no later commits or phantoms. `BEGIN ISOLATION LEVEL READ COMMITTED` is the explicit default. There is no `SERIALIZABLE` level.
- `ROLLBACK` discards every change since `BEGIN`, including DDL (`CREATE TABLE`, `DROP TABLE`, `CREATE INDEX`, `DROP INDEX`).
- Each statement inside a transaction is atomic: a statement that fails validation, a constraint, or expression evaluation (for example division by zero in an `UPDATE`) leaves no partial changes, and the transaction remains usable.
- `COMMIT` is the durability point. Persistent mutations and explicit `COMMIT` append `begin`, `write`, and `commit` WAL records with SHA-256 checksums and call `os.fsync()` before atomically checkpointing `.aaydb` (format v3). Failures before `COMMIT` restore the pre-`BEGIN` state; failures during post-commit checkpointing preserve the committed state and WAL; `COMMIT` `fsync` errors raise `AmbiguousCommitError` and require reopen recovery.
- Nested `BEGIN`, and `COMMIT` / `ROLLBACK` without an active transaction, raise `ExecutionError`. A stale write at `COMMIT` raises `WriteConflictError` (protocol code `WRITE_CONFLICT`).
- Closing the engine or exiting the CLI with an active transaction rolls it back. The CLI prints `WARNING: rolling back uncommitted transaction on exit` to `stderr`.
- Autocommit behavior outside explicit transactions persists every mutating statement through the WAL before returning.
- The model is single-process and single-commit-generator. There are no savepoints, `SERIALIZABLE` isolation, row-level version chains, or multi-writer execution. Independent sessions read concurrently; writers may be rejected at commit with `WriteConflictError` and must retry the whole transaction.

### `UPDATE` & `DELETE`
```sql
AAYUSH UPDATE table_name SET balance = balance - 100, name = 'Updated' WHERE id = 1;
AAYUSH DELETE FROM table_name WHERE score IS NULL;
```
Every assignment in `UPDATE ... SET` requires an explicit `=`, and the right-hand side may be any scalar expression. Assignments are evaluated for every matching row before any row is modified, so an arithmetic or constraint failure aborts the whole statement. In a multi-column assignment, each expression is evaluated against the row as it was before the statement.

## 8. Client/Server Compatibility

The network protocol is unchanged (`PROTOCOL_VERSION = 1`), because SQL is carried as a string. Every statement in this specification works identically through `Engine.execute()` and `AayushSQLClient.execute()`, and returns the same result structure. A `ParserError` maps to `SQL_PARSE_ERROR`, an `ExecutionError` (including division by zero and type errors) to `SQL_EXECUTION_ERROR`, and a `ConstraintError` to `CONSTRAINT_ERROR`; none of these drop the connection.

## 9. Optimizer Support and Known Gaps

- **Index-assisted**: `=`, `>`, `>=`, `<`, `<=` against a literal, and `BETWEEN` between two literals.
- **Table scan only**: `IS NULL` / `IS NOT NULL` (NULLs are not indexed), `LIKE` (no pattern statistics), `IN` (no multi-lookup plan yet), `NOT BETWEEN`, arithmetic and `CASE` predicates, and any comparison where an operand is an expression rather than a literal. These fall back deliberately; the engine never rewrites an arbitrary expression into an indexed column comparison.
- **Statistics**: refined selectivity exists for single-column comparison, `IS NULL`, `IN`, and `BETWEEN` against literals. `LIKE`, `CASE`, and arithmetic predicates use the documented conservative default. There are no histograms, pattern statistics, or expression statistics.
- **Hash joins**: hash keys come only from `column = column` conjuncts. New expression conjuncts are residual predicates checked on each candidate; if no equality conjunct exists, the join is a nested loop.

## 10. Unsupported / Planned Syntax

The following SQL features are **not implemented** in v0.9.0. They are rejected at parse time (or, where they parse successfully, are documented above as unsupported behaviour):

- Subqueries anywhere (including `IN (SELECT ...)`), `EXISTS`, `UNION` / `INTERSECT` / `EXCEPT`
- `RIGHT JOIN`, `FULL JOIN`, `CROSS JOIN`, natural joins, and comma joins
- Qualified wildcards (`u.*`)
- `LIKE ... ESCAPE`, `ILIKE`, regular-expression operators, and case-insensitive collations
- Negative `LIMIT`/`OFFSET`, `FETCH FIRST`, `LIMIT ALL`, and `OFFSET` before `LIMIT`
- Arbitrary expressions in `GROUP BY`; `GROUP BY` positions or aliases
- `SUM(DISTINCT x)` / aggregate `DISTINCT`, window functions, `FILTER (WHERE ...)`
- `IS TRUE` / `IS FALSE` / `IS DISTINCT FROM` (only `IS NULL` and `IS NOT NULL`)
- String concatenation (`||`), `CAST`, `COALESCE`, `NULLIF`, date/time types and functions
- Scientific-notation numeric literals
- `WITH`/CTE, `ALTER TABLE`, savepoints, nested transactions, isolation levels, and concurrent writers
- Unique, partial, expression, or multi-column indexes; index plans for `IN`
- Duplicate output labels within one `SELECT` list (rejected with `ExecutionError` rather than silently merged)
