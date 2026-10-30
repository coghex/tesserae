# Tesserae

Tesserae is a local-first tabletop roleplaying application built around an AI
referee, executable rules modules, and several clients connected to one game host.
The first game client will be a thin streaming CLI; the browser client follows.
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
the development host. Install this checkout into a virtual environment:

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
Recorded game actions will be introduced with the state and command layer.
There are no built-in critical, fumble, success, or damage rules.

No web UI, model integration, or complete game module has been implemented yet.
Recorded rolls go through the development host below. The next step is real
participant joining and a streaming CLI client.

## Working together

This is a casual hobby project with manual review and final merge approval by the
owner. Use ordinary Git branches and PRs, keeping related documentation with the
code. Do not use the owner's custom Kanban plugins, review coordinators, automatic
approval/merge gates, or `docs-push` workflow here.

The default branch is `master`.

Agents can draft and file issues without individual approval pauses, including
batches. They review changes in conversation; the owner decides whether to merge.
Passing tests alone do not authorize a merge. See [AGENTS.md](AGENTS.md).

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

Ruff is a pinned development dependency, not a game/runtime dependency. Start with
this setup; additional testing frameworks, type-checking gates, and tooling can be
added when a concrete need appears.

## Development host

`tesserae serve` runs one game in the foreground until Ctrl-C. It listens only on
127.0.0.1 and has no login: **every connection acts as one fixed development
participant, `local-development`**. This is for local development only. Playing
with friends needs the later authenticated join flow and per-player views. Keep
game databases outside this repository.

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

## License

The public project uses the [MIT license](LICENSE). Private content is maintained
separately and is not included in this repository.
