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
explicit directory ([External packages](#external-packages), #51), and
`game create` and `serve` take that directory as `--module-dir` (#52).

A module can also declare characters (`Module.characters`, #112), under the
[character contract](designs/character_contract.md): see
[Characters](#characters). The bundled original [Trailhead](trailhead.md)
module does. The host serves character games to players from M2-3's later
slices; today the store runs them.

A character module may also describe, preview and apply negotiated changes
to drafts (`Module.creation_overrides`, #174), under the
[creation overrides contract](designs/creation_overrides_contract.md): see
[Creation overrides](#creation-overrides). Trailhead does. The negotiation
itself (requests, offers, acceptance, the messages and tools) arrives with
the contract's later slices; today the runtime's hooks and the category
policy exist.

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
| `validate_seed(state, participant_ids)` | Optional. Checks imported state for a [seeded game](game_store.md#seeded-creation), including that every participant reference is one of `participant_ids`; raises `SeedError`. Without it, the module refuses seeds |
| `referee_context(state)` | Optional, for [referee mode](designs/referee_contract.md#9-referee-authority). Everything the referee should know about the bounded scene, hidden preparation included; never sent to a player. Without it, the referee gets each participant's own view |
| `referee_actions(state, participant_id)` | Optional. The commands that participant may use now, as a list of JSON objects with their arguments and stakes. Without it, the referee gets the command names |
| `referee_commands` | Optional `Command`s that run only in the host's `referee` context, which no player can authenticate as: their executors see `participant_id == "referee"`, and a player's command path never includes them. Names must not repeat the module's other commands |
| `characters` | Optional `CharacterRules`: every game of the module is then a character game ([Characters](#characters)). Such a module checks seeds with `characters.validate_seed`, and must leave `validate_seed` unset |
| `settings` | Optional `CampaignSettings`: the settings a campaign's settings document may configure, and what a character needs ([Campaign settings](#campaign-settings)) |
| `administration` | Optional `Administration`: the campaign host's inspection and operations ([Host administration](#host-administration)) |

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
    random source;
  - `character_id` is the host's actor in a character game (the ready
    character playing, or the draft being created), and always None in a game
    without characters.

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

### Characters

A module with characters supplies `CharacterRules` (character contract
section 3). The host owns each character's ID, owner, name and lifecycle
(`draft`, `ready`, `archived`); the module owns its data, keyed by character
ID in its own state.

| Field | Meaning |
|---|---|
| `version` | 1 or 2; any other is refused when the module is resolved. Version 2, only with `settings`, passes the character's effective policy as a third argument to `problems`, `complete`, `actions` and `creation_actions` |
| `start(action)` | A new draft's module data. Runs like a command (`action.character_id` is the new ID, `args` is `{}`); may draw; raises `ActionRejected` before any draw to refuse |
| `steps` | Creation `Command`s, run only on the caller's selected draft. The host adds `character.name {"name"}` to every character game |
| `problems(state, character_id)` | Pure: player-safe reasons the draft cannot be completed yet; an empty list means it can |
| `complete(state, character_id)` | Pure and deterministic, with no random source: the state with the draft made playable. Anything that needs a roll is a step |
| `sheet(state, character_id)` | Pure: what the owner sees of one character |
| `archive_veto(state, character_id)` | Pure: a player-safe reason to refuse archiving now, or None |
| `actions`, `creation_actions` | Optional, `(state, character_id) -> list`: for the referee, what a ready character may do now and which creation steps remain |
| `validate_seed(state, participant_ids, owners)` | Optional: checks a seeded game's state, where `owners` maps each declared character ID to its participant; raises `SeedError` |

The catalog refuses a step or command name used twice across `commands`,
`referee_commands` and `steps`, any name starting `character.`, which the
host reserves, and a character module that also sets `validate_seed`. In a
character game a player's command runs as an actor, and only what its mode
allows runs: creation steps on a draft, gameplay commands as a ready
character, nothing without one (see the
[game store](game_store.md#characters)). `referee_context` must leave drafts
out: the host gives a creation job its own draft.

### Campaign settings

A module may let each campaign configure it through a settings document
(governance contract sections 5 and 6), by supplying `CampaignSettings`:

| Field | Meaning |
|---|---|
| `version` | 1; any other is refused when the module is resolved |
| `defaults()` | Every supported setting with its default. No other key is accepted in a document, and the defaults must reproduce the module's rules from before it had settings |
| `validate(settings, state)` | Pure: player-safe problems with one character's or the campaign's effective settings (defaults, then the document's settings, then the character's exception, key by key); an empty list means valid |
| `requirements(policy, state, character_id)` | Pure: what a character needs (or any new character, with None), as a list of entries with a stable `id`, a `kind` of `mechanical`, `narrative` or `optional`, a `description`, `met` (true, false, or null when the module does not record it; omitted with no character) and anything else the module describes, such as `values`, a `method` (`choose`, `roll`) or a `support` note |

The **effective policy**, `{"revision", "settings"}`, is the host's: it
merges the active document for the character, and passes it to version 2
character hooks and as `Action.policy` to every command and creation step in
a game whose module has settings (None in any other). A client or a tool
argument never supplies it. `requirements` is the one description of a
requirement: the referee's context and the players' `rules_view` both show
it, after the host's own `character.name` entry in a character game, so a
module keeps no second prose copy. Nothing in a document loads code, changes
providers or installs a module.

A module may add settings under its current version only if a game with no
settings document behaves exactly as before; Trailhead does (see
[trailhead.md](trailhead.md#campaign-settings)).

### Host administration

A module may give the campaign host broad administration (governance
contract sections 6 and 8) by supplying `Administration`:

| Field | Meaning |
|---|---|
| `version` | 1; any other is refused when the module is resolved |
| `inspect(state)` | Pure: everything the host may see of the state: every character's data (drafts too), NPCs, the world, the referee's preparation. Without it the host sees `referee_context` and every sheet |
| `operations` | Host-only `Command`s. Their names share the module's namespace and the catalog's checks. Each names its target in its own validated arguments (`character_id`, `npc_id`, …) |
| `describe(state)` | Pure: the operations available now, each `{"command", "description", "target_kinds", "args"}`, where `args` describes each argument (name, type, allowed values or range, required). Guidance for clients and the referee, never a validator |
| `target_state(state, target)` | Pure: what an operation or request on `target` (`{"kind", "id"}`) depends on; raises `ArgumentError` for an unknown target. The host hashes it as the target's digest. Without the capability, a character's digest is its sheet's |
| `target(command, args)` | Optional, pure: the target (`{"kind", "id"}`) an operation changes. A guest's request for an operation must name exactly it, so a stale approval is caught; without it only an operation with a `character_id` argument can be requested |

An operation runs only through `GameStore.execute_host_command`, for the
campaign host: its executor sees `participant_id == "host:<player_id>"`,
`policy` for a `character_id` argument's character (else the campaign's),
and an empty `operations` mapping, so a correction never rolls. It checks
state and raises `ActionRejected` like any command, and recomputes derived
numbers through the module's own rules; there is no generic patch, raw SQL
or script. No player or referee runtime includes these operations.

### Creation overrides

A module with version 2 character rules may let a campaign negotiate
changes to a draft's starting qualities, by supplying `CreationOverrides`
([creation overrides contract](designs/creation_overrides_contract.md)
section 3). The framework never interprets a quality: it checks shapes and
bounds, computes digests, and calls these hooks. The module owns every
quality's meaning and effect.

| Field | Meaning |
|---|---|
| `version` | 1; any other is refused when the module is resolved |
| `categories` | 1 to 16 `Category(id, label, description)`: IDs of lowercase letters, digits and `_` (never a path), labels of 1 to 80 characters, descriptions up to 500. Category policy is per category |
| `discover(state, character_id, query)` | Pure: one page, `{"items", "next"}`, of what can be overridden on the draft now: its qualities (with their category, current value and actions, each action's parameters described by exactly their type's fields: `integer` with whole `minimum` and `maximum`, `string` with a positive `max_length`, `enum` with distinct string or integer `values`, or `option`, each with `required`) or, for a `quality_id` and `parameter`, that parameter's options. Guidance, never a validator. At most 50 items and 16,384 result bytes |
| `preview(state, character_id, intent, policy)` | Pure and deterministic, with no random source: exactly what the intent would do now: its `plan`, `headline`, `affected` qualities, `effects`, `waivers`, creation-only `allowances`, `later_steps`, `consequences`, `limitations` and `dependencies`. Everything but `dependencies` must be a function of the intent and `dependencies` alone, so an unrelated creation step leaves the preview, and its digest, unchanged. At most 12,288 result bytes |
| `apply(OverrideAction)` | Deterministic, with no random source: performs exactly the plan, enforcing every technical invariant itself, and returns an `Outcome` whose result is the receipt (at most 8,192 result bytes) |
| `allowances(state, character_id)` | Pure: the draft's live creation-only allowances, each `{"allowance_id", "quality_id", "summary", "remaining"}`. `complete` must drop them |

An intent is exactly `{"changes": [...]}`, 1 to 16 changes of exactly
`{"quality_id", "action_id", "parameters"}`, in at most 4,096 result bytes.
The runtime refuses any other shape with `IntentError` before the module
sees it. A module refuses what it cannot do as asked with
`IntentError(problems)` (`invalid_intent`), and an unknown quality, action
or parameter with `UnsupportedOverride` (`override_unsupported`). Neither
ever echoes client input. A discovery page, preview or receipt of the wrong
shape or over its bound is a module failure (`TypeError`), never cut; a
preview over its bound is refused as too many changes at once.

`OverrideAction` carries copies of the state, the plan, the draft's
effective policy and an `authorization` the host builds: who authorized the
change (`accepted_offer` or `direct`), its record, the host, the proposal
and offer, and the reason. It never comes from a client, a tool argument or
a model. `Runtime.override_apply` refuses anything else.

Result bytes are `len(json.dumps(value))` with Python's defaults, as the
referee bridge sends a tool result: every non-ASCII character is escaped.
`modules.result_bytes` measures it, and `modules.preview_digest` is a
preview's SHA-256 over its canonical JSON.

A campaign's settings document version 2 adds the category policy:
`{"overrides": {"categories": {CATEGORY: "approval_required" | "blocked"}}}`
(contract section 4). Only a module with this capability accepts version
2; version 1 documents keep their meaning, and every category is then
`approval_required`. `policy.category_policy` gives each category's policy,
and `policy.blocked_categories` the blocked categories a guest's intent
names (consequential qualities never block). Policy never limits the host.

A module may add this under its current version and state version only if
a game whose state has none of its new keys behaves exactly as before:
absent override state reads as "no overrides". The module's duties
(original values kept, one calculation, accepted authority persisting,
completion ending creation-only authority, reversal as a new intent, no
hidden families) are the contract's section 3; Trailhead's
[creation overrides](trailhead.md#creation-overrides) show them.

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

`trailhead` version 1 is the original module with characters: walkers with a
trait and stamina, created in steps, who push on or recover, a campaign
setting for their starting stamina, and creation overrides in two
categories. It requires
`dice.roll.v1` from `builtin.dice` 1, and authenticated participants. See
[trailhead.md](trailhead.md).

## External packages

A module can also live outside Tesserae, in a directory the host operator names
(#51). `tesserae.external.load_module_dir(directory)` loads it and returns a
`Runtime`, or raises `ModuleError` before any game command runs. Loading executes the
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

### Creating and serving an external game

The [external module runbook](external-modules.md) walks an operator through
these steps, including a seed and first tokens, with the Beacon example.

```sh
tesserae game create --database game.sqlite3 --module beacon --module-dir path/to/beacon
tesserae participant add --database game.sqlite3 --name Alice --token-file alice.token
tesserae serve --authenticated --database game.sqlite3 --port 8790 --module-dir path/to/beacon
```

- `game create --module-dir` loads the package before creating anything, and
  `--module` must name the package's module. Otherwise nothing is created.
- `serve --module-dir` loads the package once, before opening the game, and
  then needs an existing game: a missing path or an empty file is refused, and
  nothing is created (without `--module-dir`, `serve` still creates a `dice`
  game in a missing file). The game must be bound to exactly that package's
  module, version, state version and provider choices; any difference, a
  package that fails to load, or no `--module-dir` for a game whose module is
  not bundled, exits 1 before listening and leaves the game unchanged.
- The database stores only the binding, never a path or code. Restart with the
  same `--module-dir`; to change code, change the version and make a new game.
- `participant` commands and raw inspection (`GameStore.binding()`,
  `inspect_state()`, `inspect_history()`) never load the package. Players need
  only Tesserae, the certificate and their token; their client never loads it.

## Example

```python
from tesserae.builtin import CATALOG
from tesserae.protocol import parse_request

runtime = CATALOG.resolve("dice")
request = parse_request(raw, runtime)
outcome = runtime.execute({}, participant_id, request.command, request.args)
outcome.result  # the caller's result; outcome.state is the new module state
```
