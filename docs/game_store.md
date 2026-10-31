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
Each game is bound to one [module](modules.md) (#38) and keeps that module's
JSON state (#39): see [Modules and state](#modules-and-state). `tesserae game
create` makes a game bound to a chosen bundled module, and `serve` hosts it with
per-participant views (#41). Not implemented: turn rules, inventory, or several
games per store.
The loopback-only WebSocket host (`tesserae serve`, `tesserae/server.py`)
exposes this service. In development mode every connection uses one fixed
participant. With `--authenticated` (#15), each connection joins with a token,
and the host passes the context from `authenticate` to every call. Each store
call runs on a worker thread that opens its own short-lived `GameStore`,
following the ownership rules below. Unexpected failures and `StoreBusy` are
reported as `internal_error`.

## One store, one game

A `GameStore` opens or creates one SQLite file holding one game. A new store
gets a random `game_id`, which is preserved when it is reopened. Opening a
missing file creates a `dice` game, as before. `GameStore.create(path,
runtime)` creates a game bound to another module instead; see
[Modules and state](#modules-and-state).

An existing file is validated using reads only, before any persistent setting
changes. Validation checks the Tesserae `application_id`, the schema version,
each table's columns, and that there is exactly one game row and (from
schema 3) one module binding. Anything else
raises `StoreError` and leaves the file byte-for-byte unchanged. That includes
an unrelated SQLite database, one that merely shares a `user_version`, a
damaged store, and a file that is not SQLite at all. Only an empty database is
initialized. The journal mode switches to WAL only after the file is known to
be a Tesserae store. The store never clears, recreates, or silently repairs a
database.

**Upgrades.** Opening a store with an older schema upgrades it to the current
one in a single transaction, after the old schema has been validated. Either
every schema and version change commits, or the file stays exactly at its old
version. Upgrading from version 1 adds the participant and credential
tables. Upgrading to version 3 adds the module binding, binding the game to
`dice` with state `{}`. The game ID, revision, every command and its result,
participants, credentials, and the `local-development` attribution of
earlier history are untouched. A store
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
5. Otherwise run the module's command against the current state. It checks
   its own rules first. If the action is not allowed now, it raises
   `ActionRejected`, which returns `action_rejected` with the current
   revision.
6. Write the replacement state, record the result, advance the revision by
   one, and commit, all in the one transaction.

Invalid, conflicting, stale, rejected, and replayed requests draw no dice and
change no state. A rejected request leaves its `request_id` unused, like a
stale one. A failed check, such as a repair roll that misses, is a normal
committed outcome, not a rejection.
Replay comes before the module's rules, so a retry still returns its original
result after a new attempt would be refused, for example once a resource is
spent. Content compares as parsed JSON, so object key order is irrelevant.
Expressions compare as exact strings.

`execute_command(context, raw)` is `execute` plus a flag saying whether that
call committed a fresh command. The authenticated host announces a new
revision only when the flag is true, never for replays, conflicts, stale or
invalid requests, failures, or revoked sessions.

## Reads

`revision()`, `session_revision(context)`, `participant_view(context)`,
`lookup(context, request_id)`, and `history(context)` never write.
`participant_view` returns the module's view for that participant, with the
revision it belongs to. It returns `None` if the credential is no longer
active. The credential check, revision and state come from one statement, so
the view and its revision always agree. A view is the module's projection,
never the complete state, and it is `None` for `dice`. `session_revision` is the participant-facing
read the authenticated host uses for joins and syncs. It returns `None`
instead of the revision when the context's credential is no longer active,
checking and reading in a single statement. None of these advance the
revision, roll dice, or add history.
`lookup` and `history` return only the caller's own records, so another
participant's `r1` is invisible to them. `inspect_history()` returns every
participant's records, and `inspect_state()` returns the complete module
state. Both are for operators and tests, never for answering a participant. Receiving a question or chat message is not a mechanical state
change and should not go through `execute`.

## Contention and failure

- Writers are serialized by SQLite's write lock. A writer waits up to
  `busy_timeout` seconds (default 5) for the lock. If it is still unavailable,
  `execute` raises `StoreBusy` before drawing anything or changing state. It
  does not retry indefinitely. Callers may report this as `internal_error`.
- Any exception before commit rolls the whole transaction back, so no state
  change, result, history entry, or revision change is left behind, and the
  exception propagates. That includes an unexpected error in a module. Nothing is recorded, so there is nothing to replay. The client
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
question from whether they may take a new action that uses a turn. The store
runs a module's command inside the write guard, after replay and the revision
check. The module gets a private copy of the state, and its replacement
state commits with its result. The store has no turns or inventory of its
own.

## Modules and state

A game's module binding is fixed when the game is created: module ID,
implementation version, state version, and the selected provider for each
operation. The store keeps one JSON state object for it. There is no module
switch, reset, or state upgrade.

- **Creating.** `GameStore.create(path, runtime)` refuses any existing path,
  even an empty file. It writes the new store, the binding, and the module's
  initial state in one transaction, at revision 0. If that fails, the new file
  is removed. `tesserae game create --database NEW.sqlite3 --module ID`
  resolves the bundled module before creating anything. Add participants
  afterwards with `tesserae participant add`.
- **Running.** Executing a command or reading a view needs the game's
  runtime. `tesserae serve` loads it once at startup with `store.runtime()`,
  then passes each worker's `GameStore(path, runtime=...)` that runtime, which
  must match the binding exactly. Without one, the store resolves the binding
  from the bundled catalog on first use. Any mismatch
  raises `StoreError`, and an unknown module, version, state version or
  provider fails the same way. There is no fallback to `dice` and nothing is
  drawn.
- **Administering.** Participant and credential commands,
  `binding()`, `inspect_state()` and history reads need no module code. They
  work on a game whose module this Tesserae does not have.

## Connections and threads

A `GameStore` owns one SQLite connection and may only be used by the thread
that created it; Python's `sqlite3` enforces this. An asynchronous host must
not call it on the event loop thread. It should run store calls on a worker
thread that owns its own `GameStore`, for example a single-thread executor via
`loop.run_in_executor`. Several stores, in several threads or processes, can
safely open the same file, and the write guard serializes them. Never hold a
transaction across a network wait or model call. `execute` opens and closes
its transaction within the call, and the only work inside it is validation
lookups and the module's synchronous rules.

## Storage

SQLite's header `application_id` identifies a Tesserae store (`0x54455353`,
"TESS"). The schema version is SQLite's `user_version`, currently 3.

| Table | Since | Contents |
|---|---|---|
| `game` | 1 | One row: `game_id`, `revision` |
| `commands` | 1 | One row per committed command: `revision`, `participant_id`, `request_id`, canonical content JSON, result JSON |
| `participants` | 2 | `participant_id`, display `name`, `created_at` |
| `credentials` | 2 | `id`, `participant_id`, token `digest`, `issued_at`, `revoked_at` (null while active; at most one active per participant) |
| `module` | 3 | One row: `module_id`, `module_version`, `state_version`, selected `providers` JSON (`[operation, provider, version]` triples), and the module's `state` JSON |

The `commands` table is the history. It is not an event-sourcing log, and
SQLite is the only live authority. `local-development` appears in `commands`
but has no `participants` row.
