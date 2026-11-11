# v1.0.0 Stabilization Audit

Scope: the whole engine at `76a7443` (v0.9.0), audited component by component before
any v1.0.0 change. Findings are listed with the evidence that produced them, the
severity assigned, and the outcome. "Accepted" means the behavior is deliberate and
documented, not an untested gap.

Severity scale: **Critical** = data loss, silent corruption, or a security bypass.
**High** = wrong results or a failed recovery in a realistic scenario. **Medium** =
misleading behavior or an operational dead end. **Low** = cosmetic, packaging, or
diagnostic quality.

## Summary

| # | Component | Finding | Severity | Outcome |
| --- | --- | --- | --- | --- |
| 1 | No backup/restore facility | A user had no supported way to copy or restore a database; copying files beside a WAL is unsafe. | High | Fixed: `aaysql.backup` + `aaysql-backup` CLI |
| 2 | Comparisons | `BOOL` compared with `INT` followed Python equality, so `flag = 1` matched TRUE rows. | Medium | Fixed |
| 3 | Client framing | An oversized client *request* was reported as `RESPONSE_TOO_LARGE`. | Low | Fixed |
| 4 | Client diagnostics | `ProtocolError` discarded the stable error code. | Low | Fixed |
| 5 | Server config | `ServerConfig.from_environment(auth_token=…)` raised `TypeError` when the environment variable was also set. | Low | Fixed |
| 6 | CI | Only Ubuntu was tested; `fcntl`/`msvcrt` locking and Windows atomic replacement were unverified. | Medium | Fixed: 3 OS x 4 Python matrix |
| 7 | Packaging | No `readme`/classifiers/URLs, no `aaysql-server`/`aaysql-backup` scripts, no sdist content control, no artifact test in CI. | Medium | Fixed |
| 8 | Licensing | The repository declares no license and has no `LICENSE` file. | Medium | **Accepted**: choosing a license is the owner's decision; documented, not invented |
| 9 | Equality across unrelated types | `TEXT = 5` is `FALSE` rather than a type error. | Low | **Accepted** and documented in COMPATIBILITY.md |
| 10 | `IS NULL` / `LIKE` / `IN` access paths | These always table-scan. | Low | **Accepted**: conservative and correct; documented in COMPATIBILITY.md |
| 11 | Concurrency model | Single writer, no MVCC, no online backup. | — | **Accepted** by design; stated everywhere |

No Critical or High *correctness* defect was found in the existing engine. The one
High item was the operational absence of backup/restore.

---

## 1. Parser and expression engine

Reviewed: tokenization, string escaping (doubled quotes), unsigned numeric lexing
with sign folding, precedence, parentheses, unary operators, aggregate placement,
qualified identifiers, output-label uniqueness, `CASE` nesting, `IN`/`BETWEEN`
parsing, `LIKE` translation, and `NULL` handling.

Evidence gathered:

- Malformed input produces `LexerError`/`ParserError` with a position, never a raw
  `IndexError`/`KeyError`: `tests/test_expressions.py`, `tests/test_language_queries.py`,
  `tests/test_null_semantics.py`.
- `LIKE` translates `%` and `_` and `re.escape`s everything else; a user pattern is
  never executed as a regular expression (`test_like_escapes_regex_metacharacters`).
- Precedence is asserted both structurally and by result across
  `OR`/`AND`/`NOT`/comparison/`+ -`/`* / %`/unary.
- `BETWEEN` consumes only its own `AND`; `a BETWEEN 1 AND 5 AND b = 2` parses as two
  conjuncts (explicit test).
- `CASE` never matches on `FALSE` or `UNKNOWN`; `NULL` base never matches `WHEN NULL`.
- Every expression node is covered by the shared traversal
  (`test_expression_nodes_are_all_covered_by_the_shared_traversal`), so a new node
  cannot silently bypass validation or predicate placement.

**Finding 2 (Medium, fixed).** `compare_values` delegated `=`/`!=` to Python, where
`True == 1`. `AAYUSH SELECT id FROM t WHERE flag = 1` therefore returned every true
row instead of rejecting a mixed-type comparison. Fix: an explicit BOOL/non-BOOL
guard for all comparison operators, with `BOOL`↔`BOOL` still supported and `NULL`
still yielding `UNKNOWN` before the guard runs. Regression tests:
`tests/test_stability.py::test_boolean_equal_to_integer_is_rejected` and four
neighbours.

**Finding 9 (Low, accepted).** `TEXT = 5` and `FLOAT = 'a'` evaluate to `FALSE`
instead of raising. Rejected as a fix because SQL engines differ here and the current
behavior is deterministic and harmless (never a false positive match). Documented.

## 2. Execution engine

Reviewed: `INSERT`/`UPDATE`/`DELETE` atomicity, constraint validation, index
synchronization, hash/nested-loop equivalence, `LEFT JOIN` semantics, pushdown,
`DISTINCT`, `ORDER BY`/`OFFSET`/`LIMIT` ordering, aggregates, statistics
invalidation, `EXPLAIN` non-execution, and `EXPLAIN ANALYZE` counters.

Evidence gathered:

- `UPDATE` stages every assignment for every matching row before mutating, so an
  arithmetic or constraint failure changes nothing inside or outside a transaction
  (`tests/test_transactions.py`, `tests/test_language_queries.py`).
- Hash join and nested loop are asserted to return identical rows *and* order on a
  40x120 dataset, including residual `ON` predicates.
- Predicates over the nullable side of a `LEFT JOIN` are asserted to remain after
  the join by inspecting the plan tree.
- `EXPLAIN` is asserted non-executing, including `EXPLAIN SELECT 1 / 0`.
- `EXPLAIN ANALYZE` counters (`duplicates_removed`, `rows_discarded`, join
  build/probe/bucket matches) are asserted against known data.
- Python-specific hazards checked: `True == 1` (finding 2), `None == None` (NULLs
  compare as duplicates only in `DISTINCT`, which is intended), unhashable values
  (no SQL value is unhashable), `NaN`/`inf` (rejected on arithmetic and on WAL
  serialization via `allow_nan=False`), integer/float coercion (`/` always float,
  floored `%`), and dict iteration order (projection iterates the declared column
  list, not row dicts).
- Sorting mixed incomparable values raises `ExecutionError`, not `TypeError`
  (verified during the same audit).

No defect found in this component beyond finding 2.

## 3. Storage and WAL

Reviewed: snapshot deserialization and metadata validation, row/schema consistency,
index rebuild, WAL record validation, checksum verification, LSN monotonicity,
partial writes, ambiguous `COMMIT`, poisoned-WAL behavior, directory fsync errors,
atomic snapshot replacement, checkpoint ordering, crash recovery, repeated recovery,
and legacy migration.

Evidence gathered:

- `Database.from_dict` validates format name/version, `wal_lsn`, table names, column
  metadata, row keys, per-value coercion, and duplicate `PRIMARY KEY`/`UNIQUE`
  values. Corrupt fixtures are rejected and left byte-identical
  (`tests/test_format_fixtures.py`, `tests/test_hardening.py`).
- WAL parsing requires exact allowed keys per record type, a SHA-256 checksum,
  strictly increasing LSNs, `BEGIN`/`WRITE`/`COMMIT` ordering, and a `CHECKPOINT`
  that references a known commit. A torn or corrupt *trailing* record is ignored and
  repaired; corruption *before* later data raises instead of being skipped.
- Crash campaign (real subprocess `os._exit`): `after_begin`,
  `after_write_before_commit`, `partial_commit_record`, `after_commit_fsync`,
  `during_snapshot_write`, `after_snapshot_fsync_before_replace`,
  `after_snapshot_replace`, `after_checkpoint_before_truncate`,
  `during_wal_truncation`. Each has a defined expected table state and is reopened
  twice to prove recovery is idempotent.
- Ambiguous `COMMIT` (fsync failure) raises `AmbiguousCommitError`, poisons further
  writes, preserves the WAL, and is never reported as a rollback.
- Legacy v1/v2 files migrate on write, are not rewritten on read, and keep their
  index definitions (now also covered by permanent fixtures).

No defect found. The WAL implementation was **not** rewritten.

## 4. Server and client

Reviewed: TCP framing, maximum frame validation, malformed JSON, session ownership,
authentication, TLS, request correlation, cleanup, timeouts, connection limits,
shutdown, process locking, ambiguous request outcomes, secret handling, and error
sanitization.

Evidence gathered:

- Frame length is validated before buffering; an oversized or zero-length frame
  closes the connection with a structured error.
- Undecodable payloads are answered and the connection stays aligned.
- `hello` is required before `execute`; duplicate `request_id`s are rejected; a
  bounded `deque` (256 entries) keeps duplicate tracking from growing.
- Authentication uses constant-time comparison, counts failures, and closes after
  the configured attempts. Tokens are never logged, never echoed, and never written
  to database, WAL, or lock files (grep-verified across `src/` and tests).
- TLS: server minimum TLS 1.2; client always sets `check_hostname=True` and
  `CERT_REQUIRED`; there is no verification bypass. Mutual TLS is enforced when
  `--tls-client-ca` is configured.
- Internal exceptions map to `INTERNAL_ERROR` with the message "internal server
  error"; tracebacks are logged server-side only.
- Process lock uses the OS (`fcntl.flock` / `msvcrt.locking`), so a crashed server
  cannot leave a stale lock.

**Finding 3 (Low, fixed).** `send_frame` always used `RESPONSE_TOO_LARGE`, so an
oversized request built by the client was mislabelled. `send_frame` now takes an
optional `too_large_code`; the client passes `REQUEST_TOO_LARGE` and the message
says "request". Regression: `test_oversized_request_reports_request_too_large`,
`test_send_frame_default_still_labels_responses`.

**Finding 4 (Low, fixed).** `ProtocolError` carried only the message, hiding the
stable code from callers. Framing errors now raise
`ProtocolError("<CODE>: <message>")`.

**Finding 5 (Low, fixed).** `ServerConfig.from_environment(auth_token="…")` with
`AAYUSQL_AUTH_TOKEN` set passed `auth_token` twice and raised `TypeError`. The
explicit value is now popped first; the environment still wins. Regression:
`test_auth_token_from_environment_wins_without_duplicate_keyword`.

## 5. Backup and restore (new capability, finding 1)

The audit found no offline backup path. The engine's consistency unit is "snapshot
file + WAL + fsync", and copying those files independently is not safe, so
`aaysql.backup` was implemented with these properties (all tested in
`tests/test_backup.py`):

- Exclusive access via the same lock file the server uses.
- Pending WAL recovered and checkpointed before the snapshot is read.
- Manifest with backup format, database format, engine version, UTC creation time,
  table row counts, index names, `wal_lsn`, size, and SHA-256.
- Atomic staging and replacement; existing destination requires `force`.
- Restore verifies manifest and checksum, validates the candidate by opening it,
  refuses a server-owned target, refuses a target with non-empty WAL, and keeps the
  replaced database at `<target>.pre-restore-<id>` until the restored copy opens
  successfully.
- The source database and WAL are only ever read.

Limitation: offline only. Online backup is not implemented and is not claimed.

## 6. Packaging and cross-platform (findings 6, 7, 8)

- Wheel and sdist build; metadata version matches `pyproject.toml`; entry points
  present; no `.aaydb`, `.wal`, `.lock`, `.pyc`, or test files in either artifact.
- Clean virtual environments install the wheel and pass an embedded + server/client
  smoke test.
- CI now runs Ubuntu, Windows, and macOS across Python 3.10–3.13, plus a packaging
  job that builds, inspects, and clean-installs the artifacts.
- Windows-specific paths (`msvcrt` locking, directory fsync skipped, `os.replace`
  semantics, EBADF on fsyncing a read-only handle) are exercised on the Windows
  runners; the backup fsync helper was adjusted for the last of those during this
  audit.
- License metadata is intentionally absent: no license is declared in the
  repository, and this release does not invent one. Distribution beyond the
  repository should wait until the owner chooses a license.
