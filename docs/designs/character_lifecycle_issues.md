# M2-3 filed tracker and child specifications

Approved and filed on 2026-11-01: tracker #110 and children #111–#117, revised the
same day with the owner decisions recorded in the [delivery design](character_lifecycle_design.md#owner-decisions).
These are specifications, not implemented functionality. Runtime baseline:
`b28c542c41eab1d82996677e9fa7e29970c9af45`. Public issue bodies below are generic;
private module and campaign work follows separately. See the
[delivery design](character_lifecycle_design.md). This uses the ordinary
manual Tesserae workflow, without custom Kanban tools or approval automation.

## Tracker [#110](https://github.com/coghex/tesserae/issues/110): M2-3 — characters, creation and scoped play

### Outcome

A returning player chooses an existing character after entering a campaign. A
second player joins later through another client, creates a character with the
referee, approves it and enters play. Multiple characters can belong to one player;
selection is per connection. Character identity/lifecycle is common infrastructure;
fields and rules are supplied by the selected module.

### Scope

- Durable owned characters and drafts, validated completion and archival.
- Explicit optional module capability; old modules and standalone utilities stay
  compatible. No universal stats, inventory, turn model or state patch endpoint.
- Host-established actor frozen into jobs, commands, pending questions, recovery
  and exact retries. No impersonation, redirected retries or resource refresh by
  switching. Existing credential/link/grant checks continue to apply.
- Referee-guided creation in short jobs that release busy between answers.
- Lobby-hosted campaigns only; single-game hosts stay character-free.
- Equivalent CLI/browser operations, including usable empty and recovery states.
  Browser retains Console/Settings, memory-only credentials and localhost scope.
- Original public character-enabled demonstration and actual mixed-client
  acceptance; source-specific rules and the real private campaign stay external.

### Owner decisions (2026-11-01)

- Creation conversation is private to its player: every creation exchange is
  marked private, visibly, in both clients. On entering play the character uses
  ordinary table conversation. This is a stated exception for creation mode; the
  existing rule (unmarked conversation goes to the table, only explicitly
  private exchanges are private) is otherwise unchanged.
- Characters are served by the lobby host only. A single-game host
  (`serve --database`) keeps serving games without characters as today and
  refuses a character-required game with a clear error; it gets no character
  selection or creation.
- A character is owned by its campaign participant. Relinking a host player to
  another participant changes which characters that player sees and can select;
  the contract states this explicitly rather than treating it as no transfer.

CHR-1 records these in the contract.

### Children and completion

CHR-1 → CHR-2 → CHR-3 → CHR-4 → {CHR-5, CHR-6} → CHR-7. Close only after the
combined scenario and cleanup are manually reviewed. This tracker does not claim
that private character creation or actual campaign continuation is implemented.

## CHR-1 [#111](https://github.com/coghex/tesserae/issues/111): Specify characters, actor scope and creation contracts

### Problem and scope

Current identity, commands, projections and referee jobs address a participant;
there is no common character identity. Specify the boundary before extending the
store or protocol. This is a documentation-only delivery under the tracker.

### Owner decisions to record (2026-11-01)

- Creation conversation is private to its player: every creation exchange is
  marked private, visibly, in both clients. On entering play the character uses
  ordinary table conversation. This is a stated exception for creation mode; the
  existing rule (unmarked conversation goes to the table, only explicitly
  private exchanges are private) is otherwise unchanged.
- Characters are served by the lobby host only. A single-game host
  (`serve --database`) keeps serving games without characters as today and
  refuses a character-required game with a clear error; it gets no character
  selection or creation.
- A character is owned by its campaign participant. Relinking a host player to
  another participant changes which characters that player sees and can select;
  the contract states this explicitly rather than treating it as no transfer.

### Acceptance

- Define exact metadata/lifecycle (`draft`, `ready`, `archived`), immutable owner,
  stable IDs, connection-local selection, and which operations modules may veto.
  Name the connection modes exactly (no character: setup/rules questions only;
  a selected draft: creation; a selected ready character: play) and use those
  names throughout the contract and CHR-2–CHR-6.
- Specify the explicit optional module capability and compatibility with current
  participant-only hooks. Separate metadata from module-owned draft/sheet data;
  resolve atomic operation outcomes, revision semantics and seeded characters.
- Define frozen actor/mode on inputs/jobs/commands, replay conflicts under changed
  scope, scoped pending questions/recovery, legacy-record treatment and audiences.
  Apply existing acceptance-before-revocation job behavior without widening rights.
- Define the creation tools and bounded question/answer loop, approval tied to a
  draft revision, completion and narrative entry. Creation exchanges are private
  to their player and marked so; define the switch to table conversation on
  entering play, and how this exception sits beside the existing table default.
  The host enforces creation privacy, not the client's private flag: a creation
  input is stored and delivered as private whatever flag it carries, and a
  referee message addressed to the table during a creation job is never
  delivered to the table. Specify that outcome exactly (refused to the referee or
  redirected to the creating player) for every audience the reply tools accept.
- Publish exact lobby host messages/errors/notifications and proposed CLI
  commands/UI states, including archive invalidation, concurrent connections and
  reconnect. Specify the single-game host's error for a character-required game.
- State ownership under relinks: characters stay with their participant, so a
  relink changes which characters a host player sees; define what happens to that
  player's open connections, selections, drafts and unconfirmed inputs.
- Identify migrations/version negotiation. Legacy modules, existing bindings and
  utilities keep their meaning; unsupported clients cannot silently select an actor.
- Record genuinely unresolved product questions, not invented owner decisions.
  Include original examples and enough negative cases to implement CHR-2–CHR-6.

### Out of scope

Runtime implementation, private rules/adaptation, campaign import, extra screens,
queues, browser LAN/TLS or remembered login.

## CHR-2 [#112](https://github.com/coghex/tesserae/issues/112): Persist characters and add optional module lifecycle support

Depends on CHR-1.

### Scope

Implement campaign-local character metadata, the contracted optional module
capability, store primitives and explicit seed support. No network or client UI.

### Acceptance

- Versioned transactional schema migration adds stable owned records without
  rewriting existing module state, history, credentials or participant links.
  Enforce valid/unique IDs and owners, valid phases and immutable ownership.
- Keep metadata and module-owned draft/sheet changes in the same SQLite commit.
  Draft start/steps, approved completion and archive are replayable/idempotent;
  failures, stale revisions and rejected transitions leave no partial changes.
- Module validation controls sheet completeness/rules; the host controls authority
  and lifecycle. No arbitrary client/model patch or unchecked `ready` setter.
  Validate before draws and journal committed randomness like other operations.
- A versioned seed path declares characters and owners explicitly, validates module
  mappings and rejects duplicate, orphaned or mismatched declarations. Preserve
  version-1 seed/module behavior; do not infer characters from module JSON or names.
- Archive preserves history/data, refuses active-job or module-forbidden cases and
  invalidates future actor authority. No hard-delete or ownership-transfer feature.
- Add a minimal original character-capable fixture/module: multiple owned records,
  creation choices with deterministic validation, a mechanical action and a
  lifecycle veto. Keep existing Relay/dice behavior unchanged.
- Focused tests cover persistence, concurrent creation/completion/archive, atomic
  rollback, seed validation, cross-owner refusals and legacy compatibility.

## CHR-3 [#113](https://github.com/coghex/tesserae/issues/113): Freeze referee actors and support bounded character creation

Depends on CHR-2.

### Scope

Extend the referee/store/bridge path with the contracted actor and mode, creation
tools and scoped recovery. Use the original module to prove the flow without a UI.

### Acceptance

- Accepted input/jobs freeze campaign, participant, selected character or null,
  and mode. `act`, views and action descriptions use that identity; NPC/referee
  operations remain explicitly privileged. No current-selection lookup later.
- Exact retries compare actor/mode as part of content within the existing
  participant request-ID namespace. A different actor/mode conflicts instead of
  replaying private output into it or executing a new action under that ID.
- Scope questions, draft progress, last-input projections and unconfirmed recovery
  to the actor/mode. Preserve historical unscoped records as history.
- Creation jobs can mutate only the owned selected draft via validated module
  operations. No-character questions cannot perform gameplay/NPC actions; drafts
  cannot spend ready characters' resources. The referee may explain rules and
  save rulings as contracted.
- Each creation answer starts/ends one bounded job. Save progress and the next
  question, release busy while waiting, and permit other players between answers.
  Keep one referee session/active job per campaign and existing busy refusal.
- Completion requires explicit approval of the displayed draft revision and
  transactional validation. The referee introduces the completed character;
  selection never advances time or creates extra turns.
- The host enforces creation privacy as CHR-1 specifies, independent of the
  client: a creation input sent without the private flag is still private, and a
  creation job's table-addressed referee message never reaches the table or
  another participant. Tests cover both cases.
- Tests cover restart/resume, stale approval, another character's choices, relinks
  (characters stay with their participant),
  revocation, duplicate tool calls, actor changes across connections and archive
  races. Prove switching does not reset the fixture's mechanical resource/action
  state. Existing participant-only bridge behavior still passes.

## CHR-4 [#114](https://github.com/coghex/tesserae/issues/114): Serve character selection and lifecycle through the host

Depends on CHR-3.

### Scope

Implement CHR-1's host messages and per-connection character/creation selection on
lobby-hosted campaigns. Single-game hosts get no character support: they keep
serving games without characters unchanged and refuse a character-required game
with CHR-1's clear error. No CLI/browser presentation yet.

### Acceptance

- After campaign entry, return whether characters are supported/required and the
  caller's own summaries, with an ordinary empty state and resumable drafts.
  List/select/start/resume/approve/archive operations follow the published contract.
- Resolve ownership and selected actor on the host; reject foreign, archived,
  incomplete or stale selections. Starting creation is explicit and idempotent.
- Keep selection local to the connection. Existing participants can select ready
  seeded characters; newcomers can create without having a playable character.
- Creation answers/gameplay use the referee bridge. System metadata operations
  grant no narrative commitment, currency, equipment, movement or turn refresh.
- Recheck credential/link/grant, character validity and module eligibility in the
  protected transaction. Preserve cross-database ordering from the lobby contract.
- Preserve exactly one reply per request when archive/revocation/switch/delivery
  race; no unsolicited boundary notification can consume a request's reply.
- Late replies and retries keep their accepted actor/mode. Recovery communicates
  authoritative identity; unsupported/legacy clients fail clearly for a character-
  required game, while existing non-character game sessions (lobby and single-game)
  still work.
- Raw-protocol tests cover concurrent connections/players, archive during work,
  incomplete creation, host restart, lost access, cross-owner isolation, a relink
  changing a player's characters, and the single-game refusal.

## CHR-5 [#115](https://github.com/coghex/tesserae/issues/115): Add CLI character selection and creation

Depends on CHR-4; may proceed alongside CHR-6.

### Scope and acceptance

- Add discoverable system commands for owned-character listing, selection,
  creation/resumption and archive, with selected character and mode visible.
  Match the host contract; reserve unsupported features honestly.
- In an empty campaign-character state, explain how to create; permit contracted
  setup questions. Creation uses ordinary referee conversation, visibly identifies
  its draft and private audience and shows the module-produced sheet for approval.
- Approve only the displayed draft revision; incomplete/stale/archived choices have
  useful outcomes. Switching never auto-answers a question or sends gameplay.
- Scope cached retry/unanswered/pending state to campaign, participant, character
  and mode. Wait for authoritative recovery before processing a piped retry; retain
  bounded EOF shutdown for initial status, selection and final draining.
- Test real client processes, piped select/create/answer flows, restart/reconnect,
  participant relinks, another character's pending question, archive invalidation,
  errors, JSON output and legacy connections. No private data/token/path leaks.
- Update CLI help and usage docs; verify an installed CLI outside the checkout.

## CHR-6 [#116](https://github.com/coghex/tesserae/issues/116): Add the browser character picker and creation workspace

Depends on CHR-4; may proceed alongside CHR-5.

### Scope and acceptance

- After campaign choice, offer owned ready characters, resumable drafts and Create
  character. Keep Console and Settings as the only navigation items; Console is
  the in-play default. Settings provides a change/archive route.
- Creation uses Console conversation and module-produced read-only sheet/progress
  data. Make character and creation mode explicit, and mark creation exchanges
  private to their player. Review/approval is
  tied to the displayed revision; no generic editable rules form/state patch.
- A ready selection opens play. A person without one can still create or ask the
  contracted setup questions; they cannot accidentally trigger gameplay.
- Clear old actor sheet, composer/private flag, pending question and resend targets
  at boundaries. Every callback and retry is tied to connection/campaign/participant/
  character/mode; preserve the campaign's correctly scoped table history.
- Reconnect restores only the exact actor. An unconfirmed input cannot be resent
  after another selection or relink. Archive/lost-access returns to a useful picker
  without silently replacing the selected character. No new browser storage.
- Chromium coverage includes two pages, one player with multiple characters,
  newcomer creation while an existing player acts between answers, late replies,
  refresh/reconnect, archive/revocation, privacy and safe text rendering. Maintain
  token file, navigation and existing non-character browser behavior.

## CHR-7 [#117](https://github.com/coghex/tesserae/issues/117): Accept the mixed-client character flow and publish the runbook

Depends on CHR-5 and CHR-6.

### Scope and acceptance

- Use a clean pinned installation and only the small original public module. An
  existing CLI player selects a seeded character and starts a short scene. A second
  browser player joins later with no characters, creates one with the real referee,
  explicitly approves it and is introduced into play.
- The existing player can interact between creation answers; jobs do not remain busy
  waiting for input. The new player's draft never acts in the scene before ready.
- One player owns two characters: selection stays per connection, mechanical state
  does not reset on switch, pending questions/retries do not cross actors and a
  relink/archive cannot replay an old input as a new character's action.
- Show incomplete/stale completion, reconnect/refresh, duplicate submission,
  exact retries, archive and revocation; inspect durable ownership, jobs, questions,
  histories and recipient projections for wrong-actor execution or private leaks.
- Creation exchanges reach only the creating player, marked private; the table
  sees the character only once introduced into play. A single-game host refuses
  the character-required game clearly and still serves the legacy examples.
- Keep legacy utility/module examples working. Record exact commits, installation
  fingerprints, commands, outcomes and limits. Clean up credentials, files, owned
  processes and sockets on early failure, diagnostic failure and success.
- Publish an operator/player runbook covering creation-capable modules, supported
  seed/adoption setup, selection, creation, approval, recovery, archive and backups.
  If raw evidence is kept privately, obtain a named-head manual review before close.
- Distinguish this accepted generic flow from the still-separate private rules,
  campaign import/continuation and actual two-client campaign playtest. Close the
  tracker only after live combined acceptance is reviewed.
