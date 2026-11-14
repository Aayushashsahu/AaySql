# AayushSQL — Native JSON & Document Model (Phase 5)

## 1. Overview and Architectural Position

AayushSQL 2.0 introduces first-class document capabilities while remaining a **SQL-first, embedded-first relational database** (ADR-01, ADR-03).
Documents are not stored in an isolated second database engine; instead, native JSON values live directly inside relational tables as typed column values (`JSON` or `DOCUMENT`), or within collections that compile directly into relational tables with standard primary key indexing (ADR-06).

Every document operation integrates directly with:
- Relational tables and columns
- SQL parsing, query planning, and evaluation
- Single-owner process leasing (`DatabaseLease`)
- Delta Write-Ahead Log (WAL)
- Format v4 segmented checkpoint storage
- In-memory inverse mutation undo journal
- MVCC snapshot readers and WAL group commit
- Offline backup and restore

---

## 2. Document Model and Value Representation

### 2.1 The Three States
AayushSQL recognizes **three distinct states** for values (ADR-07):

| State | Meaning | Example |
|---|---|---|
| **SQL NULL** (`None`) | A relational column has no value | `INSERT INTO users (id, profile) VALUES (1, NULL)` |
| **JSON NULL** (`JSON_NULL`) | A JSON property exists and explicitly holds a null value | `{"name": "Aayush", "phone": null}` |
| **ABSENT** (`ABSENT`) | A JSON property does not exist in the document | `{"name": "Aayush"}` queried at `phone` |

### 2.2 Predicate Truth Table
To preserve standard SQL intuition while giving callers access to explicit three-state distinction, predicates evaluate as follows:

| Value State | `expr IS NULL` | `expr IS NOT NULL` | `expr IS MISSING` | `expr IS NOT MISSING` | `expr IS JSON NULL` | `expr IS NOT JSON NULL` |
|---|---|---|---|---|---|---|
| **SQL NULL** | `TRUE` | `FALSE` | `FALSE` | `FALSE` | `FALSE` | `TRUE` |
| **JSON NULL** | `TRUE` | `FALSE` | `FALSE` | `TRUE` | `TRUE` | `FALSE` |
| **ABSENT** | `TRUE` | `FALSE` | `TRUE` | `FALSE` | `FALSE` | `TRUE` |
| **Normal Value** | `FALSE` | `TRUE` | `FALSE` | `TRUE` | `FALSE` | `TRUE` |

- `IS NULL` evaluates to `TRUE` for SQL NULL, JSON NULL, and ABSENT, maintaining SQL conventions.
- `IS MISSING` evaluates to `TRUE` only when a path does not exist (ABSENT).
- `IS NOT MISSING` evaluates to `TRUE` if the path exists (even if it contains JSON null).
- `IS JSON NULL` evaluates to `TRUE` only when the path exists and holds an explicit JSON null.

### 2.3 Value Types and Numeric Discipline
Supported JSON value constructs:
- **Object**: Unordered string-to-value map. Key ordering is not preserved in comparison; structural equality is order-insensitive.
- **Array**: Ordered list of values, allowing arbitrary nesting and mixed types.
- **String**: Valid UTF-8 text. Null bytes (`\u0000`) and invalid surrogates are rejected.
- **Integer**: Exact 64-bit signed integer (`-2^63` to `2^63 - 1`). Out-of-bounds integers are rejected (never silently converted to float).
- **Float**: IEEE 754 float64. Non-finite values (`NaN`, `Infinity`, `-Infinity`) are rejected.
- **Boolean**: `true` or `false`. Never coerced to or compared equal to integers (`1` / `0`).
- **Duplicate Keys**: Explicitly **rejected** with an error. No silent last-wins or first-wins.

### 2.4 Resource Limits
- **Maximum document size**: 1 MiB (1,048,576 bytes in canonical UTF-8).
- **Maximum nesting depth**: 64 levels.
- **Maximum key length**: 1,024 bytes.
- **Maximum path segments**: 32 segments.

### 2.5 Canonical Serialization
All stored and hashed JSON bytes follow canonical serialization:
- Encoding: UTF-8.
- Sorting: Object keys sorted lexicographically by Unicode code point.
- Compact separators: `(",", ":")` with no insignificant whitespace.
- Finite values only (`allow_nan=False`).

---

## 3. SQL Interface and Syntax

### 3.1 DDL
Columns can be declared with data type `JSON` or `DOCUMENT`:
```sql
AAYUSH CREATE TABLE users (
    id INT PRIMARY KEY,
    name TEXT,
    profile JSON
);
```

Collections are created with:
```sql
AAYUSH CREATE COLLECTION events;
```
This compiles to a table with columns: `_id TEXT PRIMARY KEY`, `_rev INT`, and `body JSON`.

### 3.2 Path Navigation
Path access expressions support dot navigation and bracket indexing:
- Dot notation: `profile.preferences.theme`
- Bracket indexing: `profile.tags[0]`
- Path function: `JSON_EXTRACT(profile, '$.preferences.theme')` or `JSON_VALUE(profile, '$.preferences.theme')`
- Path existence: `JSON_EXISTS(profile, '$.preferences.theme')`

When a path is absent or points to a non-existent index:
- Navigation yields `ABSENT`.
- In arithmetic or comparisons, `ABSENT` propagates as SQL NULL (`UNKNOWN`), preventing query crashes.

### 3.3 Document Mutations
Nested document updates use `JSON_SET` and `JSON_REMOVE`:
```sql
AAYUSH UPDATE users
SET profile = JSON_SET(profile, '$.role', 'lead')
WHERE id = 1;
```
```sql
AAYUSH UPDATE users
SET profile = JSON_REMOVE(profile, '$.preferences.notifications')
WHERE id = 1;
```

---

## 4. Durability, Undo, and Storage Integration

1. **Storage (Format v4)**:
   - Root manifest and immutable table segments in `<db>.seg/` serialize JSON values natively through `canonical_json_bytes`.
   - Existing format v3 and v4 databases remain fully compatible.
2. **Delta WAL**:
   - Delta mutation records (`insert`, `update`) store deep-copied rows containing JSON objects and arrays.
   - Replay applies `coerce_value` on JSON columns, ensuring verified document reconstruction on recovery.
3. **In-Memory Undo Journal**:
   - `UpdateUndo` captures deep-copied before-images of rows, guaranteeing that rolled back transactions restore pristine nested structures without side effects.
4. **MVCC Snapshots**:
   - Published roots are immutable. Writers use isolated copy-on-write workspaces.
   - Read queries return deep-copied document values, preventing external Python mutation from corrupting database roots.
