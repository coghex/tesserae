# Missions

Status: **rough draft for refinement**, 2026-11-12. It records the owner's
direction and a first proposal for how the framework could carry it. Nothing
here changes runtime behavior. Public baseline:
`d76deddb94cccedfb7c59b62808359d8f1e77577`.

The [MS-1 contract proposal](missions_contract.md), under
[#199](https://github.com/coghex/tesserae/issues/199), specifies the proposed
hooks, records, tools, messages and migration for the next delivery slices.
The [delivery roadmap](missions_delivery.md) links public tracker #202, private
integration tracker #203 and their dependency-ordered children.

It would extend the [referee contract](referee_contract.md), the
[character contract](character_contract.md) and the
[governance contract](governance_contract.md). Its shape follows
[negotiated creation overrides](negotiated_creation_overrides_design.md):
an optional module capability, records kept by the host, tools for the
referee, controls for players, and a game that supplies the specifics.

Public examples and tests use only original public content. No private
module names, paths, data, rules, source material or transcripts belong here.

## Problem and outcome

Today a referee improvises a job from free-form preparation that a module
may store. When the players take the obvious route and it works, nothing was
prepared along that route: no discovery, no decision, no consequence. The
job succeeds and is dull. Nothing in the framework represents a mission, so
nothing can check one, offer one, or say afterwards what was missed.

Add an optional **missions** capability. Python assembles a mission from a
game's library, or loads one written by hand. The referee fills in the prose
and runs it. Players choose and commit their own characters. The host keeps
the records, applies prepared consequences atomically and closes the mission
with a reveal. Games without the capability are unaffected.

## Owner decisions

The owner has agreed:

- **One mission per request.** The tool returns one mission for one request:
  generated at a requested length (short, medium or long), or a named
  premade. Offering a short, a medium and a long one together is how a
  referee normally uses it, not a structure of the tool.
- **Objectives.** A mission has 1–2 primary and 0–4 secondary objectives.
- **Routine success stays routine.** Some missions are peaceful. Tension
  comes from play: a decision or an outcome can start a consequence. A time
  limit announced at the start is not the main source of tension, and
  pressure is never the permanent state of play.
- **Tone.** Each mission has a tone, calm, reactive or volatile, drawn by
  Python independently of its length.
- **Premade missions.** Handwritten missions, complete in every part, are
  requested by a stable name and use the same form and runtime as generated
  ones.
- **Easter eggs.** A short mission has no egg slot, a medium mission one, a
  long mission two, and players are told this rule. What an egg is gets
  rolled only when its slot is found, from the eggs the campaign has not
  met. A campaign checklist records eggs met. When the list is exhausted
  there are no more.
- **Open mechanics, hidden spoilers.** How the system works is open to
  everyone. A particular mission's spoilers are kept out of offers and
  ordinary views by default but are not locked away. The host sees
  everything, as today.
- **Reveal at close.** A closed mission's spoilers can be shown to its
  players. For eggs it shows how many were found, with the found ones'
  details, and only a count of slots that went unfound. Everyone who
  actually took part keeps access to the reveal while they keep access to
  the campaign, including a participant who left before the end. Found eggs
  reveal all layers, including unexamined ones, subject to continuing-story
  and unresolved-consequence redactions.
- **Commitment.** Each player commits their own character.
- **Collective abandonment.** Every active player confirms abandonment through
  the proposal mechanism. They can confirm conversationally through the referee.
  Individual departure remains that player's action.
- **Joining in progress.** A character can join a mission that is already
  under way, by their player's own commitment to stated terms. This is part
  of the first delivery.
- **Personal threads.** A mission can bind one of its stakeholders to someone
  from a participating character's recorded history. Threads can carry a
  character's longer story across many missions.
- **Content belongs to games.** A library is judged by the quality of its
  entries. The framework supplies the machinery and an original example
  library; each game supplies its own content and effects.

Implementation defaults in this draft, not owner decisions: one active
mission per campaign; at most five open offers; equal tone weights; the
tripwire counts per tone; every size bound; that an offer states what a
roster must be able to do instead of naming a fixed roster; that a
consequence ends with its mission unless its entry says it outlasts it;
that carried consequences are kept durably without a count limit, with
bounded views, rather than reserving capacity when a mission starts; and the
rules for threads' boundaries. The owner intends to read about ten generated
missions before settling weights and counts, so the first delivery includes
a way to produce them without touching a campaign.

## Vocabulary

| Term | Meaning |
|---|---|
| Mission | Prepared work with objectives, people, places, hidden truths and prepared events. |
| Offer | The short, disclosed summary of a mission that players can choose. |
| Objective | A stated result with an observable completion condition. Primary or secondary. |
| Component | A library entry of some kind: client, stakeholder, location, secret, obstacle, clue, reward, discovery, tripwire, twist. |
| Tone | Calm, reactive or volatile. It sets how many tripwires are prepared. |
| Tripwire | A decision point with a consequence: a choice or outcome that starts something, and the choices that do not. |
| Consequence | What a tripwire starts. It runs on the game's own clock from the moment it is tripped. |
| Fact, clue, observation, claim | A prepared truth; a source of evidence for it; the record that a character met that source; what a person or document asserts. |
| Event | A prepared happening with a condition, effects and a status. Tripwires, discoveries and twists are events. |
| Twist | An event that changes the stakes, the relationships or the objectives. |
| Egg slot, egg | A prepared hiding place or odd spot; the strange thing rolled for it when found. |
| Premade | A mission written by hand and requested by name. |
| Thread | A link between a mission and a person from a character's history, which can continue across missions. |

## Framework and game responsibilities

Add an optional versioned `Missions` capability beside `CharacterRules`,
`CampaignSettings`, `Administration` and `CreationOverrides`. It needs a
character game.

| The framework owns | A game supplies |
|---|---|
| The mission form and its validation. | Its library: templates, themes and components. |
| The generator: seeded choice, requirements and exclusions, tone, tripwire counts, egg slots by length, digests. | Its premade missions and its Easter eggs. |
| Offers, publication, each player's commitment, expansion, the active mission, close and reveal. | What a roster can do: its roles and capabilities, and which named mechanics exist. |
| Typed conditions, event status, atomic resolution, observation records, the checklists. | Effect handlers, which change the game's own state for a named effect. |
| Authority, privacy, revisions, idempotency and exact replay. | Its clock: the current game time and how to compare and advance it. |
| What the referee, each player and the host see, within the bridge's bounds. | The people in a character's history, for threads. |

A game's hooks are pure and deterministic except the effect handlers, which
run inside the host's transaction and may draw dice through the existing
operations. A handler returns validated state or refuses with nothing
changed. An effect name the game does not declare makes a component
ineligible before any offer is made: the framework never improvises an
effect, and neither does the referee.

Library data is data. It has stable IDs, weights, requirements and named
references. There is no expression language, no script and no path into a
game's state.

## The mission form

One form serves generated and premade missions. A sketch, not a schema:

```json
{
  "id": "m-example", "origin": {"kind": "generated", "seed": "...",
    "generator": 1, "library": "example@3", "digest": "..."},
  "length": "medium", "tone": "reactive",
  "summary": {"title": "...", "premise": "...", "disclosed_objectives": ["..."],
    "scope": "...", "terms": "...", "known_risks": ["..."]},
  "objectives": [{"id": "return_the_pack", "role": "primary",
    "target": "pack", "complete_when": "...", "status": "open"}],
  "people": [{"id": "client", "component": "deadline_operator", "name": null}],
  "places": [{"id": "hut", "component": "shared_premises"}],
  "facts": [{"id": "pack_was_swapped", "truth": "...", "required_by": []}],
  "clues": [{"id": "ledger", "origin": "hut_keeper", "place": "hut",
    "supports": ["pack_was_swapped"], "access": "...", "observation": "..."}],
  "events": [{"id": "forced_entry", "kind": "tripwire", "when": {"...": "..."},
    "signs": "...", "effects": [{"start": "keeper_returns", "after": 20}],
    "status": "prepared"}],
  "egg_slots": [{"id": "slot-1", "kind": "cache", "place": "hut", "access": "..."}],
  "prompts": ["Name the client.", "Describe the hut."]
}
```

`summary` is everything an offer may disclose. Everything else is a spoiler
until play reveals it. `prompts` are the gaps the referee must fill before
the mission starts. Names the referee publishes in an offer cannot be
contradicted later.

### Library entries

| Field | Purpose |
|---|---|
| `id`, `kind`, `version` | Identify an entry and its type. |
| `weight` | Nonnegative integer selection weight. Zero disables selection. |
| `requires`, `provides`, `excludes` | A small vocabulary of properties, roles and named mechanics. |
| `bindings` | Named references to a selected person, target, place or objective. |
| `facts` | Truths the referee uses to keep the component coherent. |
| `prompts` | Details the referee must fill. |
| `effects` | Named effects the component may apply, each declared by the game. |
| `audience` | Whether an offer or a later discovery may disclose it. |

References are explicit: a client, a stakeholder and an objective that mean
the same shipment point at the same target ID. A template declares which two
primaries can be paired and how they depend on each other.

### Length, tone and tripwires

| Length | Primaries | Secondaries | Egg slots | Scale |
|---|---:|---:|---:|---|
| Short | 1 | 0–1 | 0 | One principal site, a compact sequence of choices. |
| Medium | 1–2 | 1–2 | 1 | Several linked scenes. |
| Long | 2 | 2–4 | 2 | Several stages, with distinct places or stakeholders. |

| Tone | Tripwires prepared |
|---|---|
| Calm | None. Optional discoveries only. |
| Reactive | One or two. |
| Volatile | Three or four; one consequence may arm another. |

The rules that keep tripwires fair:

- No consequence is in motion when a mission starts, in any tone. A
  deadline that is a disclosed, ordinary term of the job is not a
  consequence and may exist from the start.
- Every tripwire has at least one clean way through that needs no roll. The
  generator attaches a tripwire only where the mission actually has such a
  way: an obstacle with a listed approach that calls for no roll.
- A tripped consequence has a prepared sign the players can perceive, so
  they can connect it to what they did.
- A consequence either has a prepared way to stop it, or its entry declares
  it a **trade**: it cannot be undone, and its cost must be perceivable
  before the choice is made, not only afterwards.
- A failed check trips a tripwire only where the entry says so.
- An offer does not state the tone. "Known risks" are what the client knows
  and says.

## Requests, offers and expansion

**Request.** The referee asks for one mission: a length for a generated one,
or a premade's name, optionally narrowed by template or theme, and the
characters it is meant for (by default every ready character). Anything left
open is drawn by Python. For a generated mission the host:

1. records the compact inputs the generator will use: policy, those
   characters' capabilities, the people and places already established;
2. draws a seed from its journaled random source;
3. runs the whole generator in memory, with bounded backtracking, and
   refuses clearly if nothing compatible exists;
4. validates the result: references, dependency cycles, count bounds, clue
   coverage, declared effects, the tripwire rules, size;
5. stores only the offer: its ID, length, seed, generator and library
   versions, recorded inputs, a digest of the full result, the summary and
   its **requirements**: the capabilities the mission relies on and any
   character a thread is bound to.

For a premade the offer stores its name and version. A premade is refused if
the campaign has already played it or lacks what it declares it needs.

**Offer.** The referee writes a short pitch at the offer's revision and
publishes it. Reading or asking about an offer commits nothing. Before a
choice the referee answers from the published summary and does not invent
undisclosed specifics.

**Expansion.** Once the roster has committed, the host produces the full
mission. For a generated offer it runs the generator again from the stored
seed and inputs with the pinned versions, and compares digests. A mismatch,
or a library that is no longer the pinned version, refuses: the offer is
stale and must be requested again. The host never substitutes a different
mission. The referee then fills the prompts, each refinement at the expected
revision and validated, and starts the mission. This can span several jobs.

Choices derive from the seed by a fixed, documented method that does not
depend on the Python version. The same seed, inputs, generator and library
always give the same result. The referee refers to an offer by ID and never
handles a seed. The referee's prose is not reproducible and is not expected
to be.

A **dry run** produces generated missions in full for a library and a set of
inputs without touching any campaign, so an author can read a batch.

## Commitment

An offer does not name a fixed roster. It states its requirements, and any
roster that meets them can take it.

The referee opens a proposal: one published offer and a roster. The host
refuses a roster that does not meet the offer's requirements, such as one
missing a capability the mission relies on or the character a thread is
bound to. A proposal has a revision, which changes whenever its roster or
the offer's terms change.

Each player on the roster commits their own character, in their own job or
with a control in their client. A commitment names the offer and the
proposal revision it was made to, and the host checks it against the
authenticated player. A commitment to an earlier revision does not count:
when the roster or the terms change, everyone still on it is asked again.
Activity that changes neither, such as a character spending money, leaves
commitments standing.

The host checks the roster again at expansion and again when the mission
starts: each character is still owned by the player who committed it, still
ready and able to take part, and the roster still meets the requirements. A
player can withdraw at any point before the start, including while the
referee is filling the prompts. A withdrawal makes a new proposal revision.
If the remaining roster still meets the requirements, the prepared mission
is kept and the remaining players are asked again. If not, the offer is
withdrawn with the reason; facts already disclosed about it stay disclosed,
and a replacement is a new offer with fresh commitments.

Offers not chosen are withdrawn when a mission starts. The roster need not
include every character.

The referee can cancel an unapplied proposal with a reason; the host can also
cancel through a guarded correction. Cancellation releases the proposal slot
and retains its history. Cancelling initial preparation withdraws that unstarted
mission without consuming a premade; cancelling an active-change proposal
leaves current membership and accepted terms intact.

### Joining and leaving a mission in progress

A character can join a mission that has already started. The referee
proposes the join: which ready characters, and on what terms, such as their
share of a reward and what they are told on arrival. The host checks them as
it checks any roster member: owned by their player, ready, and able to take
part. A join applies only when each joining player has committed their own
character to that proposal.

Existing members keep the terms they agreed. If a join would change those
terms, for example how a fee is divided, every member whose terms change
must commit to the change as well, and until they do the mission carries on
as it was. A proposal that is withdrawn or cancelled changes nothing.

Joining does not restart anything. Objectives keep their statuses, events
keep theirs, and consequences already in motion stay in motion. A joiner is
not credited with what was discovered before they arrived: they learn it
when a character who knows shares it, as an established action, and that
sharing is recorded like any other. Their participation is recorded from the
moment the join applies.

A player can take their own character out of a mission in progress. Leaving
is that player's action, never the referee's on their behalf; the host can
end a membership as a recorded correction. A character who leaves keeps
their participation history and their access to the reveal. Leaving does not
end the mission for those who remain, even when the roster no longer meets
the offer's original requirements; what the remaining party can still
achieve is for play to decide. A mission with no members left can be closed
as abandoned (see Close and reveal).

## Play

**Obstacles.** An obstacle states where it is, which objective it affects,
its visible signs, plausible approaches and what a failed attempt costs. A
routine approach needs no roll. The referee may accept an approach nobody
prepared and records that it did.

**Clues.** The generator does not interpret language or decide what players
believe. It prepares facts and sources of evidence. The referee decides
whether an action meets a source and records an observation: which clue, to
whom, when. Discovering a clue does not establish its fact for the players.
A fact a primary objective needs has at least two independent routes: not
two copies of one source, not two sources behind one gate, and at least one
needing no roll. Structural checks cannot prove a mystery is convincing; the
referee reviews that.

**Events.** An event has a condition, effects, a repeat policy and a status:
prepared, resolved, avoided or expired. Conditions are typed: an objective's
status, an observation, a declared place entered, another event resolved, a
game time reached, or a prepared question the referee adjudicates and
records with its reason. They combine with a bounded `all` or `any`.
Resolving an event applies all its effects and records the resolution in one
transaction, or changes nothing. A second request for a resolved one-shot
event returns its recorded outcome. Follow-up events are resolved
explicitly; nothing cascades unbounded.

**Consequences.** A tripwire's effect schedules a follow-up at a game time.
The game reports its clock; when the clock passes a due time, the host names
the due events in the command's result and in the referee's context, and
refuses to resolve an objective while an earlier one is outstanding. Real
elapsed time and disconnected clients advance nothing.

When one advance of the clock passes several due times, the events resolve
in order of their due times. A follow-up armed by an event is timed from
that event's due time, not from the moment the referee resolved it, so a
long wait cannot postpone a chain. If that makes the follow-up already
overdue, it is due at once and takes its place in the order.

**Twists** use the same mechanism. Their underlying truth is prepared before
play. A changed assignment or reward is put to the players and needs their
agreement; every branch keeps the objective bounds.

## Easter eggs

A slot is prepared with its own access condition, often off the direct route.
It has a kind, so a game can tell something found in a hiding place from
something that happens at an odd spot. Slots are prepared only while the
campaign has enough unmet eggs of fitting kinds; none once the list is
exhausted, and none if the game has written no eggs.

When a slot is found the host draws among the unmet eggs that fit, records
the egg on the campaign's checklist and returns its text. An exact retry
returns the same egg. A slot nobody finds uses nothing up, and nothing was
rolled for it. Policy can disable eggs. An egg may give a descriptive item
or a declared effect; an unusual label creates no mechanical bonus.

## Personal threads

A game may report the people in a character's history: family, friends,
rivals, whoever its rules record. For some missions the generator binds one
stakeholder to such a person from a participating character. The referee
shapes the story to fit them. The framework records the thread: which
person, which missions, how each ended. A later mission can continue an open
thread in preference to starting a new one, so a character's longer story
can play out over many missions. A thread never supplies a player
character's decision, feeling or consent, and a game that reports no history
simply has no threads.

An established person keeps their established nature. For each person, the
game's hook reports the relationship, the stakeholder roles they may fill,
the roles they must not, and anything that may not be disclosed. A campaign
may record, for instance, that a character's settled family is never to be
made a hostage, a debtor or a hidden opponent; such a person can still be a
client or a bystander with something useful. The generator binds only
permitted roles, the validator refuses a mission that breaks a restriction,
and the referee's refinement is checked against the same restrictions. A
game can also limit how often one character's people are drawn, so not every
job involves them.

## Close and reveal

A mission can close when every primary objective has a terminal status or
the party abandons it. The host also has a previewed, confirmed administrative
close for a stuck active mission, recorded as a host override to abandoned,
with its reason. It does not claim player agreement, complete objectives or
grant completion rewards. Empty-mission closure also permits memberships ended
by an audited host correction. All closure paths first settle what is outstanding:

- An event that is already **overdue** must be resolved, or lapsed with a
  recorded reason, before the mission can close.
- A consequence scheduled for later ends with the mission unless its entry
  says it **outlasts** the mission. One that ends is recorded as lapsed, with
  its due time and the reason.
- A consequence that outlasts the mission is carried into the campaign: it
  stays on the game's clock, appears in the referee's context when due, and
  is resolved then, whatever mission is running. It may attach to a thread.
  Every carried consequence has a due time, so each one eventually resolves
  or lapses. Carried consequences are durable records with no limit on
  their number: closing never fails for want of room and never drops one
  that was declared. Only what is shown at once is bounded: the referee's
  context names those due, and the rest are read in pages.
- Abandoning a mission follows the same rules; an entry can say what
  abandonment itself sets off.

Closing then records each objective's outcome, releases the campaign for a
new mission and enables the reveal for its players: the tone, tripwires
never tripped and what they would have started, consequences that lapsed,
secrets and discoveries missed, routes not taken, eggs found with their
details, and a count of egg slots that went unfound. A consequence still
carried is a spoiler until it resolves, so the reveal gives only how many
remain in motion. A fuller completion report is separate later work.

## Records

The host keeps mission records in the campaign file beside its other
records, not inside the module's state: offers, the active mission,
commitments, observations, event resolutions, carried consequences, the egg
and premade checklists, threads and closed missions. A module's state limits
therefore do not bound them, and an upgrade of existing campaign files is
needed. A mission's own lists are bounded by its preparation or by a fixed
count, and carried consequences are durable with bounded views, so no
record limit can leave a mission unable to finish. The command and step
journals keep full history.

## Authority and visibility

| Who | Can |
|---|---|
| Referee, in a play job | Request, refine and publish offers; open a proposal; expand and start; read preparation in bounded sections; record observations; resolve objectives and events; open an egg slot; close. |
| A player | Read offers and their own discoveries; commit or withdraw their own character, including to join a mission in progress or to agree to changed terms; take their own character out of a mission; read the reveal of a mission they took part in. |
| The host | Everything the campaign holds, as today, and corrections with a reason. Policy: which library, tone weights, whether eggs are enabled. |

The referee cannot add code or patch state: preparation is data for trusted
handlers. Routine requests and adjudication need no host approval. A
mission's contribution to the referee's ordinary context is small and fits
within the bridge's existing bounds; details are read on demand in bounded
sections that never truncate a clue or hide that something is missing.

## Example library

The public example game gets a small original library so the capability can
be tested and shown. A starting vocabulary, all original:

**Templates:** `recovery` (return a named thing to its recipient), `escort`
(get a person or shipment to a place), `investigation` (substantiate an
answer to one question), `watch` (keep something undisturbed for a period),
`broker` (bring two parties to an agreement), `locate` (find someone or
something whose whereabouts are unknown).

**Themes:** `divided_loyalties`, `scarcity`, `appearance_and_reality`,
`ambition`, `obligation`, `old_ties`, `small_mercy`.

**Tripwires** (trips when the party…): `forced_entry` (forces a way in),
`wrong_ear` (asks a particular person), `side_payment` (takes something from
another interested party), `opened_seal` (opens what was sealed),
`named_the_client` (uses the client's name openly), `shortcut` (crosses
ground someone controls), `told_a_lie` (deceives a witness they need again),
`left_a_mark` (leaves signs of a visit), `bought_the_answer` (pays for
information), `secondary_first` (does an optional goal first),
`showed_steel` (displays a weapon), `stopped_to_help` (helps a bystander),
`failed_quietly` (fails while trying to go unnoticed). `secondary_first` and
`stopped_to_help` are trades: nothing undoes them, and what they cost is
plain before the choice.

Clients, stakeholders, places, secrets, obstacles, clue kinds, rewards,
discoveries and twists follow the same pattern and are written with the
example game's own theme in the implementing slice. No eggs or premades are
published until someone writes original ones for the example game.

## Proposed delivery

| Slice | Deliverable |
|---|---|
| MS-1 | The contract: exact hooks, shapes, records, states, tools, client messages, bounds and the upgrade of existing campaign files. |
| MS-2 | The mission form and validator, the generator with seeds and digests, premade loading, the dry run, and the example library. |
| MS-3 | Offers, refinement, publication, commitment, expansion and start; joining and leaving a mission in progress; the referee's tools and context; authority, privacy, retries and stale offers. |
| MS-4 | Observations, atomic event effects through a game's handlers, consequences on a game's clock, twists, eggs and checklists, threads, close and reveal. |
| MS-5 | CLI controls for offers, commitment, the active mission and the reveal. |
| MS-6 | The same in the browser. |
| MS-7 | Model-free acceptance across CLI and browser: a party chooses an offer, plays scripted branches to a close and a reveal, then takes a second mission. |

All tests use Python fixtures or a scripted referee. No model runs.

## Acceptance outcomes

- A requested mission of each length meets its bounds, with consistent
  references. An undeclared effect or an impossible binding refuses before
  an offer exists.
- The same seed, inputs, generator and library reproduce the same mission on
  every supported Python version. A chosen offer expands to its recorded
  digest; a changed library refuses and never substitutes.
- A premade is offered by name, runs through the same runtime and is refused
  once played.
- A calm mission completes by its direct route with nothing happening. In a
  reactive one, one branch of a prepared decision starts a consequence on
  the game's clock and the other does not. No consequence is in motion at
  the start; a disclosed deadline that is a term of the job may be.
- A fact a primary needs has two independent routes, one needing no roll.
- A player can commit only their own character; the mission waits for the
  whole roster. A roster missing a required capability, or the character a
  thread is bound to, is refused. A withdrawal while the referee is filling
  prompts makes earlier commitments stale; the mission is kept if the rest
  still qualify and withdrawn if not.
- A character joins a mission in progress only by their player's own
  commitment. Existing members' terms change only by their own commitments.
  The joiner gains no earlier discoveries except by recorded sharing, and
  nothing restarts. A member who leaves keeps their history and can read the
  reveal; one who only committed and withdrew before the start cannot.
- An event's effects and status stay coherent after duplicate requests,
  stale revisions, interruption, restart and a refused effect.
- Slots follow the length rule. Opening one draws an unmet egg, records it
  and never repeats it. The reveal shows found eggs and only a count of
  unfound slots.
- A thread binds a stakeholder to a person from a participating character's
  history and can be continued by a later mission. A role a person is
  excluded from is never bound to them.
- A mission cannot close over an overdue event. A consequence scheduled for
  later lapses with a record, or is carried and resolves during a later
  mission. A chain crossed by one long wait resolves in due-time order.
- With many consequences already carried from earlier missions, an active
  mission arms several more that outlast it, closes, and the host restarts:
  every declared consequence is still recorded and scheduled, none was
  dropped, the close succeeded, and the referee's context stays within its
  bounds with the rest readable in pages.
- Offers and player views contain no spoilers; the host sees everything.
- Existing campaign files upgrade with their contents intact. A module
  without the capability behaves as before.

## Questions for refinement

1. **Tools or commands.** Should the referee reach missions through bridge
   tools, as it does creation overrides, or through module commands? Bridge
   tools know the job's player, which commitment needs.
2. **The clock.** What is the smallest interface that lets the framework
   schedule and detect due consequences without knowing a game's calendar?
3. **Effects.** How does a game declare an effect's name, arguments and
   preview so the validator can check a library before any offer?
4. **History for threads.** The character contract has no general notion of
   the people in a character's past. Is a game hook enough, or does the
   contract need one?
5. **Records.** Which tables, what bounds, and what a campaign file upgrade
   does to games that never use missions.
6. **Reveal.** Where it appears in each client, and whether a player who
   left the roster early sees it.
7. **Uninvited missions.** Later work: a mission with no client and no
   offer, where trouble simply arrives. Does the form above allow it?
8. **More than one mission.** One active mission per campaign is a default.
   What would a second concurrent one need?
