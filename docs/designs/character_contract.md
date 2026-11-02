# M2-3 character contract (CHR-1)

Status: specification for [#111](https://github.com/coghex/tesserae/issues/111)
under tracker [#110](https://github.com/coghex/tesserae/issues/110), written
before any implementation. It turns the
[character lifecycle design](character_lifecycle_design.md) and the owner's
decisions into exact rules for CHR-2 to CHR-6: the stored character records,
the optional module capability, the actor every input and job keeps, how the
referee guides creation, and the host messages and client states.

Implemented so far: CHR-2 (#112), sections 2, 3, 5 (for players' commands),
12 and 13 in `tesserae/modules.py`, `tesserae/store.py` and `tesserae/seed.py`,
with the original fixture module `trailhead` (section 14's illustration,
built as [trailhead.md](../trailhead.md) describes); and CHR-3 (#113), the
referee's side of sections 5 to 8 in `tesserae/referee/` (frozen actors,
the bridge's mode table, actor-scoped choices and last inputs, the per-job
prompts and reply schemas, private creation and its results, and
introduction). The standing referee instructions (`AGENTS.md`) are
unchanged: each job's prompt carries its mode's rules, so no `referee setup`
rerun is needed. CHR-4 (#114) added the host's side of sections 9 to 11 in
`tesserae/protocol.py`, `tesserae/server.py` and `tesserae/lobby.py`: the
character messages and errors, each connection's own selection and actor,
`say`'s actor, scoped snapshots and status, the `actor` notices, the `select`
gate for older clients and the single-game refusal. The clients (section 15)
are not yet: until CHR-5 and CHR-6, today's CLI and browser are told to update
when they select a character campaign.

It extends the [lobby contract](lobby_contract.md) and the
[referee contract](referee_contract.md). Everything they say still holds for
campaigns without characters, and for character campaigns wherever this
contract does not say otherwise. All examples use `trailhead`, an illustrative
original module (section 14); CHR-2 builds the real fixture.

## 1. Decisions this contract follows

Owner decisions (D-24, tracker #110, 2026-11-01):

- A player owns any number of characters in a campaign; each connection
  chooses its own. Common records give identity and lifecycle (`draft`,
  `ready`, `archived`); the module owns fields, creation rules, resources and
  turns. There is no universal sheet and no state patch.
- Active play needs a completed character. Before that a player can see their
  own characters, ask setup and rules questions, or create or resume a draft.
  Completing means the player approves a validated draft; the referee then
  brings the character into the scene.
- An accepted input keeps its actor. Switching characters never grants a turn
  or refreshes a resource. Creation releases the referee between answers.
- Deleting archives: history stays. Ownership cannot be changed, and transfer,
  shared control, restoring and hard deletion are later work. Names are labels.
- **Creation is private.** Every creation exchange is private to its player
  and visibly marked so; once in play the character uses ordinary table
  conversation. This is a stated exception for creation only: the referee
  contract's rule (unmarked conversation goes to the table, only explicitly
  private exchanges are private) is otherwise unchanged. The host enforces it
  (section 8), not the client's private flag.
- **Lobby only.** Characters are served by the lobby host. A single-game host
  refuses a character game (section 11) and serves every other game as today.
- **Characters belong to their campaign participant**, not to the host player.
  A relink therefore changes which characters a player sees (section 10).

Engineering choices made here, within that scope:

- A game is a character game exactly when its module declares the character
  capability (section 3). There is no optional-character mode: in a character
  game, play always needs a ready character. A game's module binding never
  changes, so a game is a character game from creation or never.
- The connection's mode follows from what it has selected: nothing selected
  is `setup`, a draft is `creation`, a ready character is `play`. One table
  (section 5) says what each mode allows, for raw commands and the referee's
  tools alike.
- Lifecycle operations are host messages recorded in the game's command
  history, so they get the existing exact replay and revision rules.
- The player approves; the referee never completes a character.
- A `say` in a character game names the actor the client believes it has,
  as `expected_revision` names a revision, so a draft completed elsewhere can
  never turn private creation words into table play.

## 2. Characters

A character is one row in the campaign's own database (section 12):

| Field | Meaning |
|---|---|
| `character_id` | `c_` and 16 lowercase hexadecimal digits, made by the host; never reused |
| `participant_id` | The owner, set at creation, never changed |
| `name` | A display label, 1 to 100 characters like a participant name, or null for an unnamed draft. Never an identity |
| `phase` | `draft`, `ready` or `archived` |
| `draft_revision` | 1 at creation; +1 for each committed creation step on the draft; unchanged once ready, and shown as null in messages from then on |
| `created_at`, `completed_at`, `introduced_at`, `archived_at` | Times; `introduced_at` is set when the referee has brought the character into play (section 7) |

Transitions, each one host operation (section 9):

```text
(none) --character_create--> draft --character_complete--> ready
draft  --character_archive--> archived
ready  --character_archive--> archived
```

Nothing else changes `phase`. There is no `ready` setter, no way back from
`archived`, and no transition a client, a module or the referee can make
except through these. The module's own data for a character lives in the
module's JSON state, keyed by `character_id`; the host never reads it.
Metadata and module state change in the same transaction (section 9).

## 3. The module capability

A module opts in with one new optional field, `Module.characters`, holding a
`CharacterRules` (in `tesserae/modules.py`):

```python
@dataclass(frozen=True)
class CharacterRules:
    version: int  # 1; any other is refused
    start: Callable[[Action], Outcome]  # a new draft's module data
    steps: tuple[Command, ...]  # creation commands
    problems: Callable[[dict, str], list[str]]  # pure: why not complete yet
    complete: Callable[[dict, str], dict]  # pure: the draft's playable state
    sheet: Callable[[dict, str], dict]  # pure: the owner's view of one character
    archive_veto: Callable[[dict, str], str | None]  # pure: a reason, or None
    actions: Callable[[dict, str], list] | None = None  # play: what it may do
    creation_actions: Callable[[dict, str], list] | None = None  # next steps
    # state, participant IDs, {character ID: owner participant ID}
    validate_seed: Callable[[dict, frozenset, Mapping], None] | None = None
```

- `Action` gains one field, `character_id: str | None = None`. The host sets
  it from the frozen actor (section 6), never from a message. Modules without
  the capability always see None, and their hooks keep their current
  signatures and meaning.
- `start` runs like a command: `action.args` is `{}`, it raises
  `ActionRejected` before any draw, and its outcome replaces the state. Any
  draws go through the same operations and are recorded in the result, like a
  command's.
- `complete(state, character_id)` is pure and deterministic: it returns the
  replacement state for the draft becoming playable (deriving values from
  choices already made, say) and gets no random source. Anything that needs a
  roll is a creation step, so every roll is on the sheet the player approves.
  Only `problems` can refuse completion; a `complete` that raises is a module
  failure (`internal_error`, nothing changed), not a veto.
- `steps` are ordinary `Command`s that run only in creation mode, on the
  selected draft (`action.character_id`). Their names share the module's
  command namespace; the catalog refuses a duplicate across `commands`,
  `referee_commands` and `steps`, and any name starting `character.`, which
  the host reserves. The host adds one step every character game has,
  `character.name {"name"}`, which sets the draft's label under section 2's
  rules (an invalid name is `invalid_request`) and counts as a step.
- `problems(state, character_id)` lists player-safe reasons a draft cannot be
  completed yet ("Choose a trait."); an empty list means it can be.
- `sheet(state, character_id)` is what the owner sees of that character, as a
  JSON object; it is never sent to anyone else.
- `archive_veto(state, character_id)` returns a player-safe reason to refuse
  archiving now (for example, during an encounter), or None.
- `actions` and `creation_actions` describe, for the referee, what a ready
  character may do now and which creation steps remain, with stakes. Without
  them `list_actions` gives the command or step names.
- `validate_seed` checks seeded state against the participants and a mapping
  of character ID to owner (section 13). A character module uses it instead
  of `Module.validate_seed`, which must then be None (the catalog refuses
  both).
- `view(state, participant_id)` stays the participant's view of the shared
  game; a character game's snapshot adds the selected character's sheet
  beside it (section 9). `referee_context(state)` must leave out drafts' data:
  the host adds the job's own draft itself (section 7).

Modules may veto: starting a draft (`start`), a creation step, completion
(`problems`), archiving (`archive_veto`), and any gameplay
action, as today. They cannot veto or react to selection, which changes
nothing.

`HOST_API_VERSION` stays 1: the field is optional and old modules are
unaffected. A `CharacterRules.version` other than 1 is a `ModuleError` when
the module is resolved.

## 4. Modes

| Mode | Selected | Meaning |
|---|---|---|
| `setup` | Nothing (the default after entering a campaign) | The participant, without a character: questions only |
| `creation` | One of the participant's drafts | Building that draft with the referee, privately |
| `play` | One of the participant's ready characters | Playing as that character |

Games without characters have no mode: their inputs, jobs and records stay
as they are (mode null) and behave exactly as today.

## 5. What each mode allows

The same rules apply to the player's raw `command` (a character game without
referee mode) and to the referee's tools in a job of that mode:

| | `setup` | `creation` | `play` |
|---|---|---|---|
| `say` | Allowed; table unless marked private | Allowed; always private (section 8) | Allowed; table unless marked private |
| Module commands (`command`, `act`) | Refused | Refused | Allowed, as this character |
| Creation steps (`command`, `act`) | Refused | Allowed, on this draft only | Refused |
| Referee-only commands (`referee_act`) | Refused | Refused | Allowed |
| `record_ruling` | Allowed | Allowed | Allowed |
| Reply audiences | `table`, `sender` | `sender` only | `table`, `sender` |
| `choices.ask` and `choices.resolve` may address | The sender's setup scope | This draft only | Any ready character, or any participant's setup scope |
| `narrated` may list | Earlier play results | This draft's creation results | Earlier play results |

A refusal is `mode_forbidden` for a player's command, or the bridge's tool
error `mode_forbidden`, with nothing journaled or run. So a no-character
question can never move the fiction, a draft can never spend a ready
character's resources, and play can never edit a draft. The lifecycle
operations (section 9) are separate host messages, allowed in every mode.

## 6. The frozen actor

The actor is (campaign, participant, character or null, mode). The host
establishes it from the connection's selection when it accepts an input, and
stores it with that input. From then on:

- The job's prompt, `act` and `list_actions` use the stored actor. Nothing
  looks up the current selection later: the sending connection may switch or
  leave and the job is unchanged.
- `act` runs as the participant with `Action.character_id` set to the stored
  character. The command history records the character in the command's
  content (`"character_id"`, present only in character games), so replay
  compares it.
- `referee_act` stays in the `referee` context, with `character_id` None.
- **Exact retries.** Request IDs stay unique within the campaign's
  participant namespace. A `say` replays only if its text, its effective
  privacy (always true in creation), its character and its mode all equal the
  stored input's. Anything else under the same ID is `request_conflict`: an
  input is never replayed into another character or mode, and never runs again
  as a new action. A raw command's retry compares its character the same way.
  This holds within one participant; across a relink only the client's rule
  protects a resend (section 10).
- **Pending choices** are scoped to (participant, character): one per
  character, and one for the participant's setup scope. A connection sees only
  the choice of its current actor. Completion and archiving end the
  character's choice.
- **The last input** in `referee_status` and snapshots is the participant's
  latest input with the connection's current character and mode. A client
  never sees another character's last input as its own.
- **Revocation** follows the lobby contract: a job accepted before its
  player lost access keeps its actor and finishes; nothing new is accepted.
  Archiving waits for no job: it is refused while one runs (section 9).
- **History.** Records from before schema 7 and from games without characters
  keep a null character and mode. They are history: a character-mode retry
  never matches one.

## 7. The referee in a character game

**Prompts** name the actor and its rules. For example:

```text
New input (job j-7) from Bob (p_…), creating draft c_… "Unnamed" (revision 3),
privately to you: "I'd like someone who reads the weather."
Mode: creation. Use only this draft's creation steps (list_actions). Reply only
to the sender. End by asking the next creation question with choices.ask, or
say the draft is ready for Bob to approve. Call get_context before anything else.
```

A play input says "from Alice (p_…) as Mara (c_…), to the table"; a setup
input says "from Bob (p_…), who has no character selected: answer questions
only, nothing in the game may change". The first play input of a ready
character whose `introduced_at` is null adds "Mara has just been completed
and is not yet in the scene: bring her in under the module's scene rules
before anything else." `introduced_at` is set when that job's reply is
published.

**Tools.** The same seven, with these changes in a character game:

- `get_context` adds `characters` (every ready character: ID, name, owner,
  introduced or not), and `input` gains `actor` (mode, and the character's ID,
  name, phase and draft revision). It adds `sheet`, the job's own character's
  sheet, in creation and play. Other participants' drafts are never listed,
  and `pending_choices` omits other drafts' questions.
- `list_actions` follows the mode: `actions` or `creation_actions` for the
  job's character, nothing to act on in setup.
- `act` and `referee_act` follow section 5's table.
- **Choices name exact actors.** The reply schema's `choices.ask` entries
  are `{"actor", "question"}`, and `choices.resolve` is a list of actors, in
  the same form: a character ID (that character's question), or a participant
  ID (only that participant's setup question, never their characters'). Each
  entry clears or sets exactly one scope. Asking and resolving follow the same
  mode rules (section 5): a setup job may address only its sender's setup
  scope; a creation job only its own draft; a play job any ready character or
  any participant's setup scope, but never a draft. An unknown, archived or
  out-of-mode actor in either list is `malformed_reply`. A draft's question
  otherwise ends only by completion or archiving. Games without characters
  keep `participant_id` and participant-wide resolution.

**Creation, one answer at a time.** Each creation answer is one input and
one job: the referee reads the draft, runs creation steps, saves progress (the
steps commit as they run), asks the next question with `choices.ask` for the
draft, and the job ends. Busy is released while the player thinks; anyone else
may be answered in between. While a job runs, the existing per-campaign busy
refusal applies; there is no queue and no second session. The draft's pending
question survives other players' jobs, like any choice.

**Completion and entry.** When `problems` is empty the referee tells the
player the draft is ready to approve. Only the player completes it (section 9,
`character_complete`), at the draft revision they were shown. The referee
then brings the character in on their first play input, under the module's
scene rules. Selecting changes nothing. Completing changes only the draft's
own data, deterministically (section 3), and never advances time, takes a
turn or draws.

**Rulings** may be recorded in any mode; they change nothing mechanical.

## 8. Creation privacy, enforced by the host

- A `say` accepted in creation mode is stored and delivered as private to
  its sender, whatever `private` flag it carried. Its `message` (and every
  replay of it) has `audience: "private"` and a `creation` marker (section 9).
- A creation job runs with the **creation reply schema**: the section 7 schema
  of the referee contract with `audience` limited to `["sender"]`, and
  `choices.ask` and `choices.resolve` each limited to one entry. So the model
  cannot write a table message in a creation job.
- The host also validates every creation reply: a `table` message, an ask or
  resolve for any actor but the job's own draft, or a narrated step that is
  not this draft's creation result (below) is `malformed_reply`. The job
  fails and nothing from it is published, as for any unusable reply.
  Nothing is redirected: the host never guesses what the referee meant.
- **Creation results stay private through recovery.** A step committed in a
  creation job is a creation result of its draft. If the job fails or is
  interrupted before a reply, its creation results are unnarrated, as any
  job's are (referee contract section 10), but they are listed in
  `earlier_results` only for later creation jobs of the same draft, and only
  such a job's reply may narrate them: privately, to the draft's owner. Any
  other job (another player's, a setup or play job, even the same player's in
  another mode) neither sees them nor may list them in `narrated`
  (`malformed_reply`). Completing or archiving the draft ends them: the
  approved sheet, or the archive, supersedes them, and they are no longer
  listed.
- Today, by contrast, a private input's job may also write table messages
  (referee contract section 7). That stays true in setup and play.
- The referee sees creation exchanges as it sees every private exchange: in
  its transcript, so a rebuilt session is complete. Its prompt tells it they
  are private. That is the existing trust in the referee, not a new channel.

Tests (CHR-3): a creation `say` with `private: false` is stored, delivered and
replayed as private; a creation reply with a `table` message, or one asking or
resolving another actor, fails the job with nothing delivered to anyone; a
creation step whose job then fails is absent from another player's next
job's `earlier_results`, a reply there that lists it in `narrated` fails, and
the draft's next creation job lists it and narrates it privately.

## 9. Host messages

All version 1, strict as the protocol is, inside a selected campaign of a
lobby host. In a campaign without characters every message below is refused
with `characters_unsupported` and nothing changes.

**Entering.** `select` gains an optional field `"characters": 1`, the client's
character support. Selecting a character campaign without it is
`characters_unsupported` ("This campaign uses characters; update your
client."), and the connection stays in the lobby. A campaign without
characters ignores the field.

**The actor object**, in snapshots and replies:

```json
{"mode": "creation",
 "character": {"character_id": "c_4be0…", "name": null, "phase": "draft", "draft_revision": 3}}
```

`character` is null in setup. A snapshot has `actor` and `sheet` (the
selected character's sheet, or null) exactly when the campaign is a character
game, read from the same committed state as the rest; its `referee`
projection is scoped to the actor. A `revision` notification is the client's
cue to `sync` for a fresh sheet, as for any view.

Client to host:

| Message | Fields | Reply |
|---|---|---|
| `characters` | `version`, `type` | `characters`: the caller's own characters and actor |
| `character_select` | `character_id` (an ID, or null for setup) | `snapshot` for the new actor |
| `character_create` | `request_id`, `name` (string or null) | `character` |
| `character_complete` | `request_id`, `character_id`, `draft_revision` | `character` |
| `character_archive` | `request_id`, `character_id` | `character` |

```json
{"version": 1, "type": "characters", "actor": {"mode": "setup", "character": null},
 "characters": [{"character_id": "c_19aa…", "name": "Mara", "phase": "ready", "draft_revision": null},
                {"character_id": "c_4be0…", "name": null, "phase": "draft", "draft_revision": 3}]}
{"version": 1, "type": "character", "request_id": "b-4", "revision": 9,
 "character": {"character_id": "c_4be0…", "name": "Ash", "phase": "ready", "draft_revision": null},
 "actor": {"mode": "play", "character": {"character_id": "c_4be0…", "name": "Ash", "phase": "ready", "draft_revision": null}}}
```

- `characters` lists only the caller's participant's drafts and ready
  characters, ready first, then drafts, each by creation time. Archived ones
  are not listed. An empty list is the ordinary onboarding state.
- `character_select` changes only this connection, and only after checking
  ownership and phase inside a guarded read (lobby contract section 6). It
  stores nothing, runs no module code and starts no job. Selecting a draft is
  resuming it. Another connection of the same player keeps its own actor;
  two connections may select the same character.
- `character_create` makes a draft owned by the caller (`start` runs in the
  same transaction) and does not select it; the client selects it next. An
  exact retry returns the same character; only a new `request_id` makes
  another draft.
- `character_complete` succeeds only if the character is the caller's draft,
  `draft_revision` is current, no running job's actor is that character, it
  has a name (the host's own check, listed with the module's), `problems` is
  empty. Then `complete` runs, the phase is `ready`, the creation choice and
  any unnarrated creation results end (section 8), and a connection that had
  it selected is now in play. Nothing is drawn.
- `character_archive` succeeds only if the character is the caller's draft or
  ready character, no running job's actor is it, and `archive_veto` allows.
  History, sheet data and messages stay. A connection that had it selected
  returns to setup.
- Each lifecycle operation is one command record (content: its type and
  fields), advances the game revision, and is protected and replayed like a
  command (same participant namespace; a different message under a used
  `request_id` is `request_conflict`). They are not gameplay, so referee mode
  allows them, and so does a busy referee, except for the character a running
  job acts for (`character_busy`).

**`say` names its actor** in a character game: one extra field,
`"actor": {"character_id": ID or null, "mode": MODE}`, required there and
refused elsewhere. If it is not the connection's current actor, derived inside
the acceptance transaction from the selection and the character's current
phase, the reply is `character_changed` and nothing is stored. The rest of
acceptance is unchanged (referee contract section 5), with section 6's replay
rule.

**Messages.** In a character game a player's `from` gains `"character"`, the
speaking character's name or null in setup. A creation exchange (the input
and the referee's reply) carries `"creation": {"character_id", "name"}` and
is always `audience: "private"`.

**When the actor changes underneath.** If another connection completes or
archives this connection's character, the host sends this connection,
unsolicited:

```json
{"version": 1, "type": "actor", "reason": "completed", "actor": {"mode": "play", "character": {"...": "..."}}}
```

with `reason` `completed` or `archived`. It follows `left`'s rules: it never
takes the place of a reply, and a request racing it gets its own one reply,
judged against the actor at that moment (for example `character_changed`).

Errors, with fixed messages and the request's `request_id`:

| Code | Cause | Then |
|---|---|---|
| `characters_unsupported` | A character message in a campaign without characters; `select` of a character campaign without `"characters": 1`; an `actor` field in a game without characters | Nothing changes |
| `character_unavailable` | The ID is unknown, archived or another participant's (one code for all, so nothing leaks) | If it was this connection's selection, it is back in setup |
| `character_changed` | A `say` whose `actor` is not the connection's current actor | Nothing stored; the client refreshes with `characters` |
| `mode_forbidden` | A raw command the mode does not allow (section 5) | Nothing runs |
| `stale_draft` | `character_complete` at an old `draft_revision` | Nothing changes; carries the current one |
| `character_incomplete` | No name, or `problems` is not empty | Nothing changes; carries `problems`: "Give the character a name." first if needed, then the module's list |
| `character_busy` | Completing or archiving a character a running job acts for | Nothing changes |
| `action_rejected` | `start` or `archive_veto` refused | Nothing changes; the module's message |

Every lifecycle operation and selection is checked twice, in the lobby
contract's order: registry (credential, grant, activation) under the read
guard, then in one campaign transaction the participant link, the character's
owner and phase, and for completion and archiving, the running job. Nothing
crosses databases atomically; the order is the guarantee, as there.

## 10. Ownership, relinks and connections

- A character's owner is its participant, permanently. The host player sees
  their linked participant's characters; nothing else connects them.
- Ending a link (`host campaign unlink`) already returns that player's
  connections in the campaign to the lobby (`left`, `campaign_unavailable`).
  Their characters, drafts and pending questions stay with the old
  participant, untouched. Relinking the player to another participant shows
  that participant's characters; linking them back shows the old ones again.
  This is not a transfer: no record changes owner. Moving a player to an
  existing participant (lobby contract section 5) moves them to that
  participant's characters.
- A client keeps an unconfirmed input with its campaign, participant,
  character and mode. It may resend it only when the snapshot names the same
  participant and it has reselected exactly that character and mode;
  otherwise it drops it and says so. This client rule is the guarantee across
  participants, as the lobby contract's is across campaigns: request IDs
  belong to one participant, so after a relink the new participant has no
  record to compare, and the host would take the resend as fresh input.
- What the host does catch, within one participant: a resend whose `actor`
  is not the connection's current actor is `character_changed` (nothing
  compared or stored); one whose `actor` matches the connection but not the
  stored input's character or mode is `request_conflict`. Neither runs
  anything.
- Selection is not persisted. A reconnect is login, `select` (setup), then
  `character_select` of the same ID. If that is refused, the client is back
  at its character list with the reason.

## 11. Single-game hosts and compatibility

- `tesserae serve --database` (any mode) refuses a character game before
  listening, exit 2: "this game uses characters; serve it from a lobby host
  (serve --registry); nothing was started". Games without characters are
  served as today.
- Old clients cannot enter a character campaign (section 9's `select` field),
  so they can never act without an actor. Campaigns without characters work
  with old and new clients alike.
- Protocol version stays 1. Every new field and message appears only in
  character games, except `select`'s optional `characters`.
- A store opened by this version is upgraded to schema 7, which older versions
  refuse, as with earlier upgrades.
- `tesserae roll`, `dice` and Relay games, version 1 seeds and every existing
  module behave as before.

## 12. Storage

Schema version 7, applied in one transaction when a store is first opened:

```sql
CREATE TABLE characters (
    character_id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL REFERENCES participants (participant_id),
    name TEXT,
    phase TEXT NOT NULL CHECK (phase IN ('draft', 'ready', 'archived')),
    draft_revision INTEGER NOT NULL CHECK (draft_revision >= 1),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    introduced_at TEXT,
    archived_at TEXT
);
ALTER TABLE referee_inputs ADD COLUMN character_id TEXT;
ALTER TABLE referee_inputs ADD COLUMN mode TEXT
    CHECK (mode IS NULL OR mode IN ('setup', 'creation', 'play'));
ALTER TABLE referee_steps ADD COLUMN character_id TEXT;
ALTER TABLE referee_messages ADD COLUMN character_id TEXT;
ALTER TABLE referee_messages ADD COLUMN creation INTEGER NOT NULL DEFAULT 0
    CHECK (creation IN (0, 1));
```

`referee_choices` is rebuilt with a nullable `character_id`, unique on
(`participant_id`, `character_id`) with null as the setup scope; its existing
rows are copied with null. Nothing else is rewritten: existing state,
history, participants, credentials and links keep their values, and no
character is inferred from module state or names. A game without characters
never gets a `characters` row.

## 13. Seeds

Seed version 2 adds characters; version 1 is still read, unchanged.

```json
{"seed_version": 2,
 "module": {"id": "trailhead", "version": "1", "state_version": 1,
            "providers": [{"operation": "dice.roll.v1", "provider": "builtin.dice", "version": "1"}]},
 "participants": [{"participant_id": "p_a1a1a1a1a1a1a1a1", "name": "Alice"}],
 "characters": [{"character_id": "c_0123456789abcdef", "participant_id": "p_a1a1a1a1a1a1a1a1", "name": "Mara"}],
 "state": {"characters": {"c_0123456789abcdef": {"trait": "steady", "stamina": 3}}},
 "provenance": {"source": "..."}}
```

- Version 2 only for a character module; version 1 for one is read as having
  no characters. A module without the capability refuses version 2.
- The host checks each character's ID form, that IDs are unique, that every
  owner is a seeded participant, and each name. The module's
  `validate_seed(state, participant_ids, {character_id: owner})` checks the
  state, including that it has data for exactly the declared characters.
- Seeded characters are `ready` and introduced. Drafts and archived
  characters cannot be seeded.
- All or nothing, as for version 1: every check runs before the file exists.
  Converting a real campaign is later, separate work.

## 14. Illustrative module and exchange

`trailhead` (original, for illustration; CHR-2 decides the fixture): each
character has a `trait` (`steady`, `quick` or `keen`) and `stamina`.
Creation steps are `trailhead.trait {"trait"}` and `trailhead.stamina {}`
(rolls 1d3+1 once per draft; a second call is rejected). `problems` asks for a
trait and stamina; the host itself requires a name. Its one gameplay command, `trailhead.push {}`,
spends a stamina for a roll. `archive_veto` refuses while the character is
`in_danger`.

Messages are abbreviated (no `version`, replies summarized):

```text
bob  > {"type": "characters"}                      -> characters: [] (setup)
bob  > {"type": "say", "request_id": "b-1", "text": "What kind of people walk the trail?",
        "private": false, "actor": {"character_id": null, "mode": "setup"}}
         (setup job: no step may run; the reply goes to the table, as asked)
bob  > {"type": "character_create", "request_id": "b-2", "name": null}  -> character c_4be0… draft 1
bob  > {"type": "character_select", "character_id": "c_4be0…"}          -> snapshot, mode creation
bob  > {"type": "say", "request_id": "b-3", "text": "Someone who reads the weather.",
        "private": false, "actor": {"character_id": "c_4be0…", "mode": "creation"}}
bob  < message, audience private, creation c_4be0…   (stored private despite the flag)
         (creation job: act character.name {"name": "Ash"}, act trailhead.trait {"trait": "keen"};
          draft revision 3; choices.ask {"actor": "c_4be0…", "question": "Roll stamina now?"})
alice> {"type": "say", "request_id": "a-9", ...}    (accepted: Bob's job had ended)
bob  > ... "Yes, roll." -> act trailhead.stamina {}; revision 4; "Ash is ready for you to approve."
bob  > {"type": "character_complete", "request_id": "b-5", "character_id": "c_4be0…",
        "draft_revision": 4}                       -> character ready; actor play
bob  > ... "Ash walks up the trail." (play, table) -> the referee introduces Ash to the table
```

## 15. Clients (proposed for CHR-5 and CHR-6)

**CLI.** After `/join` of a character campaign the client says how many
characters the player has and what to do next. Commands:

```text
/characters        your characters: number, name, ready or draft (revision)
/character N       select one (N, ID or name); a draft resumes creation
/character none    back to setup questions
/create [NAME]     start a draft (then select it)
/sheet             the selected character's sheet
/approve           complete the draft at the revision last shown
/archive N         asks for /archive N confirm, which archives it
```

The status line names the actor: `[character] playing as Mara`,
`[character] creating Ash (draft, revision 3); private to you`, or
`[character] no character selected; ask setup questions, or /create`.
Creation lines read `[private · creating Ash] Referee: …`; table lines
`[table] Alice as Mara: …`. `/private` is refused in creation (everything is
private already). `--character ID` selects one after `--campaign`. Retry
state is keyed by campaign, participant, character and mode, and a piped
`/retry` waits for the authoritative recovery after selection, within the
existing EOF bound. `/approve` sends the revision of the sheet it last
printed; a `stale_draft` prints the new sheet instead.

**Browser.** After choosing a campaign the Console shows the character
picker: Play for each ready character, Resume for each draft, Create
character, and, with no characters, a short explanation and a composer for
setup questions labelled as such. Creation shows "Creating Ash · private to
you" on the status line, the module's sheet read-only with its revision, and
"Approve Ash (revision 4)"; the private box is hidden and every line is marked
private. Play is the ordinary Console, with the status line naming the
character. Settings gains a Character row and "Change character" and "Archive
character…" (with a confirmation naming it). Console and Settings remain the
only navigation items. At every actor change the page clears the sheet,
draft, private box, pending question and resend targets; the campaign's
visible conversation stays, since it is the table's. Callbacks and retries are
tied to connection, campaign, participant, character and mode; nothing new is
kept in browser storage.

## 16. Tests each slice needs

- **CHR-2 (store):** the schema 7 upgrade leaves existing data untouched;
  `complete` gets no random source;
  create, steps, complete and archive commit metadata and module state
  together, replay exactly, and leave nothing on any refusal (stale revision,
  problems, veto, wrong owner, archived); concurrent completion and archive;
  `character.` names and duplicate step names refused by the catalog; seed
  version 2 accepted, and refused for duplicate, orphaned or undeclared
  characters, drafts, and a module without the capability; version 1 seeds
  and Relay unchanged.
- **CHR-3 (referee):** section 5's table for every tool and mode; the actor
  survives the sender switching or leaving; a retry under another character or
  mode is `request_conflict`; pending choices and last inputs per actor, and
  resolving one character's question leaving its owner's other characters'
  and other players' questions alone; section 8's privacy cases, including a
  failed creation job's results; creation releasing busy between answers while
  another player plays; completion only by approval at the current revision;
  introduction on the first play input; switching never resets the fixture's
  stamina; restart mid-creation; archive refused while that character's job
  runs; revocation mid-job; existing participant-only jobs unchanged.
- **CHR-4 (host):** every message and error above over raw WebSocket; two
  connections of one player with different characters; `actor` notices for
  completion and archive elsewhere, racing requests, with exactly one reply
  each; `character_changed`; relink changing the visible characters; foreign
  IDs indistinguishable from unknown; `select` without `characters`; host
  restart; and `serve --database` refusing a character game while serving
  others.
- **CHR-5 and CHR-6:** as their issues list, against this contract.

## 17. Open questions

These are not decided; each has a default that stands until the owner says
otherwise.

- **A table roster.** Should players see a list of everyone's ready
  characters? Default: no host roster; the module's view shows what it chooses.
- **Archiving in the fiction.** Should archiving a character tell the table
  or the referee? Default: no; it is a system operation, and the player can
  say something in play first.
- **Renaming after completion.** Default: names are fixed once ready.
