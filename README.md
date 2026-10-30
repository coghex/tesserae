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
It requires Python 3.11 or newer and no third-party packages. Install this checkout
into a virtual environment:

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
The dice utility is not yet a network client. The agreed next step is command
handling on a minimal authoritative server, SQLite persistence, and a streaming
CLI client. Transport libraries are proposed in the design; none are installed yet.

## Documentation delivery

The repository now tracks `coghex/tesserae` on GitHub. Publish documentation with
the owner's `docs-push` workflow; the local-only documentation exception has ended.
New implementation work follows issue → isolated worktree/branch → pull request.
Keep the primary checkout clean.

For this hobby project, the owner has authorized agents to draft and file issues
without individual approval pauses, including batches. Verification and clear
scope still apply; code review and CI requirements remain in force. See
[AGENTS.md](AGENTS.md) for the project-specific working agreements.

The first server scope is [tracker #1](https://github.com/coghex/tesserae/issues/1).
Its four children and implementation order are recorded in the
[server delivery design](docs/designs/foreground_server_design.md).

## Server process plan

The first server will run in the foreground in its own terminal, with visible
startup/error logs and Ctrl-C shutdown. Clients connect to that running process;
they do not start or daemonize a server implicitly. The first server slice is
loopback-only; the player join/identity flow precedes LAN access for friends.

## License

The public project uses the [MIT license](LICENSE). Private content is maintained
separately and is not included in this repository.
