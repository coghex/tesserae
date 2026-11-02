# M2-3: characters, creation and scoped play

Status: approved for delivery, 2026-11-01; implementation not started. The
exact rules are in the [character contract](character_contract.md) (CHR-1).
Tracker [#110](https://github.com/coghex/tesserae/issues/110); children #111–#117. Based on D-24 and the
completed M2-2 lobby, public baseline
`b28c542c41eab1d82996677e9fa7e29970c9af45`.
[Filed issue specifications](character_lifecycle_issues.md).

## Outcome and boundary

A returning player logs in, chooses a campaign and selects an existing character.
Another player may enter that campaign later, create a character with the referee,
approve the completed sheet and join play. A player may own several characters;
each connection chooses its own. Common records supply identity and lifecycle;
modules supply fields, creation rules, arithmetic, resources and turn rules.

This tracker proves that flow with a small original public module. It does not
import or continue the private campaign, build its creation rules or expand its
combat engine. Those follow against the resulting contracts. No additional
browser navigation item, LAN/TLS expansion, remembered login, command queue,
universal sheet or generalized rules language is included.

## Owner decisions

Already agreed: host tokens identify players; characters belong within campaigns;
multiple characters per player; connection-local selection; a completed character
for active RPG play; CLI/browser parity; deletion archives history; rules modules
own character mechanics; all gameplay goes through the referee. An accepted input
must retain its actor, and switching must not grant turns or reset resources.
Creation questions must release the referee between answers.

Defaults approved with this plan:

- Lifecycle states are `draft`, `ready`, `archived`. Completing a character means
  the player approves a validated draft; narrative admission happens through the
  referee, not by advancing the world in a selection handler.
- Before choosing a ready character a player can inspect their own records, ask
  setup/rules questions or start/resume creation. They cannot execute gameplay.
- Ownership is initially immutable. Transfers, shared control, restoration of
  archived records and hard deletion are later work. Names are labels, not IDs.
- A player may choose different owned characters in two connections. Authority,
  turns and resources come from the persisted characters and module rules,
  never from how many connections exist.

Decided by the owner on 2026-11-01, after the plan was filed:

- **Creation is private.** Every creation exchange is private to its player and
  visibly marked so in both clients; on entering play the character uses ordinary
  table conversation. This is a stated exception for creation mode. The existing
  rule (unmarked conversation goes to the table, only explicitly private
  exchanges are private) is otherwise unchanged. The host enforces it, not the
  client's private flag: a creation input is private whatever flag it carries,
  and a referee message addressed to the table during a creation job never
  reaches the table. Today a private input can receive a table-addressed reply,
  so this needs an explicit rule in the contract.
- **Lobby only.** Characters are served by the lobby host. A single-game host
  (`serve --database`) keeps serving games without characters as today and
  refuses a character-required game with a clear error.
- **Characters belong to their campaign participant.** Relinking a host player
  to another participant changes which characters that player sees and can
  select. The contract states this, and what happens to that player's open
  connections, selections, drafts and unconfirmed inputs.

## Current code that needs to change

- `tesserae/store.py` has schema 6, participants and host links, with authenticated
  contexts naming a participant and credential/link. There is no common character
  record. A host-player link establishes ownership identity, not a character.
- `tesserae/modules.py` gives `Action` a participant ID; views and action hooks
  likewise take a participant. Existing modules must not silently choose the
  first character owned by that participant.
- `tesserae/referee/store.py` scopes inputs, jobs, pending choices and last-input
  recovery to participants. `tesserae/referee/tools.py` acts as a job's sender.
  Both need an immutable character/mode scope.
- Seed version 1 carries module state and participant declarations only. An
  explicit character-aware seed path is needed; no guessing from names or module
  JSON is allowed.
- CLI retry state is now keyed by campaign and participant. The browser binds
  callbacks to its connection/campaign entry. Both boundaries need the character
  added, including reconnects, archive notifications and unanswered inputs.

## Storage, module integration and compatibility

Use the campaign's existing SQLite database for character metadata and module
state so a creation/update/completion is one transaction. The host owns stable
character ID, owning participant ID, display name and lifecycle. The module owns
its draft/sheet data keyed by that ID. Do not create a second authoritative
sheet, duplicate ownership in prose, or put game records in the host registry.

The first contract defines a small explicit optional character capability, with
pure validation/projection and transactional creation/lifecycle outcomes. Use it
to start a draft, resolve creation steps, validate completion, project a sheet and
check selection/archive eligibility. The host validates transitions/ownership and
commits metadata, module state, randomness results and replay records together.
Clients and the model cannot submit arbitrary state patches or set `ready`.

A versioned seed extension explicitly declares characters and owners and lets the
module validate the corresponding data. Existing version-1 seeds and modules
without character support retain their current behavior. Schema migration adds
empty host metadata; it does not guess, duplicate or reattribute old characters.
No existing binding/state/history is silently rewritten to enable this feature.
Actual private conversion/adoption is subsequent work, not this tracker.

Character capability is explicit executable configuration, not activation by a
tag. Old participant-based module hooks keep their signatures and semantics;
character-aware hooks receive a host-established actor. The contract must resolve
exact types, hook/version compatibility and error behavior before store code.
Standalone dice and existing non-character games remain usable.

## Actor and referee authority

Within a character-enabled campaign, one connection selects an owned draft or
ready character. A draft selects creation mode; a ready character can enter play.
Selection itself does not draw dice, change mechanical state, complete a turn or
refresh resources. Modules can refuse in-world actions or unsafe lifecycle changes.

An accepted input/job freezes campaign, participant, character (or null for a
setup question), mode and request identity. Bridge commands use those values even
if another connection selects something else or the sending connection leaves.
They never recover the actor from the current UI or select a default character.

Keep request IDs unique within the existing campaign/participant namespace and
compare actor/mode as part of replay identity: reusing an accepted ID under a
different character/mode is a conflict, not a fresh action or a replay delivered
under the wrong actor. Preserve existing unscoped historical records as history.
Pending questions and recovery include their character/mode scope. Table history
remains campaign conversation; private audiences remain participant recipients.
Creation histories are private to their player and must not leak drafts to others.

Creation uses the existing bounded job/bridge path, with module-validated tools,
not a long-running conversation waiting for user input. Each answer is a new
input/job; the referee saves progress and ends the job after asking the next
question. Between answers other players can play. While a job actually runs the
existing per-campaign busy refusal remains; there is no new queue or parallel
referee session. Null/draft actors cannot use play or NPC-action tools to advance
fiction; character-creation tools cannot spend a ready character's resources.
Read-only setup/rules questions and recorded rulings remain possible.

Finalization requires explicit player approval of the displayed draft at a known
revision plus module validation in the commit transaction. A stale approval or an
incomplete draft makes no partial change. Selection/completion grants no free
mechanical actions; the referee handles introduction under the module's scene
rules. The scope of an already accepted job survives disconnection/revocation as
in the lobby contract; new work and delivery still require current authority.

Archive is a persistent lifecycle transition, not removing a row or rewriting
history. It is denied for a character with an active job or when module policy
forbids it (for example during an encounter). Other selected connections must
observe archive/ownership validity before further protected work. No automatic
fictional death, transfer of equipment or world-time advance is implied.

## Client experience

After campaign selection, show owned ready characters, resumable drafts and a
Create character action. Empty ownership is an ordinary onboarding state. Starting
creation is explicit, durable and idempotent; refreshing does not duplicate drafts.
Creation stays in Console, visibly identifying the draft, mode and audience. Show
module-produced sheet/progress as data, with an explicit review/approval step.
A ready character opens the Console for play; Settings can change the selection.
Console and Settings remain the only navigation items.

CLI equivalents should list, select, create/resume and archive characters plus
show the selected actor/mode and available commands. Exact spellings and messages
are CHR-1's contract. Gameplay and creation answers still use the referee path;
listing/selecting/archiving are host system operations, not invented fiction.

At a character boundary clear active sheet, draft text/private checkbox, pending
question and retry/unconfirmed state for the old actor. Recovery restores only the
exact campaign/participant/character/mode. Late responses cannot touch the new
selection. The client must wait for authoritative recovery before interpreting a
piped retry and retain bounded EOF shutdown. A table transcript is campaign-wide;
changing character does not erase the shared campaign history or expose another
participant's private messages. Rules and data projections still belong to modules.

## Delivery slices and dependency order

| Key | Deliverable | Depends on |
|---|---|---|
| CHR-1 [#111](https://github.com/coghex/tesserae/issues/111) | Exact character/module/actor/onboarding contract; docs only | None |
| CHR-2 [#112](https://github.com/coghex/tesserae/issues/112) | Character storage, optional module capability, transactions and seed support | CHR-1 [#111](https://github.com/coghex/tesserae/issues/111) |
| CHR-3 [#113](https://github.com/coghex/tesserae/issues/113) | Frozen actor, scoped jobs/questions/retries and creation bridge | CHR-2 [#112](https://github.com/coghex/tesserae/issues/112) |
| CHR-4 [#114](https://github.com/coghex/tesserae/issues/114) | Host character selection/lifecycle protocol and admission | CHR-3 [#113](https://github.com/coghex/tesserae/issues/113) |
| CHR-5 [#115](https://github.com/coghex/tesserae/issues/115) | CLI character selection and creation flow | CHR-4 [#114](https://github.com/coghex/tesserae/issues/114) |
| CHR-6 [#116](https://github.com/coghex/tesserae/issues/116) | Browser character picker and creation flow | CHR-4 [#114](https://github.com/coghex/tesserae/issues/114) |
| CHR-7 [#117](https://github.com/coghex/tesserae/issues/117) | Original mixed-client scripted acceptance and operator guidance | CHR-5, CHR-6 |

The small original demonstration module develops with CHR-2/CHR-3; a separate
large example game is not a prerequisite. CHR-5 and CHR-6 can proceed independently
once the host contract is implemented. Meaningful failure/concurrency tests belong
with each slice. Acceptance proves the final combined flow with scripted referee
input/output, not just unit tests. The [testing policy](../testing.md) requires
model-free automated tests and acceptance, superseding earlier live-model
requirements.

After this tracker: adapt the external private module for creation and multiple
playable characters; import the frozen campaign into a separate database with
explicit ownership mappings; then run the requested returning-CLI/newcomer-browser
playtest. Those steps require their own scoped plan and private review.
