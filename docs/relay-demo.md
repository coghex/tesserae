# Relay demo runbook

Two players repair a storm-damaged relay through the [Relay module](relay.md),
using only the commands Tesserae ships: `game create`, `participant add`,
`serve --authenticated` and `connect`. Each player types one JSON message per
line and reads one JSON message per line back. The host keeps all state and
applies all rules; the client only forwards lines.

This runbook uses one computer, with each player in their own terminal. For a
friend on another computer, add TLS and `--host` as in the
[LAN runbook](lan-runbook.md); the player messages are the same. The
[acceptance record](acceptance/relay-demo.md) shows a run of these steps.

Paths, IDs and tokens below are placeholders. Keep the game directory outside
the repository: it will hold the database and the join tokens.

## 1. Install

Python 3.11 or newer. From a checkout:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .
```

## 2. Create the game, then add players

Create a fresh Relay game first. `game create` refuses a path that exists, and
`serve` or `participant add` on a missing database would create a plain dice
game instead.

```sh
mkdir -m 700 ~/tesserae-relay
python3 -m tesserae game create --database ~/tesserae-relay/relay.sqlite3 --module relay
python3 -m tesserae participant add --database ~/tesserae-relay/relay.sqlite3 \
  --name Alice --token-file ~/tesserae-relay/alice.token
python3 -m tesserae participant add --database ~/tesserae-relay/relay.sqlite3 \
  --name Bob --token-file ~/tesserae-relay/bob.token
```

Each token is written only to its file, readable only by you. Give each player
their own file privately. Never paste a token into a chat, a command line or a
URL. The display name is only a label.

## 3. Start the host

```sh
python3 -m tesserae serve --authenticated --database ~/tesserae-relay/relay.sqlite3 --port 8790
```

The first line says `listening on ws://127.0.0.1:8790/` and `module: relay 1`.
Leave it running in the foreground. Relay needs `--authenticated`, even on this
computer: without it, `serve` exits 2 and starts nothing. If the game's
module or its dice provider is missing or a different version, `serve` exits
1 with `cannot open database` and starts nothing. It never falls back to dice
or resets the game.

## 4. Connect each player

In one terminal per player:

```sh
python3 -m tesserae connect ws://127.0.0.1:8790/ --token-file ~/tesserae-relay/alice.token
```

The first line printed is your snapshot: the game, your own participant ID,
the current `revision`, the module and your `view`.

```json
{"version": 1, "type": "snapshot", "game_id": "GAME_ID", "participant_id": "p_alice", "revision": 0,
 "module": {"id": "relay", "version": "1", "state_version": 1},
 "view": {"scene": {"id": "relay_station", "title": "The storm-damaged relay", "description": "...", "restored": false},
          "rules": {"relay.enlist": "...", "relay.repair": "..."},
          "character": null, "commands": ["relay.enlist"]}}
```

`character` is your worker, or `null` before you enlist. `commands` lists
what you can do right now. You never see the other player's worker.

## 5. Play

Every command is one line with a `request_id` you choose and the
`expected_revision` you last saw:

- `request_id`: 1 to 128 characters with no whitespace. Use a new one for
  each new action. It is yours alone; Bob's `r1` is not Alice's `r1`.
- `expected_revision`: the newest revision from your last snapshot,
  notification or result. If the game has moved on, the command is refused
  with `stale_revision` and nothing happens.

Enlist your worker once:

```
> {"version": 1, "type": "command", "request_id": "a1", "expected_revision": 0, "command": "relay.enlist", "args": {"name": "Wren"}}
< {"version": 1, "type": "result", "request_id": "a1", "revision": 1, "result": {"worker": {"name": "Wren", "repair": 1, "effort": 3}, "relay": {"restored": false}}}
```

The other player is told only that the revision changed:

```
< {"version": 1, "type": "revision", "game_id": "GAME_ID", "revision": 1}
```

To see what changed, sync. It is read-only:

```
> {"version": 1, "type": "sync"}
< {"version": 1, "type": "snapshot", ..., "revision": 1, ..., "view": {..., "character": null, "commands": ["relay.enlist"]}}
```

Attempt a repair. It rolls `1d6`, adds your repair, and succeeds on 5 or more.
It spends one effort either way:

```
> {"version": 1, "type": "command", "request_id": "a2", "expected_revision": 2, "command": "relay.repair", "args": {}}
< {"version": 1, "type": "result", "request_id": "a2", "revision": 3, "result": {"check": {"roll": {"schema_version": 1, "expression": "1d6", "terms": [{"kind": "dice", "count": 1, "sides": 6, "rolls": [5], "sign": 1, "subtotal": 5}], "total": 5}, "repair": 1, "total": 6, "target": 5, "success": true}, "effort": {"before": 3, "after": 2}, "relay": {"restored": true}}}
```

The dice are real. A failed check is a normal outcome: the effort is spent and
the relay stays offline. If every worker runs out of effort first, that is
how this game ends.

| You send | Reply |
|---|---|
| `relay.enlist` with a name of 1 to 40 characters | `result` with your worker |
| `relay.repair` with `{}`, effort left, relay offline | `result` with the check, success or failure |
| A second enlist, a repair with no worker or no effort, or a repair once the relay is restored | `action_rejected` with a reason; nothing changed, no dice rolled |
| Any extra argument (an owner, a difficulty, an expression) or a bad name | `invalid_request` |
| `roll`, or any other command | `unsupported_command` |
| An old `expected_revision` | `stale_revision` with the current revision |

## 6. Retry and reopen

If your client loses its connection, exits 3, or you are unsure whether a
command went through, run `connect` again and resend the same line unchanged.
That works after the host restarts too.

- If it had committed, you get the exact original result. Nothing is rolled
  or spent again, even if the same action would now be refused.
- If it never committed and the revision has moved on, you get
  `stale_revision`. Deciding whether to try again, with a new `request_id`
  and the current revision, is up to you.

The client never retries by itself. To restart the host, press Ctrl-C and run
the same `serve` command; the game continues where it was.

## 7. Clean up

Stop the host with Ctrl-C. When the game is over, revoke the tokens and remove
the game directory:

```sh
python3 -m tesserae participant list --database ~/tesserae-relay/relay.sqlite3
python3 -m tesserae participant revoke --database ~/tesserae-relay/relay.sqlite3 PARTICIPANT_ID
rm -r ~/tesserae-relay
```

Ask the other player to delete their token file too.
