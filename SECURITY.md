# Security

AayushSQL 1.0.0 is a database with concurrent snapshot readers and one admitted
commit generator at a time, plus an optional TCP server. This
document states the security boundary and how to configure it. It does not claim
that the engine is immune to attack.

## Supported versions

Security fixes are provided for the latest 1.x release only. There is no LTS
branch.

## Reporting a vulnerability

Report privately through GitHub's "Report a vulnerability" advisory for
`Aayushashsahu/AaySql`. Do not open a public issue containing an exploit. Include
the version, platform, configuration, and a reproduction. There is no bounty
program and no guaranteed response time; reports are handled on a best-effort basis.

## Network exposure

| Rule | Behavior |
| --- | --- |
| Default bind address | `127.0.0.1` (`DEFAULT_HOST`). |
| Non-loopback bind | Refused at startup unless **both** `AAYUSQL_AUTH_TOKEN` and TLS (`--tls-cert`/`--tls-key`) are configured. |
| Hostname check | Always performed by the client; `check_hostname=True`, `CERT_REQUIRED`. |
| Verification bypass | None exists. There is no "insecure" or "skip verify" option in the client or server. |
| TLS minimum | TLS 1.2 on the server (`ssl.TLSVersion.TLSv1_2`). |
| Mutual TLS | Enabled with `--tls-client-ca`; server then requires and verifies client certificates. |
| Failure mode | A missing certificate file, half-configured TLS (cert without key), or unused client CA is a startup error, never a silent downgrade to plaintext. |

The server refuses ambiguous configurations rather than guessing. A loopback bind
with no token means "any local process may connect", which is the documented
default for a development database.

## Authentication

- The token is read from `AAYUSQL_AUTH_TOKEN` on the server. It is deliberately
  **not** accepted as a command-line argument, so it does not appear in process
  listings or shell history.
- Comparison uses `hmac.compare_digest` over UTF-8 bytes (constant-time for equal
  lengths).
- A connection must authenticate during `hello`; `execute` before successful
  authentication returns `AUTHENTICATION_FAILED`. After `auth_max_attempts`
  (default 3) failures the connection is closed.
- The client accepts `token=` or the same environment variable.

Verified: tokens never appear in `.aaydb`, `.wal`, `.server.lock`, server logs,
client exception messages, or the built wheel/sdist. The server logs only
`auth=on|off` and session identifiers, never the token or a hash of it.

## Protocol hardening

- 4-byte big-endian length prefix; the declared length is validated against
  `max_frame_size` **before** allocation, so an untrusted header cannot trigger a
  large buffer.
- Default maximum frame: 1 MiB; configurable 1 KiB–64 MiB. An oversized request is
  answered with `REQUEST_TOO_LARGE` and the connection closes.
- A response that exceeds the frame limit is replaced with `RESPONSE_TOO_LARGE`.
- Malformed UTF-8 or JSON yields `INVALID_REQUEST` without killing an aligned
  stream; a truncated frame or invalid length closes the connection.
- Request IDs: UUID hex from this client, ≤128 printable characters, unique per
  connection (bounded 256-entry duplicate window).
- Resource bounds: `max_clients` (default 32) with a semaphore, per-connection
  threads created only after a slot is acquired, `read_timeout` 60 s,
  `write_timeout` 30 s, `idle_timeout` 300 s, `request_queue_size` 64, and a bounded
  graceful shutdown (`shutdown_timeout` 10 s).
- A client that stops mid-frame is closed on read timeout; buffered partial data is
  discarded with the connection.
- No Python traceback is ever sent to a client. Unexpected exceptions become
  `INTERNAL_ERROR` with the fixed message `internal server error`; details go to the
  server log.

## Filesystem and database security

- Database, WAL, and lock files are created with the process's default umask;
  AayushSQL does not change permissions or attempt to harden them. Use OS-level
  access control for a shared machine.
- The server lock (`<db>.server.lock`) is advisory at the path level but enforced by
  the OS (`fcntl.flock` / `msvcrt.locking`), so a crashed process cannot leave a
  stale lock behind.
- Snapshots are written to a temporary file in the target directory, fsynced, then
  moved into place with `os.replace`; a crash leaves either the old or the new
  snapshot, never a half-written one. Stale `.<name>.*.tmp` files are removed on a
  successful load or checkpoint.
- Path traversal: the database path is taken from the operator (`--db`) or the API
  caller. User-supplied SQL cannot open, read, or write arbitrary paths.
- A recovery or backup operation never deletes the source database or its WAL. A
  restore never removes an existing database without `force`, and keeps the replaced
  copy until the restored database opens successfully.
- Never delete or truncate a WAL file to work around an error. Use
  `docs/OPERATIONS.md`.

## SQL and expression safety

- `LIKE` patterns are translated character by character: only `%` and `_` become
  wildcards, everything else is `re.escape`d, and the result is anchored. A user
  pattern can never become an arbitrary regular expression.
- A pattern cache is bounded (256 entries, LRU).
- Invalid or unknown AST nodes are rejected by the shared traversal
  (`ensure_known_expr`), so an unknown node is an error rather than a skipped check.
- SQL text is never passed to a shell, `eval`, or `exec`.
- Aggregates, grouped expressions, and output labels are validated before execution.

## Denial of service

Known, accepted risks in the local-server model:

- A client may issue a query whose result is large; the response is capped by
  `max_frame_size`, and the work itself is bounded only by available memory. There is
  no query timeout and no per-query memory quota.
- A client may keep a slot open until `idle_timeout` expires.
- A single very large frame (up to `max_frame_size`) is buffered per connection.

Mitigations: connection limits, frame limits, timeouts, and a single writer. If you
expose the server to an untrusted network, put it behind a firewall and a proxy with
its own request timeouts. **Non-loopback exposure is not recommended for untrusted
clients even with TLS and a token.**

## What this release does not provide

- No user accounts, roles, or per-table privileges: one token grants full SQL access.
- No audit logging of individual statements beyond request/error lines.
- No encryption at rest.
- No protection against a hostile operator with filesystem access.
- No MVCC or isolation guarantees beyond "one writer at a time".

See [THREAT_MODEL.md](THREAT_MODEL.md) for the assumptions behind these decisions.
