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
CLI client. Transport libraries are proposed in the design; none are installed yet.

## Documentation delivery

While this repository has no origin, the owner has authorized local documentation
commits and fast-forward updates to `main`. Keep the primary checkout clean. Once
an origin exists, return to the owner's `docs-push` workflow. This exception does
not authorize publication or change the code review/delivery policy.
