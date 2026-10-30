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
It requires Python 3.11 or newer and no third-party packages. From this checkout:

```sh
python3 -m tesserae --help
python3 -m tesserae roll '2d6+3'
python3 -m tesserae roll '2d6+3' --json
python3 -m unittest discover -s tests
```

This standalone utility does not load a game, update a campaign, or persist rolls.
Recorded game actions will be introduced with the state and command layer.
There are no built-in critical, fumble, success, or damage rules.

No web UI, model integration, or complete game module has been implemented yet.
The dice utility is not yet a network client. The agreed next step is command
handling on a minimal authoritative server, SQLite persistence, and a streaming
CLI client. `websockets` is selected for transport but is not installed yet.

## Working together

This is a casual hobby project with manual review and final merge approval by the
owner. Use ordinary Git branches and PRs, keeping related documentation with the
code. Do not use the owner's custom Kanban plugins, review coordinators, automatic
approval/merge gates, or `docs-push` workflow here.

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

## Server process plan

The first server will run in the foreground in its own terminal, with visible
startup/error logs and Ctrl-C shutdown. Clients connect to that running process;
they do not start or daemonize a server implicitly. The first server slice is
loopback-only; the player join/identity flow precedes LAN access for friends.

## License

The public project uses the [MIT license](LICENSE). Private content is maintained
separately and is not included in this repository.
