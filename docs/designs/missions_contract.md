# Missions contract — MS-1

Status: proposed specification for [MS-1, #199](https://github.com/coghex/tesserae/issues/199),
for manual review, 2026-11-12. Public baseline
`c6053b6a09fd1969fe6c55ca4bd98b29283bccb1`. The
[missions design](missions_design.md) records the owner's direction.
This contract specifies the capability and behavior for MS-2 through MS-7.
It extends the [referee](referee_contract.md), [character](character_contract.md),
[governance](governance_contract.md) and [protocol](../protocol.md) contracts.

Examples use original content. Implementation follows review of this document.

## 1. Decisions and implementation choices

The design's owner decisions apply, including one mission per request,
generated and premade missions, 1–2 primary and 0–4 secondary objectives,
independent tone selection, egg slots by length, player-owned commitments,
personal threads and post-mission reveals.

The owner additionally decided that every actual participant retains access
to the post-mission reveal while they retain campaign access, including a
participant who left the mission before its end. A commitment withdrawn before
start is not participation.

This contract proposes these engineering choices.

- Dedicated bridge tools and a framework mission store own the lifecycle.
  Game modules supply content, validation, their clock and effect execution.
- A mission has one stable ID from request to closure. Offers and preparations
  are stages of that record. A separate proposal revision binds commitments.
- Campaign schema 11 adds mission tables. Module state keeps its existing
  representation and limits. Optional `Module.missions` uses capability version 1.
- Every fresh mission mutation advances the game revision and is journaled in
  `commands`. Exact retries return their committed result. Reads change nothing.
- Framework defaults are five open offers, one preparing/active mission, eight
  proposed characters, equal tone weights and the design's length profiles.
  These are reviewable defaults. Games can supply stricter content constraints.
- Deliberate host corrections use a preview and explicit confirmation. Routine
  referee preparation does not require individual host approvals.

The proposal mechanism also covers joining a mission in progress and agreeing
to a prepared alternate contract. Existing participants keep their agreed terms
while those proposals await the affected players' own commitments.

## 2. Names, bytes and immutable history

IDs allocated by the host have a prefix and 16 lowercase hexadecimal digits.
Prefixes are `m_` for missions, `mp_` for proposals, `mr_` for receipts,
`mc_` for carried consequences and `mt_` for threads. Library-local IDs match
`[a-z][a-z0-9_]{0,47}`. Character and participant IDs retain their existing forms.
Strings are valid Unicode. Integer fields reject booleans. JSON rejects
duplicate keys, non-finite numbers and invalid Unicode as the protocol does.

Byte limits use `result_bytes`, the length of default `json.dumps`, as in the
governance contract. Canonical digests use SHA-256 over UTF-8 JSON with sorted
keys, compact separators and `ensure_ascii=False`. Lists with defined order
retain it. Catalog lists are sorted by stable ID before generation or hashing.

Every record carries `schema_version:1`. Stored command receipts and prior
published versions remain immutable. Corrections append their reason, author
and before/after revision. They preserve what players were previously told.

## 3. The optional capability

Add these frozen declarations to `tesserae.modules`, with the existing `Action`
and `Outcome` types. `Module.missions` defaults to `None`.

```python
@dataclass(frozen=True)
class MissionContext:
    state: dict
    characters: tuple[dict, ...]
    policies: dict
    mission_policy: dict


@dataclass(frozen=True)
class MissionEffect:
    name: str
    version: int
    description: str
    validate: Callable[[dict], None]


@dataclass(frozen=True)
class MissionEffectsAction:
    action: Action
    mission_id: str
    resolution_id: str
    effective_time: dict | None
    plan: dict


@dataclass(frozen=True)
class Missions:
    version: int
    library: Callable[[], dict]
    inputs: Callable[[MissionContext], dict]
    clock: Callable[[dict], dict | None]
    validate_mission: Callable[[MissionContext, dict], list[str]]
    effects: tuple[MissionEffect, ...]
    preview_effects: Callable[[MissionContext, list[dict]], dict]
    apply_effects: Callable[[MissionEffectsAction], Outcome]
```

The catalog accepts version 1 with `CharacterRules` version 1 or 2. Other
capability versions, duplicate effect names or invalid library data are module
errors. `HOST_API_VERSION` remains 1. A module without missions retains all
existing behavior. Loading data uses its existing trusted package mechanism.

All hooks except `apply_effects` are pure, deterministic and receive copies.
No hook receives database handles, filesystem paths from a player or referee,
or authority to mutate mission tables. Exceptions use existing module failures
and `ActionRejected` for state-dependent refusals.

### Hook inputs and outputs

- `library()` returns the complete versioned library of section 4. Load and
  validate it once per runtime. Its content digest is checked independently
  of the author's version label.
- `inputs(ctx)` returns exactly `capabilities`, `people`, `facts`. Capabilities
  map each requested character ID to a sorted list of module-defined tags.
  People are `{id, character_id, relationship, allowed_roles, excluded_roles,
  disclosure, facts}`. Facts are `{id, value}`. People and facts carry only
  needed preparation data, within the input budget. Existing boundaries must
  be translated into roles and disclosure constraints here.
- `ctx.characters` contains host-built `{character_id, participant_id, name,
  phase}` metadata for the requested roster. `policies` maps those IDs to the
  host's current effective game policy or null. `mission_policy` is section 5.
  The caller cannot supply ownership or authorization through these values.
- `clock(state)` returns null or `{domain, tick, unit}`. `domain` is a stable
  module-defined ID. `tick` is a monotone integer in 0..2^63−1. `unit` is a
  display label such as `minute` or `turn`. Arithmetic uses ticks. Null makes
  timed components ineligible. The framework never interprets a calendar.
- `validate_mission(ctx, mission)` returns player-safe problems with binding,
  catalog, rule and established-world compatibility. It must also validate
  current eligibility before expansion and start. Framework structural checks
  run first. Semantic review of narrative prose belongs to the referee.
- Each effect validator checks its args alone and raises `ArgumentError`.
  The framework supplies only names/versions declared in `effects`.
- `preview_effects(ctx, effects)` validates the entire ordered batch against
  game state and returns exactly `{plan, dependencies, summary}`. It has no
  random source. It checks the resources, prerequisites and worst-case size
  needed for the whole batch. The plan includes any required per-character
  authorizations already established in the fiction. The referee remains
  responsible for recording those choices truthfully.
- `apply_effects(effect_action)` executes exactly that plan against the
  candidate state in `action.state`, using only its declared operations when
  drawing dice. It returns an `Outcome` with validated replacement state and
  `{effects:[receipts]}` as result. The module reuses its existing validated
  internal mechanics. Expected refusals occur before the first draw. Any
  failure rolls back the whole state/event transaction.

Effect previews are recomputed inside the execution transaction. Each batch
is checked before its first draw. The callback cannot write a database or
install code. An effect describing a fight can return a narrative cue and
use the game's existing combat lifecycle subsequently. Its capability does
not imply that every game permits ordinary bookkeeping during combat.

## 4. Library and mission shapes

`library()` returns exactly `{schema_version, id, version, templates, themes,
components, premades, eggs}`. Version is a nonblank string of at most 80
characters. Template, theme, component, premade and egg IDs are unique within
their respective collections. References include kind plus ID where necessary.
Each collection has at most 512 entries. Library size is at most 2 MiB.

Each component has `{id, kind, weight, requires, provides, excludes, bindings,
body}`. `kind` is one of `client`, `stakeholder`, `place`, `secret`, `obstacle`,
`clue`, `reward`, `discovery`, `tripwire`, `twist`. `weight` is an integer
0..1,000,000. The three property lists contain unique tag strings. Requirements
are conjunctions, exclusions forbid overlap, and provided properties add to
the chosen set. Tags describe compatibility rather than activating mechanics.
Bindings map local role IDs to selected person/place/target/objective IDs.
References are explicit and acyclic where they describe prerequisites.

Templates declare `{id, weight, primary_options, secondary_options, pairings,
slots, body}`. Each objective option has `{id, role, target_kind, complete_when,
requires, effects}`. Pairings enumerate compatible distinct primary IDs and
their prerequisite direction. Slots are `{id, kind, min, max, requires}` and
select component kinds. Themes have `{id, weight, requires, excludes, body}`.
Each template's body supplies the fixed mission structure; selected entries
fill named slots. Counts and bindings are checked after assembly.

A fully assembled mission has these sections.

| Section | Exact shape or meaning |
|---|---|
| `identity` | `{mission_id, origin, length, tone, binding, revision}`. Origin names generated provenance or a premade version. |
| `summary` | `{title, premise, disclosed_objectives, scope, terms, known_risks, deadline}`. Objectives are `{id, description}` and include every initial primary. Terms are `{text, effects}` with declared effect references for mechanical promises. Deadline is null or `{text, domain, tick}`. |
| `requirements` | `{capabilities, bound_characters}`. Capabilities are `{tag, count}` records counted across distinct roster characters. Bound characters include every selected personal-thread owner. |
| `objectives` | `{id, role, target, complete_when, depends_on, disclosure, evidence, effects}` records. Role is primary or secondary. Evidence names acceptable clue observations or referee-adjudicated conditions. |
| `branches` | `{id, replaces, objectives, summary, requirements, effects}` records for prepared alternate assignments. The initial branch is `initial`. Each branch names the objective IDs it activates and the old ones it supersedes. |
| `people`, `places`, `targets` | Stable-ID records with structured bindings and description prompts. History people retain their source ID and restrictions. |
| `facts` | `{id, truth, required_by, disclosure}`. Facts and witness claims remain distinguishable. |
| `clues` | `{id, origin, place, supports, contradicts, access, levels, disclosure}`. Levels are ordered `{id, observation, access}` entries. |
| `obstacles` | `{id, place, affects, signs, approaches}`. Each approach has `{id, description, needs_roll, procedure, consequence}`. |
| `events` | The event shape below. |
| `egg_slots` | `{id, kind, place, access}`. Kind is `cache` or `occurrence`. No egg identity is chosen here. |
| `prompts` | `{id, target, type, choices, required}`. Target identifies section, item ID and field. Types are text or choice. |

Every effect reference is exactly `{name, version, args}` and passes the declared
validator. Descriptions are bounded text. `disclosure` is `table`, `observers`
or `host_referee`. A fact can additionally carry character-specific restrictions
from the history hook. Referee observation recipients never override an explicit
restriction. Public summaries contain only disclosed facts.

Events are exactly `{id, kind, when, signs, effects, schedule, repeat,
exclusive_group, persistence, trade, stop, disclosure}`. Kind is discovery,
tripwire or twist. Schedule is null or `{delay_ticks, followup_id}`.
Persistence is `mission` or `campaign`. Repeat is `{max_occurrences}` with
1..16. A trade has its perceivable cost declared before the triggering choice.
Every other consequence declares a stop condition and its resulting outcome.

Conditions are exactly one of these objects, or `{all:[conditions]}` or
`{any:[conditions]}`. Nesting is at most three and each list has 1..8 items.

```json
{"objective": {"id": "return_pack", "status": "completed"}}
```

```json
{"observation": {"clue_id": "ledger", "level": "basic", "character_id": null}}
```

```json
{"visited": {"place_id": "hut", "character_id": null}}
```

```json
{"event": {"id": "alarm", "occurrence": 1}}
```

```json
{"time": {"domain": "example_minutes", "tick": 120}}
```

```json
{"adjudicate": {"id": "forced_entry", "question": "Was the door forced?"}}
```

Null in an observation or visit means any active participant. Referenced
IDs must exist. Time and delay fields require a non-null compatible clock.
Adjudications are supplied with a reason by the referee and saved with the
resolution. A descriptive location-summary change never records a visit.

The validator enforces objective counts, permitted pairs, a DAG of objective
dependencies and reachable declared routes for essential evidence. Essential
facts have two origins and independently accessible paths, one with a no-roll
approach. The referee reviews the actual plausibility. Calm has no tripwires,
reactive 1–2 and volatile 3–4. All tripwires have a real no-roll clean route.
Timed consequences start only after their tripwire. Disclosed deadlines may
exist from the start. Branches declared by twists retain objective bounds.

Premades contain `{id, version, requirements, mission}` with the complete
shape and all required prompts already filled. They declare their tone.
Eggs contain `{id, version, kind, first_sight, layers, object, effects,
aftermath, threads, restrictions}`. Layers are `{id, access, text}`. An object
is null or a descriptive definition understood by an implemented effect.
Effects and restrictions must be supported before an egg is eligible.

## 5. Policy, randomness and reconstruction

Campaign mission policy is exactly `{enabled, library_id, tone_weights,
eggs_enabled, history_enabled, max_open_offers}`. Defaults enable missions
when the capability is present, use the module's library ID, set calm/reactive/
volatile weights to 1 each, enable eggs/history, and allow five open offers.
Weights are 0..1,000,000 with at least one positive. Offer count is 1..5.
The host changes policy with a reason and expected policy revision. Disabling
new requests does not strand a preparing mission, active mission or consequence.

Generated requests name a length and optionally template/theme IDs and a target
roster. Omitted roster means all ready characters, up to eight. More needs an
explicit selection. An empty roster is refused. Capture the input hook output,
effective policies, restrictions, egg availability and relevant clock values.

Draw a 64-bit seed through eight host random-source calls `randint(0,255)`.
The injected `GameStore` source is used when provided, otherwise Python's
system random source. Record the eight bytes, seed and purpose in the receipt.
This does not require a game to adopt a dice provider. Exact retries draw again
only if no generation command ever committed.

The deterministic stream is SHA-256 of ASCII `tesserae-missions-v1`, a zero
byte, the eight-byte seed and a big-endian 64-bit counter starting at zero.
Concatenate blocks in counter order. A choice below n takes the minimum whole
number of bytes covering n and rejection-samples values above the largest
multiple of n. For n=1 it returns zero without consuming bytes. Weighted
choices use sorted IDs and cumulative integer weights. This defines behavior
across supported Python versions.

Draw tone independently of length. Once selected, its profile remains fixed
during at most 128 assembly/backtracking attempts. A lack of compatible content
is `mission_generation_failed`; it never silently converts volatile to calm.
The pure generator accepts saved inputs and reads no current game state.

Request stores summary, seed, input snapshot, generator version, library
version/content digest, skeleton digest and requirements. Hidden skeletons
are generated in memory for validation, then discarded. Referee summary edits
are saved separately and never change a skeleton's identity. Expansion reruns
the pinned generator over the recorded inputs and compares the digest before
applying saved summary answers. A library mismatch or differing skeleton makes
the offer stale. No replacement or re-roll occurs implicitly.

The saved generation envelope includes requested length, any narrowed template
or theme, the full mission policy and hook input snapshot. Hash generator-owned
sections only. Host-allocated mission IDs, receipt IDs and runtime revisions
are excluded. Deterministic local instance IDs derive from the selected slot
and entry bindings. The dry run uses the same envelope and digest definition.
Thus independent requests with the same supplied seed/inputs can be compared
without host allocation metadata changing their skeleton digest.

Premade requests save name, version and content digest. At expansion the exact
premade is loaded. Mark a premade played when its mission begins, including a
later abandonment. Withdrawal before begin does not consume it.

## 6. States, proposals and participation

The mission lifecycle is `draft → published → preparing → active → closed`.
An unstarted offer may instead become `withdrawn` or `stale`. Closed,
withdrawn and stale versions are historical. Referee inspection can read
them with their original binding even when their library is no longer loaded.
Executing their effects still requires the matching capability/effect versions.

Draft summaries and prompt answers can be refined. Publish validates the
summary, freezes its terms at `offer_revision` and exposes it to players.
Normal preparation cannot change those terms. An explicit host amendment
before begin creates a new offer revision, retains the old version in history
and invalidates affected commitments. A different generated skeleton requires
a new request. An active contract changes only through a declared twist and
the affected players' own commitments to revised terms.

`mission.propose` names one published offer and a nonempty roster. Check
ownership, ready phase, campaign access, required capabilities and bound
characters. At most one proposal can be open/preparing for a campaign. The
proposal stores `{proposal_id, kind, mission_id, revision, offer_revision,
roster, required_committers, branch_id, state}`. Kind is initial, join or
terms_change.
Roster entries are `{character_id, participant_id}` built by the host.
State is open, ready, preparing, started or cancelled.

Commitment and withdrawal authenticate the player and freeze the selected
ready character exactly as character play does. A commitment names the
proposal and its current revision. One player can commit several of their
own characters separately. A host's presence alone does not commit a guest.
Once every required committer has a valid commitment, the proposal is ready.
An initial proposal requires every roster entry. A join requires the joining
characters. A terms change requires every participant whose assignment or
promised reward changes. The host derives that set from the declared branch
and current roster and checks it through game compatibility validation.

A changed roster or offer revision increments the proposal revision and
requires fresh commitments from everyone retained. Unrelated activity leaves
existing commitments valid. It can still stale a transport request's game
revision, which the client refreshes using existing recovery behavior.

At activate and begin, recheck the actual roster, current ownership/access,
readiness, offer requirements and game compatibility. Use current state for
eligibility, and the saved snapshot for skeleton reconstruction. A change to
tone weights alone does not alter an already published offer. A library
version/content mismatch refuses expansion as `mission_offer_stale`.

Activation expands into preparing state. The referee fills required prompts
across as many jobs as necessary. Begin checks prompt completion and valid
commitments, registers any declared world effects through the effect batch,
marks the premade checklist where relevant, starts objective states and
creates participation rows. Preparation and publication spend no game time
or money. Begin applies a fee or advance only if prepared terms and existing
player choices authorize that specific effect.

For an initial proposal, withdrawal remains available until begin, including after expansion. It
increments proposal revision. Preserve a prepared mission if the remaining
nonempty roster remains eligible, then ask them to commit again. Otherwise
withdraw the offer with a recorded reason. Preserve disclosed facts and
release the preparing slot. New missions never replace it implicitly.

Participation starts at begin or an explicit later join. Record its owning
participant and character at that moment. A later ownership change does not
transfer earlier private discoveries or reveal eligibility. Archiving a
character who is actively participating requires first ending that membership
through a recorded host correction. A departing member retains historical
participation and campaign-access-dependent reveal rights.

For an active join, the referee proposes adding ready characters with explicit
terms. Only their own commitments apply the join. Existing members keep their
terms. A prepared branch that changes those terms uses a terms_change proposal
and their commitments as well. A last valid commitment applies a ready join or
terms change atomically, including its declared effects and participation rows.
It does not restart the mission or retroactively credit earlier discoveries.
The referee can record an established departure with `mission_leave` on the
departing player's own job actor. Host correction can also end a membership
with a recorded reason. Departures retain their actual participation history.

## 7. Observations, events and time

Objective runtime states are open, completed, failed, abandoned or superseded.
Primaries start open at begin. Optional objectives can be undisclosed until
their discovery condition occurs. An objective outcome records the prepared
evidence references and referee reason. Failed or superseded objectives remain
in history. A branch checks the active objective bounds before it applies.

`mission.observe` names a prepared clue level and recipient character IDs.
The referee establishes that those characters encountered its source. Validate
audiences, current participation and access conditions. Save one observation
per clue/level/recipient, including first observation time, source and receipt.
The clue text stays in immutable preparation. Sharing records additional
recipients after the established sharing action. Distinguish direct observations,
claims and verified conclusions. The system does not infer a player's belief.

Improvised observations use a short statement with provenance and a unique
ID. They are separately paged records, with the same visibility rules. The
referee may use an improvised source to support an objective, with its reason.
It cannot retroactively change prepared truth or a previously recorded claim.

Event runtime states are prepared, armed, resolved, avoided or expired.
Prepared means eligible for condition evaluation. Armed means a timed
occurrence has a due time. A false condition stays prepared. Avoided or
expired needs an established closing condition and a reason.

Each occurrence has one immutable resolution receipt. Resolving a one-shot
event again returns that outcome and never reapplies it. Repeatable events
require the next occurrence number, within the declared maximum. An exclusive
group's resolved occurrence blocks incompatible outcomes in that group.

Resolution checks structural conditions against current records and stores
referee answers for declared adjudication questions. It builds the effect
batch, previews it, then commits its game-state changes, observations,
scheduled follow-ups, checklist entries and receipt atomically. There is
no separate successful status write before effects have succeeded.

### Clock integration

Before and after every game-state mutation, including host administration and
mission effects, evaluate the module clock hook. A decreasing tick or changed
domain while timed records remain outstanding is `mission_clock_changed` and
the mutation is refused. An explicit operator migration can convert domains
with a named mapping and preserved old values. Null to a first non-null clock
is allowed before any timed record is armed.

After a successful advance, query the mission store for due occurrences.
Return a bounded due-event page and add a compact due count to context. This
single store integration covers ordinary minutes, seconds and any other
game-owned advancement. Games need no calls into mission storage themselves.

Due means `due_tick <= now.tick`. Resolve ordered by `(due_tick, mission_id,
event_id, occurrence)`. A newly armed successor uses its predecessor's
effective due time, so a large advance cannot postpone a chain. If it is
already due, insert it into that same order. Each command resolves one
occurrence. The referee can continue through pages across jobs. No automatic
unbounded event cascade occurs inside one mutation.

The effect context carries effective time separately from the current game
clock. Never rewind the clock. The referee resolves due occurrences before
adjudicating later fictional outcomes. Resolving an objective or closing a
mission checks the earliest outstanding due occurrence in the campaign and
refuses with its ID if it must be settled first. Lapsing a due event requires
an established stop/expiry condition or an explicit host correction reason.

### Eggs and continuing threads

Short has zero slots, medium one and long two, reduced only by policy or
insufficient eligible unseen eggs. Validate kind and implemented effects.
Before placement, test that distinct eligible eggs can fill the proposed
slots. During discovery, choose from the unseen compatible entries. A choice
must leave a valid assignment for this active mission's other unresolved
slots. Mark the entry met in the same transaction as revealing its first
layer and any automatic effects. No candidate is pre-drawn for a hidden slot.

Use the host random source for the weighted draw and journal its result.
Only `{library_id, egg_id}` determines met status across entry versions.
Inspection, refresh and repeated discovery return saved layers without draws.
Examining another layer requires its declared action. Found items are granted
only after the player's established action through the appropriate effect.
An undiscovered slot consumes no egg.

A personal thread stores its owning character, person source ID, relationship
facts, restrictions and an open/closed state. Missions link to that ID with
their outcomes. Never embed a growing list of whole missions into a thread.
Future generation receives eligible open thread summaries and may continue
one. A restriction newly recorded in the campaign applies before a future
binding or refinement. Previously established events remain in history.

## 8. Closure and reveal

Close is allowed when all primaries are terminal or the party has actually
abandoned the mission. Record the choice of each affected player when
abandonment commits their character. Resolve or validly lapse every earlier
due occurrence first. Apply any prepared completion/abandonment effects in
one batch with the closure receipt.

Future consequences with persistence `mission` expire with recorded due time
and reason. Those with persistence `campaign` keep the same occurrence and
definition under campaign scope. No copy becomes a second authority. They
remain scheduled through closure and restart. There is no count limit on
durable carried rows. Context and reads remain paged even when many are due.

Record terminal objective outcomes, participation history, reward receipts
and links to continuing threads. Release the active mission slot only after
that transaction succeeds. Full reports and downtime are later features.

The reveal is an on-demand historical view of the closed mission. It includes
tone, untripped events and their hypothetical results, missed evidence and
routes, expired consequences, found eggs and only a count of undiscovered
slots. Never draw an egg during reveal. While a consequence still runs,
withhold its definition and any connected secret that would reveal its
unresolved future effect. Show only the count of continuing consequences.
Those entries can become visible after the corresponding occurrence resolves.
Cross-mission thread preparation remains outside this mission's reveal.

Any participant recorded during actual play may read the reveal while their
campaign access remains valid. This includes an earlier departing player.
An unplayed withdrawn commitment grants none. Host inspection retains full
visibility. Historical ownership and audiences survive later character transfer.

## 9. Tables and transaction boundaries

Campaign schema 11 adds these tables. Text JSON columns hold canonical JSON.
Foreign keys point to existing characters/participants where appropriate.
Historical memberships keep their IDs if a character is archived.

| Table | Columns and uniqueness |
|---|---|
| `mission_policy` | One row: revision, document. |
| `missions` | mission_id PK; state, origin, binding, offer_revision, preparation_revision, data, created_revision, updated_revision. |
| `mission_proposals` | proposal_id PK; kind, mission_id, revision, offer_revision, state, roster, required_committers, branch_id. One open/preparing proposal per campaign. |
| `mission_commitments` | proposal_id, proposal_revision, character_id, participant_id, state, receipt_id. Unique first three columns. |
| `mission_members` | mission_id, character_id, participant_id, joined_revision, left_revision, reason. Unique mission_id/character_id/joined_revision. |
| `mission_observations` | mission_id, clue_id, level_id, character_id, participant_id, observed_at, source, receipt_id. Unique mission_id/clue_id/level_id/character_id/participant_id. Improvised clue IDs occupy a separate namespace. |
| `mission_visits` | mission_id, place_id, character_id, occurrence, game_time, receipt_id. |
| `mission_events` | mission_id, event_id, occurrence, state, scope, consequence_id, domain, due_tick, definition, receipt_id, revision. Unique first three columns; consequence_id unique when present. |
| `mission_receipts` | receipt_id PK; mission_id nullable for policy, operation, author, revision, input, outcome, game_time. Immutable. |
| `mission_checklist` | kind, library_id, entry_id, mission_id, receipt_id. Unique kind/library_id/entry_id. Kind is egg or premade. |
| `mission_threads` | thread_id PK; character_id, person_id, state, data, revision. |
| `mission_thread_links` | thread_id, mission_id, outcome_receipt. Unique thread_id/mission_id. |

Index open offers, current proposals, active memberships and events by
`(state, domain, due_tick, mission_id, event_id, occurrence)`. Reads of due
events use the index and a page limit. Closed mission and thread reads are
keyset-paged. Foreign-key checks and row JSON validation precede commit.

Fresh mutations use the existing game command transaction and history with
their operation name, authenticated actor, args and expected game revision.
The new record updates, replacement module state, receipt and game revision
commit together. Metadata-only actions also advance the revision so clients
can sync. Player transport mutations and corresponding bridge operations use
the same implementation. Reads see a single coherent transaction snapshot.

Existing busy and access rules apply. A client mutation is refused while a
referee job is running. The referee may mutate inside its own live authorized
job. Every mutation rechecks session/job authority inside the write transaction
before replay lookup, as current guarded commands do. Host transfer, revoked
access and concurrent character changes are ordered by those guards.

Exact replay after restart returns the recorded result. A new request to repeat
a completed one-shot occurrence returns its immutable receipt without effects.
Repeating a valid commitment is similarly harmless. No duplicate payment,
receipt, checklist entry or membership is created. Stale/failed mutations
commit no partial mission or game state. Pending due events are recomputed
from durable records rather than an in-memory timer.

## 10. Reads, authority and bounds

All read views apply current session/campaign access. A revoked connection or
unlinked participant gains no new data from an old cursor. The host may inspect
every mission and private preparation. A referee in its authorized play job
may inspect all relevant mission preparation, including carried consequences.
Other players see published summaries, disclosed objective statuses, their own
observations and eligible reveals. A changed character owner does not inherit
the earlier participant's private observation rows through that transfer alone.

A mutation receipt has exactly `{receipt_id, operation, mission_id, revision,
changes, due}`. Changes are bounded IDs/statuses and game effect receipts.
Detailed private preparation is retrieved separately. Player mutations use
the player projection of their receipt. Referee and host receipts never flow
through a guest view or public broadcast by default.

Every paged read returns `{revision, items, next}`. Items are complete records
or section entries, within the limits below. Cursor `next` binds query,
mission/section, audience and game revision. The server validates those values
and current permission; a cursor cannot grant authority. A changed revision
returns `mission_page_stale`, so the caller restarts that query. After resolving
a due event, restart from the earliest due page to catch new successors.

| Limit | Result bytes or count |
|---|---:|
| Generation input snapshot | 16 KiB |
| Published summary | 4 KiB |
| Expanded mission, excluding separate runtime rows | 128 KiB |
| Individual text, clue level or egg layer | 4 KiB |
| Event effects in one batch | 16 |
| Effect preview plan/dependencies | 12 KiB |
| Effect receipts and final mutation receipt | 8 KiB each, with envelope check |
| Detail page | 16 KiB, at most 32 complete items |
| Mission header in ordinary referee context | 1 KiB |
| Requested roster | 1–8 ready characters |
| Library-local objectives | 1–2 primary and 0–4 secondary active in each branch |

The 1 KiB context block is exactly `{supported, policy_revision, active,
proposal, due}`. Active is null or `{mission_id, state, primary_counts,
secondary_counts}`. Proposal is null or `{proposal_id, revision, missing_count}`.
Due is `{count, first}` with first null or `{mission_id, event_id, occurrence,
due_tick}`. Full titles, rosters, observations and lists are read through tools.
Context never enumerates an unbounded due set. The existing composed 44 KiB
bridge limit still applies to the whole job result, including all other blocks.

Validate a component's individual fields and full preparation limits before
publishing an offer. Publish/expand must reserve each mission's bounded
structural counts so later prepared observations or event receipts fit their
individual limits. Persistent runtime rows, receipts, closed missions, threads
and carried consequences have no global count cap. Only their views are paged.
Full preparation records remain durable through closure and package upgrades.

At most 64 people/places/targets total, 64 facts, 32 clues with at most four
levels each, 32 events and 16 prompts are allowed in one expanded mission.
These are resource limits for this capability. Each repeat has its prepared
maximum. They do not define universal game concepts outside missions.

## 11. Protocol and bridge tools

All messages are protocol version 1 in an authenticated selected lobby campaign.
Without `Module.missions`, each mission operation returns `mission_unsupported`.
Every mutation uses `request_id` and `expected_revision` in addition to the
fields listed below. Replies use the normal result/error pairing. Unknown
fields are refused. Notes and reasons are 1..1,000 characters and at most 4 KiB.

### Client messages

| Message | Fields | Reply and permission |
|---|---|---|
| `mission_list` | `states`, `after` | Paged published/active/closed summaries permitted to this participant. |
| `mission_read` | `mission_id`, `section`, `after` | Paged permitted section. Section is offer, objectives, observations, reveal or host. Host requires current host authority. |
| `mission_policy` | none | Current common policy and library metadata, including public tone weights and slot rules. |
| `mission_commit` | `proposal_id`, `proposal_revision`, `character_id` | Owner's commitment receipt. Character must match the connection's selected ready play actor. |
| `mission_withdraw` | `proposal_id`, `proposal_revision`, `character_id` | Owner withdraws before an initial proposal begins or an active-change proposal applies. |
| `mission_leave` | `mission_id`, `character_id`, `reason` | Owner ends their own active membership. |
| `mission_host_policy` | `policy`, `policy_revision`, `reason` | Host policy receipt. |
| `mission_host_preview` | `target`, `intent`, `reason` | Host-only `{plan, dependencies, digest, summary}`. No mutation or dice. |
| `mission_host_apply` | `target`, `intent`, `reason`, `preview_digest` | Host correction receipt. Re-preview inside the transaction and compare digests. |

Read fields `states` and `after` are optional, default all visible states and
first page. Sections have the audience rules in section 10. A host read can
inspect every preparation section and effect receipt but is still paged.
Missing and unauthorized private records use the same `mission_unavailable`
response. Normal clients do not receive an existence oracle for hidden records.

Host correction targets are exactly `{kind, id}` where kind is mission,
proposal, consequence or membership. Intents are exactly `{operation, values}`.
Operations are amend_terms, replace_prompt_answers, revise_roster, end_member,
lapse_event, reschedule_event and withdraw_offer. Values are respectively
`{summary}`, `{answers}`, `{character_ids}`, `{character_id}`, `{event_id,
occurrence, outcome}`, `{event_id, occurrence, domain, due_tick}`, and `{}`.
Targets name the mission or proposal, or a carried consequence ID. Amendments
are pre-start only; an active assignment change uses a prepared branch and
the normal terms_change proposal. Replace_prompt_answers is preparing only.
Lapse outcome is avoided or expired. Host actions
preserve history and technical invariants. A correction cannot forge a player's
commitment or fabricate a previously applied game effect receipt. Existing game
administration remains available for separate supported character/world edits.

### Bridge operations

Add dedicated tools. Mutating tools take `step_id` and the listed arguments.
The bridge creates a normal journaled mission mutation inside its live job
guard. It supplies the current game revision. Read tools have no `step_id`.
All mission tools run only in play-mode jobs. They remain unavailable in setup
and creation. They execute through framework mission handling, with no new
module command names or shell execution permission.

| Tool | Arguments beyond `step_id` | Authority |
|---|---|---|
| `mission_request` | `length` or `premade_id`, optional `template_id`, `theme_id`, `character_ids` | Referee preparation. Exactly one origin selector. |
| `mission_refine` | `mission_id`, `target`, `expected_record_revision`, `summary`, `answers` | Referee; target summary or preparation. Exactly the relevant summary or answers field. Answers map prompt IDs to their validated values. |
| `mission_publish` | `mission_id`, `expected_record_revision` | Referee freezes a valid draft offer. |
| `mission_propose` | `mission_id`, `kind`, `character_ids`, optional `branch_id`, `proposal_id`, `proposal_revision` | Referee creates/revises an initial, join or terms_change proposal. Last two fields occur together on revision. Branch is required for terms_change. |
| `mission_commit` | `proposal_id`, `proposal_revision` | Job sender's frozen ready character. |
| `mission_withdraw` | `proposal_id`, `proposal_revision` | Job sender's frozen ready character, before the proposal applies. Initial proposals remain withdrawable through preparation. |
| `mission_leave` | `mission_id`, `reason` | Job sender's own active membership. |
| `mission_activate` | `proposal_id`, `proposal_revision` | Referee expands the fully committed offer. |
| `mission_begin` | `mission_id`, `preparation_revision`, `proposal_revision` | Referee, after all readiness checks. |
| `mission_visit` | `mission_id`, `place_id`, `character_ids`, `reason` | Referee records established arrival. |
| `mission_observe` | `mission_id`, `clue_id`, `level_id`, `character_ids`, `reason` | Referee records observation/sharing. Improvised clues additionally supply their observation and provenance. |
| `mission_objective` | `mission_id`, `objective_id`, `status`, `evidence`, `reason` | Referee resolves a supported objective outcome. |
| `mission_event` | `mission_id`, `event_id`, `occurrence`, `adjudications`, `reason` | Referee resolves one eligible occurrence. |
| `mission_open_egg` | `mission_id`, `slot_id`, `character_ids`, `reason` | Referee records established discovery. |
| `mission_egg_layer` | `mission_id`, `slot_id`, `layer_id`, `character_ids`, `reason` | Referee reveals an established subsequent examination. |
| `mission_close` | `mission_id`, `outcome`, `reason` | Referee; outcome completed or abandoned with recorded player decisions. |
| `mission_list` | optional `states`, `after` | Sender's normal projected list. |
| `mission_inspect` | `mission_id`, `section`, optional `after` | Referee-only bounded private read. Section is summary, objectives, people, places, facts, clues, obstacles, events, eggs, prompts, receipts or threads. |
| `mission_due` | optional `after` | Referee-only ordered due page across the campaign, including closed missions' carried events. |
| `mission_policy` | none | Sender reads common policy and public library identity/version, with tone weights and slot rules. Hidden entry names are excluded. |
| `host_mission_policy` | `policy`, `policy_revision`, `reason` | Current campaign host. |
| `host_mission_preview` | `target`, `intent`, `reason` | Current host, pure preview with no step ID. |
| `host_mission_apply` | `target`, `intent`, `reason`, `preview_digest` | Current host, guarded application with step ID. |

There is no player-originated request/publish/event operation. Players ask the
referee conversationally. Referee authority does not depend on whether the
current sender is the host. `mission_commit` and `mission_withdraw` instead
use the job's authenticated sender and frozen character. No character ID in
their tool arguments can substitute another player. Host correction tools use
the existing guarded host authority and are unavailable in a guest's job.

Opening or continuing an active join proposal is a referee operation with its
own revision. The joining player uses the same commitment control. The join
applies only after that valid commitment. Existing members' participation
records and promised terms stay intact unless they separately commit a revised
proposal. A cancelled or withdrawn active-change proposal leaves the existing
mission running under its current terms.

### Stable errors

Reuse invalid_request, request_conflict, stale_revision, referee_busy,
mode_forbidden and existing actor/session errors. Add these codes.

| Code | Meaning |
|---|---|
| `mission_unsupported` | The selected module lacks Missions version 1. |
| `mission_unavailable` | Missing record or caller lacks access. |
| `mission_generation_failed` | No compatible assembly exists within the fixed attempt bound. |
| `mission_offer_stale` | Pinned content or reconstruction digest differs. |
| `mission_proposal_stale` | Proposal/offer revision or eligibility changed. |
| `mission_not_ready` | Required prompts or commitments are missing, or another mission owns the preparing/active slot. |
| `mission_condition_false` | A requested objective/event/discovery does not meet its declared conditions. |
| `mission_due_first` | An earlier due occurrence must resolve or validly lapse. |
| `mission_clock_changed` | Invalid tick decrease or incompatible outstanding clock domain. |
| `mission_too_large` | A proposed record or result exceeds its declared bound. |
| `mission_page_stale` | The read cursor's game revision changed. |
| `mission_effect_failed` | The module's effect preparation/execution failed. No partial change committed. |

Errors contain safe diagnostics for that audience and IDs only when permitted.
Never leak hidden effect names or facts through a guest's refusal. An exact
committed retry returns its receipt; a new incompatible operation is refused.

## 12. CLI and browser controls

Both clients call the same messages. Neither builds or validates missions.
Player controls display their actual proposal and character before commitment.
The CLI adds these commands.

```text
/missions [open|active|closed] [more]
/mission ID [offer|objectives|observations|reveal] [more]
/mission commit PROPOSAL
/mission withdraw PROPOSAL
/mission leave ID REASON
/mission policy
/mission host ID SECTION [more]
```

Commit/withdraw read the current proposal revision and show its summary before
sending. A changed proposal requires a new review by the player. A lost reply
uses the original request ID for exact retry and the receipt for recovery.
IDs are copyable controls; the referee can perform the same action from the
player's natural-language request through its authenticated commitment tool.

The browser shows offers, the active proposal/mission and reveal links within
Console/Settings. Commit and Withdraw use the same actor/revision guards.
History pages preserve their explicit More cursor and refresh on page staleness.
Opening a reveal is deliberate. It does not automatically replace the mission
summary with spoilers. Host private inspection is a deliberate host action.
This contract does not settle the separate text-composer/location redesign.

## 13. Schema upgrade and adoption

Upgrade campaign schema 10 to 11 in one transaction that adds the tables and
indexes. Preserve all existing rows and module state. The registry remains
schema 2. Existing supported older campaigns follow their current upgrade path
through schema 10, then this additive step. Validate runtime binding before
committing an upgrade, as the store currently does.

Games without the capability have empty mission tables and unchanged behavior.
No mission, person, observation, command, payment or dice result is invented
from legacy prose. Adopting a game's legacy mission fields is a separate typed
host operation with a backup and explicit source mapping. Framework missions
become the authority only when that module integration is adopted. Retire or
label legacy caches so current context does not show conflicting mission states.

Active mission records carry immutable selected preparation and required effect
versions. A package upgrade can leave them readable, but executing effects
needs those versions. A replacement must retain compatibility or provide an
explicit reviewed migration. Unknown versions refuse new mutations while
operator/host historical inspection remains available.

An old schema-10 binary refuses schema 11. Rollback restores both campaign and
registry backups plus matching packages and local referee configuration. Never
only switch binaries against an upgraded live campaign. MS-7 provides the
specific handoff and checks against its actual pins.

## 14. MS-1 review and downstream acceptance

This contract is complete when owner and independent review resolve its
proposed API, lifecycle, bounds and message choices. It needs no live AI run.
Later slices demonstrate the following cases using original game fixtures.

1. An opting-out module retains behavior through schema upgrade. Upgrades
   preserve existing rows, fail atomically and refuse incompatible bindings.
2. Fixed inputs, library content and seed produce identical skeletons on
   supported Python versions. Changed pinned content refuses expansion.
3. Generated and named premade missions use the same runtime. A played premade
   stays used after abandonment and restart. Withdrawn preparation consumes none.
4. Offer inspection and refresh produce no randomness, commitments or game time.
   A dry-run command prints complete generated missions from explicit test inputs
   and seed, with a bound count, while writing no campaign database.
5. Required capabilities and personal-history restrictions constrain the actual
   roster. Preparation withdrawal and terms changes invalidate relevant consent.
   Unrelated character activity preserves commitments. Revoked/archived/transferred
   characters cannot begin through a stale proposal.
6. Essential evidence has two independent plausible routes. Observations,
   sharing, failed approaches and source reliability preserve exact provenance.
   A clean no-roll route can finish a calm mission.
7. A trade states its cost before choice. Other tripwires have a valid clean
   route, a visible sign and a supported stopping condition. Alternate objective
   branches require the affected players' own consent.
8. One event's effects, receipts, checklist changes and status remain atomic
   through rejected effects, budget failure, duplicate requests, restart and
   concurrent mutations. Expected failures draw nothing.
9. A long advance catches due chains in effective-time order through minute,
   sub-minute and game-defined clock mutations. It cannot skip an earlier event
   by completing an objective. Domain changes with outstanding events refuse.
10. Discovery selects an eligible unseen egg and preserves compatible choices
    for remaining slots. Opening again or revealing a closed mission draws none.
    Object receipt follows the established action. Disabled/exhausted catalogs
    place no unusable slots.
11. Closure preserves every declared carried consequence with its due time.
    Many carried events remain durable and paged through a second mission and
    restart. No record count limit prevents close or silently discards an effect.
12. Actual participants who later leave retain reveal access while authorized.
    Unplayed commitments grant none. Private observations, thread restrictions
    and continuing-consequence spoilers remain correctly projected.
13. Both clients show the same proposal, active mission, observations and reveal.
    Busy rejection, reconnect and exact retry preserve actor and audience rules.
    Maximum legal preparation and due sets fit bounded reads and composed host
    and guest job contexts without silently cutting records.

MS-2 owns generation and library validation. MS-3 owns the lifecycle, tables,
tools and context. MS-4 owns observation/event/clock behavior, eggs, threads and
closure. MS-5/MS-6 expose the agreed controls. MS-7 exercises the whole path
with scripted referee output, then records an independently reviewed live
deployment procedure. Implementation may share prerequisite helpers through
small focused PRs while preserving those acceptance responsibilities.
