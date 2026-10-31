# Tesserae

Tesserae is a local-first tabletop roleplaying application built around an AI
referee, executable rules modules, and several clients connected to one game host.
The first game client is a thin streaming CLI; the browser client follows in a
later milestone, beyond Milestone 1.
Friends should be able to join the same locally hosted game from other computers.

Different games select different modules. The application does not assume that
every game has dice, inventory, combat, a map, or the same character attributes.

The public application and an original demonstration game will be kept separate
from private rulebooks, game-specific adaptations, and campaign records. Private
content will load from an explicitly configured directory outside this repository.

This project is at the bootstrap stage. See the [design](docs/tesserae_design.md)
for agreed direction, proposals, and open questions, and the
[first milestone](docs/milestone-1.md) for implementation order and progress.

## First executable slice

The initial command-line interface exercises a generic dice expression evaluator.
It requires Python 3.11 or newer; the only runtime dependency is `websockets`, for
the host and network client. Install this checkout into a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
```

Then, from any directory:

```sh
python3 -m tesserae --help
python3 -m tesserae roll '2d6+3'
python3 -m tesserae roll '2d6+3' --json
```

The install also provides a `tesserae` command equivalent to `python3 -m tesserae`.
Run the tests from the checkout; GitHub Actions runs the same command on every
pull request:

```sh
python3 -m unittest discover -s tests -v
```

This standalone utility does not load a game, update a campaign, or persist rolls.
Use the foreground host below for recorded game actions.
There are no built-in critical, fumble, success, or damage rules.

No web UI or model integration has been implemented yet. The only game module
is the small original [Relay demo](docs/relay-demo.md).
Authenticated participant joining and the streaming CLI support localhost and
explicit encrypted LAN connections. The [localhost acceptance](docs/acceptance/localhost-multiplayer.md)
covers tracker #12. The [encrypted-network acceptance](docs/acceptance/encrypted-lan.md)
covers tracker #25, including an independently installed Ubuntu VM connecting to
the Mac host. Physical-device Wi-Fi testing is deferred. Follow the
[LAN runbook](docs/lan-runbook.md) to connect another client.

[M1-4 / tracker #37](https://github.com/coghex/tesserae/issues/37) added
minimal bundled modules and an original relay-repair demo: the
[module contract](docs/modules.md), `dice` adapter, persisted module state
(`tesserae game create`), [Relay rules](docs/relay.md), and hosting module games
with per-player views. Two players can follow the
[Relay demo runbook](docs/relay-demo.md); the
[demo acceptance](docs/acceptance/relay-demo.md) records live runs, one with
a player on the Ubuntu VM. Next is M1-5, a private module loaded from outside
the repository.

## Working together

This is a casual hobby project with manual review and standing owner authorization
to merge ready PRs. Use ordinary Git branches and PRs, keeping related documentation
with the code. Do not use the owner's custom Kanban plugins, review coordinators, automatic
approval/merge gates, or `docs-push` workflow here.

The default branch is `master`.

Agents can draft and file issues without individual approval pauses, including
batches. When asked to check a PR, agents review it and merge if it is ready and
the agreed checks pass at its current head, without another approval prompt.
This authorization applies only to Tesserae. Explicit review-only or hold requests
take precedence, and passing tests alone do not replace review. See
[AGENTS.md](AGENTS.md).

The first server scope is [tracker #1](https://github.com/coghex/tesserae/issues/1).
Its four children and implementation order are recorded in the
[server delivery design](docs/designs/foreground_server_design.md).

## Development checks

Install development tools in the same virtual environment:

```sh
python3 -m pip install -e '.[dev]'
```

Run the same small set of checks locally and in GitHub Actions:

```sh
python3 -m ruff check .
python3 -m ruff format --check .
python3 -m unittest discover -s tests -v
```

[Ruff linting](https://docs.astral.sh/ruff/linter/) checks basic Python errors and
import ordering. Its [formatter](https://docs.astral.sh/ruff/formatter/) handles
layout consistently; apply formatting with `python3 -m ruff format .`. Tests check
behavior. CI repeats these checks on GitHub and checks the installed CLI from
outside the checkout.

The TLS tests also need an `openssl` executable on PATH, either OpenSSL 3.x or
LibreSSL 3.3.6 (macOS's `/usr/bin/openssl` is fine). They make throwaway
certificates with it and fail with a setup message if it is missing. See
[docs/tls.md](docs/tls.md).

Ruff is a pinned development dependency, not a game/runtime dependency. Start with
this setup; additional testing frameworks, type-checking gates, and tooling can be
added when a concrete need appears.

## Development host

`tesserae serve` runs one game in the foreground until Ctrl-C. It listens only on
127.0.0.1 and has no login: **every connection acts as one fixed development
participant, `local-development`**. This is for local development only. Playing
with friends on another computer uses the [encrypted hosting](#encrypted-hosting)
procedure. Authenticated participants can also use the localhost procedure
below. Keep game databases outside this repository.

Start it in its own terminal (`--port 0` picks a free port):

```sh
python3 -m tesserae serve --database ~/tesserae-dev.sqlite3 --port 8790
```

It prints the address to connect to, for example
`Tesserae development host listening on ws://127.0.0.1:8790/ (database: ...)`,
and logs connections to stderr. In a second terminal, connect with the
`websockets` library's interactive client:

```sh
python3 -m websockets ws://127.0.0.1:8790/
```

The host greets you with the game's current revision:

```
< {"version": 1, "type": "welcome", "revision": 0}
```

Send a roll by pasting one line of JSON at the `>` prompt. `expected_revision`
is the revision from the welcome, and `request_id` is any ID you choose:

```json
{"version": 1, "type": "command", "request_id": "r1", "expected_revision": 0, "command": "roll", "args": {"expression": "2d6+3"}}
```

The reply is the committed result, and the game is now at revision 1. Sending
exactly the same line again, even after reconnecting or restarting the host,
returns the original result without rolling again. A new roll needs a new
`request_id` and the current `expected_revision`. A stale revision is rejected
with `stale_revision`, and reusing an ID for a different roll is rejected with
`request_conflict`. See the [protocol](docs/protocol.md) and the
[game store](docs/game_store.md) for the full contract.

Press Ctrl-C in the host's terminal to stop it. The database keeps every
committed roll.

## Participants and join tokens

Players join an authenticated host with a personal token. Manage participants
on the host machine with local operator commands, against the game's database:

```sh
python3 -m tesserae participant add --database ~/tesserae-dev.sqlite3 \
    --name Alice --token-file ~/alice.token
python3 -m tesserae participant list --database ~/tesserae-dev.sqlite3
```

`add` prints Alice's participant ID, such as `p_7bd3a7ab7d45f27b`, and writes
her token to `~/alice.token` with user-only permissions. It refuses to replace
an existing file. Give the file only to Alice, and keep token files out of this
repository. The token is never printed. `list` shows each participant's ID,
status, creation time, and name, but never a token.

```sh
python3 -m tesserae participant rotate --database ~/tesserae-dev.sqlite3 \
    p_7bd3a7ab7d45f27b --token-file ~/alice-new.token
python3 -m tesserae participant revoke --database ~/tesserae-dev.sqlite3 \
    p_7bd3a7ab7d45f27b
```

`rotate` issues a new token and stops the old one working. Use it if a token is
lost, because tokens cannot be recovered. `revoke` stops the token without a
replacement. Either way the participant keeps their ID and history. Names are
labels, not identities. These commands never change the game's revision.

## Two players on one computer

This is the localhost part of Milestone 1's multiplayer step: an authenticated
host and the thin `tesserae connect` client. Playing with a friend on another
computer uses the [LAN runbook](docs/lan-runbook.md).

**1. Provision two participants** (see above), keeping token files outside
this repository:

```sh
python3 -m tesserae participant add --database ~/tesserae-dev.sqlite3 --name Alice --token-file ~/alice.token
python3 -m tesserae participant add --database ~/tesserae-dev.sqlite3 --name Bob --token-file ~/bob.token
```

**2. Start the authenticated host** in its own terminal. It listens only on
127.0.0.1 and runs until Ctrl-C:

```sh
python3 -m tesserae serve --database ~/tesserae-dev.sqlite3 --port 8790 --authenticated
```

**3. Connect each player** in their own terminal:

```sh
python3 -m tesserae connect ws://127.0.0.1:8790/ --token-file ~/alice.token
python3 -m tesserae connect ws://127.0.0.1:8790/ --token-file ~/bob.token
```

The client joins with the token and prints the host's reply, a snapshot of
the game, your participant ID, and the current revision:

```
{"version": 1, "type": "snapshot", "game_id": "...", "participant_id": "p_7bd3a7ab7d45f27b", "revision": 0}
```

Add `--game-id ID` to refuse to send anything unless the host serves that game.

**4. Play by typing JSON lines.** Every nonblank line you type is sent as one
message, and every message from the host is printed as one JSON line, even
while you are idle. Alice rolls:

```json
{"version": 1, "type": "command", "request_id": "r1", "expected_revision": 0, "command": "roll", "args": {"expression": "1d20"}}
```

Only Alice sees her `result`. Both players see
`{"version": 1, "type": "revision", "game_id": "...", "revision": 1}`, which
never says who rolled or what. To check the current state at any time, send
`{"version": 1, "type": "sync"}`.

- **Stale requests.** A command whose `expected_revision` is behind gets
  `stale_revision` with the current revision. Nothing is retried for you;
  send a new command with a new `request_id` and the current revision if you
  still want it.
- **Request IDs are per player.** Alice's and Bob's `r1` are separate.
- **Lost replies and restarts.** If the client or host stops before you saw a
  reply, run `tesserae connect` again and resend the same line unchanged. A
  command that committed returns its original result without rolling again.
- **Piping.** Stdout is only JSON lines and diagnostics go to stderr, so you
  can pipe a file of commands in. Lines are sent as they are read, without
  waiting for each reply:

  ```sh
  python3 -m tesserae connect ws://127.0.0.1:8790/ --token-file ~/alice.token < moves.jsonl > replies.jsonl
  ```

**Ending.** When stdin ends (Ctrl-D, or the end of a piped file), the client
waits up to 10 seconds (`--drain-timeout`) for replies to everything it sent,
then exits. It does not wait for further notifications. Closing the client
never stops the host. Exit status:

| Status | Meaning |
|---|---|
| 0 | Input ended and every request was answered |
| 1 | It could not start (token file, connection, join, or `--game-id`), or the host ended the session |
| 3 | Some requests got no reply. Stderr names their request IDs; resend those lines unchanged |
| 130 | Ctrl-C |

A wrong or revoked token gets `authentication_failed` and exit status 1,
without anything about the game being revealed. Revoking or rotating a token
while its player is connected ends their session. A client that stops reading
is disconnected (code 1013); reconnecting catches it up. Notifications cover
only rolls made through this host process. The token is read only from the
file, never from the command line or the URI, and is never printed.

`python3 -m websockets ws://127.0.0.1:8790/` still works for probing an
authenticated host by hand. Send `{"version": 1, "type": "join", "token":
"..."}` first, within 10 seconds. Pasting a token shows it on screen, so only
do that on your own machine. Development mode, without `--authenticated`, is
unchanged.

## Encrypted hosting

An authenticated host can also use TLS, which is required to listen on
anything but this computer. Make a certificate and key with the recipe in
[docs/tls.md](docs/tls.md), keeping both outside this repository, then:

```sh
python3 -m tesserae serve --database ~/tesserae-dev.sqlite3 --port 8790 \
    --authenticated --tls-cert ~/tesserae-host/host.crt --tls-key ~/tesserae-host/host.key \
    --host 192.168.1.50
```

`--host` takes one IPv4 address of this computer; leave it out to stay on
127.0.0.1. The host then listens on `wss://192.168.1.50:8790/`. Plaintext or
unauthenticated settings for any address but loopback are refused before
anything starts.

Give each player the public `host.crt` and their own token file. They connect
with `wss://`, telling the client to trust that certificate:

```sh
python3 -m tesserae connect wss://192.168.1.50:8790/ \
    --token-file ~/alice.token --ca-file ~/host.crt
```

The address or name in the URI must be one the certificate lists in its
`subjectAltName`. The client checks the certificate and the name before
sending anything. If either fails, it stops with "cannot trust ..." and
exit status 1, and the host never sees the token. Without `--ca-file`,
`wss://` trusts only the system's certificate authorities, which do not know
a self-signed certificate. Plaintext `ws://` is refused for anything but this
computer (a loopback address or `localhost`), before connecting.

For the whole procedure across two computers, including installing Tesserae on
the friend's computer, which files go where, firewall notes, and a checklist,
follow the [LAN runbook](docs/lan-runbook.md).

## License

The public project uses the [MIT license](LICENSE). Private content is maintained
separately and is not included in this repository.
