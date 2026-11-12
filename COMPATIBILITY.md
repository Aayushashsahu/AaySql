# AayushSQL Compatibility Contract — v1.0.0

This document defines exactly what AayushSQL 1.0.0 supports and what it does not.
It describes the implementation, not an aspiration: every "supported" row is
exercised by the test suite listed in [AUDIT.md](AUDIT.md).

AayushSQL is **not** a drop-in replacement for PostgreSQL, MySQL, or SQLite, and
it is **not** fully ANSI-SQL compliant. It is a small relational engine with a
defined subset, a mandatory `AAYUSH` statement prefix, and one writer at a time.

Version identifiers for this release:

| Component | Version |
| --- | --- |
| Package / engine | `1.0.0` |
| Database file format | `3` |
| Network protocol | `1` |

---

## 1. SQL language

### 1.1 Statements

| Statement | Supported | Notes |
| --- | --- | --- |
| `CREATE TABLE` | Yes | Column-level constraints only; no table constraints, no `IF NOT EXISTS`. |
| `DROP TABLE` | Yes | Fails when the table does not exist. Indexes on it are removed with it. |
| `INSERT` | Yes | Multi-row `VALUES`, optional column list, literal values only (`DEFAULT` resolved when a column is omitted). |
| `SELECT` | Yes | Requires `FROM`; `SELECT` without a table is not supported. |
| `UPDATE` | Yes | Multiple `SET` assignments, optional `WHERE`, expression values. |
| `DELETE` | Yes | Optional `WHERE`; `DELETE` without `WHERE` removes every row. |
| `CREATE INDEX` | Yes | Exactly one column per index; no `UNIQUE` indexes. |
| `DROP INDEX` | Yes | Fails when the index does not exist. |
| `BEGIN` | Yes | One explicit transaction per engine; no nesting, no `SAVEPOINT`. |
| `COMMIT` | Yes | Single durable WAL commit of the whole transaction. |
| `ROLLBACK` | Yes | Restores the pre-`BEGIN` snapshot, including DDL and indexes. |
| `EXPLAIN` | Yes | Prints the plan; never executes the statement or mutates state. |
| `EXPLAIN ANALYZE` | Yes | Executes the statement and reports measured per-operator counters. |

Every statement must start with `AAYUSH` (a doubled prefix is a parse error).
Statements may end with `;`. The CLI and client accept one statement per call.

### 1.2 Query features

| Feature | Supported | Notes |
| --- | --- | --- |
| `WHERE` | Yes | Three-valued logic; only `TRUE` admits a row. |
| `INNER JOIN` | Yes | `JOIN` and `INNER JOIN` are the same. Only `ON` conditions. |
| `LEFT JOIN` | Yes | Unmatched left rows get `NULL` on every right column. |
| `GROUP BY` | Yes | Simple column references only (qualified or unqualified). No positional or aliased grouping. |
| `HAVING` | Yes | Aggregates allowed; grouped-expression rules are validated before execution. |
| `ORDER BY` | Yes | Multiple keys, `ASC`/`DESC`, columns, aliases, aggregates, or arbitrary expressions. `NULL` sorts **last** ascending and **first** descending. |
| `LIMIT` | Yes | Non-negative integer literal only; no expressions, no `LIMIT ALL`. |
| `OFFSET` | Yes | With or without `LIMIT`; applied after ordering and before limiting. `OFFSET` before `LIMIT` is a parse error. |
| `DISTINCT` | Yes | `SELECT DISTINCT` / `SELECT DISTINCT *`. Deduplicates the projected row; first occurrence wins. With `DISTINCT`, `ORDER BY` must reference the projected row. |
| Aggregates | Yes | `COUNT(*)`, `COUNT(col)`, `SUM`, `AVG`, `MIN`, `MAX`. No `COUNT(DISTINCT …)` and no `FILTER`. |
| Arithmetic | Yes | `+ - * / %` and unary `+`/`-`. |
| `CASE` | Yes | Searched (`CASE WHEN …`) and simple (`CASE expr WHEN …`); first `TRUE` branch wins; missing `ELSE` yields `NULL`. |
| `LIKE` / `NOT LIKE` | Yes | `%` and `_`, case-sensitive, no `ESCAPE`, no `ILIKE`. |
| `IN` / `NOT IN` | Yes | Literal lists only. Empty list and `IN (SELECT …)` are parse errors. |
| `BETWEEN` / `NOT BETWEEN` | Yes | Inclusive. |
| `IS NULL` / `IS NOT NULL` | Yes | `IS TRUE`, `IS FALSE`, `IS DISTINCT FROM` are **not** supported. |
| Boolean expressions | Yes | `AND`, `OR`, `NOT`, parentheses, `TRUE`/`FALSE` literals. |
| Three-valued `NULL` logic | Yes | `UNKNOWN` is distinct from `FALSE`; `NOT UNKNOWN` is `UNKNOWN`; `5 = NULL` is `UNKNOWN` while `5 IS NULL` is `FALSE`. |

Precedence, lowest to highest: `OR`; `AND`; `NOT`; comparisons and postfix
predicates (`IS`, `IN`, `BETWEEN`, `LIKE`); `+ -`; `* / %`; unary sign; primary.
See [LANGUAGE_SPEC.md](LANGUAGE_SPEC.md) for the full grammar.

### 1.3 Data types and constraints

| Type | Aliases | Notes |
| --- | --- | --- |
| `INT` | `INTEGER` | 64-bit-range Python integers; checked to be integers. |
| `TEXT` | `VARCHAR` | No length limits; length modifiers are not parsed. |
| `FLOAT` | `DOUBLE` | IEEE-754 double; stored values are floats. |
| `BOOL` | `BOOLEAN` | `TRUE`/`FALSE`, plus `0`/`1` accepted on write. Never treated as a number. |

| Constraint | Supported | Notes |
| --- | --- | --- |
| `PRIMARY KEY` | Yes | At most one column per table; implies `NOT NULL` and uniqueness. |
| `NOT NULL` | Yes | Enforced on `INSERT` and `UPDATE`. |
| `UNIQUE` | Yes | Single-column uniqueness, `NULL`s are not compared. |
| `DEFAULT` | Yes | Literal values only; applied when a column is omitted from `INSERT`. |

Mixed-type comparisons: numeric types compare numerically across `INT`/`FLOAT`;
a `BOOL` never compares with a non-`BOOL` (an error rather than Python's
`True == 1`); a `TEXT` compared with a number is simply unequal and never raises.

### 1.4 Indexes and optimizer

| Capability | Supported | Boundary |
| --- | --- | --- |
| B+ tree index | Yes | In-memory tree rebuilt from authoritative rows on load. |
| Single-column indexes | Yes | One column per `CREATE INDEX`. |
| Index equality lookup | Yes | `column = literal`. |
| Index range scan | Yes | `column < <= > >= literal` and `column BETWEEN literal AND literal`. |
| Index treatment of `NULL` | By design | `NULL` values are not indexed; `IS NULL` and `IN` always scan the table. |
| Cost-based scan choice | Yes | Compares table-scan versus index-candidate cost; may legitimately choose a scan. |
| Hash join | Yes | Selected when every `ON` conjunct is `column = column` across the two sides. |
| Nested-loop join | Yes | Fallback, including residual predicates and non-equality `ON` conditions. |
| Statistics | Yes | Exact row/NULL counts; distinct counts exact up to 4096 values then estimated from an 8192-bit hashed bitmap; `min`/`max` only when values are comparable. |
| `EXPLAIN` | Yes | Estimated rows, chosen access paths, predicate placement, projection and wrapper nodes. |
| `EXPLAIN ANALYZE` | Yes | Rows in/out, rejected rows, index candidates, hash build/probe/bucket matches, comparisons, duplicates removed, offset/limit counts, per-operator timing. |

No histograms, no expression statistics, no index-only scans, and no multi-column
indexes exist. Selectivity for `LIKE`, `CASE`, `IS NULL`, `IN`, and arithmetic is
a conservative default, not an estimate derived from data.

### 1.5 Explicitly unsupported

Anything in this list is a parse error or an execution error, never a silent
misinterpretation:

- Subqueries of any kind, including `IN (SELECT …)` and scalar subqueries.
- `UNION`, `INTERSECT`, `EXCEPT`.
- `EXISTS`.
- `WITH` / common table expressions.
- Window functions (`OVER`, `PARTITION BY`, `ROW_NUMBER`, …).
- `CAST`, `COALESCE`, `NULLIF`, `IFNULL`.
- String concatenation (`||`), string functions, `SUBSTRING`, `UPPER`/`LOWER`.
- `ILIKE`, `LIKE … ESCAPE`, `SIMILAR TO`, regular expressions.
- Aggregate `DISTINCT`, `GROUP BY` on expressions, `GROUPING SETS`/`ROLLUP`/`CUBE`.
- Multi-column indexes, `UNIQUE` indexes, partial indexes, `CREATE INDEX IF NOT EXISTS`.
- `RIGHT JOIN`, `FULL JOIN`, `CROSS JOIN`, `NATURAL JOIN`, `USING`.
- `ALTER TABLE`, `TRUNCATE`, `CREATE VIEW`, `CREATE SCHEMA`, `CREATE DATABASE`.
- `INSERT … SELECT`, `UPDATE … FROM`, `DELETE … USING`, `RETURNING`.
- `SAVEPOINT`, `RELEASE`, `SET TRANSACTION`, isolation-level control.
- `SELECT` without `FROM` (`AAYUSH SELECT 1` is rejected).
- Scientific-notation numeric literals (`1e5`), hexadecimal literals, binary literals.
- `IS TRUE` / `IS FALSE` / `IS DISTINCT FROM`.
- Multiple statements in one `Engine.execute` call.
- `MVCC`, snapshot isolation, concurrent writers, parallel query execution.

---

## 2. Database file compatibility

A database is a single UTF-8 JSON snapshot `<name>.aaydb`, with an adjacent
append-only write-ahead log `<name>.aaydb.wal` while a transaction is pending.

| Format | Read | Write / migrate |
| --- | --- | --- |
| v1 | Yes | Upgrades to v3 on the next durable write or recovery checkpoint. |
| v2 | Yes (including persisted index definitions) | Upgrades to v3 on the next durable write or recovery checkpoint. |
| v3 | Yes (current) | Written by v0.9.0 and v1.0.0. |
| Unknown / newer | No | Rejected with `StorageError`; the file is not modified. |

Verified behavior:

- **Reads do not rewrite.** Opening a v1/v2/v3 file and closing it without a
  mutation leaves the bytes unchanged (`tests/test_format_fixtures.py`,
  `tests/test_wal_recovery.py`).
- **Migration moment.** The file is rewritten as format 3 only when a mutation is
  durably committed (autocommit or `COMMIT`) or when recovery completes with a
  checkpoint. The rewrite is an atomic `os.replace` of a fsynced temporary file.
- **WAL interaction.** On open, the snapshot is loaded, then WAL records with
  `commit_lsn > snapshot.wal_lsn` are replayed from the newest committed image.
  For a legacy file (`wal_lsn` absent, treated as `0`), a leftover WAL can
  therefore be replayed too; recovery then checkpoints in format 3.
- **Migration failure.** The snapshot is only replaced after a successful fsync of
  the temporary file. A failure raises `StorageError`, removes the temporary file,
  and leaves the original snapshot (and any WAL) untouched.
- **Downgrade is not supported.** No earlier application version is tested against
  a format-3 file written by v1.0.0, so downgrading is explicitly outside the
  compatibility contract. Keep a backup before upgrading applications.
- Format 3 adds `wal_lsn` as a required field. Format 2 added the `indexes`
  catalog. Both remain readable.

Index trees are never persisted; only their `{table, column}` definitions are.
Every load rebuilds the tree from table rows and validates it, so a stale or
corrupt index cannot survive a restart.

---

## 3. Network protocol (version 1)

**Framing.** Four-byte big-endian unsigned payload length, then that many bytes of
UTF-8 JSON. No newlines and no bare SQL strings. Default maximum frame size is
1 MiB (`DEFAULT_MAX_FRAME_SIZE`), configurable between 1 KiB and 64 MiB. A declared
length above the limit is rejected *before* any payload is buffered.

**Operations.** `hello`, `ping`, `execute`. Every request carries
`protocol_version` (integer `1`), a unique `request_id` (≤128 printable characters,
no control characters), and `operation`. A connection must send `hello` before any
other operation (`HELLO_REQUIRED`). Duplicate request IDs on one connection are
rejected.

**Handshake.** `hello` returns `server_version`, `protocol_version`,
`capabilities`, `authenticated`, `auth_required`, `session_id`, and `tls`. If a
token is configured, `hello` must include the matching `token`; three failures
close the connection. A wrong protocol version is answered with
`UNSUPPORTED_PROTOCOL_VERSION` and the connection stays open; a malformed envelope
yields `INVALID_REQUEST`.

**Authentication.** Bearer token from `AAYUSQL_AUTH_TOKEN` on the server (never a
command-line flag) and `token=`/`AAYUSQL_AUTH_TOKEN` on the client. Comparison uses
`hmac.compare_digest`. Tokens never appear in logs, errors, database files, or
lock files.

**TLS.** Enabled with `--tls-cert`/`--tls-key`. Server minimum version is TLS 1.2.
Optional mutual TLS via `--tls-client-ca` (`CERT_REQUIRED`). Clients always verify
the certificate chain and hostname; there is no "disable verification" switch.

**Responses.** `{protocol_version, request_id, status: "ok", result}` or
`{protocol_version, request_id, status: "error", error: {code, message, retryable}}`.
Result payloads mirror `Engine.execute()` minus `status`.

**Stable error codes.** `INVALID_REQUEST`, `UNSUPPORTED_PROTOCOL_VERSION`,
`AUTHENTICATION_FAILED`, `SQL_PARSE_ERROR`, `SQL_EXECUTION_ERROR`,
`CONSTRAINT_ERROR`, `TRANSACTION_BUSY`, `TRANSACTION_ERROR`, `REQUEST_TOO_LARGE`,
`RESPONSE_TOO_LARGE`, `DATABASE_UNAVAILABLE`, `RECOVERY_REQUIRED`,
`CONNECTION_TIMEOUT`, `TOO_MANY_CONNECTIONS`, `HELLO_REQUIRED`,
`UNKNOWN_OPERATION`, `INTERNAL_ERROR`.

**Retry semantics.** `TRANSACTION_BUSY`, `TOO_MANY_CONNECTIONS`, and
`CONNECTION_TIMEOUT` are marked `retryable`. A lost connection or a read timeout
after a request was sent is **not** retryable: the outcome is unknown and the
client raises with `outcome_unknown = True` instead of replaying the statement.

**Client compatibility.** Adding a result field or an operation does not change the
protocol version. A client that receives an unknown `protocol_version` must refuse
to continue. This client refuses responses whose version is not `1`.

---

## 4. Python API

**Python versions:** 3.10, 3.11, 3.12, 3.13 (CPython). No third-party runtime
dependencies.

Stable public surface:

| Import | Purpose |
| --- | --- |
| `from aaysql import Engine, __version__` | Embedded engine. |
| `aaysql.errors` | `AayushSQLError` and subclasses: `LexerError`, `ParserError`, `ExecutionError`, `ConstraintError`, `StorageError`, `AmbiguousCommitError`, `DatabaseLockedError` (stable code `DATABASE_LOCKED`). |
| `aaysql.client` | `AayushSQLClient` and client exceptions: `AayushSQLClientError`, `ConnectionError`, `ConnectionLostError`, `TLSHandshakeError`, `AuthenticationError`, `ProtocolError`, `ServerError`, `RequestTimeoutError`. |
| `aaysql.server` | `AayushSQLServer`, `ServerConfig`, `ServerConfigError`, `ServerLock` (compatibility wrapper), `LOCK_SUFFIX`. |
| `aaysql.locking` | `DatabaseLease` (exclusive ownership), `LOCK_SUFFIX`. |
| `aaysql.backup` | `backup_database`, `restore_database`, `verify_backup`, `read_manifest`, `BackupError`. |
| `aaysql.protocol` | Protocol constants, framing, and error codes. |

`Engine(db_path)` behavior: `None` or `":memory:"` is in-memory; any other path is
a persistent database. `execute(sql)` returns a dict with `columns`, `rows`,
`row_count`, and `message` or `rows_affected` where applicable. `close()` is
idempotent, rolls back an uncommitted transaction, and never deletes committed
data. `Engine` is a context manager. `Engine.execute` is not thread-safe; use one
engine per thread, or the server.

`AayushSQLClient(host, port, *, token, tls, ca_certs, server_hostname,
client_cert, client_key, connect_timeout, read_timeout, max_frame_size,
client_name)` opens one connection. It is safe to share between threads (requests
are serialized by a lock) and never pipelines.

Considered internal (no stability guarantee): `aaysql.parser`, `aaysql.lexer`,
`aaysql.ast`, `aaysql.planner`, `aaysql.execution`, `aaysql.executor`,
`aaysql.storage`, `aaysql.wal`, `aaysql.index`, `aaysql.btree`,
`aaysql.statistics`, `aaysql.query`, `aaysql.transaction`, `aaysql.formatting`,
`aaysql.token`, and any monkeypatch/crash-test environment variable
(`_AAYSQL_TEST_*`).

**Entry points after `pip install`:** `aaysql` (SQL shell), `aaysql-server`
(`python -m aaysql.server` also works), `aaysql-backup`.

---

## 5. Operational contract

- **One writer per database.** The server serializes all engine access; there is no
  MVCC and no concurrent write execution. Other sessions receive `TRANSACTION_BUSY`
  while a transaction is owned elsewhere.
- **One owner per database file.** An embedded `Engine`, a server, a backup and a
  restore all take the same exclusive OS-level lease on `<db>.server.lock`
  (`fcntl` on POSIX, `msvcrt` on Windows) before loading or recovering anything. The
  OS releases it when the process dies; there are no stale-PID heuristics and a
  stale lock file does not block a new owner. A conflict raises
  `DatabaseLockedError` (code `DATABASE_LOCKED`) rather than a raw OS error. This
  is an ownership rule, not concurrency: it does not add MVCC, multiple writers,
  shared embedded readers or multi-process access.
- **Durability scope.** The WAL is fsynced before a transaction is reported as
  committed. Tests prove process-crash (`os._exit`) recovery; power-loss behavior
  is bounded by the platform's fsync semantics (see [WAL.md](WAL.md)) and is not
  claimed beyond that.
- **Backup is offline only.** `aaysql-backup` refuses to run while any owner (server,
  embedded `Engine`, or another backup) holds the database. Restore refuses to
  replace a database another owner holds, holds the target lease through the whole
  replacement and validation, and refuses a target with unrecovered WAL data.
- **Non-loopback binding** requires both authentication and TLS; the server refuses
  to start otherwise.

See [SECURITY.md](SECURITY.md) for the security posture and
[THREAT_MODEL.md](THREAT_MODEL.md) for the assumptions behind it.
