# M2-4 governance contract (GOV-1)

Status: specification for [#132](https://github.com/coghex/tesserae/issues/132)
under tracker [#131](https://github.com/coghex/tesserae/issues/131), written
before any implementation. It turns the
[campaign authority design](campaign_authority_design.md) and the owner's
decisions (D-25) into exact rules for GOV-2 to GOV-7: where the campaign host
is recorded and how it is checked, the campaign settings document, the
optional module capabilities, host inspection and administration, guest
requests and host decisions, and the messages and client states that carry
them.

Implemented so far: GOV-2 ([#133](https://github.com/coghex/tesserae/issues/133))
delivers sections 3 and 4, the snapshot's `role`, and section 7's `host_view`
and `host_messages`. GOV-3 ([#134](https://github.com/coghex/tesserae/issues/134))
delivers section 5, section 6's settings and policy-aware character rules,
section 7's `rules_view` and `host_view` policy, the `rules_*` messages and
the snapshot's `policy_revision`, the policy and requirements in the
referee's context (section 10), and schema 8 without `host_requests`
(section 12). GOV-4 ([#135](https://github.com/coghex/tesserae/issues/135))
delivers section 6's `Administration`, section 7's `host_operations`,
`host_audit` and `inspect`, section 8's `host_command` with its replay, busy
rule and audit, and target digests (for GOV-5). GOV-5
([#136](https://github.com/coghex/tesserae/issues/136)) delivers sections 9,
10 and 11 and schema 9: proposals and host decisions, the referee's
proposal and host tools, and narrative facts in the original module.
GOV-6 ([#137](https://github.com/coghex/tesserae/issues/137)) delivers
section 15's CLI, and GOV-7 ([#138](https://github.com/coghex/tesserae/issues/138))
its browser controls.

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
    request_id TEXT,                  -- a transfer's retry identity (section 8)
    changed_at TEXT NOT NULL,
    UNIQUE (campaign_id, by, request_id)
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
(who decided what). Setup is ordered so nobody can enter half-adopted:

1. create the game (and its participants, from a seed if any);
2. `host campaign add --host` registers it, **inactive**, with its host;
3. `host campaign link` each player who already has a participant, the host
   included (the one step that writes the campaign file, as today; links are
   allowed while the campaign is inactive);
4. `host campaign activate`.

Nobody is admitted before step 4, so no player can be given a new
participant in place of their existing one. A failure before step 4 leaves
an inactive campaign; finishing the remaining steps completes it, and nothing
needs undoing. Adopting a legacy campaign follows the same order:
deactivate it, link any player not yet linked, `set-host`, activate.
Recovering an adopted campaign (replacing its host) is `set-host` alone.

## 4. Checking authority

`HostRegistry.authorize_campaign(context, campaign_id)` (the guard taken for
every protected lobby operation, lobby contract section 6) also yields the
campaign's `host_player_id`, read in the same read transaction. The caller's
**role** is `host` when it equals the context's player ID, else `guest`.
Because the guard's read lock blocks a registry writer, a transfer, a
`set-host` or a credential revocation is ordered wholly before or wholly after
each guarded operation. A transfer also needs the sender's own campaign
session, like any campaign request: it takes the registry exclusively, then
checks the sender's participant link and the campaign file's game identity
under a campaign write lock held until the registry commits, so an unlink or a
replaced file is ordered wholly before or after it too. An ended link or a
replaced file answers as for any other request (back to the lobby).

- **Direct host messages** (section 13) check the role inside the guard, then
  do their campaign work in one campaign transaction under it, in the lobby
  contract's lock order: registry, then campaign.
- **Host projections** (section 7) are read under the guard; a session whose
  role has become `guest` gets `not_campaign_host` and no host data.
- **Bridge calls with host authority** (section 10) take the same registry
  guard for the job's sender at the moment of the call, before any campaign
  transaction. The lobby host gives each campaign's service a
  registry-backed authority check for this; a single-game host has none, so
  host tools are absent there.
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
drops every host-only datum it holds on a `role` of `guest`. A transfer
tells every connection of the campaign at once. An operator's `set-host`
is made by another process, so each connection learns of it at its next
guarded check (its next request or delivery), and that request's reply is
sent first.

## 5. The campaign settings document

### Shape

```json
{"document_version": 1,
 "module": {"id": "trailhead", "version": "1", "state_version": 1},
 "settings": {"starting_stamina_bonus": 0},
 "exceptions": [
   {"exception_id": "x-1", "character_id": "c_4be0…",
    "settings": {"starting_stamina_bonus": 1},
    "reason": "Agreed: a mountain guide by trade.", "proposal_id": "hp-7f2a…"}
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
- Every field is required (`guidance` may be null). An exception is exactly
  `{exception_id, character_id, settings, reason}`: a lowercase
  `exception_id`, a character of this game, at least one setting, and a
  reason of up to 1,000 characters; one exception per character. An
  approved request's exception also carries its `proposal_id` (section 9). A document is at most
  256 KiB as canonical JSON.
- Values compare as JSON, never by a language's equality: `false` is not
  `0`, nor `1.0` `1`, for the binding or for a draft's effective settings.
- A document is checked whole, against the module and the game's state, when
  it is imported and again when it is activated; a refusal
  (`invalid_document`, with its `problems`) stores and changes nothing.
  Problems show client input only as a lowercase identifier, so a pasted
  token is never echoed.

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
| Import | `tesserae game rules import --database DB --file F` (operator) or `rules_import {request_id, content}` (host) | Validates the shape and binding, stores a `candidate`. Nothing live changes |
| List | `tesserae game rules list --database DB` or `rules_list` (host) | Every revision's number, state, origin, SHA-256, dates and who activated it; no content |
| Export | `tesserae game rules export --database DB [--revision N] --file F` or `rules_export {revision}` (host) | Writes the canonical JSON of a revision (default the active one) |
| Activate | `rules_activate {request_id, revision, expected_policy_revision}` (host), or `tesserae game rules activate --database DB N` (operator) | Validates the candidate against the module and current state (section 6); in one transaction makes it `active`, the previous active `superseded`, and records who. A different current policy revision is `stale_policy` |
| Reject | `rules_reject {request_id, revision}` (host) | A candidate becomes `rejected` |

Every host mutation carries a `request_id` and replays exactly (section 8,
"Replay"): a lost `rules_import` reply retried returns the same candidate,
never a second one, and a retried activation that succeeded returns its
success, not `stale_policy`. The operator commands need no replay. Each is
one transaction, so they are safe beside a running host, and activation
obeys the busy rule like the host's; `--module-dir` names the package of a
game whose module is not bundled.

An activation that changes any draft's effective settings adds one to that
draft's `draft_revision` in the same transaction, so a displayed approval of
the old revision is `stale_draft` (character contract section 9). It never
changes a completed character's sheet, rerolls anything or refreshes a
resource; changing an existing sheet is a separate host operation
(section 8). It does not advance the game revision, and sends no notice:
connections see the new `policy_revision` in their next snapshot, and a
draft's new revision when they next read it. Completion judges a draft
under the policy in force in its own transaction.

## 6. Module capabilities

Two new optional fields on `Module`, both independent of the character
capability and of each other.

### Settings and requirements

```python
@dataclass(frozen=True)
class CampaignSettings:
    version: int  # 1
    # The module's own settings.
    defaults: Callable[[], dict]
    # Pure: (settings, state) -> player-safe problems.
    validate: Callable[[dict, dict], list]
    # Pure: (policy, state, character_id) -> requirements.
    requirements: Callable[[dict, dict, str | None], dict]
```

- `validate(settings, state)` checks one character's or the campaign's
  effective settings (defaults, then the document's settings, then the
  character's exception, merged key by key) and returns player-safe problems;
  empty means valid. It is called for every candidate before activation, for
  each exception it carries, and for every request's proposed settings.
- `requirements(policy, state, character_id)` returns a JSON list describing
  what a character needs (or any new character, with None): entries with a
  stable `id`, a `kind` of `mechanical`, `narrative` or `optional`, a
  `description`, allowed values or methods (`choose`, `roll`) where they
  apply, whether it is `met` (true, false, or null when the module does not
  record it; omitted with no character), and a `support` note where an option
  needs further development. It is the one source for both the referee's
  context and the player's view; modules must not keep a second prose copy.
  In a character game the host lists its own `character.name` requirement
  first. Declaring an option does not prove every mechanic it touches in
  play is implemented.

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
    version: int  # 1
    # Pure: everything the host may see of the state.
    inspect: Callable[[dict], dict]
    # Host-only commands.
    operations: tuple[Command, ...]
    # Pure: (state) -> the operations available now, for clients and the referee.
    describe: Callable[[dict], list]
    # Pure: (state, target) -> the module data an operation on that target depends on.
    target_state: Callable[[dict, dict], dict]
    # Optional, pure: (command, args) -> the target {"kind", "id"} that operation changes.
    target: Callable[[str, dict], dict] | None = None
```

- `inspect(state)` is the host's view of module state: every character's
  data, NPCs, the world and the referee's preparation. Without it the host
  sees the referee context (`referee_context(state)`) and every sheet.
- `operations` are commands that run only in a host context (section 8).
  Their names share the module's command namespace and the catalog's checks
  (no `character.` prefix, no duplicates). Each states its target in its own
  validated arguments (for example `{"character_id", …}` or `{"npc_id", …}`);
  the host checks a `character_id` argument names a character of this game
  (else `character_unavailable`), and an operation on a draft adds one to
  its `draft_revision`, so a shown approval of it is stale.
  They receive the host context's sender (section 8) as
  `Action.participant_id`, never a player's participant, and may raise
  `ActionRejected`, like any command. `Action.policy` is the effective
  policy of a `character_id` argument's character, else the campaign's.
  They get no random source (`Action.operations` is empty): a correction
  never rolls. No player or referee runtime, and so no `act` or
  `referee_act` of any job, includes them.
- `describe(state)` lists, for each operation available now, `{"command",
  "description", "target_kinds", "args"}`, where `args` describes each
  argument (name, type, allowed values or range, required); like
  `CharacterRules.actions`, it is guidance for clients and the referee,
  never a validator. Clients build their administration controls from it
  (section 15).
- `target_state(state, target)` returns the part of the state an operation
  or request on `target` (`{"kind": "character" | "npc" | …, "id"}`, kinds
  the module names) depends on, or raises `ArgumentError` for an unknown
  target. The host hashes it (canonical JSON, SHA-256) as the target's
  **digest** (section 9). A module without `Administration` has the digest of
  `CharacterRules.sheet` for a character, and no other target kinds.
- `target(command, args)` names what an operation changes, from its
  validated arguments. A request for an operation must name exactly that
  target, so its digest guards what the approval will change. Without it,
  only an operation with a `character_id` argument (that character) can be
  requested.

`HOST_API_VERSION` stays 1; unknown capability versions are refused when the
module is resolved, as for `CharacterRules`.

**Adding capabilities to an existing module.** A game's module binding is
fixed and the catalog holds one version of each module, so a new module
version would leave existing games unable to load. A module may therefore
add these capabilities, and new optional state keys, under its current
version and state version only if a game with no settings document and no
new keys behaves exactly as before: the defaults reproduce the old rules, and
an absent key reads as empty. Anything else is a new module.

## 7. Reads

### For every player

Any admitted player of a campaign with settings may read these; they are
guarded like every lobby operation and change nothing.

| Message | Reply |
|---|---|
| `rules_view {character_id?}` | `{"type": "rules_view", "policy_revision", "settings", "guidance", "requirements"}`: the campaign's settings and guidance, and for `character_id` (one of the sender's own unarchived characters, else `character_unavailable`; the host may name any) its effective settings, `character_id` and its own `exception` (`{exception_id, settings, reason}` or null), and `requirements` (section 6). Never another character's exception or reason. Without the module capability, `settings` is `{}` and `requirements` null |
| `requests` | `{"type": "requests", "requests": [...]}`: a guest's own proposals (section 9), any state, with their target, payload, explanation, state and decision note. The host gets every proposal |

A guest's referee context and snapshot show the same projection for the
job's or the connection's character, and nothing more.

### For the campaign host

All host reads are guarded (section 4) and refused to guests with
`not_campaign_host`. None changes anything. Besides these, the host has
`rules_list` and `rules_export` (section 5).

| Message | Reply |
|---|---|
| `host_view` | `{"type": "host_view", "revision", "inspect", "characters": [every character with owner, phase, sheet], "participants", "pending_choices", "rulings", "policy": {"revision", "document"}}` |
| `host_messages {after_seq}` | Every conversation message after `after_seq`, private and creation ones included, in the referee transcript's pages (up to 200 messages or 32 KiB), with `next_after_seq`. Each is a conversation message as its recipients saw it; a private one adds `to`, `[{"participant_id", "name"}]` |
| `host_operations` | `Administration.describe(state)` with the game revision, so clients can offer each operation and its arguments (section 6); `[]` without the capability |
| `host_audit {after}` | `{"records", "next_after"}`: the audit records (section 8) after an ID, oldest first, each `{id, kind, host_id, player_id, via, job_id, target, detail, revision, created_at}`, in pages of up to 200 records or 32 KiB, with the `after` of the next page (null at the end) |

- Until a settings document exists (GOV-3), `host_view`'s `policy` is null.
  Without `Administration` (GOV-4), `inspect` is `referee_context(state)`, or
  each participant's view as the referee's `get_context` shows them.
- Host data is never pushed: the host asks. Ordinary guest projections,
  snapshots, `messages` and deliveries are unchanged, so the host's own
  connection still sees only its own private messages in its ordinary
  conversation view.
- Guest creation is told, in the creation prompt and the clients (section 15),
  that the campaign host can read creation conversations.
- Infrastructure is never included: no tokens, token digests, paths, bridge or
  session IDs beyond what the referee contract already shows players.

## 8. Host administration

**Running an operation.** `host_command {request_id, expected_revision,
command, args}` from the campaign host runs one `Administration` operation
through `GameStore.execute_host_command` in a host context: a trusted
`Decider(player_id, host_id)` that only the lobby host builds, from the
session's authenticated player once the registry guard has found it the
campaign host, and rechecked inside the command's transaction. Its commands are recorded with the sender `host:<player_id>` (like
the referee's `referee`, no participant or token is that ID), so each host
player has its own replay namespace, separate from its own participant's.
Replay, conflict and revision rules are the protocol's: an exact retry
returns the recorded result; a used ID with other content is
`request_conflict`; a moved revision is `stale_revision`. The game revision
covers every character, NPC and scene, so a stale editor cannot overwrite
intervening play or another host edit. A commit is announced to every
connection with the usual `revision` notice. Its `host_audit` row (kind
`operation`, the command and arguments in `detail`, and as `target` its
`character_id` argument, else its first other `…_id` argument) commits with
the command, or neither does.

**Replay.** Every mutation in this contract has an identity and an exact
retry:

| Mutation | Identity | Recorded in |
|---|---|---|
| `host_command` | `request_id` in the sender's host namespace `host:<player_id>` | `commands`, as any command |
| `rules_import`, `rules_activate`, `rules_reject`, `request_decide` | `request_id` in `host:<player_id>` | `host_mutations` |
| `request_cancel` | `request_id` in the requester's participant namespace | `host_mutations` |
| `campaign_host_transfer` | `request_id` in `host:<player_id>` | the registry's `campaign_host_changes.request_id` |
| Host bridge tools and `propose_host_decision` | `step_id`, mapped to `ref:<job_id>:<step_id>` (referee contract section 6) | `referee_steps`, and the effect's own record above |

```sql
CREATE TABLE host_mutations (
    sender TEXT NOT NULL,       -- "host:<player_id>" or a participant ID
    request_id TEXT NOT NULL,
    kind TEXT NOT NULL,         -- the message or tool
    content TEXT NOT NULL,      -- canonical JSON of its fields
    result TEXT NOT NULL,       -- the recorded reply
    created_at TEXT NOT NULL,
    PRIMARY KEY (sender, request_id)
);
```

- The record commits in the same transaction as the effect, so a mutation
  is either applied and recorded or neither. A refusal that changed nothing
  is not recorded, and the same ID may be retried.
- The same sender, ID and content returns the recorded reply, even if the
  state has moved since: a retried activation that succeeded returns its
  success. The same ID with other content is `request_conflict`.
- A replay requires the authority the original needed, checked as for a new
  request, except `campaign_host_transfer`, whose recorded reply replays to
  the former host (section 3's `campaign_host_changes.request_id`).
- Notices are not repeated on replay.

**The busy rule.** While a referee job is running, `host_command`,
`rules_activate` and `request_decide` (section 9) are refused with the
existing `referee_busy` error and nothing changes; reads, imports and
rejections are unaffected. The one exception is the host tools of the
running job when it is the host's own (section 10); the host's own client
messages still wait for the job to end.

**Audit.** Every host mutation, by message or by the referee for the host,
adds one row to the campaign's `host_audit` table in the same transaction as
its effect:

```sql
CREATE TABLE host_audit (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('operation', 'import', 'activation', 'rejection', 'decision')),
    host_id TEXT, player_id TEXT NOT NULL,     -- the deciding campaign host; null and "operator" for the operator
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

A **proposal** is a durable request for a host decision. Its identity is
`proposal_id`, made by the host; a message's `request_id` stays its retry
identity (section 8).

```sql
CREATE TABLE host_requests (
    -- "hp-" and 12 hex digits, made by the host.
    proposal_id TEXT PRIMARY KEY,
    -- The proposing job, and its step's mapped ID ref:<job_id>:<step_id>.
    job_id TEXT NOT NULL,
    step_request_id TEXT NOT NULL UNIQUE,
    requester_participant TEXT NOT NULL,
    requester_player TEXT,
    -- The target, {"kind", "id"}, or null for a campaign-wide request.
    target TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('exception', 'campaign_setting', 'operation')),
    -- JSON: the exact settings, or command and args.
    payload TEXT NOT NULL,
    explanation TEXT NOT NULL,
    policy_revision INTEGER NOT NULL,
    -- A draft target's draft_revision, else null.
    target_revision INTEGER,
    -- The target's digest (section 6) when proposed, else null.
    target_digest TEXT,
    state TEXT NOT NULL CHECK (state IN ('pending', 'approved', 'denied', 'cancelled', 'superseded', 'stale')),
    created_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT,
    effect TEXT
);
```

- **Proposing.** The referee calls `propose_host_decision` (section 10) in
  any job; the requester is the job's sender, and the target defaults to the
  job's character. A campaign-wide request must say so (`"scope":
  "campaign"`; any other is `"scope": "target"`). An exception is for one of
  the sender's own unarchived characters only; an operation names its
  target, which must be exactly what the operation changes by the module's
  `target` rule (section 6), else `invalid_request`. The explanation is 1 to 1,000 characters. The payload is validated now, as it will be at decision time
  (`validate` for settings; the operation's own `validate` for an
  operation); an unsupported request is refused to the referee, not stored.
  The request records the policy revision, a draft target's draft revision
  and the target's digest. It is a journaled step: the same `step_id` with
  the same content returns the same proposal; other content is
  `request_conflict`.
- **Superseding.** A new pending proposal from the same requester for the
  same target and kind supersedes the older pending one.
- **Deciding.** `request_decide {request_id, proposal_id, decision:
  "approve" | "deny", note}` from the campaign host. Approval revalidates
  against the current state and policy. The proposal becomes `stale`, and
  nothing changes, if since it was proposed the policy revision has moved,
  a draft target's draft revision has moved, or the target's digest differs:
  a ready character, NPC or scene changed by play or another edit is never
  overwritten by an approval made against its old state. A new proposal is
  needed. An approved exception or campaign setting is a new active document
  revision (origin `request`); an approved operation is that exact operation
  run in the host context. Either way the effect, the proposal's state, the
  `host_mutations` record and the audit row commit together. Denial changes
  no mechanics. A second decision on a decided proposal, under another
  `request_id`, is `proposal_decided`.
- **Cancelling.** The requester's `request_cancel {request_id, proposal_id}`
  (a guest's only control) or the host's denial. A target archived, or a
  requester whose link ended, makes a pending proposal `cancelled`, in the
  same transaction as the archive or the unlink. A decision or a cancel of an
  unknown, foreign, cancelled, superseded or stale proposal is
  `proposal_unavailable`.
- **The effect.** An approved exception is merged into the character's
  exception (its settings key by key, the request's explanation as its
  reason, and its `proposal_id`); an approved campaign setting into the
  document's `settings`. The new revision is checked whole, like any
  activation, and the reply's `effect` is `{"policy_revision", "drafts"}`.
  An approved operation runs under the request ID of its proposal in the
  host's namespace, and its `effect` is `{"revision", "result"}`. The
  decision's audit row (kind `decision`) records the outcome and effect; an
  operation also writes its own.
- **Transfer.** Pending proposals stay; only the current campaign host can
  decide them.
- **No standing grant.** An approval applies its exact effect once. It never
  lets a guest, or a later job, use host tools, and it never performs the
  guest's next action: the guest's following input does that, under the new
  policy.

### Waiting

`propose_host_decision` does not wait: the job replies and ends, as every job
does, and the proposal stays pending in the store. The requester's referee
projection gains `requests`, once it has made any: its pending proposals and
its ten most recently decided, each `{proposal_id, kind, target, payload,
explanation, state, policy_revision, requester: {participant_id, name},
created_at, decided_at, decision_note, effect}`. The `requests` read
(section 7) and an unsolicited `proposal` notice (`{"type": "proposal",
"proposal_id", "state"}`, to the connections of the host and of the
requester only, decided when it is sent, while their access holds) tell the
people involved. A proposal, a decision and a cancel each send one. Other
players keep playing; the referee is busy only while a job actually runs.

## 10. The referee

**Context.** In a game whose module has settings, or that has an active
document, `get_context` adds `policy` (the revision, the effective settings
for the job's character, `{}` without the capability, and the campaign's
`guidance`, which reaches the referee either way), `requirements` for the
job's character (section 6; a setup job's are any new character's; null
without the capability) and, once the sender has made any, its pending and
recent proposals as `requests` (section 9's projection). `list_actions`
describes creation and play under the same policy. A job of the campaign
host adds `host: true` and the campaign's pending proposals as
`pending_requests`. In a game whose module has settings or administration,
every job's prompt says how to ask the host (`propose_host_decision`, then
end the job) and that a ruling, a choice or a claim approves nothing.

**New tools.**

| Tool | Who | Arguments | Effect |
|---|---|---|---|
| `propose_host_decision` | Any job | `step_id`, `kind`, `scope`, `target`, `payload`, `explanation` | Section 9 |
| `host_operations` | The campaign host's job | none | `Administration.describe(state)` (section 6) |
| `host_act` | The campaign host's job | `step_id`, `command`, `args` | Runs an `Administration` operation for the host, as section 8 |
| `host_decide` | The campaign host's job | `step_id`, `proposal_id`, `decision`, `note` | Section 9's decision |
| `host_propose_rules` | The campaign host's job | `step_id`, `content` | Stores a candidate document (origin `referee`); activation stays a separate, explicit host operation |
| `host_activate_rules` | The campaign host's job | `step_id`, `revision`, `expected_policy_revision` | Section 5's activation, when the host has explicitly asked for it |

- **Journal and retries.** Every mutating tool here is a journaled step
  under the referee contract's section 6 rules: `step_id` is unique within
  the job across all step kinds, and maps to the request ID
  `ref:<job_id>:<step_id>`. `propose_host_decision` is a step of kind
  `propose`, in the job sender's namespace; the host tools are steps of kind
  `host`, in the host namespace `host:<player_id>`. Reconciliation looks the
  mapped ID up where its effect is recorded (section 8, "Replay"; for a
  proposal, `host_requests.step_request_id`): found is `committed`, with the
  recorded result; absent is `not_committed`. An exact retry replays; other
  content is `request_conflict`.
- **A host job** is a job whose input came from a session of the campaign
  host player: the lobby host gives the job's grant that session's
  `HostPlayerContext`, held in memory only. Each host tool takes the registry
  guard for that context at the call (section 4) and refuses with
  `not_campaign_host` if the designation or credential has changed; a guest's
  job never has these tools. The prompt of a host job says that host tools
  act only on the host's explicit instruction, only with the exact proposal
  ID or revision, and that referee-only preparation never goes in a table
  message. Host tools are available in a host job of any mode.
- **Inside the host's job** the busy rule does not apply to these tools: the
  job is the running work.
- **The host's own exceptions** need no request: on the host's instruction,
  its job writes the exception with `host_propose_rules` and applies it with
  `host_activate_rules` (or the host uses the `rules_*` messages), audited as
  any host change.
- **Host results stay with the host through recovery.** A step of kind
  `host` is a **host result**, whatever the job's mode. If its job fails or
  is interrupted before a reply, the result is unnarrated, but it is listed
  in `earlier_results` only for later jobs of the current campaign host, and
  only such a job's reply may list it in `narrated`. A guest's job (setup,
  creation or play) never sees it and may not narrate it
  (`malformed_reply`), so a preparation edit or an inspection-derived result
  never reaches a guest through recovery. A host result is listed only to
  later jobs of the same player while it is the campaign host (it ran as
  `host:<player_id>`), so a transfer ends every unnarrated host result: the
  new host sees them in `host_audit`, not as results to narrate.
- **Proposals stay with their requester.** A `propose` step is the
  requester's own result: it is listed to, and may be narrated by, only a
  later job of its requester (under the usual mode rules: a creation job's
  proposal is a creation result of its draft). Any other job, the host's
  included, could tell it only to the table, so it never sees it as a
  result; the host reads requests through `pending_requests`.
- **The host's own session.** Each host tool also checks, inside its own
  transaction, that the host's participant session in this game still holds
  (its link, and the game in its file), as any guarded operation does; a
  host unlinked mid-job has no host tools and its `get_context` is a
  guest's.
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
- **Campaign schema 8** (GOV-3): `campaign_documents` (section 5),
  `host_mutations` and `host_audit` (section 8). Nothing else is rewritten.
- **Campaign schema 9** (GOV-5): `host_requests` (section 9), and
  `referee_steps.kind` gains `propose` and `host` (the table is rebuilt to
  widen its check, every row copied).
- A campaign without host data behaves exactly as at schema 7.

## 13. Messages

All version 1, inside a selected lobby campaign. Each reply follows the
existing pairing rules; errors carry the message's `request_id`, or null
for a read without one (paired by order, as every reply is).

| Message | Fields | Who | Reply |
|---|---|---|---|
| `rules_view` | `character_id` (optional) | Anyone | `rules_view` (section 7) |
| `requests` | | Anyone | `requests` (section 7) |
| `host_view` | | Host | `host_view` (section 7) |
| `host_messages` | `after_seq` | Host | `host_messages` |
| `host_audit` | `after` | Host | `host_audit` |
| `host_operations` | | Host | `host_operations {revision, operations}` |
| `host_command` | `request_id`, `expected_revision`, `command`, `args` | Host | `result` or an error |
| `rules_list` | | Host | `rules_list {policy_revision, revisions}` |
| `rules_export` | `revision` (optional) | Host | `rules {revision, state, content}` |
| `rules_import` | `request_id`, `content` | Host | `rules {revision, state, request_id}` |
| `rules_activate` | `request_id`, `revision`, `expected_policy_revision` | Host | `rules {revision, state, request_id}`; `stale_policy` carries `policy_revision` |
| `rules_reject` | `request_id`, `revision` | Host | `rules {revision, state, request_id}` |
| `request_decide` | `request_id`, `proposal_id`, `decision`, `note` (null or up to 1,000 characters) | Host | `proposal {proposal_id, state, effect, request_id}`; `state` is `approved`, `denied` or `stale` |
| `request_cancel` | `request_id`, `proposal_id` | The requester | `proposal {proposal_id, state, request_id}` |
| `campaign_host_transfer` | `request_id`, `player_id` | Host | `role {role: "guest", request_id}` for the old host; `invalid_request` if the player is the host already or lacks an active grant or token |

Every mutation replays exactly (section 8, "Replay"). The snapshot of a
campaign with a designated host gains `role` (`host` or `guest`) and, from
GOV-3, `policy_revision` (in any game whose module has campaign settings);
its referee projection gains `requests` (section 9).
Unsolicited: `role` (section 4) and `proposal` (section 9).

New errors (a host mutation during a job reuses `referee_busy`; a reused
`request_id` with other content, `request_conflict`):

| Code | Cause |
|---|---|
| `not_campaign_host` | A host message or tool from anyone but the current campaign host, or a campaign with no designated host |
| `stale_policy` | `rules_activate` against another policy revision |
| `invalid_document` | A document that fails its shape, binding or module validation; carries `problems` |
| `proposal_unavailable` | An unknown, foreign or no longer pending proposal (one code for all) |
| `proposal_decided` | A second decision on a decided proposal |

Single-game hosts have no host messages: each is refused with
`not_campaign_host`, and a game is played there exactly as before.

## 14. Illustrative module

Trailhead, still version 1 under section 6's rule: a setting
`starting_stamina_bonus` (0 to 2, default 0) added at completion;
`requirements` listing `character.name` (the host's), `trait` (mechanical,
choose), `stamina` (mechanical, roll), `background` (narrative) and `home`
(optional); and administration operations `trailhead.set_stamina
{character_id, stamina}`, `trailhead.set_trait {character_id, trait}` (the
maximum recomputed by the module's rule), `trailhead.set_gear {character_id,
gear}`, `trailhead.describe {character_id, description}`,
`trailhead.npc_set {npc_id, name, note, visible}`, `trailhead.scene_set
{description}` and `trailhead.prep_set {note}` (referee-only preparation),
with `inspect` returning every walker, NPC, the scene and the preparation
(see [trailhead.md](../trailhead.md)).

A guest's referee proposes `exception {starting_stamina_bonus: 1}` for their
draft and ends the job; the host's `request_decide approve` makes revision 2
active with that exception and bumps the draft's revision; the guest's next
look at the sheet shows the new revision, and approval completes with the
bonus. A denial leaves revision 1 and the draft unchanged.

## 15. Clients (the CLI delivered by GOV-6, the browser by GOV-7)

**CLI** (`connect --lobby`, inside a campaign):

```text
/role              your role here: host or guest                 (snapshot)
/rules             the rules and your character's requirements   (rules_view)
/rules list        host: every revision, candidates included     (rules_list)
/rules show [N]    host: revision N's document, or the active one (rules_export)
/rules import F    host: import a file as a candidate            (rules_import)
/rules activate N  host: activate candidate N (asks to confirm)  (rules_activate)
/rules reject N    host: reject candidate N                      (rules_reject)
/requests          guest: yours; host: all, with IDs             (requests)
/decide ID approve|deny [NOTE]   host                            (request_decide)
/cancel ID         guest: withdraw your pending request          (request_cancel)
/inspect [TARGET]  host: the host view, or one character's       (host_view)
/history-all [N]   host: every message, private ones included    (host_messages)
/audit [N]         host: the audit records after N               (host_audit)
/admin             host: the operations available now            (host_operations)
/admin COMMAND ARGS-JSON   host: run one (asks to confirm)       (host_command)
/transfer PLAYER   host: hand the campaign over (asks to confirm) (campaign_host_transfer)
```

The client checks nothing about authority: every command is sent, and the
host refuses what the sender may not do (`not_campaign_host`, with a hint).
A confirmation repeats the command with `confirm` (`/admin confirm`,
`/rules activate N confirm`, `/transfer PLAYER confirm`) and sends exactly
what was shown: `/admin` the game revision the prompt named, and `/rules
activate` the settings revision last seen, so a change made meanwhile is
refused as `stale_revision` or `stale_policy`, never applied over it. A
confirmation waiting is forgotten when the role changes.

Each mutation's `request_id` is made once per attempt. `/retry` resends the
last one unchanged only after `referee_busy` or `internal_error` (nothing was
stored), and never after the campaign, the participant session or the role
has changed since: any change of role ends it for good, even if the role
later comes back. Any other reply, or a new mutation or input, also ends it.
A reply saying the campaign is no longer available returns the client to
the lobby, with nothing of the campaign kept (role, revisions, pending
confirmations or retries).
Requests, the role and the settings revision are read from the host again
on every join or reconnect; nothing is kept locally. With `--json`, every
message is one JSON line on stdout and the client's own notes go to
stderr.

**Browser:** Settings gains the campaign role and the rules and requirements
(`rules_view`), and, for the host, Rules (`rules_list`, the active document,
import and activate), Requests (approve or deny each, with its exact
content) and Administration (pick a target and an operation from
`host_operations`, fill its described arguments, confirm). A retried
mutation reuses its `request_id`. The Console shows a guest's
pending requests and outcomes, and creation says that the campaign host can
read it. Host data is cleared on a `role` of `guest`, on leaving, on lost
access and on reconnect until reauthorized; nothing is stored in the browser.

As delivered (GOV-7): Settings' "This campaign" panel shows the role (a
guest is told the campaign host can read its private creation and
conversation, and no other guest can) and `rules_view` for the selected
character. The host's panels follow it, shown only while the snapshot or the
last `role` says host: Requests (each request's exact content, an optional
note, Approve and Deny), Rules (the revisions; Show, Activate… after a
confirmation naming the revision it replaces, Reject; import of a JSON
file), Administration (an operation from `host_operations`, its arguments
as one JSON object prefilled from the module's description rather than a
generated form, Review showing exactly what runs at which revision, then
Run it), Inspection (`host_view`, `host_messages` and `host_audit`, in
their own panel, never merged into the Console's conversation) and Hand the
campaign over (a player ID, then a confirmation; the reply's `role` of
`guest` clears every host datum at once). The Console lists the
player's own requests from its referee projection, with Withdraw for a
pending one. Nothing is changed without a button press; reads happen on
entering, on a `role` or `proposal` notice and on Refresh. A change refused
as `referee_busy` or `internal_error` can be resent exactly (Retry exactly);
a stale one says to review again. A `proposal` notice re-reads the sheet of
a shown draft, whose revision an approval may have moved. All host data is
also cleared when the connection drops, and any reply that arrives after it
was cleared is dropped. The rules are the actor's: they are read again when
the actor changes, and a reply asked for an earlier actor is dropped. The
messages and the audit are read a page at a time (Show more), to the end.

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
  and their refusals (no host grant revocation, unknown player); setup
  linking existing participants before activation; transfer replay; role in
  the guard ordered against transfer and revocation; host reads refused to
  guests and after transfer; offline host; rotation keeps the designation;
  no-character host inspection; `role` notices racing requests.
- **GOV-3:** schema 8; import, export, list and activation round trips;
  exact replays (a retried import makes no second candidate; a retried
  successful activation returns success); stale and unauthorized
  activation; `rules_view` showing a guest no other character's exception;
  unsupported keys and wrong bindings refused with nothing changed;
  effective policy per character; activation bumping drafts and never
  touching completed sheets; version 1 modules unchanged.
- **GOV-4:** host operations on another character, NPCs and scene;
  `host_operations` discovery; replay and stale revisions; the busy rule;
  audit atomicity; guests and guest jobs refused; history preserved.
- **GOV-5:** the proposal lifecycle (propose, supersede, approve, deny,
  cancel, stale, replay, conflict, competing decisions, transfer); approval
  refused as stale when a ready character, NPC or scene target changed
  meanwhile; host tools journaled and reconciled; an interrupted host job's
  results listed only to the host's later jobs, never to a guest's, and
  ended by a transfer; the job ending and others playing meanwhile; host
  tools reauthorized per call and absent from guest jobs; a ruling, a choice
  or a claim never approving anything.
- **GOV-6 and GOV-7:** as their issues list, against this contract.

## 18. Open questions

These are not owner decisions; each has a default that stands until the
owner says otherwise.

- **Live private messages to the host.** Default: the host reads them on
  request (`host_messages`), not pushed live.
- **Operator activation.** Default: allowed (`tesserae game rules
  activate`) for setup with the owner's assistant, audited as `operator`.
