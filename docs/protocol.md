# Protocol, version 1

This is the host/client message contract. Commands come from
[issue #3](https://github.com/coghex/tesserae/issues/3) (HOST-2 in the
[server delivery design](designs/foreground_server_design.md)), and the
[authenticated session layer](#authenticated-sessions) comes from
[issue #13](https://github.com/coghex/tesserae/issues/13) (tracker
[#12](https://github.com/coghex/tesserae/issues/12)). The implementation is
`tesserae/protocol.py`. It validates messages and formats replies; it does not
execute commands, store state, verify credentials, or open connections. Hosts,
stores, and clients consume this contract rather than defining their own.

Each message is one JSON object. Field order is insignificant.

## Messages

Request, client to host:

```json
{"version": 1, "type": "command", "request_id": "r1", "expected_revision": 0,
 "command": "roll", "args": {"expression": "2d6+3"}}
```

Result, host to client. `result` is the dice evaluator's structured result
unchanged, the same object `tesserae roll --json` prints:

```json
{"version": 1, "type": "result", "request_id": "r1", "revision": 1,
 "result": {"schema_version": 1, "expression": "2d6+3",
            "terms": [{"kind": "dice", "count": 2, "sides": 6, "rolls": [4, 3],
                       "sign": 1, "subtotal": 7},
                      {"kind": "constant", "value": 3, "sign": 1, "subtotal": 3}],
            "total": 10}}
```

Error, host to client:

```json
{"version": 1, "type": "error", "request_id": "r1", "revision": null,
 "code": "invalid_request", "message": "Human-readable explanation"}
```

Welcome, host to client on connection in [development mode](#host-modes):

```json
{"version": 1, "type": "welcome", "revision": 0}
```

## Request rules

- A request has exactly the six fields shown above; missing or extra fields are
  rejected.
- `version` is an integer. Only `1` is supported.
- `type` is the string `"command"`.
- `request_id` is a string of 1 to 128 characters containing no whitespace
  anywhere. It is preserved exactly; nothing is trimmed or normalized.
- `expected_revision` is an integer from 0 through 9223372036854775807
  (2^63 − 1).
- `command` is a string and `args` is a JSON object.
- JSON booleans are never accepted as integers.
- Only the game's [module](modules.md) commands are accepted. A game created
  by `serve` or `participant add` uses the `dice` compatibility module, whose
  only command is `roll`. A [Relay](relay.md) game has only `relay.enlist` and
  `relay.repair`; `roll` there is `unsupported_command`.
  Its `args` has exactly one field, `expression`, a string validated with the
  existing dice rules and limits (at most 4096 characters, 1000 dice, and
  1,000,000 sides). Validation never rolls dice.
- Malformed JSON, duplicate object keys at any depth, non-finite numbers
  (`NaN`, `Infinity`, `-Infinity`, or overflowing literals such as `1e999`),
  nesting too deep to parse, and strings that are not valid Unicode (unpaired
  surrogate escapes such as `"\ud800"`, in any key or value) are all rejected.

## Errors

Error codes are stable and clients may branch on them. Messages are readable
diagnostics only and may change; do not parse them.

| Code | Meaning | Defined by |
|---|---|---|
| `invalid_request` | Malformed JSON, wrong structure, missing/extra field, or wrong type or range | HOST-2 |
| `unsupported_version` | A well-formed request with a version other than 1 | HOST-2 |
| `unsupported_command` | A well-formed request naming an unknown command | HOST-2 |
| `invalid_expression` | A well-formed `roll` whose expression is invalid | HOST-2 |
| `action_rejected` | A well-formed, current command the game's rules do not allow now; nothing changed | #38, #39 |
| `stale_revision` | `expected_revision` does not match the current state | HOST-3 |
| `request_conflict` | The sender reused a request ID for different content | HOST-3 |
| `internal_error` | The host failed unexpectedly or its store was busy | HOST-4 |
| `authentication_failed` | A join's token is malformed, unknown, revoked, or rotated; these are never distinguished | #13 |
| `join_required` | An authenticated connection's first message was a well-formed command or sync, not a join | #13 |
| `join_timeout` | No join arrived within 10 seconds of connecting | #13 |
| `already_joined` | A joined connection sent another join | #13 |
| `session_revoked` | The joined session's credential was revoked or rotated | #13 |
| `referee_busy` | A new `say` while the referee answers another input; nothing was stored. Carries `job: {"state", "for"}` | #68 |
| `referee_mode` | A gameplay `command` in a game with a referee, busy or not | #68 |
| `referee_off` | A `say`, `referee_status` or `messages` in a game without a referee | #68 |
| `referee_needs_rebuild` | A `say` while the referee's lost session awaits the operator's rebuild | #68 |

The session codes are used only in [authenticated mode](#authenticated-sessions).
Each has one fixed message (`SESSION_ERROR_MESSAGES`), and their `request_id`
and `revision` are always `null`.

Checks run in this order, and the first failure decides the code:

1. JSON decoding and top-level object shape.
2. Field presence and types of every top-level field, then, when `command` is
   one of the game's commands, the shape and type of its `args`.
3. Version.
4. Command.
5. The command's argument values, such as a `roll` expression.

So any structural error is `invalid_request` even if the version or command
is also unsupported. Note that step 2 checks argument shape before the
version check. A module validator's `invalid_request` errors are step 2, and
its other codes are step 5 ([modules.md](modules.md#argument-errors-and-the-protocol)).

An error's `request_id` is the request's own ID when the JSON parsed to an
object whose `request_id` is valid, even if other fields are wrong. Otherwise
it is `null`. An error's `revision` is the host's current revision when known,
otherwise `null`. Result and welcome revisions are integers in the same range
as `expected_revision`.

## Replay identity

The host uses this to return the original result for a retried request instead
of executing it twice. See [game_store.md](game_store.md) for the execution
order and failure behavior.

- The lookup key is `request_id` within the sender's server-side namespace:
  (game, participant, `request_id`). The host establishes the participant from
  its own trusted context, never from the message, so the wire format has no
  identity fields. Two participants may both use `r1` without conflict, and
  neither can see or replay the other's record.
- The request's content is its parsed `version`, `type`, `command`, and `args`
  (`CommandRequest.content()`). Content compares by parsed JSON value, so object
  key order does not matter.
- `expected_revision` is not part of the content, so an otherwise identical
  retry still retrieves the original result after the revision has moved on.
- Expressions compare as exact strings. `"2d6+3"` and `"2d6 + 3"` are different
  content; equivalent arithmetic and whitespace are not normalized.
- The same ID with the same content is a replay. The same ID with different
  content is a `request_conflict`.

## Host modes

A host serves one persisted game. Choosing the endpoint chooses the game.
There is no automatic fallback from one mode to the other. Both listen on
127.0.0.1 by default. Authenticated mode can also use TLS (`wss://`), and only
authenticated TLS may listen on another address of this computer (`--host`).
See [tls.md](tls.md). The messages are the same with or without TLS.

- **Development mode** is the existing `tesserae serve --database PATH --port
  PORT`. Every connection acts as the fixed participant `local-development`.
  The host sends `welcome` on connect and replies to commands. It does not
  understand `join` or `sync` (they get `invalid_request`), and it sends no
  notifications, so the `python3 -m websockets` probe is unchanged.
- **Authenticated mode** is
  `tesserae serve --database PATH --port PORT --authenticated`
  ([#15](https://github.com/coghex/tesserae/issues/15)). Every connection must
  join with a personal token, and acts as that token's participant. Joined
  connections receive shared `revision` notifications
  ([#16](https://github.com/coghex/tesserae/issues/16)).

**Browser origins.** Every host refuses, with HTTP 403, a WebSocket handshake
whose `Origin` header is anything but browser mode's own page (below), so a
web page elsewhere cannot open a socket to it. Command-line clients send no
`Origin` and are unaffected.

**Browser mode** is `tesserae serve --authenticated --browser`, for a game in
referee mode ([#84](https://github.com/coghex/tesserae/issues/84)). The same
port also serves the packaged browser page at `http://127.0.0.1:PORT/`, for
players on this computer: no TLS and no other `--host`. Every request must
name the host as `127.0.0.1:PORT` or `localhost:PORT` (otherwise 421), the
page's own origins may open sockets, and only the page's packaged files are
served. The page uses the same messages as any client. The exact rules are in
the [browser contract](designs/browser_contract.md#2-serving), sections 2 to 4.

At startup the host loads the game's bound module and providers exactly
([#41](https://github.com/coghex/tesserae/issues/41)). If they are unknown or
do not match, `serve` exits 1 before listening, with no fallback to `dice`. A
module that requires authenticated participants, such as Relay, refuses
development mode, even on 127.0.0.1: `serve` exits 2 and starts nothing. The
host's first line names the module, for example `module: relay 1`. A module
from an explicit package is loaded the same way with `serve --module-dir`
([#52](https://github.com/coghex/tesserae/issues/52), see
[modules.md](modules.md#creating-and-serving-an-external-game)); the protocol is
unchanged.

## Authenticated sessions

### Messages

Join, client to host. It must be the first message:

```json
{"version": 1, "type": "join", "token": "<contents of your token file>"}
```

Snapshot, host to client. It is the reply to a successful join and to every
sync, and is the recipient's whole view. In a `dice` game it is metadata only:

```json
{"version": 1, "type": "snapshot", "game_id": "6f1c2a9e-3b7d-4c51-9a2e-8d0f4b6c1e37",
 "participant_id": "p_7h3k9q2m", "revision": 4}
```

A game whose module projects views, such as [Relay](relay.md#what-players-see),
adds the module's identity and this participant's `view`:

```json
{"version": 1, "type": "snapshot", "game_id": "6f1c2a9e-3b7d-4c51-9a2e-8d0f4b6c1e37",
 "participant_id": "p_7h3k9q2m", "revision": 4,
 "module": {"id": "relay", "version": "1", "state_version": 1},
 "view": {"scene": {"id": "relay_station", "restored": false, "...": "..."},
          "character": null, "commands": ["relay.enlist"], "rules": {"...": "..."}}}
```

Sync, client to host. It asks for a fresh snapshot and is read-only:

```json
{"version": 1, "type": "sync"}
```

Revision notification, host to client, unsolicited:

```json
{"version": 1, "type": "revision", "game_id": "6f1c2a9e-3b7d-4c51-9a2e-8d0f4b6c1e37",
 "revision": 5}
```

Commands, results, and errors are exactly the messages above. Session messages
are an additive layer; the six-field command and its replay content are
unchanged, and no message carries a client-asserted identity or role.

### Message rules

- `join` has exactly `version`, `type`, and `token`. `token` is a string. A
  token that is empty, longer than 512 characters, or contains whitespace is
  `authentication_failed`, exactly like an unknown token, so a token's format
  reveals nothing.
- `sync` has exactly `version` and `type`.
- Structure and types are checked first (`invalid_request`), then the version
  (`unsupported_version`). The same strict JSON rules apply as for commands.
  Any other `type` is judged as a command, so its errors are unchanged.
- `snapshot` contains `game_id`, the recipient's own `participant_id`, and
  the current mechanical `revision`. It has no roster, history, dice results,
  credentials, or operator view.
- A module game's snapshot also has both `module` (exactly `id`, `version` and
  `state_version`) and `view`, a JSON object; a `dice` snapshot has neither.
  The module builds `view` for that participant from the game state at
  `revision`, read in the same transaction as the credential check. It is
  never the complete stored state. The view's fields belong to the module;
  the protocol fixes only that it is an object.
- `revision` contains only `game_id` and the new `revision`. It never names a
  participant, request ID, expression, or result.
- `game_id` and `participant_id` are opaque strings of 1 to 128 characters
  without whitespace.
- No error message ever includes a presented token or a client-supplied field
  name. Unexpected and duplicate fields are counted or described generically,
  never named, because a token pasted as a key would otherwise be echoed back.

### Joining

The token travels only in the first application message, never in the URL, a
query parameter, or a command. A display name never selects an identity.

| The connection's first message, or event | Host reply | Then |
|---|---|---|
| Valid `join` for an active credential | `snapshot` | Joined |
| `join` with a malformed, unknown, revoked, or rotated token | `authentication_failed` | Close 1008 |
| Well-formed `command` or `sync` | `join_required` | Close 1008 |
| Malformed JSON, any other message, or a binary frame | `invalid_request` | Close 1008 |
| A well-formed `join` for an unsupported version | `unsupported_version` | Close 1008 |
| The host fails while checking the join | `internal_error` | Close 1008 |
| Nothing within 10 seconds (`JOIN_TIMEOUT_SECONDS`) | `join_timeout` | Close 1008 |

Before a successful join, the host sends nothing that describes the game: no
welcome, revision, or game ID. Failures are reported identically whichever
lookup failed. A connection joins at most once. A later `join` gets
`already_joined`, and the connection stays open as before.

### Replies and notifications

**Reply pairing.** After joining, the host answers each client text message
with exactly one reply, in the order received. A `command` gets a `result` or
`error`, a `sync` gets a `snapshot` or `error`, and a binary frame gets
`invalid_request`. `revision` notifications are unsolicited, use their own type,
and never count as replies. A session error that closes the connection is the
final reply. A client can therefore count its outstanding requests.

**Privacy.** Only the requesting connection receives a command's `result` or
`error`. Every joined connection may learn the shared revision; this
metadata is deliberately visible to all participants in this first slice.

**Notifications.** After a fresh command commits, the host sends `revision` to
every joined connection, including the requester's. The requester's
notification may arrive before or after its `result`. A notification never
carries a view: to see what changed, send a `sync`. Replays, rejected
requests, failures, joins, syncs, and credential changes never produce a
notification, because they do not change the revision.

- **Monotonic.** On each connection, the revisions in snapshots and
  notifications never decrease. The host skips a notification at or below a
  revision that connection has already been sent. Results do not count, since
  a replayed result carries its original, older revision.
- **Coalescible.** The host may skip intermediate revisions and send only the
  newest. A notification means "the game is at least at this revision", not a
  history entry.
- **No gap at join.** The host subscribes a connection to notifications before
  reading the snapshot it sends. A commit that races a join is therefore covered
  either by the snapshot or by a later notification. The same holds for a sync.
- **A view keeps its revision.** The host holds back that connection's
  notifications from just before it reads a snapshot until it has sent it, so
  a snapshot never follows a notification newer than its own revision. It
  never relabels a view with a revision it was not read at. A commit that lands
  while the snapshot is read is announced right after it. Other connections
  are not held up, and no database transaction is open during the send.
- **Slow clients.** Pending notifications for one connection collapse to the
  newest revision, so there is no unbounded queue. Announcing a commit never
  waits for any connection. If one message to a connection cannot be written
  within 10 seconds (`WRITE_TIMEOUT_SECONDS`), the host closes that connection
  with code 1013 and reason `slow consumer`. If even the close cannot be sent,
  it drops the connection. The client recovers by reconnecting and joining. A
  slow connection never holds a database transaction or delays another
  connection's replies.
- **Never silently behind.** Before each notification the host rechecks the
  session against the store. If that check fails, for example because the
  store is busy, it retries twice (`NOTIFY_RETRY_DELAYS`). If the check still
  fails, the host closes the connection with code 1011 rather than leave the
  client unaware of a newer revision. Reconnect and join for a fresh snapshot.

There is no event log or history cursor. Current state always comes from a
fresh snapshot. Notifications work only within one host process; another
process writing the same database does not trigger them.

### Credentials, revocation, and rotation

Operators provision participants and token files locally
([#14](https://github.com/coghex/tesserae/issues/14)); there is no network
administration. After joining, the host rechecks that the session's credential
is still valid for every protected operation: syncs, replays, fresh commands,
and notification delivery.

For a fresh command, that check runs inside the store's write transaction, the
same guard that checks the revision and commits the effects. A revocation
commits under the same guard, so the two are strictly ordered. A command whose
transaction started before the revocation committed may finish and is
recorded. After that, protected operations get `session_revoked`, and the host
closes the connection with 1008. If the host notices the revocation while
delivering a notification, no request is being answered, so it closes with
1008 and reason `session_revoked` without sending an error. Either way the
revoked session receives no further views or notifications. Revocation never
undoes committed actions.

Rotation invalidates the old token the same way. The participant joins again
with the new token and keeps the same `participant_id` and replay namespace.

### Recovery

When a reply is uncertain (for example, the connection dropped after sending):

1. Reconnect and join. The snapshot gives the current revision, which may be
   newer than before.
2. Resend the uncertain command unchanged, with its original `request_id`
   and `expected_revision`.
3. The reply is one of three outcomes:
   - **It had committed.** The `result` is the original outcome, with no new
     roll. Its `revision` is the revision that command produced, which may be
     older than the snapshot's.
   - **It never committed, and `expected_revision` still matches** the current
     revision. It executes now, for the first time, and the `result` is that
     fresh outcome.
   - **It never committed, and the revision has moved on.** The reply is
     `stale_revision`. Deciding whether to submit a new action, with a new
     `request_id` and the current revision, is the player's choice.

Clients and hosts never silently replace `expected_revision`, mint a new ID, or
rerun a rejected action.

### Transcripts

Lines starting `> ` are sent by the client; copy them without the prefix.
Lines starting `< ` are received. Token values are placeholders.

Initial join:

```
> {"version": 1, "type": "join", "token": "ALICE-TOKEN"}
< {"version": 1, "type": "snapshot", "game_id": "6f1c2a9e-3b7d-4c51-9a2e-8d0f4b6c1e37", "participant_id": "p_alice", "revision": 0}
```

Synchronization:

```
> {"version": 1, "type": "sync"}
< {"version": 1, "type": "snapshot", "game_id": "6f1c2a9e-3b7d-4c51-9a2e-8d0f4b6c1e37", "participant_id": "p_alice", "revision": 0}
```

Alice's private result, and the shared update Bob sees. Alice's connection:

```
> {"version": 1, "type": "command", "request_id": "r1", "expected_revision": 0, "command": "roll", "args": {"expression": "2d6+3"}}
< {"version": 1, "type": "result", "request_id": "r1", "revision": 1, "result": {"schema_version": 1, "expression": "2d6+3", "terms": [{"kind": "dice", "count": 2, "sides": 6, "rolls": [4, 3], "sign": 1, "subtotal": 7}, {"kind": "constant", "value": 3, "sign": 1, "subtotal": 3}], "total": 10}}
< {"version": 1, "type": "revision", "game_id": "6f1c2a9e-3b7d-4c51-9a2e-8d0f4b6c1e37", "revision": 1}
```

Bob's connection, idle, receives only:

```
< {"version": 1, "type": "revision", "game_id": "6f1c2a9e-3b7d-4c51-9a2e-8d0f4b6c1e37", "revision": 1}
```

Reconnect and retry. Alice's connection dropped before she saw her reply to
`r2`, and Bob has rolled since:

```
> {"version": 1, "type": "join", "token": "ALICE-TOKEN"}
< {"version": 1, "type": "snapshot", "game_id": "6f1c2a9e-3b7d-4c51-9a2e-8d0f4b6c1e37", "participant_id": "p_alice", "revision": 3}
> {"version": 1, "type": "command", "request_id": "r2", "expected_revision": 1, "command": "roll", "args": {"expression": "1d20"}}
< {"version": 1, "type": "result", "request_id": "r2", "revision": 2, "result": {"schema_version": 1, "expression": "1d20", "terms": [{"kind": "dice", "count": 1, "sides": 20, "rolls": [17], "sign": 1, "subtotal": 17}], "total": 17}}
```

`r2` had committed at revision 2, so the retry returns that original outcome
without rolling. Had it not committed, the reply would be `stale_revision` with
`revision` 3, because its `expected_revision` of 1 no longer matches. Had the game
still been at revision 1, the retry would have executed for the first time.

## Client

The thin client ([#17](https://github.com/coghex/tesserae/issues/17),
`tesserae/client.py`) is:

```sh
python3 -m tesserae connect URI --token-file PATH [--game-id ID] [--ca-file CERT]
```

`URI` is `wss://HOST:PORT/` for an encrypted host, verified against `--ca-file`
or the system's trusted authorities. It can be `ws://` only for this computer
(a loopback address or `localhost`). Certificate and name checks finish before
the join is sent. See [tls.md](tls.md).

- The token file holds the token alone, as UTF-8 text with an optional trailing
  newline. Operator commands create it with user-only permissions. Keep it
  outside the repository. The token never appears on a command line or in a URL.
- The client joins automatically. With `--game-id`, it exits nonzero if the
  snapshot names another game, before sending any input. A snapshot must have
  both `module` and `view` or neither; the client checks only their shape and
  prints the view unchanged. It has no game rules or local state. It refuses a URI
  that carries a query, fragment, or credentials, without echoing it.
- Stdout is one JSON message per line: the snapshot, every reply, and every
  notification. Diagnostics go to stderr. The token is never printed.
- Each nonblank stdin line is sent unchanged as one message, as soon as it is
  read. The client never validates, rewrites, or retries a request.
- **Reply pairing in practice.** The client keeps the lines it has sent in
  order. A message from the host answers the oldest one only if it is a
  well-formed version 1 reply to that line:
  - a `command` gets its own `result` (same `request_id`, a revision, and a
    result object);
  - a `sync` gets this session's `snapshot` (same game, participant and
    module);
  - any line can get an `error` with a code and a message.

  Anything else is a protocol failure, for example a snapshot answering a
  roll, or a result with no payload. The client stops without printing it,
  and reports the requests still unanswered as uncertain.
- **End of input.** The client waits up to `--drain-timeout` seconds (default
  10) for replies to everything sent, then closes. It does not wait for
  further notifications. If replies are missing, or the host closes with
  requests outstanding, it names their request IDs on stderr and exits 3.
  Resend those lines unchanged. Ctrl-C exits 130, and a host that ends the
  session exits 1.
- There is no automatic reconnection. Recovery is running the client again,
  as described under [Recovery](#recovery).

## Referee mode

A game with a referee (`tesserae referee enable`) is played by talking: the
host refuses every gameplay `command` from a player (`referee_mode`), idle or
busy, and the referee runs the module's commands. It needs an authenticated
host. The [referee contract](designs/referee_contract.md) (sections 7, 8 and
12) is the full reference.

To run one: `tesserae referee setup` once, then log Codex in as it says;
`tesserae referee enable --database DB`; then `tesserae serve --authenticated
--database DB --port PORT` (with `--referee-dir` for another referee folder,
and `--referee-timeout` to change the 300 s limit on one job). `tesserae
referee stop --database DB` stops a running job, which ends `interrupted`. If
input is refused with `referee_needs_rebuild` (Codex lost the session), stop
the host and run `tesserae referee rebuild --database DB`; the next job
starts a new session from the stored conversation.

In short:

| Client sends | Host replies |
|---|---|
| `{"version": 1, "type": "say", "request_id", "text", "private"}` | `{"type": "accepted", "request_id", "job_id", "seq"}`, or `referee_busy` (nothing stored), or another error |
| `{"version": 1, "type": "referee_status"}` | `{"type": "referee_status", "state", "choices", "last_seq", "your_last_input"}` |
| `{"version": 1, "type": "messages", "after_seq"}` | `{"type": "messages", "messages": [...]}`: only what the caller may see |

Unsolicited, in order: `{"type": "message", "seq", "from": {"kind": "referee"}
or {"kind": "player", "name"}, "audience": "table" or "private", "text"}` to
each recipient (each session is rechecked first; a revoked one is closed and
gets nothing), and `{"type": "referee", "state": "busy" or "idle"}` to everyone
when a job starts or ends. These follow the store's order: a job's reply
and `idle` are queued before the next input is accepted, so they never arrive
after the next job's message or `busy`. The sender gets its `accepted` before
its own message; an accepted job runs even if that reply cannot be delivered.
Join and sync snapshots carry the caller's
`referee` object, the same as `referee_status`; only the caller's own pending
choice is in it.

- One job at a time, with no queue: a `say` while busy is refused, and the
  player types it again later.
- An exact retry (same request ID, text and privacy) returns its recorded
  `accepted`, busy or not, plus the stored reply messages to that connection;
  it never starts a second job.
- An unmarked `say` goes to the whole table; `"private": true` goes only to the
  referee and the sender, and so does a reply to it.

### The client in referee mode

When the join snapshot carries `referee`, `tesserae connect` is a
conversation: each typed line is the player's words, sent as a `say`, even if
it looks like JSON or a command. Lines starting with `/` are plumbing:
`/private TEXT`, `/status`, `/sync`, `/history`, `/retry` and `/help`; `//TEXT`
sends text that starts with `/`. It prints readable lines:

```text
[joined] Type to talk to the referee; /help lists commands.
[status] idle; nothing waiting for you; last message 0
[table] Alice: Can Wren fix the relay?
[busy] The referee is answering.
[table] Referee: Wren can try: 1d6 plus repair 1, needing 5. Try now?
[idle] The referee is ready.
```

On joining it asks for `referee_status`, so it shows the player's last input
and its state (`[your last input] "Yes, go." completed`). `/retry` resends the
client's input still waiting for its reply, else its last accepted input (a
later input refused as busy does not replace it), else that reported input, with
its original request ID: the host returns its recorded state and reply, never
a second job. `connect --json` prints JSON lines instead, for scripts and
tests; input is still plain text. Games without a referee are unchanged.
