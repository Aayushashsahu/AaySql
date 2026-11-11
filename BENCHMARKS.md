# AayushSQL Benchmarks

## v0.8.0 client/server benchmarks

Produced by `benchmarks/benchmark_server.py`. Nothing here is enforced in CI: the
script has no timing assertions and CI does not fail on slow or noisy runners.

### Environment

- OS: Windows 10 (10.0.26300), Python 3.11.15 (CPython)
- Server and client on the same machine, over real TCP on `127.0.0.1` (ephemeral port)
- Database: on-disk `.aaydb` in a temporary directory, so autocommit `INSERT`
  numbers include the engine's normal WAL `fsync` durability cost
- One discarded warm-up per scenario; reported value is the **median** per-request
  wall clock, with p95 and the observed min/max range
- Benchmark tables carry no `PRIMARY KEY`/`UNIQUE` constraints (the engine's
  per-row uniqueness check is linear in table size)
- 4 clients in the multi-client scenario, 60 requests each; 1,360 requests total

Reproduce with:

```bash
python benchmarks/benchmark_server.py
```

### Connection and transport

| Scenario | n | median | p95 | min | max |
| --- | --- | --- | --- | --- | --- |
| connect + `hello` handshake | 30 | 1.49 ms | 18.10 ms | 1.14 ms | 22.92 ms |
| `ping` round trip (no SQL) | 300 | 0.22 ms | 0.46 ms | 0.09 ms | 1.37 ms |
| connect + ping + close (churn) | 50 | 2.64 ms | 11.06 ms | 1.89 ms | 26.27 ms |

`ping` measures framing plus the TCP round trip and nothing else. Connection
setup is dominated by socket accept and handshake bookkeeping, and its p95 is
noisy on Windows.

### SQL execution

| Scenario | n | median | p95 | min | max |
| --- | --- | --- | --- | --- | --- |
| point `SELECT`, 1 column (1,000-row table, scan) | 200 | 2.05 ms | 2.65 ms | 1.52 ms | 3.11 ms |
| point `SELECT`, 3 columns | 200 | 1.92 ms | 2.41 ms | 1.54 ms | 4.65 ms |
| full scan `SELECT` (1,000 rows) | 50 | 2.55 ms | 3.40 ms | 2.11 ms | 5.49 ms |
| `INSERT` autocommit (WAL `fsync`) | 100 | 6.03 ms | 50.13 ms | 4.39 ms | 315.44 ms |
| `BEGIN` + 20 `INSERT` + `COMMIT` | 10 | 15.33 ms | 19.89 ms | 13.45 ms | 19.89 ms |

A single `INSERT` costs roughly 2–3× a `SELECT` because autocommit pays for the
WAL append and `fsync`; the p95/max outlier is an `fsync` stall on this machine,
not protocol overhead. An explicit transaction amortizes one commit over 20
inserts. These numbers are per *statement*; statements execute serially (see
below).

### Large result transfer

| Scenario | n | median | p95 | min | max |
| --- | --- | --- | --- | --- | --- |
| `SELECT` 2,000 rows × 200 chars (~0.46 MB JSON) | 20 | 3.91 ms | 8.34 ms | 3.50 ms | 26.23 ms |

A ~0.46 MB result travels well inside the 1 MiB frame limit; the database file
was 0.54 MB at that point. The dominant cost is materialising rows and JSON
encoding, not the socket.

### Multiple simultaneous clients (serialized execution)

| Scenario | n | median | p95 | throughput | failures |
| --- | --- | --- | --- | --- | --- |
| 4 clients × 60 `ping` | 240 | 0.44 ms | 0.76 ms | 7,995 requests/s | 0 |

Concurrent clients do **not** scale database work. Every statement passes through
one execution lock, so aggregate throughput is bounded by serialized execution:
multiple clients improve accessibility and application integration, not raw
query throughput. The multi-client scenario uses `ping` deliberately — it shows
the transport stays correct and correlated under concurrency without implying
parallel database work. No row corruption, cross-session response mix-up, or
transaction leakage was observed (`tests/test_client_server.py` asserts this over
real sockets).

### Caveats

- Loopback only: these numbers say nothing about network latency or bandwidth.
- Windows timing granularity and antivirus/`fsync` behaviour explain the wide
  `INSERT` p95 compared with its median; the median is the comparable figure.
- In-memory mode (`--memory`) removes WAL `fsync` cost from `INSERT` entirely.
- Absolute numbers will vary by machine; the ratios (SQL ≫ transport, autocommit
  ≫ transactional insert) are the reproducible part.

## v0.7.0 optimizer and hash-join benchmarks

Produced by `benchmarks/benchmark_optimizer.py`. Nothing here is enforced in CI:
the script has no timing assertions and CI does not fail on slow or noisy runners.

### Environment

- OS: Windows 10 (10.0.26300), Python 3.11.15 (CPython), Intel64 Family 6 Model 189
- Engine: in-memory (`Engine()`), so no `.aaydb` write or WAL `fsync` is included
- Seed: `20261111` (deterministic datasets); repeats: 5 (3 for the nested-loop
  reference), plus 1 discarded warm-up; reported value is the **median** wall-clock
  per query, with the observed min/max spread
- Reference path: forced nested-loop joins over table scans
  (`plan_select(..., force_join_algorithm="NESTED LOOP", force_access_path="TABLE")`),
  i.e. the v0.6.x implementation
- Benchmark tables carry no `PRIMARY KEY`/`UNIQUE` constraints: the engine's existing
  per-row uniqueness check is linear in table size, so constraints would make dataset
  creation the bottleneck rather than the query

Reproduce with:

```bash
python benchmarks/benchmark_optimizer.py
```

### Join benchmarks

`users` (left, `id`) joins `orders` (right, `user_id`) on `u.id = o.user_id`.
Every optimized row count was compared with the reference path wherever the
reference path was measured, and they are identical.

| Scenario | Join | Rows out | Plan | Optimized median | Reference median | Speedup |
|---|---|---:|---|---:|---:|---:|
| 1k × 1k | INNER | 1,000 | HASH INNER ← TABLE SCAN ← TABLE SCAN | 7.0 ms | 3,374.8 ms | 480× |
| 1k × 1k | LEFT | 1,368 | HASH LEFT ← TABLE SCAN ← TABLE SCAN | 9.9 ms | 2,749.2 ms | 277× |
| 10k × 10k | INNER | 10,000 | HASH INNER | 84.4 ms | not measured (10⁸ comparisons) | — |
| 10k × 10k | LEFT | 13,624 | HASH LEFT | 108.1 ms | not measured | — |
| 100k × 10k | INNER | 10,000 | HASH INNER | 184.6 ms | not measured (10⁹ comparisons) | — |
| 100k × 10k | LEFT | 100,498 | HASH LEFT | 560.6 ms | not measured | — |
| 10k × 100k | INNER | 100,000 | HASH INNER | 964.1 ms | not measured (10⁹ comparisons) | — |
| 10k × 100k | LEFT | 100,001 | HASH LEFT | 867.8 ms | not measured | — |
| 10k × 10k high duplicate (10 keys) | INNER | 10,000 | HASH INNER | 66.9 ms | not measured | — |
| 10k × 10k high duplicate (10 keys) | LEFT | 19,990 | HASH LEFT | 106.6 ms | not measured | — |
| 10k × 10k skewed (80% in 25 keys) | INNER | 10,000 | HASH INNER | 78.1 ms | not measured | — |
| 10k × 10k skewed | LEFT | 18,143 | HASH LEFT | 110.9 ms | not measured | — |
| 10k × 10k many unmatched | INNER | 0 | HASH INNER | 17.2 ms | not measured | — |
| 10k × 10k many unmatched | LEFT | 10,000 | HASH LEFT | 48.5 ms | not measured | — |

Observed spread for the measured reference runs: 1k × 1k INNER 2,681.7–4,336.1 ms,
LEFT 2,624.5–3,611.7 ms. Optimized spreads stay within roughly ±25% of the median
(e.g. 10k × 100k INNER 876.4–1,149.0 ms).

### Why most reference runs were not measured

The reference nested-loop path performs about **320,000 comparisons/second** on this
machine (measured: 500 × 500 INNER, 250,000 comparisons, 780.0 ms median). A
10k × 10k join needs 10⁸ comparisons and 100k × 10k needs 10⁹, so those rows report
extrapolations instead of measurements:

| Scenario | Comparisons | Extrapolated reference time |
|---|---:|---:|
| 10k × 10k | 100,000,000 | ~312 s |
| 100k × 10k | 1,000,000,000 | ~3,120 s |
| 10k × 100k | 1,000,000,000 | ~3,120 s |

These are extrapolations from a measured rate, not measurements, and no speedup
figure is claimed for them.

### Access-path selection (`t` 50,000 rows, index on `k`)

The planner chose as follows; both paths were measured on identical data and returned
identical rows.

| Predicate | Rows out | Chosen plan | Table scan | Index scan | Chosen time |
|---|---:|---|---:|---:|---:|
| `k = 4242` | 1 | INDEX SCAN | 68.89 ms | 0.06 ms | 0.02 ms |
| `k >= 49990` | 11 | INDEX SCAN | 73.87 ms | 0.04 ms | 0.04 ms |
| `k >= 25000` | 25,001 | INDEX SCAN | 135.46 ms | 108.20 ms | 105.22 ms |
| `k >= 0` | 50,000 | TABLE SCAN | 216.47 ms | 217.28 ms | 214.16 ms |

- Narrow lookups are ~1,000× faster with the index, which is why the cost model
  selects it there.
- For a half-table range the index is still cheaper (108 ms vs 135 ms) because it
  avoids evaluating the predicate over 50,000 rows, even though it must restore table
  order. The model selects the index.
- For a full-table range the two paths are equal within noise (216 ms vs 217 ms).
  The model selects the table scan because the index must read every row *and* restore
  order; the small absolute gap is honestly reported rather than presented as a win.

### Caveats

- One machine and one Python version; absolute timings will vary, and the relative
  ordering of two in-memory operators this fast is sensitive to cache and GC noise.
- In-memory execution excludes persistence cost; persistent databases clone the
  database and rewrite the JSON snapshot per mutating statement, which dominates there.
- Hash-join timing includes plan construction and statistics computation on the first
  call only; repeated runs reuse the revision-validated statistics cache.
- No speedup is claimed where the reference path was not measured.

---

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
