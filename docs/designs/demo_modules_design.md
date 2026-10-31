# M1-4: original demo and minimal module loading

Status: in progress; tracker [#37](https://github.com/coghex/tesserae/issues/37).
M1-3 is complete at `1afa9a7`. DEMO-1 (#38) delivers the module contract and
`dice` adapter ([modules.md](../modules.md)); DEMO-2 (#39) the schema-3 binding,
module state and `game create` ([game_store.md](../game_store.md#modules-and-state));
DEMO-3 (#40) the [Relay module](../relay.md); DEMO-4 (#41) hosting with
per-player snapshot views ([protocol.md](../protocol.md#messages)). Combined
demo acceptance (DEMO-5/#42) is not done yet. The relay theme
and small rule numbers are an original example, not universal game concepts.

## Outcome

Run one original public scene through the existing foreground host and thin
streaming CLI. Two authenticated participants see the shared scene and their
own small character, take a module-defined action, and recover the same state
and result after reconnect/restart. SQLite remains authoritative.

No AI/model calls, free-form conversation, browser, map rendering, inventory,
combat, private game content, external code loading, marketplace, discovery,
dependency solver or override chains in this tracker. The public demo exercises
mechanics and state; M1-6 adds the referee workflow and complete play experience.

## Evidence and boundaries

At `1afa9a7`, `protocol.py` validates only `roll` and its expression;
`GameStore.execute_command` directly calls `roll_expression`. Store schema 2
has revision/history/credentials but no module binding or live game-state
object. `GameHost._view` returns metadata-only snapshots. The CLI prints JSON
and accepts any dictionary result, but snapshot checks know only metadata.

Preserve the current six-field v1 command envelope, bootstrap roll results,
participant-scoped replay, credential checks, shared revision, metadata-only
notifications, foreground lifecycle and TLS rules. Extend game state below the
transport; do not put module rules in the CLI or server.

## First module contract

Use a small explicit in-process catalog of bundled, trusted Python modules.
Select a module by stable ID, never by descriptive tags or scanning directories.
An external path loader is M1-5 work; no import path or executable definition
comes from a network message. These trusted modules are not sandboxed.

A module definition provides:

- Stable module ID, exact implementation version, state schema version and
  supported host API version (initially 1). Tags are descriptive strings.
- Required operation IDs and an explicit provider selection for each operation.
  The first actual operation is `dice.roll.v1`, supplied by `builtin.dice` using
  the existing dice arithmetic. Pin selected provider IDs/versions too.
- Its command definitions: names, stateless argument validation and synchronous
  execution. Execution receives a private copy of module state, the trusted
  participant context, validated arguments and selected operations; it returns
  the replacement state and the caller's result dictionary.
- Fresh initial JSON state and a pure participant-view function. The host owns
  the envelope and identity; the module owns its fields and disclosure.
- Whether authenticated participants are required. Relay requires them; the
  existing development dice workflow remains available.

Use exact operation/API identifiers. Reject a missing operation, an unsupported
API, unknown selection, incompatible provider or multiple selected providers
for one required operation. Never choose a winner by registration/load order.
No generic dependency graph or semantic-version solver is needed. A fixture
with no dice requirement must load without a dice provider or implicit `roll`.

The `dice` module adapts the current `roll` command and result unchanged. It is
the compatibility default for old databases and old create-on-open entry points.
New module games expose only their registered commands: Relay uses its selected
dice operation internally and does not automatically acquire a raw `roll` action.

The internal Python class/function layout is an implementation choice. Keep
catalog loading and argument validation independent of sockets and SQLite.
Randomness is injected per execution; shared module definitions hold no live
state, connection or mutable random source.

## Durable game creation and state

Add one local operator command:

```sh
python3 -m tesserae game create --database NEW.sqlite3 --module relay
```

It validates the selected module/providers before creating the database, creates
a fresh bound game at revision 0, and refuses an existing path. Provision
participants afterwards using the existing `participant` commands. This avoids
changing the module of a game that `participant add` already created as a legacy
dice game. Existing `serve`/participant create-on-open dice behavior is retained.

Schema 3 adds a singleton module binding and JSON state. Persist module ID,
exact implementation version, state schema version, selected provider
IDs/versions and module-owned state. Initialize that binding and initial state
atomically. The module binding is immutable for this slice. No module switch,
reset or state-upgrade mechanism is supplied.

Upgrade schema 1/2 stores transactionally to the `dice` compatibility binding
and empty module state; preserve IDs, revisions, all results/history and
credentials. Failed migration and unrelated/newer/damaged database handling
retain the existing no-partial-change guarantees.

On host startup, resolve the stored selection from the bundled catalog and
verify the bound module/provider versions. Unknown or mismatched bindings fail
clearly before listening or rolling; do not fall back to dice or reset state.
Load the runtime once and pass it to the worker-owned stores. Local credential
administration and raw operator inspection must still work on a known-schema
store without importing unavailable module code. Execution/views require the
matching runtime.

Fresh execution keeps the current order: parse/validate pure input, acquire
`BEGIN IMMEDIATE`, recheck credentials, return an authorized replay/conflict,
check expected revision, then run fresh module state checks and resolution.
Replay must precede fresh-action guards: a completed task or exhausted worker
must not prevent retrieving an already committed result.

A handler validates ownership, resources and shared-scene conditions before its
first draw. Persist replacement state, caller result/history and one revision
increment in the same transaction. Publish only after commit. Expected illegal
actions use a stable `action_rejected` error with a player-safe message; they
roll nothing, write nothing and consume no revision/request ID. Unexpected
failures roll back all persistent effects and retain the existing recovery
semantics. There is no network/model wait inside the transaction.

## Player views and protocol

For module games, join and sync return the existing v1 snapshot plus two fields:

```json
{"version":1,"type":"snapshot","game_id":"GAME","participant_id":"PLAYER","revision":2,
 "module":{"id":"relay","version":"1","state_version":1},
 "view":{"scene":{"id":"relay_station","restored":false},"character":null}}
```

`view` is a module-produced JSON object, not the complete stored state.
The concrete Relay view below also includes concise scene/rule instructions.
The `dice` compatibility snapshot remains exactly metadata-only. New clients
validate the optional module/view pair structurally, print it unchanged, and
contain no character, repair or resource logic. Notifications retain their
four metadata fields; clients explicitly sync to refresh a view.

Read credential validity, revision and state in one consistent SQLite read
transaction, and construct the recipient's view from that state. Module
projection cannot persist changes, roll, advance revision or create characters.
No complete state, other participant's character, credential or referee-only
field is sent as a result, view, notification or rejection.

The current `_Session.send` can raise an older snapshot's revision to match a
notification it already sent. That is valid only for today's metadata snapshot.
With a view, never label old state with a newer revision. Serialize snapshot
acquisition/send with notification delivery for that session (not globally),
keeping store work on worker threads and SQLite transactions closed before
network writes. Preserve notification coalescing, monotonic delivery, slow-client
handling and join cleanup. Test a commit racing both join and sync.

Relay hosting requires `--authenticated`, including on loopback. Refuse its
unauthenticated startup clearly. Default dice development/authenticated modes
and encrypted LAN configuration retain their current behavior.

## Relay repair: bounded original example

Two workers arrive at a small storm-damaged relay station. The relay is offline.
Each authenticated participant may enlist one worker; the same module can admit
more participants without the host assuming a universal party size.

Initial module state has `relay.restored = false`, an empty worker map keyed by
trusted participant ID, and one demonstration referee-only maintenance note.
That note is deliberately absent from every player payload. It establishes the
projection boundary; this public module is not a vault for secret source code.

Commands:

| Command | Arguments | Effect |
|---|---|---|
| `relay.enlist` | Exactly `name`: display text, 1–40 nonblank characters | Create the caller's worker with repair 1 and effort 3. A new request cannot enlist again or reset effort. No dice. |
| `relay.repair` | Exactly an empty object | Requires an enlisted caller, effort > 0, and an unrestored relay. Draw `1d6`; total = die + repair, target 5. Spend one effort on success or failure. Success sets shared `restored = true`. |

Reject booleans where integers are expected, extra fields, client-chosen owner,
rating, difficulty, expression or outcome. The rules and cost appear in the view
before a player attempts repair. There are no implicit criticals or fumbles.

Enlist and each legal repair commit once and advance the shared revision once.
A failed check is still a committed outcome with its spent effort. Once restored,
new repair requests are rejected before rolling; prior retries still replay.
If every worker exhausts effort, report that state honestly. Do not force a
winning roll or grant an automatic reset to make the demonstration pass.

The view includes the public scene/restoration state, the caller's worker or
null, concise rules and available command names. It excludes the worker map,
other workers' records and the referee note. Results include only the caller's
relevant outcome: inspectable die/arithmetic, target, success, effort before/
after and public relay state. The host has no built-in worker/effort/repair model.

## Delivery slices

| Key | Outcome | Depends on |
|---|---|---|
| [DEMO-1 (#38)](https://github.com/coghex/tesserae/issues/38) | Module/operation/command contract; adapt legacy dice dispatch | None |
| [DEMO-2 (#39)](https://github.com/coghex/tesserae/issues/39) | Schema-3 binding/state, atomic execution and explicit game creation | DEMO-1 |
| [DEMO-3 (#40)](https://github.com/coghex/tesserae/issues/40) | Original Relay module with deterministic rule/projection checks | DEMO-1 |
| [DEMO-4 (#41)](https://github.com/coghex/tesserae/issues/41) | Host runtime loading, consistent player snapshots and thin CLI wiring | DEMO-2, DEMO-3 |
| [DEMO-5 (#42)](https://github.com/coghex/tesserae/issues/42) | Combined demonstration, runbook and recorded acceptance | DEMO-4 |

DEMO-2 and DEMO-3 can proceed independently once DEMO-1 establishes the contract.
DEMO-1 is a behavior-preserving refactor with no new user-facing module flags.
DEMO-2 can exercise custom stateful fixtures before Relay is available.
Each issue/PR owns its relevant implementation docs. No custom Kanban or
`docs-push`; use ordinary branches, focused PRs and manual review.

## Acceptance and test discipline

- Preserve existing roll, session, credential, streaming, replay and TLS tests.
- Use small fixtures to prove missing/competing providers and a module without
  dice. Test interfaces through actual callers; avoid class-layout assertions.
- Store checks prove guarded state decisions before dice, atomic state/history/
  revision, two competing fresh actions, failure rollback and exact reopen/replay.
- Module checks inject fixed draws for both success and failure, exhausted/already
  enlisted/already restored guards, independent game state and recipient views.
  Live runs keep actual dice; acceptance does not require a lucky outcome.
- One combined real host/two-CLI flow proves join/sync views, state changes,
  private character boundaries, shared notifications, stale rejection and replay
  after restart. Reuse process helpers rather than duplicating every old test.
- Record a live walkthrough using the original public module and installed CLI,
  preferably reusing the Ubuntu VM for one player. Name the actual environment;
  do not reopen the deferred physical-device gate from M1-3.
- Run the normal Ruff/unittest/CI checks. Keep private pilot material and live
  credentials/state out of public examples, fixtures and Git history.

This tracker completes M1-4 only. Private module loading/import and reusable
inventory/combat providers belong to M1-5; model/referee workflow decisions
Q-1 and Q-7 remain for M1-6. Broader provider replacement can grow from the
first private validation scene after this small contract is exercised.
