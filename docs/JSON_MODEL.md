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

### 2.5 Storage Boundary Encoding & Serialization
Standard JSON representations cannot distinguish a relational SQL NULL (`null`) from a top-level JSON null (`null`). AayushSQL resolves this unambiguously at the storage boundary without requiring format version bumps or magic wrapper fields:
- **Relational SQL NULL**: For a `JSON` column, SQL NULL is stored as `None` (standard JSON `null` at rest).
- **JSON Values**: Every valid JSON value in a `JSON` column (including top-level `JSON_NULL`, JSON numbers, booleans, strings, arrays, and objects) is canonicalized to compact UTF-8 JSON text upon serialization:
  - Top-level `JSON_NULL` -> `"null"`
  - JSON string `"null"` -> `"\"null\""`
  - JSON number `42` -> `"42"`
  - JSON object `{"a": 1}` -> `"{\"a\":1}"`
- **Reconstitution**: During row coercion (`coerce_value`), `None` is preserved as SQL NULL, while string payloads in `JSON` columns are parsed via `parse_json_document`, perfectly restoring native Python structures and distinguishing `JSON_NULL` from string `"null"` and SQL NULL.
- **Sentinel Discipline**: The internal `ABSENT` sentinel cannot be serialized and is rejected with `ConstraintError` if presented to table insertion, updates, or WAL recording.
- **Scalar Table Isolation**: Relational scalar columns (`INT`, `FLOAT`, `TEXT`, `BOOL`) strictly accept only scalar types and reject `dict` or `list` payloads.

---

## 3. SQL Interface and Syntax

### 3.1 DDL and Collections
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
This compiles to a relational table with strict nullability constraints:
- `_id TEXT PRIMARY KEY NOT NULL`
- `_rev INT NOT NULL`
- `body JSON NOT NULL`

Inserting an explicit SQL NULL (`None`) into `_id`, `_rev`, or `body` raises a `ConstraintError`. However, inserting a top-level JSON null (`JSON_NULL` or string `'null'`) into `body` is permitted because JSON null is a valid document value and satisfies the relational `NOT NULL` constraint.

### 3.2 Path Navigation and Query Functions
Path access expressions support dot navigation and bracket indexing:
- Dot notation: `profile.preferences.theme`
- Bracket indexing: `profile.tags[0]`

AayushSQL provides three dedicated SQL functions with distinct semantics:
1. `JSON_EXTRACT(doc, path)`: Extracts the document or value at the specified path.
   - If the path exists and holds an explicit JSON null, it preserves and returns `JSON_NULL` (matching `IS JSON NULL`).
   - If the path is absent, it returns SQL NULL (`None`).
2. `JSON_VALUE(doc, path)`: Extracts an SQL scalar value at the specified path.
   - If the path points to an atomic scalar (`str`, `int`, `float`, `bool`), it returns the scalar.
   - If the path is absent, points to an explicit JSON null, or points to a complex structure (`dict`, `list`), it returns SQL NULL (`None`).
3. `JSON_EXISTS(doc, path)`: Tests whether a path exists in the document.
   - Returns boolean `TRUE` if the path exists (even if its value is `JSON_NULL`).
   - Returns boolean `FALSE` if the path is `ABSENT`.

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

#### Mutation Semantics and Copy-on-Write
Point mutations via `JSON_SET` and `JSON_REMOVE` perform semantic field-level updates on documents. Rather than raw byte patching, the engine performs an in-memory deep copy of the document, applies the mutation along the parsed path, validates resource constraints (depth, size, keys), and writes the complete row post-image into the Delta WAL.

#### Memory Ownership Boundaries
To ensure transaction isolation and prevent external memory corruption:
- Values passed into `INSERT` and `UPDATE` statements are deep-copied into the database row storage. Mutating the input Python object after execution has no effect on the stored row.
- Results returned from `SELECT` queries are deep-copied prior to delivery. Callers modifying dictionaries or lists received from query results cannot corrupt the database's internal row cache or published snapshots.

---

## 4. Durability, Backup, and Storage Integration

1. **Storage (Format v4)**:
   - Root manifest and immutable table segments in `<db>.seg/` serialize JSON values through schema-aware canonical text encoding.
   - Existing format v3 and v4 databases remain fully compatible.
2. **Delta WAL**:
   - Delta mutation records (`insert`, `update`) persist serialized JSON values, converting `JSON_NULL` to canonical `"null"` while preserving SQL NULL.
   - Recovery re-materializes rows through `coerce_value`, guaranteeing round-trip fidelity across crashes.
3. **In-Memory Undo Journal**:
   - `UpdateUndo` captures deep-copied before-images of rows, guaranteeing that rolled back transactions restore pristine nested structures without side effects.
4. **MVCC Snapshots**:
   - Published roots are immutable. Writers use isolated copy-on-write workspaces.
   - Read queries return deep-copied document values, preventing external Python mutation from corrupting database roots.
5. **Offline Backup and Restore**:
   - Databases containing JSON columns and collections are fully supported by `backup_database`, `verify_backup`, and `restore_database`.
   - Backup manifests record cryptographic SHA-256 hashes for all database files and segment directories; corruptions or tampering are detected fail-closed.

---

## 5. Scalar JSON Path Indexes (Phase 6)

### 5.1 DDL Syntax
Scalar path indexes are created using dot-navigation over a JSON or DOCUMENT column:

```sql
AAYUSH CREATE INDEX idx_cust_city ON customers (body.address.city);
AAYUSH CREATE INDEX idx_events_user_id ON events (payload.user.id);
AAYUSH DROP INDEX idx_cust_city;
```

### 5.2 Memcomparable Binary Key Encoding
Path indexes utilize a typed, order-preserving memcomparable byte encoding (`aaysql.index_encoding`) that preserves SQL comparison order under standard byte comparisons:

- **Type Ordering**: `SQL NULL < BOOL < NUMBER < TEXT`.
- **Boolean Encoding**: `0x10` for FALSE, `0x11` for TRUE.
- **Numeric Encoding**: 64-bit IEEE float representation with sign bit inversion and scientific exponent bias; integer and float equivalents compare equal (`42 == 42.0`).
- **Text Encoding**: UTF-8 bytes terminated by `0x00`, with escaping (`0x00 0xFF` for literal null bytes), guaranteeing prefix-freedom.
- **Index Eligibility**: Only scalar values (null, bool, int, float, str) are indexable; arrays and objects are omitted from the index without error.

### 5.3 Query Planner & Optimization
The query planner detects scalar path expressions matching registered indexes:
- Supports `=`, `>`, `>=`, `<`, `<=`, and `BETWEEN`.
- Generates `INDEX SCAN` with candidate range bounds and path annotation:
  ```
  INDEX SCAN: idx_events_user_id
    TABLE: events
    PATH: payload.user.id
    CONDITION: payload.user.id = 42
  ```
- Evaluates candidate selectivity against in-memory path statistics (`StatisticsStore`).
- Re-verifies predicates on candidate rows to guarantee filtering precision.

---

## 6. Collection Validation Modes & Schema Versioning

### 6.1 Validation Modes
Collections support three validation modes:
1. **`FLEXIBLE`** (Default): Accepts any structurally valid JSON document up to resource limits.
2. **`WARN`**: Validates incoming documents against the collection schema. If invalid, the write succeeds, `warn_count` increments, and a diagnostic summary is saved in `last_warn`.
3. **`STRICT`**: Enforces schema validation before mutations occur. Invalid writes raise `ConstraintError` naming the exact failing path and reason; mutations are aborted without side effects.

### 6.2 Supported JSON Schema Subset
A documented, strict subset of JSON Schema is supported fail-closed:
- Keywords: `type`, `required`, `properties`, `additionalProperties`, `items`, `enum`, `minimum`, `maximum`, `minLength`, `maxLength`, `pattern`.
- Types: `object`, `array`, `string`, `integer`, `number`, `boolean`, `null`.
- Unknown or unsupported keywords (e.g. `$ref`, `oneOf`, `allOf`) fail-closed at definition time.

### 6.3 Schema DDL & Inspection
```sql
AAYUSH CREATE COLLECTION users
  VALIDATION MODE = 'STRICT'
  SCHEMA = '{"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}';

AAYUSH ALTER COLLECTION users SET VALIDATION MODE = 'WARN';
AAYUSH ALTER COLLECTION users SET SCHEMA = '{"type": "object", "required": ["name", "email"]}';
AAYUSH VALIDATE COLLECTION users;
```

---

## 7. Collection Identity, Revisions & Concurrency

### 7.1 Monotonic UUIDv7 Identity (`_id`)
- Every collection table contains a primary key column `_id TEXT PRIMARY KEY`.
- If `_id` is omitted in `INSERT`, a time-ordered, RFC 9562-compliant UUIDv7 is auto-generated.
- Explicit `_id` values provided by callers are preserved.
- `_id` is immutable; `UPDATE` statements attempting to modify `_id` are rejected with `ExecutionError`.

### 7.2 Revision Tracking & Optimistic Concurrency Control (`_rev`)
- Collections track an auto-incrementing `_rev INT` column (starts at 1).
- Any update modifying the document `body` increments `_rev`.
- Supports optimistic concurrency control:
  ```sql
  AAYUSH UPDATE docs
  SET body = '{"status": "processed"}'
  WHERE _id = '01a1e018-fad3-7e2b-a345-6b53e246d6a0' AND _rev = 2;
  ```

---

## 8. Cross-Model Relational / Document Joins

Relational tables and document collections can be seamlessly joined across scalar paths:
```sql
AAYUSH SELECT u.id, u.name, e.body.action
FROM users u
JOIN events e ON u.id = e.body.user_id;
```

The planner supports:
- **Nested Loop Join** for smaller workloads or non-equality predicates.
- **Hash Join with Path Extraction**: The hash join builder extracts join keys directly from dotted document paths (`_hash_key`), evaluating joins up to 370x faster than nested-loop scans.


