# AayushSQL Server Architecture (v0.8.0)

This document describes the client/server layer added in v0.8.0: one server
process, one database engine, many client connections, and a versioned network
protocol. It also states exactly what the concurrency contract is — and is not.

**The database engine did not change.** The server uses the existing
`aaysql.engine.Engine`, the existing parser, planner, executor, B+ tree indexes,
statistics, WAL and crash recovery. `.aaydb` format version stays **3**.

---

## 1. Server owns the database engine

```
client A ──┐
client B ──┼─→ TCP (framed JSON) ─→ session per connection ─→ TransactionCoordinator ─→ one Engine ─→ .aaydb + WAL
client C ──┘
```

- One server process owns exactly **one** `Engine` for its configured database.
- The server never creates an engine per connection.
- The server owns database initialization and recovery (through the normal
  `Engine(path)` startup path, so WAL replay happens before the listening socket
  is bound), statement execution, transaction coordination, WAL-backed
  persistence, request scheduling, authentication, and connection lifecycle.
- No request handler ever touches the engine directly. All engine access goes
  through `TransactionCoordinator` (`src/aaysql/server.py`).

Startup order is strict: acquire the process lock → open the engine (recovery
runs here) → bind the socket → begin accepting. A client can therefore never
reach a database that has not finished recovering.

## 2. Multiple clients, serialized execution

Multiple clients may hold connections at the same time. They do **not** get
parallel database execution.

- `TransactionCoordinator` holds one `threading.Lock`. Every statement is
  executed while holding it.
- The lock is released before the response is written to the socket, so a slow
  client cannot stall the engine.
- Each connection has its own thread (`socketserver.ThreadingTCPServer`), but a
  thread that is not executing is blocked waiting for the lock, not touching the
  engine.

This is the deliberate trade-off: **more clients improve accessibility, but
actual database work is serialized.**

## 3. Session model

Each accepted connection gets a server-generated session ID (UUID4 hex). A
session carries:

| Field | Meaning |
| --- | --- |
| `session_id` | Server-generated identity. Never accepted from the client. |
| `authenticated` | Set only by a successful handshake. |
| `hello_done` | Handshake completed. |
| `protocol_version` | Protocol spoken on this connection. |
| `owns_transaction` | Mirrors the engine's authoritative transaction state. |
| `last_activity` | Drives the idle timeout. |
| `requests` | Statement counter for diagnostics. |
| `client_name`, `client_version` | Client-supplied labels, sanitized and length-capped for logs. |
| `auth_failures` | Counts failed handshakes on this connection. |

Sessions are removed when the connection ends, and session cleanup is where
disconnect rollback happens.

## 4. Transaction ownership

The engine has exactly one `TransactionManager`, so the server must decide which
session may use it.

- `BEGIN` from session *A*: under the coordinator lock, if another session owns
  the transaction the request is rejected with `TRANSACTION_BUSY` **before** the
  engine is called; otherwise `engine.begin()` runs and the ownership record
  points at *A*.
- While *A* owns a transaction, every other session's `execute` (including
  `SELECT`, `EXPLAIN`, DML and DDL) is rejected with `TRANSACTION_BUSY`
  (`retryable: true`). Non-owners never reach the engine, so they cannot observe
  uncommitted rows or schema changes and cannot absorb someone else's statements.
- After every successful statement the coordinator re-derives ownership from
  `engine.in_transaction` — the engine is the authority. SQL text is never
  parsed to guess transaction state (it is only inspected to choose a more
  precise error code).
- `COMMIT` / `ROLLBACK` run through the engine; ownership is released based on
  the resulting engine state.
- Duplicate `BEGIN` from the owner is rejected by the engine itself and reported
  as `TRANSACTION_ERROR`.

### Busy policy

Busy is an **immediate** retryable error, not a queue. The server does not hold
requests on behalf of a blocked client, and the client library never replays a
mutation. Applications retry the *whole* transaction on `TRANSACTION_BUSY` with
their own backoff, and only when their transaction did not begin.

### Disconnect

If the owner disconnects while holding an active transaction, the server rolls
it back, releases ownership, and logs the cleanup (no SQL values are logged). No
uncommitted mutation reaches persistent storage.

## 5. Ambiguous commit handling

If a `COMMIT` cannot be proven durable, the outcome is **unknown**, not "rolled
back":

- `Engine` raises `AmbiguousCommitError` when the WAL commit record may or may
  not have reached disk.
- The coordinator marks the whole database service unavailable and keeps the
  owner tombstone so nothing else can start using the engine.
- Every subsequent request from every session gets `RECOVERY_REQUIRED`
  (`retryable: false`).
- The client receives a definite `RECOVERY_REQUIRED` error — never a rollback
  claim.
- Shutdown preserves the WAL: the engine is closed without rollback so the next
  open runs normal WAL recovery. Recovery decides the real state.

The same path is taken if the engine reports `needs_recovery` or a poisoned WAL.

## 6. Protocol

### Transport and framing

- TCP, standard library only, default `127.0.0.1:8765`.
- Each message is `4-byte unsigned big-endian length` + `UTF-8 JSON` payload.
- Maximum frame size defaults to 1 MiB (`--max-frame-size`). The length is
  validated **before** any allocation, so an untrusted header cannot force a
  huge buffer.
- The reader handles partial headers, partial payloads, and several frames
  arriving in one `recv` (`FrameReader`).
- Invalid UTF-8, malformed JSON, invalid lengths, and oversized frames produce a
  structured error; a stream that is no longer trustworthy is closed.

### Protocol version

`PROTOCOL_VERSION = 1`, independent of the package version (`0.8.0`). A message
with any other `protocol_version` gets `UNSUPPORTED_PROTOCOL_VERSION` and the
connection stays open, because framing is still aligned and the client may
retry with a supported version.

### Handshake

```
→ {"protocol_version": 1, "request_id": "h1", "operation": "hello",
   "client": {"name": "aaysql-python", "version": "0.8.0"}, "token": "<optional>"}
← {"protocol_version": 1, "request_id": "h1", "status": "ok",
   "result": {"server_version": "0.8.0", "protocol_version": 1,
              "capabilities": ["execute", "transactions", "ping"],
              "authenticated": true, "auth_required": false,
              "session_id": "…", "tls": false}}
```

`hello` is required before any other operation; otherwise `HELLO_REQUIRED`.

### Operations

| Operation | Payload | Result |
| --- | --- | --- |
| `hello` | optional `client`, `token` | handshake result above |
| `ping` | — | `{"pong": true, "server_time": …, "session_id": …, "in_transaction": …}` |
| `execute` | `sql` | the engine result minus `status` (`columns`, `rows`, `row_count`, or `rows_affected`, plus `message` for DDL) |

### Errors

```json
{"protocol_version": 1, "request_id": "req-7", "status": "error",
 "error": {"code": "SQL_PARSE_ERROR", "message": "…", "retryable": false}}
```

Stable codes: `INVALID_REQUEST`, `UNSUPPORTED_PROTOCOL_VERSION`,
`AUTHENTICATION_FAILED`, `SQL_PARSE_ERROR`, `SQL_EXECUTION_ERROR`,
`CONSTRAINT_ERROR`, `TRANSACTION_BUSY`, `TRANSACTION_ERROR`,
`REQUEST_TOO_LARGE`, `DATABASE_UNAVAILABLE`, `RECOVERY_REQUIRED`,
`CONNECTION_TIMEOUT`, `INTERNAL_ERROR`, plus `UNKNOWN_OPERATION`,
`TOO_MANY_CONNECTIONS` and `HELLO_REQUIRED`.

Unknown exceptions are logged with their traceback server-side and returned to
the client as a generic `INTERNAL_ERROR`. Tracebacks are never sent over the
wire.

### Reliability

Request IDs are validated (non-empty printable strings, bounded length) and
recently seen IDs are remembered per session, so a duplicate ID is rejected
instead of producing two responses to one request. Exactly one response is sent
per accepted request, always carrying the same `request_id`. Read/write
timeouts, a connection cap, broken-pipe handling and per-connection exception
isolation are all enforced in the handler.

## 7. Authentication

- Optional bearer token from the environment variable `AAYUSQL_AUTH_TOKEN`
  (never from argv, never logged, never persisted in `.aaydb`).
- Compared with `hmac.compare_digest`.
- When configured, `hello` must carry a matching `token`; SQL execution without
  a successful handshake returns `AUTHENTICATION_FAILED`.
- Repeated failures on one connection are counted and the connection is closed
  after `auth_max_attempts` (default 5).
- An empty token is treated as "no token configured" at configuration time; the
  token is never echoed back in results or error messages.

## 8. TLS and remote binding

- Default bind is `127.0.0.1`. Substantive non-loopback binds (`0.0.0.0`,
  `::`, LAN addresses) are refused unless **both** an auth token and a TLS
  certificate/key pair are configured.
- TLS uses `ssl.SSLContext` with `PROTOCOL_TLS_SERVER` and a TLS 1.2 minimum.
  Optional `--tls-client-ca` enables `CERT_REQUIRED` client-certificate
  verification.
- The client verifies certificates by default (`ssl.create_default_context`,
  `CERT_REQUIRED`, hostname checking). There is no "skip verification" switch.
- Local loopback operation may run without TLS; documentation never claims
  encryption when TLS is off — the handshake reports `"tls": false`.

### Local development certificates

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -keyout dev-key.pem -out dev-cert.pem \
  -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

AAYUSQL_AUTH_TOKEN=dev-token python -m aaysql.server --db university.aaydb \
  --host 0.0.0.0 --port 8765 --tls-cert dev-cert.pem --tls-key dev-key.pem
```

```python
from aaysql.client import AayushSQLClient

with AayushSQLClient("127.0.0.1", 8765, token="dev-token",
                     tls=True, ca_certs="dev-cert.pem",
                     server_hostname="localhost") as db:
    print(db.ping())
```

Never commit generated keys or certificates.

## 9. Process lock

`<database>.server.lock` is locked exclusively with `fcntl.flock` (POSIX) or
`msvcrt.locking` (Windows) before the engine is opened and before the socket is
bound. The OS releases the lock when the process dies, so there is no stale-PID
logic and a crashed server never blocks a restart. The lock file records the
owning PID for diagnostics only.

A consequence: while a server owns a database, running the CLI or opening the
same file with the embedded `Engine` in another process is unsupported — that
access bypasses the coordinator. Stop the server first.

## 10. Limits, timeouts and shutdown

| Setting | Default | Effect |
| --- | --- | --- |
| `max_clients` | 32 | Simultaneous connections; extra connections are rejected with `TOO_MANY_CONNECTIONS`. |
| `max_frame_size` | 1 MiB | Request and response framing limit. |
| `idle_timeout` | 300 s | Close a connection with no traffic. |
| `read_timeout` | 60 s | Socket read timeout; a partial frame at timeout closes the connection with `CONNECTION_TIMEOUT`. |
| `write_timeout` | 30 s | Socket write timeout. |
| `shutdown_timeout` | 10 s | Bound on draining in-flight work during shutdown. |

Graceful shutdown (SIGINT/SIGTERM, or `AayushSQLServer.shutdown()`):

1. stop accepting new connections;
2. close listening socket, then shut down session sockets;
3. wait (bounded) for handlers to unwind, which rolls back any active uncommitted
   transaction through the coordinator;
4. acquire the execution lock with a bounded timeout, so an in-flight statement
   either finishes or is reported as not drained;
5. close the engine (skipping rollback if the durability state is ambiguous, so
   the WAL survives), release the process lock, and report whether work was
   drained.

A shutdown that could not drain in-flight work returns `False` and the CLI exits
with status `1`. Ambiguous commits are never reported as rollbacks.

## 11. Python client

`src/aaysql/client.py` exposes `AayushSQLClient`:

```python
AayushSQLClient(
    host="127.0.0.1",
    port=8765,
    *,
    token=None,
    tls=False,
    ca_certs=None,
    server_hostname=None,
    client_cert=None,
    client_key=None,
    connect_timeout=10.0,
    read_timeout=60.0,
    max_frame_size=1 << 20,
    client_name="aaysql-python",
)
```

Methods: `connect()`, `execute(sql)`, `ping()`, `close()`, plus context-manager
support, `connected` and `authenticated` properties, and `session_id`.

`execute()` returns the same structure `Engine.execute()` returns (minus
`status`), so results can be consumed identically in embedded and client/server
code.

Exception hierarchy:

```
AayushSQLClientError
├── ConnectionError          (includes TLSHandshakeError)
├── AuthenticationError
├── ProtocolError
├── ServerError              (.code, .message, .retryable)
├── RequestTimeoutError
└── ConnectionLostError
```

### Ambiguous requests

The client **never retries**. If a request was sent and the response was not
received (connection lost, read timeout), the outcome may be unknown: the server
may have durably committed the mutation. `ConnectionLostError` and
`RequestTimeoutError` set `outcome_unknown = True` and the connection is closed.

To resolve uncertainty, reconnect and inspect the database (for example, check
for the row you inserted or the table you created) instead of replaying the
statement. Never replay `INSERT`, `UPDATE`, `DELETE`, `CREATE TABLE`,
`DROP TABLE`, `CREATE INDEX`, `DROP INDEX`, or `COMMIT` automatically.

## 12. Concurrency limitations (explicit)

Not supported in v0.8.0:

- concurrent writes; every statement is serialized;
- MVCC, snapshot isolation, or any configurable isolation level;
- read-uncommitted / read-committed behavior;
- multiple server processes writing one database;
- parallel query execution;
- transaction handoff between sessions;
- statements from other sessions while a transaction is open (they get
  `TRANSACTION_BUSY`, not a snapshot of committed data);
- nested transactions and savepoints (unchanged from earlier versions).

Supported: many simultaneous connections, session-owned transactions with
strict single-owner enforcement, autocommit for mutations outside a transaction,
disconnect rollback, prompt recovery-required handling after an ambiguous
commit, and per-request error isolation.

## 13. Operational logging

Logged per connection and request: accepted/rejected connections, session ID,
authentication outcome, request ID, operation category, duration, row counts,
error code, disconnect reason, and transaction outcome. Tokens are never logged.
Raw SQL is not logged by default (only the operation category and, for busy or
recovery errors, the error code). Full tracebacks stay in server logs and are
never sent to clients.
