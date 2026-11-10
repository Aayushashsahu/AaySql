# AayushSQL v0.4.0 Index Benchmarks

Measurements produced by `benchmarks/benchmark_index.py` on the machine below.
Nothing here is enforced in CI: the script has no timing assertions, so CI does not
fail on slow or noisy runners.

## Environment

- OS: Windows 10, Python 3.11.15, `cpu_count=8`
- Engine: in-memory (`Engine()`), so no `.aaydb` write is included unless stated
- Dataset: `bench (id INT, age INT, name TEXT)` with `age` unique per row, built in
  2,000-row `INSERT` batches
- Method: median of 7 executions per query; `EXPLAIN ANALYZE` provides the reported
  `ROWS SCANNED` from the access path actually used

Reproduce with:

```bash
python benchmarks/benchmark_index.py --sizes 1000,10000,100000
```

## Access path: table scan versus index

`median_ms` is the unindexed table scan; `with_index_ms` is the same query after
`CREATE INDEX idx_bench_age ON bench (age)`.

| Rows | Query | Plan | Rows scanned | Table scan (ms) | Index scan (ms) |
|---:|---|---|---:|---:|---:|
| 1,000 | `age = 500` | INDEX SCAN | 1 | 1.383 | 0.084 |
| 1,000 | `age >= 900` (100 rows) | INDEX SCAN | 100 | 1.443 | 0.233 |
| 1,000 | `age >= 500` (500 rows) | INDEX SCAN | 500 | 1.366 | 0.890 |
| 10,000 | `age = 5000` | INDEX SCAN | 1 | 11.756 | 0.051 |
| 10,000 | `age >= 9900` (100 rows) | INDEX SCAN | 100 | 12.420 | 0.214 |
| 10,000 | `age >= 5000` (5,000 rows) | INDEX SCAN | 5,000 | 13.182 | 8.775 |
| 100,000 | `age = 50000` | INDEX SCAN | 1 | 167.730 | 0.065 |
| 100,000 | `age >= 99900` (100 rows) | INDEX SCAN | 100 | 178.048 | 0.226 |
| 100,000 | `age >= 50000` (50,000 rows) | INDEX SCAN | 50,000 | 234.522 | 182.385 |

Observations:

- Equality lookups stop growing with table size: the unindexed scan is linear
  (1.4 ms → 11.8 ms → 167.7 ms), while the index lookup stays roughly flat
  (0.08 ms → 0.05 ms → 0.07 ms), about 2,500× faster at 100,000 rows.
- Narrow range scans follow the same pattern because only the matching rows are read.
- Wide range scans (half the table) are dominated by materializing and order-preserving
  sorting of the result, so the gain is modest (234.5 ms → 182.4 ms). The index still
  avoids evaluating the predicate over the whole table.

## Index build cost

| Rows | Build rows (ms) | `CREATE INDEX` (ms) |
|---:|---:|---:|
| 1,000 | 17.9 | 2.112 |
| 10,000 | 248.8 | 28.906 |
| 100,000 | 2,571.8 | 470.440 |

Index construction is a single ordered pass over the table's rows, so it scales with
`n log n` on top of the row-building cost.

## Mutation overhead (10,000 rows, 200 statements, ms per statement)

| Operation | No index | With index |
|---|---:|---:|
| `INSERT` | 0.0640 | 0.0797 |
| `UPDATE ... WHERE age = k` | 11.2890 | 11.5183 |
| `UPDATE ... WHERE name = 'userk'` | 11.1617 | 11.2990 |
| `DELETE ... WHERE age = k` | 10.5228 | 10.9211 |

Optional: the extra cost of maintaining the B+ tree is small (roughly 0.02–0.4 ms per
statement here). `UPDATE`/`DELETE` row selection is **not** index-assisted in v0.4.0:
their filters still scan the table, so these columns measure index maintenance
overhead, not a speedup. Index-assisted filtering currently applies to `SELECT` only.

## Storage overhead (10,000 rows)

| `.aaydb` size | Bytes |
|---|---:|
| Without index | 997,586 |
| With index | 997,669 |

Only the index definition (`table`, `column`) is persisted; the B+ tree is rebuilt
from rows on open. The on-disk difference is therefore 83 bytes, not a serialized tree.

## Caveats

- Measurements come from one machine and one Python version; absolute timings will vary.
- The benchmark table has no `PRIMARY KEY`/`UNIQUE` constraints so that row insertion
  measures index behavior rather than the engine's existing per-insert constraint scan.
- In-memory execution excludes persistence cost. Persistent databases additionally
  clone the database and write the full JSON file per mutating statement, which is the
  dominant cost in that mode and is not an indexing regression.
- Wide range scans include materializing every matching row and restoring table order.
