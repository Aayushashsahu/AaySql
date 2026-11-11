# AayushSQL v1.0.0 Release Benchmarks

Produced by [`benchmarks/benchmark_release.py`](benchmarks/benchmark_release.py). Nothing here is enforced in CI (no timing assertions). Measurements are split cleanly between disk-backed operations (including WAL `os.fsync()`), in-memory query evaluation, and real loopback TCP transport.

## Environment

- **OS:** Windows 10 (`Windows-10-10.0.26300-SP0`)
- **Python:** CPython `3.11.15`
- **CPU:** `Intel64 Family 6 Model 189 Stepping 1, GenuineIntel` (8 cores)
- **Format / Protocol:** `.aaydb` format v3, Network protocol v1
- **Methodology:** Deterministic seeded dataset (`seed=20261111`), 1 discarded warm-up iteration per scenario, repeated timed runs reporting median and full min–max spread.

Reproduce with:

```bash
python benchmarks/benchmark_release.py
```

---

## 1. Storage, WAL, Recovery & Backup (On-Disk)

| Scenario | Runs (`n`) | Median | Spread (min–max) |
| --- | ---: | ---: | ---: |
| Database open (1,000 rows, index rebuild) | 7 | 7.56 ms | 5.76–10.53 ms |
| Database close (clean) | 7 | 0.04 ms | 0.03–0.05 ms |
| WAL autocommit INSERT (small table) | 15 | 11.64 ms | 5.49–342.58 ms |
| Explicit BEGIN + 50 INSERTs + COMMIT | 7 | 377.96 ms | 26.03–599.79 ms |
| Checkpoint atomic write + fsync (~350 rows) | 7 | 7.40 ms | 4.90–13.95 ms |
| WAL recovery on open (1,001 rows + checkpoint) | 7 | 62.62 ms | 43.00–87.05 ms |
| Offline backup (1,000 rows + index) | 5 | 38.96 ms | 28.72–46.71 ms |
| Offline restore (verify + validate + install) | 5 | 63.69 ms | 53.76–182.13 ms |

Notes: Disk-backed write latencies include real Windows NTFS `os.fsync()` + `os.replace()` calls, which account for the wide tail spread (e.g., 5.49–342.58 ms on autocommit INSERT). Explicit transactions batch all 50 mutations into a single WAL commit + checkpoint, though per-statement `db.clone()` inside transactions accounts for intra-transaction overhead.

---

## 2. Query Engine (In-Memory)

| Scenario | Runs (`n`) | Median | Spread (min–max) |
| --- | ---: | ---: | ---: |
| Indexed equality lookup (5,000 rows) | 7 | 0.32 ms | 0.29–0.45 ms |
| Indexed range scan (`BETWEEN 20 AND 30`, 5,000 rows) | 7 | 4.07 ms | 3.44–12.26 ms |
| Forced full table scan (5,000 rows) | 7 | 10.17 ms | 8.95–11.81 ms |
| Hash join (1,500 x 2,000 equi-join) | 7 | 14.34 ms | 10.95–27.46 ms |
| Hash join (300 x 400 baseline pair) | 7 | 2.68 ms | 1.92–3.92 ms |
| Nested-loop join (300 x 400 baseline pair) | 7 | 484.98 ms | 420.61–674.46 ms |
| `GROUP BY` dept + `COUNT`/`AVG` (5,000 rows) | 7 | 8.87 ms | 7.44–9.48 ms |
| `SELECT DISTINCT` title (5,000 rows -> 50) | 7 | 12.46 ms | 9.93–16.93 ms |
| `LIKE '%doc_1%'` filter (5,000 rows) | 7 | 13.24 ms | 12.67–21.28 ms |
| `CASE` 3-way projection (5,000 rows) | 7 | 30.20 ms | 28.89–46.51 ms |
| `ORDER BY` + `OFFSET 500 LIMIT 50` (5,000 rows) | 7 | 27.36 ms | 22.80–35.12 ms |
| `EXPLAIN` (non-executing plan) | 7 | 0.17 ms | 0.16–0.25 ms |
| `EXPLAIN ANALYZE` (1,500 x 2,000 hash join) | 7 | 13.15 ms | 12.82–24.48 ms |

Baseline comparison: No regression relative to the v0.7.0 / v0.8.0 / v0.9.0 baselines recorded in [`BENCHMARKS.md`](BENCHMARKS.md); on the identical 300 x 400 equi-join input, hash join completed in 2.68 ms median vs. 484.98 ms for the nested-loop baseline, and indexed equality on 5,000 rows completed in 0.32 ms median vs. 10.17 ms for a forced full scan.

---

## 3. Client / Server (Loopback TCP, On-Disk DB)

| Scenario | Runs (`n`) | Median | Spread (min–max) |
| --- | ---: | ---: | ---: |
| TCP connect + hello handshake | 15 | 2.56 ms | 1.25–16.32 ms |
| TCP ping | 25 | 0.13 ms | 0.06–0.21 ms |
| TCP point SELECT (1 row) | 25 | 1.79 ms | 1.26–4.21 ms |
| TCP large result SELECT (1,000 rows) | 10 | 3.10 ms | 2.54–27.09 ms |
| TCP autocommit INSERT (WAL `fsync`) | 15 | 12.40 ms | 9.47–137.82 ms |
| TCP explicit BEGIN + INSERT + COMMIT | 10 | 12.56 ms | 11.63–16.57 ms |
| TCP 4 concurrent clients x 5 SELECTs | 5 | 44.29 ms | 41.83–45.79 ms |

Execution across concurrent TCP clients is serialized under `TransactionCoordinator`; no parallel query speedup is claimed.

---

## 4. Repeated-Operation Memory Growth (`tracemalloc`)

| Workload | Traced Delta | Traced Peak |
| --- | ---: | ---: |
| 500 repeated `SELECT` + `LIKE` queries (in-memory) | +2.41 KiB | 221.81 KiB |
| 40 repeated TCP connect/ping/disconnect cycles | +66.54 KiB | 221.81 KiB |

Pattern caches (`_LIKE_CACHE`, max 256 entries) and per-session request-ID tracking (`MAX_RECENT_REQUEST_IDS = 256`) remain bounded across repeated queries and connection churn.
