# Threat Model

This is a working threat model for AayushSQL 1.0.0, not a certification. It records
what the system assumes, what an attacker can reach, and which controls exist.

## 1. Assets

| Asset | Why it matters |
| --- | --- |
| Committed rows in `<db>.aaydb` | The user's data; loss is the primary risk. |
| WAL `<db>.wal` | Holds the newest committed state between commit and checkpoint. |
| `AAYUSQL_AUTH_TOKEN` | Grants full SQL access to a running server. |
| TLS private key (`--tls-key`) | Impersonation and decryption of server traffic. |
| Process lock `<db>.server.lock` | Prevents two writers on one database. |
| Backup directories | Complete copies of the database plus manifest. |

## 2. Trust boundaries

1. **SQL text → parser/engine.** Untrusted in the sense that it comes from a client;
   the parser is expected to reject, not crash.
2. **Network → server.** Untrusted bytes on a socket; framing, JSON decoding, and
   envelopes are the only parser in front of the engine.
3. **Server → filesystem.** Trusted process with operator-supplied paths.
4. **Filesystem → engine on load.** The snapshot and WAL are *semi-trusted*: they may
   be corrupted by a crash or edited by a user, so they are validated on load.
5. **Client → server response.** Semi-trusted; the client validates version, request
   ID, status, and error shape before returning a result.

## 3. Assumptions

- The operating system enforces file permissions and process isolation.
- `fsync` on a file, and on a directory where the platform supports it, means the
  data survives a process crash; power-loss behavior is bounded by the filesystem.
- The operator keeps the token and TLS key out of world-readable locations and does
  not pass them on a command line.
- A single server process owns a database at a time; the OS lock enforces this.
- Clients are not adversarial *after* authentication: one token is full access.

## 4. Threats and controls

### T1 — Unauthorized network access
An attacker on the network connects and issues SQL.
**Controls:** default loopback bind; non-loopback requires token + TLS; TLS 1.2
minimum; certificate and hostname verification on the client; mutual TLS optional;
three authentication attempts; constant-time token comparison.
**Residual risk:** no per-user privileges — one token is all-or-nothing.

### T2 — Credential disclosure
Token leaks through logs, errors, artifacts, or process listings.
**Controls:** the server reads the token only from the environment; logs record
`auth=on|off`; errors are generic; tests assert the token is absent from database,
WAL, lock, logs, and client errors.
**Residual risk:** an operator who exports the variable in a shared shell or writes
it into a service file exposes it.

### T3 — Malicious or broken framing
Oversized length, truncated payload, deep JSON, garbage bytes.
**Controls:** length validated before allocation; defaults and hard bounds on frame
size; UTF-8 and JSON decode errors answered without desynchronizing an aligned
stream; fatal framing errors close the connection; connection and timeout limits.
**Residual risk:** a full-size frame (≤64 MiB when configured) is buffered per
connection.

### T4 — Malformed SQL
Adversarial SQL causing crashes, resource exhaustion, or internal detail disclosure.
**Controls:** parser and validator raise typed errors; unknown AST nodes rejected;
`LIKE` never becomes a regex; no shell/eval; tracebacks stay server-side.
**Residual risk:** expensive-but-legal queries (large joins, wide results) consume
CPU and memory; there is no query timeout or quota.

### T5 — Corrupted or tampered storage
Bit rot, a truncated write, or a hand-edited snapshot/WAL.
**Controls:** strict schema, row, and index-definition validation on load; per-record
SHA-256 checksums; monotonic LSNs; typed record fields; torn *trailing* WAL records
repaired; mid-log corruption refuses to load instead of skipping forward; indexes
rebuilt from rows and validated.
**Residual risk:** corruption inside the snapshot that still satisfies the schema
cannot be detected — the format has no page checksums or whole-file digest. A
backup manifest checksum detects a modified *backup*, not latent snapshot rot.

### T6 — Crash or power loss during a write
Partial snapshot, partial WAL record, lost directory entry.
**Controls:** WAL fsync before COMMIT is acknowledged; `os.replace` for snapshots;
directory fsync where supported; repair of invalid trailing bytes before appending a
checkpoint; `AmbiguousCommitError` instead of a false rollback.
**Residual risk:** on Windows, directory-entry durability relies on NTFS metadata
behavior because the CRT cannot open a directory (documented in WAL.md). Power-loss
guarantees are not claimed beyond platform fsync semantics.

### T7 — Transaction isolation violations
Uncommitted data visible to another session, or two writers at once.
**Controls:** immutable published MVCC roots, one writer admitted at a time,
per-session snapshots and rollback on disconnect, `WRITE_CONFLICT` for stale
writes, `RECOVERY_REQUIRED` for everyone after an ambiguous commit.
**Residual risk:** table-level first-updater-wins conflicts abort stale writers;
there is no SERIALIZABLE isolation, row-level version chain, or multi-writer
execution.

### T8 — Two processes on one database
A second server or a backup racing the server corrupts state.
**Controls:** OS-level exclusive lock for the server and for backup/restore; no
stale-PID heuristic; the lock is released by the kernel if the process dies.
**Residual risk:** a process that ignores the lock file (a user editing the JSON
directly) is not prevented.

### T9 — Destructive recovery or restore
A recovery, backup, or restore deletes or overwrites a valid database or WAL.
**Controls:** recovery only truncates invalid *trailing* WAL bytes and only after
parsing; backup reads the source and never writes it; restore requires `force` to
replace an existing database, refuses a target with non-empty WAL, validates the
candidate before replacing, and keeps the previous file at
`<target>.pre-restore-<id>` until the new one opens successfully.
**Residual risk:** an operator deleting files manually. Documented as forbidden in
`docs/OPERATIONS.md`.

### T10 — Supply chain and release artifacts
A modified or over-broad package.
**Controls:** no runtime third-party dependencies; wheel and sdist contents asserted
in CI (no tests, databases, WAL, lock, or bytecode); SHA-256 checksums published with
the release.
**Residual risk:** publishing to PyPI is out of scope for this release and is not
authorized.

## 5. Out of scope

- Multi-tenant privilege separation, row-level security, data masking.
- Encryption at rest or in transit beyond TLS.
- Protection from a malicious operator or root.
- Distributed deployment, replication, failover.
- Side-channel attacks beyond the constant-time token comparison.
- Online (live) backup.
