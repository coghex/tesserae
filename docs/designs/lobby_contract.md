# M2-2 lobby host contract (HOST-2)

Status: specification for [#97](https://github.com/coghex/tesserae/issues/97)
under tracker [#95](https://github.com/coghex/tesserae/issues/95), written
before its implementation and implemented with it in `tesserae/lobby.py`,
`tesserae/server.py` (`CampaignService`), the store's `host_links` and
`tesserae host campaign link`. It turns the
[host lobby design](host_lobby_design.md) into exact rules for the
registry-backed host: the lobby messages, how a host player becomes a
campaign participant, and how access checks are ordered against game commits.
The CLI (`tesserae connect --lobby`, #98) implements its client rules; the
browser (#99) will too. The
[registry guide](../host-registry.md) covers the HOST-1 operator tools it uses.

Inside a selected campaign every message is the existing
[protocol](../protocol.md) and [referee contract](referee_contract.md),
unchanged except for one additive snapshot field (section 4).

## 1. Decisions this contract follows

Owner decisions (D-24 and tracker #95, confirmed 2026-11-01):

- A player authenticates with a personal host token, chooses an accessible
  active campaign, and later chooses or creates a character. Campaigns and
  characters are distinct persistent entities that players pick. The
  selection belongs to the connection.
- One host serves several campaigns; each keeps its own state, history,
  busy state and referee session.
- Display names never select or merge identities.
- An existing participant is linked to a host player only by an explicit
  operator action; otherwise admission creates a stable participant without
  another bearer token.
- Localhost only. No browser login, LAN, TLS or remembered credentials in
  this slice.
- **Revocation while the referee is answering** (owner, 2026-11-01): an input
  accepted before a revocation keeps its job, which finishes and commits as
  usual, exactly as when a game token is revoked today. The revoked player
  gets nothing more; nothing new from them is accepted.

Engineering choices made here, within that scope:

- The lobby is a separate `serve` mode (`--registry`), never a fallback from
  `--database`, and its first message is `login`, not `join` (section 2).
- The host-player-to-participant link lives in the campaign's own database
  (schema version 6), created in the same transaction as a new participant,
  so a retried or concurrent admission cannot create a second participant
  (section 5).
- Selecting is only possible from the lobby: switching is `leave`, then
  `select`, so each boundary is one reply (section 3).
- Losing access to a campaign returns the connection to the lobby; losing the
  host credential closes it (section 6).
- Campaign runtimes are opened when first selected, not at startup, and a
  failed one is retried on the next selection (section 7).

## 2. Serving

```text
tesserae serve --registry host.sqlite3 --port PORT [--module-dir DIR ...]
               [--referee-dir DIR] [--referee-timeout SECONDS]
```

- `--registry` names an existing host registry (HOST-1); it is never created.
  It replaces `--database` and implies authentication. `--database` with
  `--registry` exits 2, as do `--authenticated` (redundant, refused so the
  mode is unambiguous), `--browser` (until HOST-4), `--tls-cert`, `--tls-key`
  and any `--host` other than `127.0.0.1`. A refusal exits before anything
  listens. A missing or invalid registry exits 1.
- `--module-dir` may be given more than once. Each package is loaded once at
  startup (as with a single game) and is available to every campaign; a
  campaign uses the bundled catalog or the loaded package whose module ID its
  binding names, and must match that binding exactly. Two packages with the
  same module ID exit 2. Clients never supply a path.
- `--referee-dir` is the referee folder for every referee-mode campaign. Each
  campaign has its own job lock, bridge socket, control socket and Codex
  session in it, keyed by game ID, exactly as for a single game, so
  `tesserae referee stop --database DB` reaches the lobby host's job for that
  campaign.
- Every handshake follows the existing request policy: an `Origin` header
  gets 403 (there is no page yet), and command-line clients send none.

The ready line names the mode and the registry, not a game:

```text
Tesserae lobby host listening on ws://127.0.0.1:8765/ (registry: /path/host.sqlite3,
host: 8c143463-…, log in with a host token; no encryption, this computer only).
Press Ctrl-C to stop.
```

## 3. Lobby messages

All lobby messages are protocol version 1 and follow its strict JSON rules
(no duplicate keys, no extra fields, `invalid_request` before
`unsupported_version`). The token rules are the same as for `join`: a token
that is empty, longer than 512 characters or contains whitespace is
`authentication_failed`, like any unknown token, and is never echoed.

Client to host:

| Message | Fields | When |
|---|---|---|
| `login` | `version`, `type`, `token` | First message only |
| `lobby` | `version`, `type` | Any time after login: the catalog and the current selection |
| `select` | `version`, `type`, `campaign_id` | In the lobby: enter that campaign |
| `leave` | `version`, `type` | In a campaign: return to the lobby |

Host to client:

```json
{"version": 1, "type": "lobby", "player_id": "u_9685d65c0b24294c",
 "player_name": "Alice",
 "campaigns": [{"campaign_id": "8f364986-8f31-4874-8217-4f97edea5997", "name": "Harbor"}],
 "selected": null}
```

- `lobby` is the reply to a successful `login`, to `lobby` and to `leave`.
  `campaigns` is the player's catalog: only active campaigns with an
  unrevoked grant, ordered by name, then ID, with no path or other record.
  `selected` is `null` or `{"campaign_id", "name"}`. An empty `campaigns` is
  a valid answer, not an error.
- `select` is answered by the campaign's `snapshot` (section 4) or an error.
- `campaign_id` is an opaque string of 1 to 128 characters without whitespace.

Errors, all `{"version": 1, "type": "error", "code", "message"}` with fixed
messages:

| Code | Cause | Then |
|---|---|---|
| `authentication_failed` | `login` with a malformed, unknown, revoked or rotated host token | Close 1008 |
| `login_required` | First message is not `login`, including a game-token `join` | Close 1008 |
| `join_timeout` | No `login` within 10 seconds | Close 1008 |
| `invalid_request`, `unsupported_version` | Malformed first message or a binary frame | Close 1008 |
| `already_logged_in` | A second `login` | Stays open |
| `no_campaign` | A campaign message (`sync`, `say`, `messages`, `referee_status`, a `command`) or `leave` while in the lobby | Stays open, in the lobby |
| `campaign_selected` | `select` while in a campaign | Stays open, in the campaign |
| `campaign_unavailable` | `select` of a campaign not in the caller's catalog, unknown, inactive, or that the host cannot open now (section 7); or any campaign request after access was lost (section 6) | Stays open, in the lobby |
| `session_revoked` | The host credential was revoked or rotated, noticed by any request | Close 1008 |
| `internal_error` | The host failed while checking | Stays open where it was |

A `campaign_unavailable` or `session_revoked` that ends a campaign or the
session carries no `request_id` or `revision`, like every session error; the
other errors carry the request's `request_id` where it had a valid one.

`campaign_unavailable` is the same code for a campaign the player cannot see
and one the host cannot open, so a player learns nothing about campaigns
outside their catalog. The message differs only for a campaign in their own
catalog ("That campaign cannot be opened right now."), which they already
know exists.

**Reply pairing** is unchanged: exactly one reply per client message, in
order; a closing session error is the last. Unsolicited messages are the
existing `revision`, `message` and `referee`, plus one new type:

```json
{"version": 1, "type": "left", "campaign_id": "8f364986-…", "reason": "campaign_unavailable"}
```

`left` means the host returned the connection to the lobby because access to
that campaign ended while no request was being answered (section 6). The
client sends `lobby` to see what remains. A `left` never takes the place of a
reply: a campaign request is read and answered under the connection's send
lock, so a `left` noticed meanwhile waits for that reply, and a request still
unanswered once the connection has left gets `campaign_unavailable` as its
one reply.

## 4. Inside a campaign

After a successful `select` the connection behaves exactly as a joined
connection of that campaign's game, as its participant (section 5): `sync`,
`say`, `messages`, `referee_status`, commands where the campaign is not in
referee mode, notifications, busy, privacy, exact retries and recovery all
follow the existing protocol. A `join` is `already_logged_in`.

The snapshot gains one field naming the campaign; `game_id` equals
`campaign_id`:

```json
{"version": 1, "type": "snapshot", "game_id": "8f364986-…",
 "participant_id": "p_7d1e…", "revision": 3, "participant_name": "Alice",
 "campaign": {"campaign_id": "8f364986-…", "name": "Harbor"}, "...": "..."}
```

Single-game snapshots never carry `campaign`.

**Boundaries.** All messages to one connection pass one lock, and each
campaign message is tied to the selection it was produced for:

- After the `lobby` reply to `leave` (or a `left`), the connection receives
  nothing produced by the previous campaign: no `message`, `referee`,
  `revision`, replayed reply or snapshot. A job still running there finishes;
  its reply goes to that campaign's other connections and is in its history.
- Between `select`'s `snapshot` and the next `leave`, everything campaign-
  related comes from the selected campaign.
- Selecting the same campaign again is a fresh admission: a new snapshot and
  no carried-over notification state. History is reloaded with `messages`.

**Request identity is per campaign.** Request IDs, exact retries and
`your_last_input` belong to the campaign's participant. A client keeps any
unconfirmed input with the campaign it was sent to and never resends it after
selecting another campaign; it may resend it unchanged after selecting the
same campaign again, which replays the recorded outcome. The host cannot
recognise a misdirected retry (another campaign is another namespace), so this
is a client rule, and HOST-3 and HOST-4 test it.

## 5. Admission and participant links

A campaign database gains one table at schema version 6, applied when the
store is first opened by this Tesserae version, like earlier upgrades
(registration still only reads; see the [registry guide](../host-registry.md)):

```sql
CREATE TABLE host_links (
    id INTEGER PRIMARY KEY,
    host_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    participant_id TEXT NOT NULL REFERENCES participants (participant_id),
    linked_at TEXT NOT NULL,
    unlinked_at TEXT
);
CREATE UNIQUE INDEX one_link_per_player ON host_links (host_id, player_id)
    WHERE unlinked_at IS NULL;
CREATE UNIQUE INDEX one_link_per_participant ON host_links (participant_id)
    WHERE unlinked_at IS NULL;
```

- **Admission.** On `select`, inside the access guard (section 6), one write
  transaction finds the active link for (host ID, player ID). If there is
  none, it creates a participant, named with the player's registry name, and
  its link together. Retries and concurrent selections by the same player
  find that link, so there is exactly one participant per player per
  campaign. The participant has no game token.
- **Authority.** A linked session's context names its link, as a token's
  names its credential. Every check that today requires an active credential
  (snapshots, syncs, `say`, commands, `messages`, `referee_status`, delivery)
  instead requires that exact link to be active, in the same statement or
  transaction as before. A link and a credential never stand in for each
  other.
- **Linking an existing participant** is an operator command on the host
  computer, refused if the player already has an active link in that campaign
  or the participant is already linked:

  ```text
  tesserae host campaign link --registry R CAMPAIGN_ID PLAYER_ID PARTICIPANT_ID
  tesserae host campaign unlink --registry R CAMPAIGN_ID PLAYER_ID
  tesserae host campaign links --registry R CAMPAIGN_ID
  ```

  These open the registered campaign file (checking its game ID), so they also
  apply the schema upgrade, but they change no game state, history or
  credentials. Linking does not grant access, and a grant does not link.
- **Reversal.** `unlink` ends a link; the participant, its history and any
  game token are untouched, and that participant can be linked again. To move
  a player from an automatically created participant to an existing one:
  revoke their grant (or deactivate the campaign), `unlink`, `link` the
  intended participant, then grant again. If the player selects the campaign
  between `unlink` and `link`, admission creates another participant, which
  stays and is harmless; unlink it first. `links` lists every link, current
  and ended, with player and participant IDs and times.
- A participant linked to a host player can still have a game token for
  single-game mode; the two hosts must not serve the same file at once (the
  referee's per-game host lock already refuses the second in referee mode).

## 6. Authorization and ordering

Every protected operation of a lobby connection is checked twice: the host
credential, grant and activation in the registry, and the participant link in
the campaign. The registry check is HOST-1's short read guard
(`authorize_campaign`): it is taken first, held across the campaign's read or
write transaction, and released before any network send, model call or wait.
Its read lock means a registry revocation (token, grant or deactivation)
cannot commit while a guarded campaign transaction is in progress, so each
operation is ordered wholly before or wholly after the revocation:

| Operation | Checked in one guarded campaign transaction |
|---|---|
| `select` | Grant, activation, credential; then the link is found or created |
| `say` | As above; then acceptance, replay or refusal |
| A `command` (non-referee campaigns) | As above; then replay, revision check and commit |
| `sync`, `messages`, `referee_status` | As above; then the read |
| Each `revision` and `message` delivery | As above, rechecked per recipient before sending |

The `referee` busy/idle notice carries nothing about the game and is sent
without a recheck, as on a single-game host; a connection that has left a
campaign never receives one from it (section 4).

This is an ordering, not an atomic transaction across the two databases; no
claim of cross-database atomicity is made. A registry writer waits at most
SQLite's 5-second busy timeout and fails without changing access, so guarded
operations must stay short. Lock order is always registry, then campaign;
nothing holding a campaign lock waits for the registry.

**What a lost check does:**

- The host credential is revoked or rotated: the next request gets
  `session_revoked` and the connection closes with 1008; a delivery that
  notices first closes it with 1008 and reason `session_revoked`, sending
  nothing.
- The grant is revoked, the campaign deactivated, the link ended, or the
  campaign file no longer holds that game: the request gets
  `campaign_unavailable` and the connection is back in the lobby; a delivery
  that notices first sends `left` and nothing from that campaign. Login and
  the other campaigns are unaffected.
- Neither undoes anything committed.

**While the referee is answering** (owner decision, section 1): the job of an
input accepted before the revocation runs to completion. The bridge acts for
that job's participant without a registry check, exactly as for a revoked
game token, and its reply is delivered to the recipients who still have
access. The revoked player receives none of it.

A delivery whose check cannot complete (the registry or campaign busy past
the retries) closes the connection with 1011, as today: the client reconnects
and logs in again. If the check failed because the campaign file is gone or
holds another game, the connection gets `left` instead.

## 7. Campaign runtimes

- A campaign's runtime starts on its first successful `select` after the
  registry check, and then serves every connection that selects it until the
  host stops. Starting it opens the registered path as an existing game
  (never creating one), checks that its game ID is the registered campaign ID,
  loads its module exactly (bundled or from `--module-dir`) and, in referee
  mode, opens its referee in the referee folder and recovers any earlier job.
- If any of that fails (a missing, moved or replaced file, an unknown or
  mismatched module, a missing referee folder, another host already serving
  that game), the `select` gets `campaign_unavailable`, the host logs the
  reason (never a token), nothing is created, and the next `select` tries
  again.
- Each campaign has its own busy state, job, outbox, referee session and
  sessions. A job in one never makes another busy.
- Every store call reopens the registered path and checks the game ID, as the
  single-game host does, so a file replaced while serving is refused, not
  served.
- At shutdown every campaign closes admission, ends its running job
  `interrupted`, and closes its referee, as a single-game host does.

## 8. Compatibility

| Token | Where it works |
|---|---|
| Game token (`tesserae participant add`) | `serve --database`, `join`; never at a lobby host |
| Host token (`tesserae host player add`) | `serve --registry`, `login`; never at a single-game host |

`serve --database`, development mode, browser mode, TLS and the existing
clients are unchanged. A game opened by this version is upgraded to schema
version 6, which older Tesserae versions refuse to open, as with earlier
upgrades. The CLI's lobby mode (#98, `tesserae connect --lobby`) and the
browser's login and picker (#99) are separate deliveries; HOST-2's own tests
drive the lobby over raw WebSocket messages.

## 9. Tests

- Store: the version 6 upgrade; admission creating one participant under
  retries and concurrency; link and unlink; every authority check with a link
  context, and an ended link refused everywhere a revoked credential is.
- Operator commands: link, unlink and links, with their refusals.
- Lobby host, with a scripted referee for each campaign: login, catalog,
  select and leave; every error above; two players in two campaigns at once,
  each with independent busy and session; one player in both campaigns over
  two connections; no message from a campaign after leaving it, even from a
  job finishing at that moment; revocation of the token, the grant, the
  campaign and the link while connected and while busy (the job finishes, the
  revoked player gets nothing); missing, moved and replaced files and a bad
  module refused without creating a game; and single-game mode unchanged.
- `serve --registry` refusals and a real process start and stop.
