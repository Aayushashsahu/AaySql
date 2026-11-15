# AayushSQL 2.0 — Phase 6: Completing Documents v1 Design Document

## 1. Implementation Matrix & Architecture Audit

| Capability | Research Requirement | Current Implementation (Phase 5.1) | Gap | Proposed Architectural Change |
|---|---|---|---|---|
| **Scalar Path Indexes** | ADR-13: B+ tree backed, scalar-only, single path expression | Table-column indexes only (`table`, `column`) | No path index syntax, storage metadata, or tree builders | Support `CREATE INDEX ... ON tbl (col.path.to.field)`, persist parsed path in index catalog, index scalar leaf values |
| **Typed Key Encoding** | ADR-13: Memcomparable byte encoding with total order across JSON types | None (indexes compare raw Python types directly) | Heterogeneous JSON values (`int`, `float`, `bool`, `str`, `JSON_NULL`) crash or misorder Python comparisons | Implement `index_encoding.py` using `type_tag \| encoded_value \| terminator \| row_id` with unified binary scientific numeric space |
| **FLEXIBLE Validation** | ADR-08: Structural and resource validation only | Structural parsing and 1 MiB / depth 64 / duplicate key limits enforced | Collection-level validation mode metadata not tracked | Track collection `validation_mode = 'FLEXIBLE'` by default in catalog |
| **WARN Validation** | ADR-08: Evaluate schema, record warnings, permit write | Not implemented | No schema validator, no warning counter or sample recorder | Implement schema validator, bounded warning counter (`warn_count`, `last_warn_sample`, `last_warn_at`) in catalog |
| **STRICT Validation** | ADR-08: Reject writes violating schema before mutation | Not implemented | No write-time schema enforcement | Evaluate schema in `INSERT`/`UPDATE`, reject violations with `ConstraintError` naming violation path and reason |
| **Schema Versions** | ADR-08: Catalog versioning, document tracking, explicit revalidation | None | No schema versioning or `VALIDATE COLLECTION` | Catalog stores `schema_version`, documents record validation version, support `VALIDATE COLLECTION <name>` |
| **Automatic `_id`** | Architecture §13.4: String, unique, immutable, UUIDv7 on omit | `_id TEXT PRIMARY KEY NOT NULL` created, but caller must supply `_id` | Missing UUIDv7 generator, `_id` immutability check, and result return | Implement dependency-free UUIDv7 generator; auto-generate on omit and return in execute result; enforce immutability |
| **`_rev` Semantics** | Architecture §13.4: Monotonic integer, incremented on mutate, optimistic lock | `_rev INT NOT NULL` created, but static unless updated manually | No automatic revision increment or optimistic concurrency contract | Auto-init `_rev = 1` on insert; auto-increment `_rev = _rev + 1` on mutate; support `WHERE _id = ? AND _rev = ?` |
| **Cross-Model Joins** | ADR-06 / ADR-09: Relational planner joins across models | Hash join planner only extracted `Identifier` equality pairs | `PathAccess` in `JOIN ... ON` fell back or was rejected for hash join | Enhance `_equality_pairs` and `_hash_key` to support `PathAccess`; verify equivalence with nested-loop join |
| **Path Statistics** | Architecture §14.4: Bounded stats for declared indexed paths | Only table-level scalar column statistics tracked | Path statistics absent | Maintain per-path counts, nulls, missing, min/max, distinct estimates in `StatisticsStore` |

---

## 2. Stage 2: Typed Memcomparable Index Key Encoding

### 2.1 Specification & Type Tags
A unified B+ tree requires a consistent, memcomparable total order over arbitrary JSON scalars:

$$\text{NULL} (0\text{x}10) < \text{BOOL} (0\text{x}20) < \text{NUMBER} (0\text{x}30) < \text{TEXT} (0\text{x}40)$$

Composite Key Layout:
```text
type_tag (1B) | encoded_value (variable) | terminator (0x00) | row_id (8B big-endian)
```

1. **NULL Tag (`0x10`):**
   - Encodes `JSON_NULL` explicitly: `b"\x10\x00"` + row_id.
   - Relational SQL NULL (`None`) and `ABSENT` are **not** indexed (they are skipped).
   - Arrays and Objects are non-scalar and **not** indexed (they are skipped).

2. **BOOL Tag (`0x20`):**
   - `False` $\to$ `b"\x20\x00\x00"` + row_id
   - `True` $\to$ `b"\x20\x01\x00"` + row_id
   - Booleans never compare equal to integers or floats; `0x20 < 0x30` guarantees complete separation from numeric 0 and 1.

3. **NUMBER Tag (`0x30`):**
   - Sub-tags:
     - Negative numbers: `0x01`
     - Zero (`0`, `0.0`, `-0.0`): `0x02`
     - Positive numbers: `0x03`
   - Unified Binary Scientific Form for non-zero finite numbers:
     $$|x| = 2^e \times (1 + \text{fraction}), \quad e = b - k, \quad \text{fraction} \in [0, 1)$$
     Where $(num, den) = x.\text{as\_integer\_ratio()}$, $den = 2^k$, $b = num.\text{bit\_length}() - 1$.
   - Positive Payload:
     - Exponent: `e + 2000` (biased 16-bit unsigned big-endian integer, `>H`).
     - Fraction: $\text{frac\_u64} = (num - 2^b) \ll (64 - b)$ (64-bit unsigned big-endian integer, `>Q`).
     - Byte string: `b"\x30\x03" + struct.pack(">HQ", e + 2000, frac_u64) + b"\x00"`.
   - Negative Payload:
     - Byte-inverted positive magnitude payload: `b"\x30\x01" + bytes(255 - b for b in payload) + b"\x00"`.
   - Zero:
     - `b"\x30\x02\x00"`.
   - Invariants:
     - $1 == 1.0 \implies \text{encode}(1) == \text{encode}(1.0)$.
     - $0 == 0.0 == -0.0 \implies \text{all encode to } b"\x30\x02\x00"$.
     - Negative numbers sort strictly before 0; 0 sorts strictly before positive numbers.
     - Preserves full 64-bit integer precision and full float64 subnormal range.

4. **TEXT Tag (`0x40`):**
   - UTF-8 binary byte order.
   - Since null bytes `\u0000` are strictly prohibited in AayushSQL strings, terminator `0x00` guarantees prefix safety:
     `"abc"` (`b"\x40abc\x00"`) sorts strictly before `"abcd"` (`b"\x40abcd\x00"`).

---

## 3. Stage 3 & 4: Scalar Path Index DDL and Maintenance

### 3.1 DDL Syntax & Catalog Representation
Syntax:
```sql
AAYUSH CREATE INDEX idx_name ON table_or_collection (column.path.to.field);
```
Catalog representation in database `indexes` dictionary:
```json
{
  "name": "idx_name",
  "table": "events",
  "column": "body",
  "path": ["user", "country"]
}
```

### 3.2 Index Lifecycle & Invariants
- **Creation:** Validates table exists, column is typed `JSON` or `DOCUMENT`, parses path, scans existing rows, encodes indexable scalar values, builds B+ tree, and publishes atomically.
- **Mutation Maintenance:**
  - `INSERT`: Extracts path, encodes if scalar, inserts into B+ tree.
  - `UPDATE`: Extracts old value at path and new value at path. If changed, removes old key and inserts new key. If untouched, does nothing.
  - `DELETE`: Extracts path, removes entry from B+ tree.
  - `ROLLBACK`: Uses inverse entries in in-memory undo journal to restore row and index state.

---

## 4. Stage 5 & 6: Planner Selection, Recheck, and Statistics

### 4.1 Plan Selection
When matching predicates in `WHERE` / `JOIN ON`:
- Identifies `PathAccess(Identifier(col), path) <op> Literal(val)` or `Between`.
- Matches against declared path indexes where index `column == col` and index `path == path`.
- For equality: point lookup `search(encode_value(val))`.
- For range (`<`, `<=`, `>`, `>=`, `BETWEEN`): encoded boundary range scan.
- **Predicate Recheck:** Every candidate row returned by index scan is rechecked against the original SQL predicate, maintaining the predicate as the ultimate source of truth.
- **EXPLAIN Output:** Displays `INDEX SCAN (path body.user.country)` with index name, estimated rows, and filter recheck.

### 4.2 Path Statistics
`StatisticsStore` tracks per-path metadata:
- Total rows, present scalar count, JSON null count, missing count.
- Min and max values in encoded order.
- Distinct value estimates using bounded exact set / HyperLogLog approximation.

---

## 5. Stage 7, 8 & 9: Collection Validation, Schema Subset & Versioning

### 5.1 Validation Modes
- `FLEXIBLE` (default): Enforces RFC 8259, document size ($\le 1$ MiB), depth ($\le 64$), key length ($\le 1$ KiB), path segments ($\le 32$), no duplicate keys, finite floats, valid UTF-8.
- `WARN`: Evaluates declared schema. If invalid, increments collection `warn_count`, records sample `{"_id": id, "path": path, "reason": reason, "timestamp": ts}`, but allows the write.
- `STRICT`: Evaluates declared schema. If invalid, immediately raises `ConstraintError(f"Validation failed at '{path}': {reason}")`, aborting the statement atomically.

### 5.2 Supported Schema Subset
- Keywords: `type`, `required`, `properties`, `additionalProperties`, `items`, `enum`, `minimum`, `maximum`, `minLength`, `maxLength`, `pattern` (safe bounded regex).
- Supported types: `object`, `array`, `string`, `integer`, `number`, `boolean`, `null`.
- Unsupported keywords (`$ref`, `allOf`, `anyOf`, `oneOf`) fail closed with an explicit error during schema definition.

### 5.3 Catalog Metadata & `VALIDATE COLLECTION`
Collection catalog schema:
```json
{
  "name": "events",
  "validation_mode": "STRICT",
  "schema_version": 1,
  "schema": { ... },
  "warn_count": 0,
  "last_warn": null
}
```
`AAYUSH VALIDATE COLLECTION <name>;` executes a read-only scan against the active schema returning:
`{"collection": name, "schema_version": v, "total": N, "valid": V, "invalid": I, "violations": [...]}`.

---

## 6. Stage 10: Collection Identity and Concurrency

- **UUIDv7 Generator:** Generates monotonic, 128-bit RFC 9562 UUIDv7 strings without external dependencies.
- **Automatic `_id`:** If `_id` is omitted in `INSERT INTO collection (body) VALUES (...)`, generates UUIDv7, assigns to `_id`, and returns `inserted_id` to caller.
- **`_id` Immutability:** Any `UPDATE` statement attempting to set `_id` raises `ConstraintError`.
- **`_rev` Monotonicity:** Initialized to 1 on insert; incremented by 1 on every row mutation.
- **Optimistic Concurrency:** `UPDATE events SET body.k = v WHERE _id = 'e1' AND _rev = 1;` successfully updates and sets `_rev = 2`. Stale revision matches 0 rows.

---

## 7. Stage 11: Cross-Model Relational / Document Joins

- `_equality_pairs` in `planner.py` extracts equality pairs when either or both operands are `PathAccess` nodes (e.g. `u.id = e.body.user_id`).
- `_hash_key` in `execution.py` evaluates `eval_row_expr` for `PathAccess` keys.
- Hash joins and nested-loop joins are differentially tested for exact result and ordering equivalence.
