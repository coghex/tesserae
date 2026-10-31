# Game modules

A game module is trusted, bundled Python that defines a game's commands, its
JSON state, and what each participant may see of it. The host keeps transport,
identity, persistence, replay and revisions. This is the contract from
[#38](https://github.com/coghex/tesserae/issues/38) (DEMO-1 in the
[M1-4 design](designs/demo_modules_design.md)). The implementation is
`tesserae/modules.py`, and the bundled catalog is `tesserae/builtin.py`.

## Status

Implemented: the contract, the bundled catalog, the `builtin.dice` provider,
the `dice` compatibility module, and the original [Relay](relay.md) module
(#40). The protocol parser, the game store and
the host all dispatch `roll` through it, with unchanged behavior.

The [game store](game_store.md#modules-and-state) persists each game's
binding and module state, and runs commands with `action_rejected` handling.
`tesserae game create` makes a game bound to a bundled module (#39).

`tesserae.external.load_module_dir` loads one trusted module package from an
explicit directory ([External packages](#external-packages), #51). `game
create` and `serve` do not use it yet (#52).

`serve` loads the game's bound runtime once at startup, refusing an unknown or
mismatched binding before it listens, and sends each joined participant the
module's view of the game in join and sync snapshots (#41,
[protocol.md](protocol.md#messages)). A `dice` game's snapshots stay metadata
only, since `dice` has no view.

## Catalog and selection

`Catalog(modules, providers)` is an explicit list. Nothing is discovered or
scanned, nothing comes from a network message, and code is imported from a
path only for an [external package](#external-packages) the operator names.
Duplicate module IDs, provider IDs, or command names within a module raise
`ModuleError` when the catalog is built. `catalog.extended(modules, providers)`
returns a new catalog with more added, under the same rule: an existing ID is a
duplicate, never a replacement.

`catalog.resolve(module_id, version=None, providers=None)` returns a
`Runtime`, or raises `ModuleError` before anything runs. By default it uses
the module's own version and provider choices. A store passes the binding a
game was created with.

- An unknown module, a different module version, or a different host API
  version (`HOST_API_VERSION`, currently 1) is refused.
- Every operation in the module's `requires` needs exactly one
  `ProviderChoice(operation, provider, version)`. A missing choice is refused
  even when only one provider could supply it. So are an unknown provider, a
  provider at another version, a provider of a different operation, two
  choices for one operation, and a choice for an operation the module does not
  require.
- Registration order never decides anything. Several providers may implement
  the same operation; the choice picks one.

Tags are descriptive only. A module tagged `dice` with no `requires` gets no
dice operation and no `roll` command.

## Module definition

`Module` fields:

| Field | Meaning |
|---|---|
| `id`, `version` | Stable ID and exact implementation version (a string) |
| `state_version` | Schema version of the module's JSON state |
| `api_version` | Host API version it was written for; defaults to 1 |
| `commands` | `Command`s; only these are accepted for its games |
| `initial_state()` | A fresh JSON object for a new game |
| `view(state, participant_id)` | What that participant may see: a JSON object, or None for a metadata-only snapshot |
| `requires`, `providers` | Required operation IDs and the explicit choice for each |
| `tags` | Descriptive strings |
| `requires_authentication` | Whether its games need authenticated participants |

A module definition is immutable and holds no game state, connection, or
random source. One definition serves every game.

### Commands

`Command(name, validate, execute)`:

- **`validate(args)`** is pure. It checks the arguments alone, never reads
  state or draws, and raises `ArgumentError(message, code="invalid_request")`.
  `exact_fields(args, names, subject)` checks the field names without echoing
  unexpected ones. A message must never echo client input.
- **`execute(action)`** is synchronous. `action` is an `Action`:
  - `state` is a private copy of the module state;
  - `participant_id` is the host's trusted identity, never taken from the
    message;
  - `args` holds the validated arguments;
  - `operations` holds the selected operations, bound to this execution's
    random source.

  It returns `Outcome(state, result)`: the replacement state, and the result
  object only the caller receives. Check every state-dependent rule, such as
  ownership, resources, or the scene, before the first draw. If the action is
  not allowed now, raise `ActionRejected(message)` with a player-safe message.

The runtime copies state, results, and views through JSON, so no module
keeps a reference to stored data. A non-object or non-JSON value raises
`TypeError`. The store:
- reports `ActionRejected` as the `action_rejected` error, committing nothing
  and using no revision or request ID;
- treats any other exception as a failure, rolling back everything (the host
  reports `internal_error`).

### Argument errors and the protocol

The parser runs the selected runtime's validator (see
[protocol.md](protocol.md#errors)):
- An `invalid_request` error from it counts as structural, and is reported
  before the version check.
- Any other code is reported after the version and command checks. `dice`'s
  `invalid_expression` is one.
- An unregistered command is `unsupported_command`.

`parse_request(text, runtime)` and `parse_client_message(text, runtime)`
default to the `dice` runtime, so existing callers keep their behavior. The
host and store parse with their game's runtime, so the host never refuses a
command the store would accept.

### Providers

`Provider(id, version, operation, implementation)` supplies one operation.
`implementation(randint, *args)` receives the execution's random source first:
a `randint(a, b)`, or None for the system source. It must keep nothing between
calls. Randomness is injected per execution, so tests can fix draws.

## Bundled catalog

`builtin.dice` version 1 supplies `dice.roll.v1`: `roll(expression)` rolls an
already-validated expression using the existing dice arithmetic. It returns
the result object from [protocol.md](protocol.md#messages): `schema_version`,
`expression`, `terms` and `total`.

`dice` version 1 (state version 1, state `{}`) is the compatibility module. It
requires `dice.roll.v1` from `builtin.dice` 1, and has one command, `roll`:
- exactly one field, `expression`;
- a string, checked against the existing dice rules and limits;
- its result is the dice result, unchanged.

Its view is None, so its snapshots stay metadata-only. `tesserae roll` and
`roll_expression` remain plain dice utilities outside any module.

`relay` version 1 is the original demo module, with two commands:
`relay.enlist` and `relay.repair`. It requires `dice.roll.v1` from
`builtin.dice` 1, and authenticated participants. See [relay.md](relay.md).

## External packages

A module can also live outside Tesserae, in a directory the host operator names
(#51). `tesserae.external.load_module_dir(directory)` loads it and returns a
`Runtime`, or raises `ModuleError` before anything runs. Loading executes the
package's Python as the host user: it is trusted code, not a sandbox. Load once
and share the runtime; there is no reloading. The original example is
`tests/fixtures/modules/beacon/`.

A package is a directory with `__init__.py` and a `module.toml`:

```text
beacon/
  __init__.py
  module.toml
  rules.py        # the entry point
  signals.py      # a helper, imported as `from . import signals`
  signals.json    # data, read with importlib.resources
```

```toml
schema_version = 1
entry_point = "rules:bundle"

[[providers]]
operation = "dice.roll.v1"
provider = "builtin.dice"
version = "1"
```

- `schema_version` is 1. `entry_point` is `module:function`, with the module's
  dotted name relative to the package. `providers` lists one choice for every
  operation the module requires, and nothing else; a module with no
  requirements has `providers = []`. All three keys are required, and no others
  are allowed.
- The function takes no arguments and returns
  `tesserae.external.Bundle(module, providers=())`: the package's one `Module`
  and any `Provider`s it adds.
- The manifest alone selects providers. The module's own `providers` field is
  ignored, and never fills a gap: a manifest that misses a required operation
  is refused even when the Python defaults are complete. A choice may name a
  bundled provider or one from the bundle, such as Beacon's `beacon.steady`,
  which also supplies `dice.roll.v1`.
- The module and its providers join the bundled catalog. An ID already there,
  such as module `relay` or provider `builtin.dice`, is a duplicate and is
  refused; nothing is replaced. The usual `resolve` checks then apply: host API
  version, and every provider's operation and version.
- The package is imported under a private package name of its own. Relative
  imports and package data work; the working directory and `sys.path` are not
  changed, and a file in the package cannot shadow an installed module. If
  loading fails, nothing from the package stays imported. An exception from the
  package's own code becomes `ModuleError`, with the original as its cause.

The protocol and the CLI client know nothing about a package's fields.

## Example

```python
from tesserae.builtin import CATALOG
from tesserae.protocol import parse_request

runtime = CATALOG.resolve("dice")
request = parse_request(raw, runtime)
outcome = runtime.execute({}, participant_id, request.command, request.args)
outcome.result  # the caller's result; outcome.state is the new module state
```
