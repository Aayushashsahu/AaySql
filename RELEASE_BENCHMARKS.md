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

---

## 5. Format v4 Storage (Phase 2, development)

Produced by [`benchmarks/benchmark_v4.py`](benchmarks/benchmark_v4.py) on the same
machine as this document (Windows 10, CPython 3.11.15, 8 cores). The dataset is one
table with an `INT PRIMARY KEY` and a `TEXT` payload; the measured operation is a
single-row autocommit `INSERT` followed by an explicit checkpoint. Medians of 5
runs; no timing assertions exist in CI. The v3 column is the same-machine baseline
(`benchmark_release.py` seeds a v3 snapshot), where an autocommit commit includes a
whole-image WAL write and a full snapshot rewrite.

| Rows | v4 commit | v4 WAL bytes | v4 checkpoint | v4 checkpoint bytes | v4 reopen (replay 1 delta) | replayed WAL bytes | v3 commit+checkpoint | v3 snapshot bytes |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 3.25 ms | 675 | 10.07 ms | 1,819 | 36.08 ms | 673 | 10.85 ms | 7,953 |
| 1,000 | 9.65 ms | 676 | 10.50 ms | 13,488 | 35.98 ms | 674 | 24.80 ms | 74,521 |
| 10,000 | 17.12 ms | 677 | 24.75 ms | 138,425 | 133.04 ms | 675 | 139.08 ms | 748,457 |
| 100,000 | 72.13 ms | 678 | 190.64 ms | 1,478,308 | 785.63 ms | 676 | 1,207.32 ms | 7,578,339 |

What this shows:

- **Commit bytes are flat.** A one-row delta costs ~675 bytes of WAL at every size,
  while the v3 image grows linearly to ~7.6 MB per commit.
- **Checkpoint bytes scale with the changed table**, not with the transaction: the
  100,000-row checkpoint writes a ~1.48 MB segment, without rewriting other tables.
- **The v3 baseline pays both costs per commit.** At 100,000 rows v4 commit plus
  checkpoint is roughly 263 ms of measured work versus ~1,207 ms for v3, but this is
  a same-machine observation, not a general speedup claim.
- **Recovery/reopen remains O(database).** Reopen loads every segment and rebuilds
  index trees; at 100,000 rows that dominates (786 ms) even though only 676 bytes of
  WAL were replayed. No O(1) claim is made while `Database.clone()` and full loads
  remain.
- **Unchanged tables get zero writes.** In the two-table check, changing and
  checkpointing `hot` left `cold`'s segment file name and mtime untouched and wrote
  0 bytes for it; the only new segment file belonged to `hot`.
- **Noise.** Windows NTFS `fsync` tails dominate single-row commit latency; the
  100-row and 1,000-row medians moved by several milliseconds between runs. Treat
  the column as an order-of-magnitude comparison, not a precise ratio.

---

## 6. Undo-Journal Transactions (Phase 3, development)

Produced by [`benchmarks/benchmark_undo.py`](benchmarks/benchmark_undo.py) with
`PYTHONPATH=src python benchmarks/benchmark_undo.py`. The script uses the same cases
and settings for the Phase 2 baseline and the Phase 3 journal: in-memory engine,
one secondary index on `value`, no index on `id`, warm-up 1, 3 measured repetitions
(median shown), sizes 100 / 1,000 / 10,000 / 100,000 rows. The baseline was re-run
from commit `070a932` in a clean worktree with this same script, so the row counts
match exactly. Timings are one machine and are not CI gates.

`MUTATIONS = 10`; `txn_100_rows` is one 100-row statement. `rollback_*` cases make
10 statements inside one transaction and roll back. `transaction_memory` reports
`tracemalloc` peak for 10 single-row UPDATEs (the database itself is not allocated
inside the probe).

| Case | 100 rows | 1,000 rows | 10,000 rows | 100,000 rows |
| --- | ---: | ---: | ---: | ---: |
| `begin_rollback` | 0.24 -> 0.04 ms | 3.60 -> 0.02 ms | 64.50 -> 0.03 ms | 1168.48 -> 0.02 ms |
| `txn_1_row` | 0.55 -> 0.12 ms | 8.15 -> 0.13 ms | 84.74 -> 0.92 ms | 3308.91 -> 9.23 ms |
| `txn_10_rows` | 1.57 -> 0.89 ms | 8.89 -> 1.40 ms | 83.13 -> 5.51 ms | 3402.57 -> 63.71 ms |
| `txn_100_rows` | 9.16 -> 8.31 ms | 22.84 -> 11.51 ms | 142.72 -> 50.16 ms | 3810.09 -> 668.03 ms |
| `multi_statement_10` | 8.32 -> 1.55 ms | 52.34 -> 2.22 ms | 568.69 -> 5.75 ms | 11952.52 -> 91.15 ms |
| `rollback_inserts` | 9.78 -> 1.90 ms | 55.14 -> 2.55 ms | 518.65 -> 6.42 ms | 11125.99 -> 89.55 ms |
| `rollback_updates` | 15.90 -> 19.74 ms | 85.75 -> 34.88 ms | 689.30 -> 348.91 ms | 13251.25 -> 3458.20 ms |
| `rollback_deletes` | 14.52 -> 18.06 ms | 78.66 -> 35.78 ms | 745.83 -> 379.94 ms | 10551.77 -> 3475.84 ms |
| `rollback_ddl` | 15.48 -> 8.56 ms | 90.78 -> 28.14 ms | 827.42 -> 367.09 ms | 16333.57 -> 8280.66 ms |
| `failed_statement` | 1.35 -> 0.28 ms | 10.46 -> 0.24 ms | 78.96 -> 1.64 ms | 1462.04 -> 14.80 ms |
| `autocommit_insert` | 0.12 -> 0.10 ms | 0.18 -> 0.19 ms | 0.51 -> 0.94 ms | 8.28 -> 8.97 ms |
| `commit_no_mutations` | 0.56 -> 0.02 ms | 5.21 -> 0.03 ms | 39.52 -> 0.02 ms | 762.41 -> 0.03 ms |
| `transaction_memory` (peak) | 76 -> 6 KiB | 530 -> 6 KiB | 4,675 -> 6 KiB | 48,905 -> 6 KiB |

Persistent v4 autocommit INSERT (median, including WAL append and size-triggered
checkpoint work; no explicit transaction):

| Rows | Phase 2 | Phase 3 |
| ---: | ---: | ---: |
| 100 | 1.82 ms | 1.97 ms |
| 1,000 | 4.68 ms | 3.08 ms |
| 10,000 | 58.78 ms | 3.19 ms |
| 100,000 | 741.33 ms | 10.73 ms |

What this shows:

- **BEGIN/COMMIT/ROLLBACK are independent of database size.** `begin_rollback` drops
  from 1.17 s to ~0.02 ms at 100,000 rows, and an empty commit from 762 ms to
  ~0.03 ms, because no clone happens.
- **Rollback cost follows the mutations.** 10 inserts roll back in ~90 ms at 100,000
  rows after paying the 10 table scans used to validate them; the undo itself is
  removal of the appended rows. Journal peak memory is ~6 KiB at every size.
- **`rollback_updates` / `rollback_deletes` are scan-bound, not undo-bound.** Each of
  the 10 statements scans the table (no index on `id`), so the case is ~3.5 s at
  100,000 rows even though only 10 inverse entries are applied. With an index the
  scans disappear; the undo work stays proportional to rows changed.
- **`rollback_ddl` is index-build-bound.** Five `CREATE INDEX` / `DROP INDEX` rounds
  build and drop trees over every row; rollback restores the prior index state
  without rebuilding. At 100,000 rows that is ~8.3 s, dominated by the five builds.
- **`txn_100_rows` is primary-key-validation-bound.** Every new row is checked
  against the existing rows in the same batch, so one 100-row statement is O(100 x
  table rows). This behavior predates the undo journal and is unchanged; inserting
  the same 100 rows as separate statements (`multi_statement_10` is 10 rows) avoids
  the quadratic validation.
- **Autocommit is unchanged within noise.** It performs the same single mutation and
  WAL commit as before; the journal is empty after success.
- **No speedup beyond these measurements is claimed.** Load, backup, restore, v3
  snapshot commits, and v4 changed-table checkpoints remain O(database/table), and
  `Database.clone()` remains O(database) for direct API users.

## Phase 4 — MVCC readers and group commit

Measured with `python benchmarks/benchmark_concurrency.py --workers 8 --per-worker 10`
on Windows 11, Python 3.11, local NTFS; 80 durable autocommit INSERT transactions
on a format-v4 database.

| Path | Throughput | WAL fsyncs | Batches | Notes |
| --- | ---: | ---: | ---: | --- |
| One session, serial | 130.2 txn/s | 80 | 80 | one fsync per transaction |
| 8 sessions, grouped | 785.8 txn/s | 10 | 10 | 8 transactions per fsync |
| Reader while writer is open | 0.08 ms median | — | — | 50 point reads, max 0.53 ms |

The grouped path produced **6.0× higher commit throughput** and reduced WAL
fsyncs from 80 to 10. All 80 rows were present after commit; the v4 WAL replay
was exercised independently by `tests/test_group_commit.py` and the subprocess
crash test. These are local single-host numbers, not a hardware-independent
claim.

## Phase 4.1 — Python 3.10 stabilization recheck

Same workload and command as Phase 4: `python benchmarks/benchmark_concurrency.py
--workers 8 --per-worker 10`, Windows 11, local NTFS, Python 3.11.15, 80 durable
autocommit INSERT transactions on format v4.

| Mode | Throughput | Transactions | WAL fsyncs | Batches |
| --- | ---: | ---: | ---: | ---: |
| Serial | 142.3 txn/s | 80 | 80 | 80 |
| Grouped | 805.5 txn/s | 80 | 10 | 10 |
| Reader while writer is open | 0.08 ms median | — | — | max 0.39 ms |

This is a 5.7× grouped-commit improvement and confirms the Python 3.10 fix did
not serialize work across the fsync. The Phase 4 run measured 130.2 / 785.8
txn/s (6.0×); the difference is normal single-host run-to-run variance.
