# M2-4 governance contract (GOV-1)

Status: specification for [#132](https://github.com/coghex/tesserae/issues/132)
under tracker [#131](https://github.com/coghex/tesserae/issues/131), written
before any implementation. It turns the
[campaign authority design](campaign_authority_design.md) and the owner's
decisions (D-25) into exact rules for GOV-2 to GOV-7: where the campaign host
is recorded and how it is checked, the campaign settings document, the
optional module capabilities, host inspection and administration, guest
requests and host decisions, and the messages and client states that carry
them. Nothing here is implemented yet.

It extends the [lobby contract](lobby_contract.md), the
[character contract](character_contract.md) and the
[referee contract](referee_contract.md). Everything they say still holds
wherever this contract does not say otherwise. Examples use the original
[Trailhead](../trailhead.md) module, extended as section 14 illustrates;
GOV-3 and GOV-4 build the real fixtures.

## 1. Decisions this contract follows

Owner decisions (D-25, tracker #131, 2026-11-02):

- Every established lobby campaign has exactly one **campaign host**: a host
  player, designated explicitly, who may own zero, one or many characters.
  Every other admitted player is a **guest**. Authority follows the
  authenticated player identity, never a character, a name, a connection or
  anything said in conversation.
- The campaign host has ultimate campaign authority: it can change or waive
  restrictions, including for its own characters, without a second approver.
  There is no co-host role.
- Broad host administration ships now: other players' characters, NPCs and
  world and scene data, through either client, with no character needed.
- The host may inspect everything in its campaign: every sheet, every private
  conversation (creation included) and the referee's preparation. Guests keep
  their privacy from each other and from the table. The host's privilege is
  campaign-scoped: no other campaign, credential, token or file.
- Campaign rules are agreed conversationally, shown as a concrete document,
  approved, then activated. The active document is versioned JSON in the
  campaign database, with file import and export; an imported file is a
  candidate, never a live change.
- Modules describe their own requirements: mechanical requirements, narrative
  guidance and optional details. There is no universal class, role, level,
  stat list or questionnaire.
- Only the campaign host changes a guest's mechanical requirements. The
  referee negotiates narrative details and asks the host for mechanical
  exceptions; it cannot change scripts, bypass validators or invent numbers.
- A human decision never holds a referee job open: the job ends, and other
  players play meanwhile. The busy refusal stays; there is no queue.
- Localhost-only browser, memory-only credentials and Console/Settings
  navigation are unchanged.

Engineering choices made here, within that scope:

- The designation lives in the **registry**, beside the grants it depends on,
  so it is checked inside the same short registry guard as a grant and is
  ordered against a transfer exactly as a grant is against its revocation.
  Campaign-owned records (settings, requests, decisions, audit) live in the
  campaign database and name the host player they were decided by.
- The campaign host must hold an active grant to its campaign. A grant the
  host needs cannot be revoked until the host is transferred.
- Settings and approved exceptions are one document: each approval or
  activation is a new immutable revision, and the active revision is the
  effective policy.
- Direct host mutations are refused while a referee job runs. Within the
  campaign host's own job, the referee may act on the host's explicit
  instructions, reauthorized at every call.
- The host reads private conversation on request; nobody else's private
  messages are pushed to it live.

## 2. Terms

| Term | Meaning |
|---|---|
| Registry host | The `serve --registry` server identity (`host_id`, unchanged) |
| Campaign host | The one host player designated for a campaign (this contract) |
| Guest | Any other admitted player of that campaign |
| Operator | Whoever runs the local `tesserae host …` and `tesserae game rules …` commands on the host computer |

The protocol and documents say "host" for the campaign host only where the
context is a campaign; registry commands keep saying "player".

## 3. The designation

### Storage

Registry schema version 2 adds one column and one table:

```sql
ALTER TABLE campaigns ADD COLUMN host_player_id TEXT REFERENCES players(player_id);
CREATE TABLE campaign_host_changes (
    id INTEGER PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
    from_player_id TEXT,              -- null when first designated
    to_player_id TEXT NOT NULL REFERENCES players(player_id),
    by TEXT NOT NULL,                 -- "operator" or the transferring player ID
    changed_at TEXT NOT NULL
);
```

A registry opened by this version is upgraded from schema 1 to 2 in one
transaction, as game stores are; existing campaigns get a null
`host_player_id` (section 3, adoption). Older Tesserae versions refuse a
schema 2 registry. This changes `HostRegistry`'s "never migrate" rule for
this additive upgrade only.

### Invariants

- An established campaign has exactly one `host_player_id`, a registered
  player with an unrevoked grant to that campaign. A null value means a
  registered campaign not yet adopted: it plays as before, with no host
  controls (section 3, adoption).
- The designation never names a participant or character. The host reaches
  the campaign through its ordinary link and participant, admitted as any
  player is (lobby contract section 5); owning characters is unrelated.
- No client message, bridge tool or model output can set or claim it.

### Setup, adoption, recovery and transfer

| Operation | Command or message | Rules |
|---|---|---|
| New campaign | `tesserae host campaign add --registry R --database DB --name N --host PLAYER_ID` | `--host` is required. One registry transaction registers the campaign, grants the host and designates it, recording `by = "operator"`. The game file is still only read (registration keeps its read-only guarantee) |
| Adopt or recover | `tesserae host campaign set-host --registry R CAMPAIGN_ID PLAYER_ID` | Operator only. Designates a campaign that has none, or replaces the current host (recovery). The player must exist; a grant is added if missing |
| Transfer | `campaign_host_transfer {player_id}` from the current host | The new host must be a registered player with an active grant and an active credential. One registry write replaces the designation and records the change, `by` the old host |
| Show | `tesserae host campaign list` and `members` show `host_player_id`; `members` marks the host | |

Guards around the designation:

- `host campaign revoke` of the host's own grant is refused: "transfer the
  campaign host first". Deactivating the campaign is allowed; nobody enters
  while it is inactive, and the designation stays.
- `host player revoke` and `rotate` of the host are allowed. Rotation keeps
  the designation (the player ID is stable). A revoked host credential leaves
  the host designated but powerless until the operator rotates it or
  designates someone else; a guest is never promoted automatically.
- An offline host changes nothing: play continues under the active rules,
  and requests wait (section 9).

**Two files, no shared transaction.** The designation is written only to the
registry; the campaign database records the host player ID only as history
(who decided what). Setup is ordered: create the game, `add --host` and
`activate` in the registry, then `link` the host player to a participant if
it already has one (the one step that writes the campaign file, as today).
A failure between steps leaves a registered campaign whose host enters as a
new participant or is linked later; nothing needs undoing.

## 4. Checking authority

`HostRegistry.authorize_campaign(context, campaign_id)` (the guard taken for
every protected lobby operation, lobby contract section 6) also yields the
campaign's `host_player_id`, read in the same read transaction. The caller's
**role** is `host` when it equals the context's player ID, else `guest`.
Because the guard's read lock blocks a registry writer, a transfer, a
`set-host` or a credential revocation is ordered wholly before or wholly after
each guarded operation.

- **Direct host messages** (section 13) check the role inside the guard, then
  do their campaign work in one campaign transaction under it, in the lobby
  contract's lock order: registry, then campaign.
- **Host projections** (section 7) are read under the guard; a session whose
  role has become `guest` gets `not_campaign_host` and no host data.
- **Bridge calls with host authority** (section 10) take the same registry
  guard for the job's sender at the moment of the call, before any campaign
  transaction. The lobby host gives
  each campaign's service a registry-backed authority check for this; a
  single-game host has none, so host tools are absent there.
- **Host authority is not frozen.** Unlike an accepted input, which keeps its
  actor and finishes after a revocation (lobby contract section 6), a host
  privilege is checked at each use. A job started by the host loses its host
  tools the moment the host is transferred or its credential revoked; its
  ordinary player actions still finish under the character contract's rules.

**Late delivery.** A host reply or projection is built under the guard; one
already built when a transfer commits may still be sent, as with any reply
ordered before a revocation, but nothing is built after it. A host message
handled after a transfer commits is checked then, and refused.

**Notices.** When a connection's role changes (transfer, `set-host`, adoption),
the host sends it, unsolicited, `{"type": "role", "role": "host" | "guest"}`,
following `left`'s and `actor`'s rules: never in place of a reply, decided
under the recipient's send lock, and only while its access holds. A client
drops every host-only datum it holds on a `role` of `guest`.

## 5. The campaign settings document

### Shape

```json
{"document_version": 1,
 "module": {"id": "trailhead", "version": "1", "state_version": 1},
 "settings": {"starting_stamina_bonus": 0},
 "exceptions": [
   {"exception_id": "x-1", "character_id": "c_4be0…",
    "settings": {"starting_stamina_bonus": 1},
    "reason": "Agreed: a mountain guide by trade.", "request_id": "hr-7f2a…"}
 ],
 "guidance": "Walkers are locals; keep newcomers' backgrounds in the valley."}
```

- `settings` holds only keys the module's capability declares (section 6);
  a module without it allows only `{}`. Each of `exceptions` overrides
  `settings` for one character (`character_id`); a campaign-wide change edits
  `settings` itself.
- `guidance` is text for the referee (up to 8,000 characters); it never
  changes a mechanical check.
- `module` must equal the game's binding exactly. Nothing in a document loads
  code, changes providers or installs a module.

### Storage

Campaign schema version 8 adds:

```sql
CREATE TABLE campaign_documents (
    revision INTEGER PRIMARY KEY,        -- 1, 2, … in creation order
    content TEXT NOT NULL,               -- canonical JSON (sorted keys)
    sha256 TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('import', 'referee', 'request', 'host')),
    state TEXT NOT NULL CHECK (state IN ('candidate', 'active', 'superseded', 'rejected')),
    created_at TEXT NOT NULL,
    activated_at TEXT,
    activated_by TEXT                    -- the host player ID, or "operator"
);
CREATE UNIQUE INDEX one_active_document ON campaign_documents (state) WHERE state = 'active';
```

Exactly one revision is `active` once a campaign has adopted settings; a game
with none behaves as having `{"settings": {}}` (module defaults). Revisions
are immutable: a change is a new revision. The **policy revision** of a
campaign is its active revision number (0 with none).

### Operations

| Operation | Who | Effect |
|---|---|---|
| Import | `tesserae game rules import --database DB --file F` (operator) or `rules_import {content}` (host) | Validates the shape and binding, stores a `candidate`. Nothing live changes |
| Export | `tesserae game rules export --database DB [--revision N] --file F` or `rules_export {revision}` (host) | Writes the canonical JSON of a revision (default the active one) |
| Activate | `rules_activate {revision, expected_policy_revision}` (host), or `tesserae game rules activate --database DB N` (operator) | Validates the candidate against the module and current state (section 6); in one transaction makes it `active`, the previous active `superseded`, and records who. A different current policy revision is `stale_policy` |
| Reject | `rules_reject {revision}` (host) | A candidate becomes `rejected` |

An activation that changes any draft's effective settings adds one to that
draft's `draft_revision` in the same transaction, so a displayed approval of
the old revision is `stale_draft` (character contract section 9). It never
changes a completed character's sheet, rerolls anything or refreshes a
resource; changing an existing sheet is a separate host operation
(section 8).

## 6. Module capabilities

Two new optional fields on `Module`, both independent of the character
capability and of each other.

### Settings and requirements

```python
@dataclass(frozen=True)
class CampaignSettings:
    version: int                                      # 1
    defaults: Callable[[], dict]                      # the module's own settings
    validate: Callable[[dict, dict], list]            # pure: (settings, state) -> problems
    requirements: Callable[[dict, dict, str | None], dict]  # pure: (policy, state, character_id) -> requirements
```

- `validate(settings, state)` checks one character's or the campaign's
  effective settings (defaults, then the document's settings, then the
  character's exception, merged key by key) and returns player-safe problems;
  empty means valid. It is called for every candidate before activation, for
  each exception it carries, and for every request's proposed settings.
- `requirements(policy, state, character_id)` returns JSON describing what a
  character needs: entries with a stable `id`, a `kind` of `mechanical`,
  `narrative` or `optional`, a description, allowed values or methods
  (`choose`, `roll`) where they apply, whether it is met, and a `support`
  note where an option needs further development. It is the one source for
  both the referee's context and the player's view; modules must not keep a
  second prose copy. Declaring an option does not prove every mechanic it
  touches in play is implemented.

The **effective policy** for a character is the dictionary
`{"revision", "settings"}` the host computes and passes to hooks; it never
comes from a client or a tool argument.

### Policy-aware character rules

`CharacterRules.version` 2 adds a `policy` argument to the hooks that judge a
draft: `problems(state, character_id, policy)`, `complete(state, character_id,
policy)`, `creation_actions(state, character_id, policy)` and
`actions(state, character_id, policy)`; and `Action` gains `policy` (None
outside a policy-aware game). Version 1 modules are called exactly as before.
A module may use version 2 only together with `CampaignSettings`.

### Administration

```python
@dataclass(frozen=True)
class Administration:
    version: int                                   # 1
    inspect: Callable[[dict], dict]                # pure: everything the host may see of the state
    operations: tuple[Command, ...]                # host-only commands
```

- `inspect(state)` is the host's view of module state: every character's
  data, NPCs, the world and the referee's preparation. Without it the host
  sees the referee context (`referee_context(state)`) and every sheet.
- `operations` are commands that run only in a host context (section 8).
  Their names share the module's command namespace and the catalog's checks
  (no `character.` prefix, no duplicates). Each states its target in its own
  validated arguments (for example `{"character_id", …}` or `{"npc_id", …}`);
  the host checks a `character_id` argument names a character of this game.
  They receive the host context's sender (section 8) as
  `Action.participant_id`, never a player's participant, and may raise
  `ActionRejected`, like any command.

`HOST_API_VERSION` stays 1; unknown capability versions are refused when the
module is resolved, as for `CharacterRules`.

**Adding capabilities to an existing module.** A game's module binding is
fixed and the catalog holds one version of each module, so a new module
version would leave existing games unable to load. A module may therefore
add these capabilities, and new optional state keys, under its current
version and state version only if a game with no settings document and no
new keys behaves exactly as before: the defaults reproduce the old rules, and
an absent key reads as empty. Anything else is a new module.

## 7. Host inspection

All host reads are guarded (section 4) and refused to guests with
`not_campaign_host`. None changes anything.

| Message | Reply |
|---|---|
| `host_view` | `{"type": "host_view", "revision", "inspect", "characters": [every character with owner, phase, sheet], "participants", "pending_choices", "rulings", "policy": {"revision", "document"}}` |
| `host_messages {after_seq}` | Every conversation message after `after_seq`, private and creation ones included, in the referee transcript's pages (up to 200 messages or 32 KiB), with `next_after_seq` |
| `host_requests` | Every request (section 9), any state |
| `host_audit {after}` | The audit records (section 8) after an ID, paged as above |

- Host data is never pushed: the host asks. Ordinary guest projections,
  snapshots, `messages` and deliveries are unchanged, so the host's own
  connection still sees only its own private messages in its ordinary
  conversation view.
- Guest creation is told, in the creation prompt and the clients (section 15),
  that the campaign host can read creation conversations.
- Infrastructure is never included: no tokens, digests, paths, bridge or
  session IDs beyond what the referee contract already shows players.

## 8. Host administration

**Running an operation.** `host_command {request_id, expected_revision,
command, args}` from the campaign host runs one `Administration` operation
through `GameStore.execute_command` in a host context: a new trusted
`HostContext(host_id, player_id)` that only the lobby host builds, under the
guard. Its commands are recorded with the sender `host:<player_id>` (like
the referee's `referee`, no participant or token is that ID), so each host
player has its own replay namespace, separate from its own participant's. Replay,
conflict and revision rules are the protocol's: an exact retry returns the
recorded result; a used ID with other content is `request_conflict`; a moved
revision is `stale_revision`. The game revision covers every character, NPC
and scene, so a stale editor cannot overwrite intervening play or another
host edit.

**The busy rule.** While a referee job is running, `host_command`,
`rules_activate` and `request_decide` (section 9) are refused with the
existing `referee_busy` error and nothing changes; reads are unaffected.
The one exception is the host's own job (section 10).

**Audit.** Every host mutation, by message or by the referee for the host,
adds one row to the campaign's `host_audit` table in the same transaction as
its effect:

```sql
CREATE TABLE host_audit (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('operation', 'activation', 'decision', 'rejection')),
    host_id TEXT NOT NULL, player_id TEXT NOT NULL,     -- the deciding campaign host
    via TEXT NOT NULL CHECK (via IN ('client', 'referee', 'operator')),
    job_id TEXT,                                        -- when via the referee
    target TEXT,                                        -- a character, NPC or other ID the module named
    detail TEXT NOT NULL,                               -- JSON: command and args, revision, request, outcome
    revision INTEGER, created_at TEXT NOT NULL
);
```

Operations never change a character's owner, an old command's sender or a
message's audience. Gameplay history stays as it was; a correction is a new
command record under the host context, so it is always distinguishable from
play.

## 9. Guest requests and host decisions

### A request

```sql
CREATE TABLE host_requests (
    request_id TEXT PRIMARY KEY,           -- "hr-" and 12 hex digits, made by the host
    job_id TEXT NOT NULL,                  -- the job that proposed it
    proposal_key TEXT NOT NULL,            -- the referee's step ID: (job_id, proposal_key) is unique
    requester_participant TEXT NOT NULL, requester_player TEXT,
    character_id TEXT,                     -- the target; null for a campaign-wide request
    kind TEXT NOT NULL CHECK (kind IN ('exception', 'campaign_setting', 'operation')),
    payload TEXT NOT NULL,                 -- JSON: the exact settings, or command and args
    explanation TEXT NOT NULL,
    policy_revision INTEGER NOT NULL,
    target_revision INTEGER,               -- a draft target's draft_revision, else null
    state TEXT NOT NULL CHECK (state IN ('pending', 'approved', 'denied', 'cancelled', 'superseded', 'stale')),
    decided_by TEXT, decided_at TEXT, decision_note TEXT, effect TEXT
);
```

- **Proposing.** The referee calls `propose_host_decision` (section 10) in
  any job; the requester is the job's sender, and the target defaults to the
  job's character. A campaign-wide request must say so (`"scope":
  "campaign"`). The payload is validated now, as it will be at decision time
  (`validate` for settings; the operation's own `validate` for an operation);
  an unsupported request is refused to the referee, not stored. Retrying the
  same `(job_id, proposal_key)` with the same payload returns the same
  request; different content is `request_conflict`.
- **Superseding.** A new pending request from the same requester for the same
  target and kind supersedes the older pending one.
- **Deciding.** `request_decide {request_id, decision: "approve" | "deny",
  note}` from the campaign host. Approval revalidates against the current
  state and policy; if the policy revision or a draft target's draft revision
  has moved since the proposal, the request becomes `stale` and nothing changes (a new
  proposal is needed). An approved exception or campaign setting is a new
  active document revision (origin `request`); an approved operation is that
  exact operation run in the host context. Either way the effect, the
  request's state and the audit row commit together. Denial changes no
  mechanics. A decided request replays its recorded outcome; a second
  decision is `request_decided`.
- **Cancelling.** The requester's `request_cancel {request_id}` (a guest's
  only control) or the host's denial. A target archived, or a requester whose
  link ended, makes a pending request `cancelled`.
- **Transfer.** Pending requests stay; only the current campaign host can
  decide them.
- **No standing grant.** An approval applies its exact effect once. It never
  lets a guest, or a later job, use host tools, and it never performs the
  guest's next action: the guest's following input does that, under the new
  policy.

### Waiting

`propose_host_decision` does not wait: the job replies and ends, as every job
does, and the request stays pending in the store. The requester's referee
projection gains `requests`, its own requests with their state; the host's
`host_requests` and an unsolicited `request` notice
(`{"type": "request", "request_id", "state"}`, to the host and the requester,
under the usual notice rules) tell the people involved. Other players keep
playing; the referee is busy only while a job actually runs.

## 10. The referee

**Context.** In a game with settings, `get_context` adds `policy` (the
revision, the effective settings for the job's character and the campaign's
`guidance`), `requirements` for the job's character (section 6) and the
requester's pending and recent requests. A job of the campaign host adds
`host: true` and the campaign's pending requests.

**New tools.**

| Tool | Who | Arguments | Effect |
|---|---|---|---|
| `propose_host_decision` | Any job | `proposal_key`, `kind`, `scope`, `payload`, `explanation` | Section 9 |
| `host_act` | The campaign host's job | `step_id`, `command`, `args` | Runs an `Administration` operation for the host, as section 8, journaled as a step of kind `host_act` |
| `host_decide` | The campaign host's job | `request_id`, `decision`, `note` | Section 9's decision |
| `host_propose_rules` | The campaign host's job | `content` | Stores a candidate document (origin `referee`); activation stays a separate, explicit host operation |
| `host_activate_rules` | The campaign host's job | `revision`, `expected_policy_revision` | Section 5's activation, when the host has explicitly asked for it |

- **A host job** is a job whose input came from a session of the campaign
  host player: the lobby host gives the job's grant that session's
  `HostPlayerContext`, held in memory only. Each host tool takes the registry
  guard for that context at the call (section 4) and refuses with `not_campaign_host` if the designation or
  credential has changed; a guest's job never has these tools. The prompt of
  a host job says that host tools act only on the host's explicit
  instruction, and only with the exact request ID or revision.
- **Inside the host's job** the busy rule does not apply to these tools: the
  job is the running work.
- **The host's own exceptions** need no request: on the host's instruction,
  its job writes the exception with `host_propose_rules` and applies it with
  `host_activate_rules` (or the host uses the `rules_*` messages), audited as
  any host change.
- **What cannot authorize anything:** a ruling (`record_ruling`), a choice
  (`choices.ask`/`resolve`), a reply's text, a player's claim of consent, or
  a guest job's call. Only a decision recorded through `request_decide` or
  `host_decide`, by the current campaign host, changes a guest's policy.

## 11. Narrative facts and provenance

Agreed narrative facts belong to the module's character data, written by the
module's own creation steps or host operations, so their validation and
projection are the module's. A module should record each fact with its
provenance (`chosen`, `rolled`, `assumed`, `corrected`) and leave optional
facts absent or explicitly `unspecified` rather than invented. The framework
only guarantees what it does for every step: it is journaled, replayed and
scoped to its actor. Prose in a reply records nothing.

**Imported history.** A module that imports historical background (the
private integration's plan) keeps it in its own state, seeded through seed
version 2's opaque module `state` and exposed through `referee_context`,
`view` and `Administration.inspect`. There is no public conversation-history
importer: the referee transcript of an imported game starts empty, and
imported records are never inserted as messages, jobs, commands or rolls.
Budget: `get_context` returns the module's referee context in one unpaged
tool result, and every commit rewrites the whole module state. A module keeps
its referee-visible history within about the 32 KiB transcript page
(`TRANSCRIPT_PAGE_BYTES`), measured as UTF-8 JSON together with the rest of
`get_context`, using summaries that cite their records. A module that cannot
fit must raise a scoped public paging requirement; this contract adds none.

## 12. Storage

- **Registry schema 2:** section 3.
- **Campaign schema 8:** `campaign_documents` (section 5), `host_audit`
  (section 8), `host_requests` (section 9), and `referee_steps.kind` gains
  `host_act` (the table is rebuilt to widen its check, rows copied). Nothing
  else is rewritten.
- A campaign without host data behaves exactly as at schema 7.

## 13. Host messages

All version 1, inside a selected lobby campaign. Each reply follows the
existing pairing rules; errors carry the request's `request_id`.

| Message | Fields | Reply |
|---|---|---|
| `host_view` | | `host_view` (section 7) |
| `host_messages` | `after_seq` | `host_messages` |
| `host_requests` | | `host_requests` |
| `host_audit` | `after` | `host_audit` |
| `host_command` | `request_id`, `expected_revision`, `command`, `args` | `result` or an error |
| `rules_import` | `content` | `rules {revision, state}` |
| `rules_export` | `revision` (optional) | `rules {revision, state, content}` |
| `rules_activate` | `revision`, `expected_policy_revision` | `rules {revision, state}` |
| `rules_reject` | `revision` | `rules {revision, state}` |
| `request_decide` | `request_id`, `decision`, `note` | `request {request_id, state, effect}` |
| `request_cancel` | `request_id` | `request {request_id, state}` (requester only) |
| `campaign_host_transfer` | `player_id` | `role {role: "guest"}` for the old host |

The snapshot of a campaign with a designated host gains `role` (`host` or
`guest`) and `policy_revision`; its referee projection gains `requests`
(section 9). Unsolicited: `role` (section 4) and `request` (section 9).

New errors (a host mutation during a job reuses `referee_busy`):

| Code | Cause |
|---|---|
| `not_campaign_host` | A host message or tool from anyone but the current campaign host, or a campaign with no designated host |
| `stale_policy` | `rules_activate` against another policy revision |
| `invalid_document` | A document that fails its shape, binding or module validation; carries `problems` |
| `request_unavailable` | An unknown, foreign or no longer pending request (one code for all) |
| `request_decided` | A second decision on a decided request |

Single-game hosts have no host messages: each is refused with
`not_campaign_host`, and a game is played there exactly as before.

## 14. Illustrative module

Trailhead, still version 1 under section 6's rule (GOV-3/GOV-4 decide the
fixture): a setting
`starting_stamina_bonus` (0 to 2, default 0) added at completion;
`requirements` listing `name` (narrative), `trait` (mechanical, choose),
`stamina` (mechanical, roll) and `home` (optional); and administration
operations such as `trailhead.set_stamina {character_id, stamina}`,
`trailhead.npc_set {npc_id, name, note, visible}`, `trailhead.scene_set
{description}` and `trailhead.prep_set {note}` (referee-only preparation),
with `inspect` returning every walker, NPC, the scene and the preparation.

A guest's referee proposes `exception {starting_stamina_bonus: 1}` for their
draft and ends the job; the host's `request_decide approve` makes revision 2
active with that exception and bumps the draft's revision; the guest's next
look at the sheet shows the new revision, and approval completes with the
bonus. A denial leaves revision 1 and the draft unchanged.

## 15. Clients (proposed for GOV-6 and GOV-7)

**CLI** (`connect --lobby`, inside a campaign):

```text
/role              your role here: host or guest
/rules             the active rules (and, for the host, candidates)
/rules import F    host: import a file as a candidate
/rules activate N  host: activate candidate N (asks to confirm)
/requests          guest: yours; host: all, with IDs
/decide ID approve|deny [NOTE]   host
/cancel ID         guest: withdraw your pending request
/inspect [TARGET]  host: the host view, or one character's
/history-all       host: every message, private ones included
/admin COMMAND ARGS-JSON          host: one administration operation (asks to confirm)
/transfer PLAYER   host: hand the campaign to another player (asks to confirm)
```

**Browser:** Settings gains the campaign role and, for the host, Rules
(the active document, candidates, import and activate), Requests (approve or
deny each, with its exact content) and Administration (pick a target, run a
module operation with its arguments, confirm). The Console shows a guest's
pending requests and outcomes, and creation says that the campaign host can
read it. Host data is cleared on a `role` of `guest`, on leaving, on lost
access and on reconnect until reauthorized; nothing is stored in the browser.

## 16. Compatibility

- A registry opened by this version is upgraded to schema 2, and a game to
  schema 8; older Tesserae versions refuse both, as with earlier upgrades.
- A campaign with no designated host, a game with no settings document and a
  module with none of section 6's capabilities all behave exactly as before.
  `host campaign add` now requires `--host`; scripts that register campaigns
  must name one.
- Single-game hosts, development mode, `game create`, `roll` and the other
  standalone commands are unchanged and designate nobody.
- Character privacy (character contract section 8) is unchanged for every
  player except the campaign host, who can read on request (section 7).
- The referee folder's standing instructions are unchanged; host jobs' rules
  travel in their prompt, as creation's do.

## 17. Tests each slice needs

- **GOV-2:** registry schema 2 upgrade; `add --host`, `set-host`, transfer
  and their refusals (no host grant revocation, unknown player); role in the
  guard ordered against transfer and revocation; host reads refused to
  guests and after transfer; offline host; rotation keeps the designation;
  no-character host inspection; `role` notices racing requests.
- **GOV-3:** schema 8; import, export and activation round trips; stale and
  unauthorized activation; unsupported keys and wrong bindings refused with
  nothing changed; effective policy per character; activation bumping drafts
  and never touching completed sheets; version 1 modules unchanged.
- **GOV-4:** host operations on another character, NPCs and scene; replay and
  stale revisions; the busy rule; audit atomicity; guests and guest jobs
  refused; history preserved.
- **GOV-5:** request lifecycle (propose, supersede, approve, deny, cancel,
  stale, replay, conflict, competing decisions, transfer); the job ending and
  others playing meanwhile; host tools reauthorized per call and absent from
  guest jobs; a ruling, a choice or a claim never approving anything.
- **GOV-6 and GOV-7:** as their issues list, against this contract.

## 18. Open questions

These are not owner decisions; each has a default that stands until the
owner says otherwise.

- **Live private messages to the host.** Default: the host reads them on
  request (`host_messages`), not pushed live.
- **Operator activation.** Default: allowed (`tesserae game rules
  activate`) for setup with the owner's assistant, audited as `operator`.
