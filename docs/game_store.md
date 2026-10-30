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
and stale-revision rejection, serialized writers, and read-only access. Not
implemented: authentication, a network endpoint, turn rules, inventory, or
hosting several games.

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

## Trusted participant context

Every execution and participant-facing read takes a `ParticipantContext`. Only
the host creates it, from its own knowledge of who is connected. It is never
read from client JSON, a nickname, request args, or a client-supplied
connection ID. The wire format has no identity fields. A context is just a
stable `participant_id` string; there is no account schema yet. HOST-4 uses one
fixed local development context. Later multi-user work will supply
authenticated contexts.

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
   error carrying the current revision.
2. Take the write guard (`BEGIN IMMEDIATE`). Every later decision is made
   under it, against the latest committed state.
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

## Reads

`revision()`, `lookup(context, request_id)`, and `history(context)` never
write. They do not advance the revision, roll dice, or add history.
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
implements no turns, inventory, or module framework.

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
"TESS"). The schema version is SQLite's `user_version`, currently 1. There are two
tables: `game` (a single row: `game_id`, `revision`) and `commands` (one row per
committed command: `revision`, `participant_id`, `request_id`, canonical
content JSON, and the dice result JSON). The `commands` table is the history.
It is not an event-sourcing log, and SQLite is the only live authority.
