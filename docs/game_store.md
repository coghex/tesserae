# Game store: durable command execution

This is the durable execution service from
[issue #4](https://github.com/coghex/tesserae/issues/4) (HOST-3 in the
[server delivery design](designs/foreground_server_design.md)). The
implementation is `tesserae/store.py`. It consumes the
[version 1 protocol](protocol.md) unchanged. It has no sockets, terminal
formatting, or model calls. The WebSocket server (HOST-4) adapts its transport
to this service rather than reimplementing any of it.

## Status

Implemented: one SQLite store per game, the `roll` command, replay, conflict
and stale-revision rejection, serialized writers, read-only access, and
[participants with personal join tokens](#participants-and-credentials) (#14).
Commands are parsed and executed through the stateless `dice` compatibility
[module](modules.md) (#38); persisted module bindings and state are #39.
Not implemented: turn rules, inventory, or hosting several games.
The loopback-only WebSocket host (`tesserae serve`, `tesserae/server.py`)
exposes this service. In development mode every connection uses one fixed
participant. With `--authenticated` (#15), each connection joins with a token,
and the host passes the context from `authenticate` to every call. Each store
call runs on a worker thread that opens its own short-lived `GameStore`,
following the ownership rules below. Unexpected failures and `StoreBusy` are
reported as `internal_error`.

## One store, one game

A `GameStore` opens or creates one SQLite file holding one game. A new store
gets a random `game_id`, which is preserved when it is reopened.

An existing file is validated using reads only, before any persistent setting
changes. Validation checks the Tesserae `application_id`, the schema version,
each table's columns, and that there is exactly one game row. Anything else
raises `StoreError` and leaves the file byte-for-byte unchanged. That includes
an unrelated SQLite database, one that merely shares a `user_version`, a
damaged store, and a file that is not SQLite at all. Only an empty database is
initialized. The journal mode switches to WAL only after the file is known to
be a Tesserae store. The store never clears, recreates, or silently repairs a
database.

**Upgrades.** Opening a store with an older schema upgrades it to the current
one in a single transaction, after the old schema has been validated. Either
every schema and version change commits, or the file stays exactly at its old
version. Upgrading from version 1 only adds the participant and credential
tables. The game ID, revision, every command and its result, and the
`local-development` attribution of earlier history are untouched. A store
whose schema is newer than this Tesserae understands is refused unchanged, with
a message to upgrade Tesserae. Older Tesserae versions refuse an upgraded store
the same way.

## Trusted participant context

Every execution and participant-facing read takes a `ParticipantContext`. Only
the host creates it, from its own knowledge of who is connected. It is never
read from client JSON, a nickname, request args, or a client-supplied
connection ID. The wire format has no identity fields. A context is a stable
`participant_id` string. An authenticated context also carries the
`credential_id` it was verified with, so its validity can be rechecked.
`tesserae serve` uses one fixed development context, `local-development`,
which has no credential. `authenticate(token)` produces the authenticated
contexts that `tesserae serve --authenticated` uses.

## Participants and credentials

Participants are durable identities in the store, managed by local operator
commands on the host. There is no network administration and no account,
password, or role system.

- **Identity.** `add_participant` assigns an opaque ID, `p_` followed by 16 hex
  characters. A generated ID can never be `local-development`, and no command
  renames a participant, so development history stays in its own namespace. A
  name is a display label only. Two participants may share one, and it never
  authenticates anyone or selects a replay namespace.
- **Tokens.** Each token is `tess_` plus 32 random bytes from `secrets`,
  URL-safe encoded. The store keeps only its SHA-256 digest, never the token.
  These are random bearer credentials, not passwords, so a fast hash is
  appropriate.
- **One active credential per participant.** `rotate_credential` revokes the
  current token and issues a new one. `revoke_credential` revokes it without a
  replacement, and a later rotation re-enables the participant. Both keep the
  participant ID, every recorded command, and the old credential rows, which
  are marked revoked with a timestamp.
- **Verification.** `authenticate(token)` returns the participant's context for
  an active token and `None` for anything else, without raising on bad input.
  `credential_active(context)` rechecks a context later. Development contexts
  are never active there. Network input becomes a trusted context only through
  `authenticate`.
- **Transactions.** Every change commits under the store's write guard with
  the usual bounded wait, and takes effect for authentication as soon as it
  commits, including in other processes and after a restart. Administration
  never changes the revision or adds history.

Operator commands (`tesserae participant add|list|rotate|revoke`) write each
new token only to a `--token-file` that must not exist yet. The file is created
with user-only permissions (0600). The token file is written before the store
change, and removed again if that change fails, so a committed credential
always has its token on disk. A token never appears in arguments, output,
errors, or the database. A lost token cannot be recovered; rotate to issue a
new one. See the README for examples.

## Revisions

The game has one mechanical revision, shared by all participants. It starts at
0, and each newly committed command advances it by exactly one. The result row,
its history entry, and the revision change are committed in one transaction
before success is returned. A result's `revision` is the revision that command
produced.

## Replay and namespaces

The replay key is (game, participant, `request_id`); the game is implicit in
the store. Alice's `r1` and Bob's `r1` are unrelated requests. Each is
attributed to its participant in the history.

The order of `execute(context, raw)`:

1. Validate the raw message with `parse_request`. Invalid input returns an
   error carrying the current revision. For an authenticated context whose
   credential is no longer active, it returns `session_revoked` instead, so a
   revoked session never learns the revision.
2. Take the write guard (`BEGIN IMMEDIATE`). Every later decision is made
   under it, against the latest committed state. For an authenticated
   context, first check that its credential is still active, and return
   `session_revoked` if not. Because revocation commits under the same guard,
   an action and a revocation are strictly ordered. A credential checked
   before waiting for the lock could otherwise act on stale authorization.
   Replays are refused the same way.
3. Look up (participant, `request_id`). If a record exists and its content
   equals `CommandRequest.content()`, return the original result message,
   including its original revision. If the content differs, return
   `request_conflict`. Replay happens before any revision check, so a retry
   still works after the game has moved on.
4. If `expected_revision` is not the current revision, return
   `stale_revision` with the current revision. The store never substitutes
   the current revision and executes anyway.
5. Otherwise roll, record, advance the revision, and commit.

Invalid, conflicting, stale, and replayed requests draw no dice and change no
state. Content compares as parsed JSON, so object key order is irrelevant.
Expressions compare as exact strings.

`execute_command(context, raw)` is `execute` plus a flag saying whether that
call committed a fresh command. The authenticated host announces a new
revision only when the flag is true, never for replays, conflicts, stale or
invalid requests, failures, or revoked sessions.

## Reads

`revision()`, `session_revision(context)`, `lookup(context, request_id)`, and
`history(context)` never write. `session_revision` is the participant-facing
read the authenticated host uses for joins and syncs. It returns `None`
instead of the revision when the context's credential is no longer active,
checking and reading in a single statement. None of these advance the
revision, roll dice, or add history.
`lookup` and `history` return only the caller's own records, so another
participant's `r1` is invisible to them. `inspect_history()` returns every
participant's records and is for operators and tests, never for answering a
participant. Receiving a question or chat message is not a mechanical state
change and should not go through `execute`.

## Contention and failure

- Writers are serialized by SQLite's write lock. A writer waits up to
  `busy_timeout` seconds (default 5) for the lock. If it is still unavailable,
  `execute` raises `StoreBusy` before drawing anything or changing state. It
  does not retry indefinitely. Callers may report this as `internal_error`.
- Any exception before commit rolls the whole transaction back, so no result,
  history entry, or revision change is left behind, and the exception
  propagates. Nothing is recorded, so there is nothing to replay. The client
  may retry the same request, which executes fresh.
- Once committed, an outcome is durable (WAL journal, `synchronous = FULL`). A
  later disconnect cannot undo it, and a retry returns it.

## Execution boundary for rules modules

Later modules must make state-dependent decisions for a fresh action inside the
same guarded transaction as its effects. Examples are whether it is this
player's turn or phase, whether they own an item, and whether a quantity is
available. Checking first and writing in a later transaction is not allowed.
For example, an inventory transfer debits one side and credits the other in
one transaction. Whether someone may read or replay a result is a separate
question from whether they may take a new action that uses a turn. This child
implements no turns or inventory. The [module contract](modules.md) (#38)
gives executors a private state copy and `ActionRejected` for exactly this;
the store runs them inside the guarded transaction from #39.

## Connections and threads

A `GameStore` owns one SQLite connection and may only be used by the thread
that created it; Python's `sqlite3` enforces this. An asynchronous host must
not call it on the event loop thread. It should run store calls on a worker
thread that owns its own `GameStore`, for example a single-thread executor via
`loop.run_in_executor`. Several stores, in several threads or processes, can
safely open the same file, and the write guard serializes them. Never hold a
transaction across a network wait or model call. `execute` opens and closes
its transaction within the call, and the only work inside it is validation
lookups and dice arithmetic.

## Storage

SQLite's header `application_id` identifies a Tesserae store (`0x54455353`,
"TESS"). The schema version is SQLite's `user_version`, currently 2.

| Table | Since | Contents |
|---|---|---|
| `game` | 1 | One row: `game_id`, `revision` |
| `commands` | 1 | One row per committed command: `revision`, `participant_id`, `request_id`, canonical content JSON, dice result JSON |
| `participants` | 2 | `participant_id`, display `name`, `created_at` |
| `credentials` | 2 | `id`, `participant_id`, token `digest`, `issued_at`, `revoked_at` (null while active; at most one active per participant) |

The `commands` table is the history. It is not an event-sourcing log, and
SQLite is the only live authority. `local-development` appears in `commands`
but has no `participants` row.
