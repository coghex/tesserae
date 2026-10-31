# M1-5: external private module and a bounded encounter

Status: in progress; tracker [#49](https://github.com/coghex/tesserae/issues/49).
M1-4 is delivered at `b205b9a`. The external package loader (#51) and its use
by `game create`/`serve` (#52) are implemented
([modules.md](../modules.md#external-packages)), and so is public seeded
creation (#53, [game_store.md](../game_store.md#seeded-creation)). The private
converter and adaptation are not.

## Outcome and accepted scope

The owner selected equipment inspection/changes, one skill check with a
declared spendable resource, and a short single-opponent ranged encounter. Use
a copy of private pilot state, independent of the original campaign. Resolve
exact scene parameters and source interpretations privately before
implementing its mechanics.

The CLI remains a JSON-line pipe. Test the mechanics through authenticated
commands, with fixed module-owned opponent behavior for this exercise. Model
conversation and general referee authority/pacing are M1-6 work. The host does
not acquire universal equipment, initiative, damage or turn rules.

Prove import, character ownership, equipped/carried distinctions, derived values,
the selected check, initiative, bounded movement, single shots, ammunition,
armor, damage, saves, ending turns and encounter termination. Agree exact
supported actions and fixture conditions in PRIVATE-1. Other combat modes,
character augmentation, crafting, advancement and a complete ruleset are outside
this scene. Changing scope requires an explicit recorded decision.

## Public and private delivery

The public repository contains host support, generic contracts, original test
packages and sanitized acceptance. Keep private game names, paths, definitions,
rulebook text, adaptations, source locators, character data, logs and databases
outside its files, issues, PRs and tracked history. Existing private originals
remain unchanged. Private work has local records and tests; a public PR is only
needed for public support changes. Close private-only children with a sanitized
verification note, never by uploading the private implementation.

The owner chose a separate local-only Git workspace without a remote. Its
private `LOCAL_HANDOFF.md` records the exact workspace and original
source/tool/ ruling locations. PRIVATE-1 adds copied-input paths and
fingerprints when those copies are created; no pilot input has been copied as
part of planning. Read that handoff before private work; no actual input
locations are published here. Track private code, specifications and tests in
local Git. Before a private child closes, another agent or the owner manually
reviews named private base/head commits and test evidence against a full
Tesserae commit ID. Record scope, reviewer, verdict, Python/dependency
versions and exact commands/results privately. Acceptance uses clean pinned
revisions; an editable path alone is insufficient. New implementation changes
require review at the new head. Public completion notes are sanitized.

The external package contains Python mechanics, JSON definition/table data and
text guidance with a structured rule-reference index. Stable rule IDs distinguish
implemented, referee-adjudicated and unsupported rules. Notes record printed
rules versus interpretations and house rules. They do not execute mechanics.
Live quantities, injuries, equipment and campaign-specific rulings belong in
SQLite; reusable definitions and adopted module-level interpretations belong in
the versioned package. Source books stay at their existing private locations.

## Explicit trusted package loading

Add host-operator `--module-dir DIR` to `game create` and `serve`. A local
`module.toml` names schema version 1, a Python entry point within the package,
and exact provider choices. The entry point returns one existing `Module`
definition plus any external `Provider` definitions. Define the small concrete
manifest/entry-point contract in PRIVATE-2 and document it with an original
example. Module metadata has one authoritative definition in Python; the
manifest configures loading and provider selection.

For external bundles the manifest must explicitly select a provider for every
required operation, and no others. A partial manifest is an error, even if the
Python module defines defaults. Ignore `Module.providers` for external selection;
never fill gaps from it. A module with no requirements has an empty selection.
Bundled module selection remains unchanged.

Combine the explicit external bundle with bundled providers. Reject duplicate
module/provider IDs, including collisions with built-ins. Alternate provider
IDs may supply an existing operation, but configuration must select exactly one
compatible provider for each required operation. Preserve existing exact API,
module, state and provider version checks. No discovery, directory scanning,
network installation, dependency solver or load-order replacement.

Loading executes trusted local Python as the host user; this is not a sandbox.
Support a package's relative imports and data access without changing process
CWD or permanently adding its directory to the global import search path.
Load once before listening, then share the resolved immutable runtime with
worker stores. Restart is required to change code; changed behavior needs a
version change. No hot reload or automatic state migrations in this slice.

`serve` verifies that the explicitly configured runtime/provider choices match
the stored binding, failing before listening or drawing on any mismatch. It
does not execute a path taken from a database or client message. With
`--module-dir`, serve requires an existing game database and must not create a
file or initialize an empty/mistyped path as a dice game. Only explicit
`game create` creates the external game. Clients receive
permitted projections/results and need no module package or source books.
Participant administration and raw operator inspection remain usable when the
private module is unavailable. Raw inspection means Python calls to
`GameStore.binding()`, `inspect_state()` and `inspect_history()`, or direct
read-only SQLite inspection; this adds no inspection CLI command. Existing
bundled creation and serve flows retain their defaults and behavior.

## Import a new baseline

Use an operator-only seed on fresh game creation, proposed as
`game create --database NEW --module ID --module-dir DIR --seed FILE`.
No in-place campaign replacement or import into a running game is provided.

PRIVATE-4 defines a versioned JSON seed containing the expected binding,
participant records without credentials, module-owned state, and private import
provenance. The private converter allocates new participant IDs and explicitly
maps legacy characters/ownership to them. The module supplies pure seed-state
validation, including ownership references, quantities, loaded/reserve ammunition,
worn versus carried equipment, derived-value consistency and scene position.
The generic host validates envelope/participant structure, including unique
participant IDs matching `p_[0-9a-f]{16}`, and calls the module validator with
the seeded participant set. The module verifies that every player-controlled
character owner references that set; it does not treat NPC identities as player
participants. Share the ID validator with normal participant creation where
appropriate. The host does not interpret game-specific ownership fields.

Create participant records, binding, state and provenance atomically in the new
database at revision 0, with empty command history. Refuse existing output paths,
incompatible seeds and invalid references. Failed validation/creation leaves no
partially usable game or token files. Unseeded creation stays compatible.

Imported participants initially have no credentials. Provision new personal
tokens through existing local `participant rotate` commands, which can issue a
credential to an existing participant with none. Do not import old tokens.
An import is a recorded baseline, not a reenactment of old rolls or transactions.
Preserve historical journals and rolls separately and keep source input hashes
or equivalent local evidence that originals were unchanged.

## Mechanics and ownership

Keep the initial equipment/check/combat implementation in the private module.
Inspect existing tools before reuse and review flagged interpretations before
encoding them. Each selected mechanic has stable rule IDs, source references,
an adopted interpretation where needed and meaningful private tests.

Commands act through the authenticated participant's character. Clients cannot
assert another owner, NPC/referee authority, arbitrary target difficulties,
damage, armor or unrestricted state changes. Selected context/stakes and resource
spending are explicit before a consequential roll; the default spend is zero.
State-dependent refusals happen before drawing. State, resources, results,
history and revision commit through the existing atomic command path.

The encounter stores active actor, initiative, action/movement usage, positions,
weapon/ammunition, protection, wounds and conditions as its own schema. A fixed
private opponent policy and termination conditions are prepared before rolling.
Ending the player's turn advances that policy through module rules; a network
client never gains an NPC-control or referee command. This is a test encounter
policy, not the final AI referee. Keep hidden preparation out of player payloads.

Extract a provider only when a working implementation establishes a reusable
contract. Any extraction has original public tests/examples and explicit
replacement tests. Do not expand Relay just to manufacture a second consumer.
If a proposed extraction has no justified boundary, record why it stays private.
PRIVATE-2 still proves real explicit replacement of an operation provider with
original fixtures; M1-5 makes no claim that tags activate features.

## Dependency-ordered delivery ledger

| Key | Deliverable | Prerequisites | Status |
|---|---|---|---|
| [#50](https://github.com/coghex/tesserae/issues/50) / PRIVATE-1 | Private scene, rule inventory and interpretation specification | Owner's accepted bounded scene | Planned |
| [#51](https://github.com/coghex/tesserae/issues/51) / PRIVATE-2 | Trusted external package/manifest loader and provider selection | Existing module contract | Implemented |
| [#52](https://github.com/coghex/tesserae/issues/52) / PRIVATE-3 | External runtime in create/serve and restart checks | PRIVATE-2 | Implemented |
| [#53](https://github.com/coghex/tesserae/issues/53) / PRIVATE-4 | Atomic seeded creation and private pilot converter | PRIVATE-1, PRIVATE-3 | Public part implemented; converter planned |
| [#54](https://github.com/coghex/tesserae/issues/54) / PRIVATE-5 | Private character/equipment commands and projections | PRIVATE-4 | Planned |
| [#55](https://github.com/coghex/tesserae/issues/55) / PRIVATE-6 | Selected skill check and explicit resource spending | PRIVATE-5 | Planned |
| [#56](https://github.com/coghex/tesserae/issues/56) / PRIVATE-7 | Bounded single-opponent ranged mechanics | PRIVATE-1, PRIVATE-6 | Planned |
| [#57](https://github.com/coghex/tesserae/issues/57) / PRIVATE-8 | Combined private acceptance and public operator guide | PRIVATE-7 | Planned |

PRIVATE-1 and PRIVATE-2 can proceed independently. Follow PRIVATE-2 with
PRIVATE-3; wait for both the private specification and host integration before
PRIVATE-4. Subsequent mechanics build in order. Keep one focused public PR per
public-support child; private-only work remains external. Split a delivery child
further if the accepted private specification reveals an oversized change.

## Acceptance and limits

Public CI uses only original fixtures, including a real externally loaded package
and alternate provider. It covers configuration errors before listening/creation,
atomic invalid-seed refusal, explicit ownership mapping, privacy, unchanged
bundled behavior and exact retry/restart behavior at the new boundaries. Reuse
existing process helpers and transport checks rather than duplicating their
matrix. Run normal Ruff/unittest checks for public code.

Run the selected private scene through real foreground serve/connect processes
with two distinct authenticated participants. A second original private test
character can exercise ownership isolation; it need not add a second opponent
or expand the scene. Verify unauthorized and out-of-turn mutations do nothing,
concurrent stale resource actions cannot both commit, displayed/persisted values
agree, and retry/restart spends and rolls nothing twice. Fixed draws cover
mechanical branches privately; keep actual live dice as rolled.

Record code/module/provider versions, private test results, baseline comparisons,
the live outcome and unchanged-original evidence locally. Publish only a sanitized
acceptance summary and generic setup instructions. The existing Mac/Ubuntu VM
substitution remains accepted; physical-device Wi-Fi is not a new gate. Friends
need the public client, public certificate and their own token, not private code.

Complete this tracker only after the actual private workflow passes. Browser
work, model tools/conversation, general referee authority and pacing, automatic
rulebook conversion and full ruleset coverage remain later work.
