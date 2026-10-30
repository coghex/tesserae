# Tesserae design

Design state: `exploring`

This document records the owner's agreed direction and distinguishes it from
implementation proposals. The public repository is being bootstrapped locally;
there is no remote or tracker yet. No issues have been created.

## Purpose and experience

Tesserae supports tabletop play with an AI referee. The player describes or
selects actions; the referee interprets intent, describes the world, runs NPCs,
and adjudicates uncertain rules. Executable tools resolve supported mechanics
and preserve reliable game state. One locally hosted server owns the game. A thin
streaming CLI is the first game client; a browser later displays the player's
view and offers interactions. Friends can join the same host from other computers.

The diversity of rulesets is a central motivation. A game can use a different
set of concepts and mechanics without being forced into a universal character,
combat, inventory, or turn model.

## Verified starting point

- A new local Git repository with `main` and an initial `.gitignore` commit.
- No pre-existing application, dependency configuration, remote, or tracker.
- A separate private pilot has demonstrated conversational play backed by Python
  dice tools, structured character/campaign records, and a browser map.
- That pilot has not been imported into this repository. Its original records
  and source materials must remain unchanged during bootstrap work.
- The standalone Python dice CLI is now implemented and verified with ten tests;
  live game storage, module loading, browser integration, and model integration
  are not implemented yet.

## Agreed direction

### D-1. Public application with private external content

The application should be publishable independently of the private pilot.
Private rulesets, source materials, adaptations, and campaign files live outside
the repository and load through explicit local configuration. An ignored folder
is not the primary separation mechanism. Tracked history also matters.

Do not copy private game names, paths, examples, source extracts, campaign
records, or game-specific assumptions into public code, tests, fixtures, or docs.
Existing tools must be inspected before any reuse. Publication and license
selection remain separate, explicit steps; this design is not a rights review.

### D-2. AI referee from the beginning

No human referee is required in the intended play experience. The AI referee
handles interpretation and judgment. The player retains control of their
character's decisions and commitments. A ruling is recorded when it changes
ongoing mechanical interpretation, so later sessions can apply it consistently.

### D-3. Optional modules rather than universal game concepts

Dice, maps, inventories, combat, and other features are optional. Descriptive tags
help identify games and features; explicit module selections determine executable
behavior. Adding a descriptive tag alone does not silently activate code.

### D-4. Dependencies refer to required operations

A module declares what it needs from another provider. For example, one crafting
module may require item quantities and transfers from an inventory provider.
Another may require abstract resources instead. The former is not a universal
definition of crafting.

Start with small, explicit contracts. Do not invent a general dependency language
before actual modules establish a need for it.

### D-5. Explicit provider selection and replacement

A configuration chooses which implementation supplies an operation contract.
Two competing providers must produce an understandable configuration error unless
one is explicitly selected. Module load order must not decide which rules win.

Prefer replacement of a whole provider initially. Fine-grained override chains,
hooks, and runtime patching are deferred until a concrete game requires them.

### D-6. Reliable execution and state

Scripts handle arithmetic and other operations that need consistency: resource
changes, item transfers, ammunition, related updates, and action history.
The AI referee supplies decisions and adjudications through explicit operations;
it must not maintain a second authoritative character sheet in prose.

The mechanics and stakes of a consequential roll are established before rolling.
Do not replace or retroactively modify dice because of their results. Explicit
corrections and rulings preserve the original history.

### D-7. Extract boundaries from working play

Begin with one private game module alongside a small original public demonstration
game. Extract components when their responsibility is clear and they can be
demonstrated independently. Do not design a universal rules engine in advance.

### D-8. Bounded first milestone

The target is the already demonstrated style of play: conversational actions,
character/equipment state, maps, checks, combat bookkeeping, recorded rulings,
and persistence. It does not mean completing every rule in the private game.

Keep new features out until that loop works. Crafting, module marketplaces,
elaborate override systems, and additional full games are deferred. Basic play
with friends on one locally hosted server is in scope following D-11; richer
multiplayer features remain deferred.

### D-9. One-time local bootstrap delivery

The owner explicitly approved committing the README and these two design documents
locally and fast-forwarding `main` to the completed bootstrap work. This is a
one-time exception because `docs-push` requires an origin and the repository has
none. It does not authorize remote publication or change future delivery policy.

### D-10. SQLite for live state; JSON for exchange

The owner accepted the proposed storage direction: SQLite owns the host's live
persisted state, with JSON import/export for portable records. Clients do not
open the database. State changes and their recorded outcomes must commit together.

### D-11. One authoritative host with several clients

Local-first means a locally hosted server that friends can connect to. The host
runs the rules modules, stores state, and coordinates the AI referee. Campaign
commands from every client pass through that authority, including clients on
the host computer. No client directly edits the database or decides a mechanical
outcome on behalf of the host.

Player identity, character-control permissions, player-specific information,
concurrent requests, and reconnection belong in the foundation. A friend using
another client must see the same committed game, subject to their own visibility.

### D-12. Server command handling, then CLI, then browser

The owner explicitly selected this order: command handling on a minimal server,
a working CLI client, then a browser client. Prove several connections against
the host before implementing a browser UI. The existing offline dice utility
remains useful but is not a campaign client.

### D-13. Thin streaming CLI and focused tests

The CLI should act like a bidirectional input/output connection. It sends input
without owning game logic and receives results and server-initiated messages
while connected. Avoid a terminal RPG, full-screen interface, custom terminal
editor, or extensive terminal-specific test suite. Reuse libraries for transport
and, if needed later, interactive line editing. Test Tesserae's own contracts and
failure behavior rather than reimplementing a library's conformance suite.

### D-14. Local documentation delivery until an origin exists

The owner explicitly authorized local documentation commits and fast-forwarding
`main` while this repository has no origin. This replaces the need for another
one-time documentation exception on each edit. Keep edits isolated and the primary
checkout clean. Once an origin exists, use the standing `docs-push` workflow again.
This exception applies only to documentation; it does not authorize publication,
remote creation, or a bypass of code delivery/review requirements.

## Proposed implementation boundaries

These are proposals to refine as implementation proceeds, not additional signed-off
product requirements.

### Small host and optional providers

The host loads a selected game, routes commands, coordinates state changes, and
returns a player projection or a referee projection. Feature-specific data and
meaning belong to the selected modules.

An inventory provider can implement ownership, quantities, transfers, and drops.
Using an item delegates its effects to an action or a referee ruling; it does not
universally mean decrementing its quantity. Maps do not assume a particular unit
or that every game uses a grid. Dice arithmetic does not imply a success rule.

### One command path, several clients

CLI and browser clients send requests to the host; eventual model tools invoke
the same host-side command handlers with explicit referee authority. Keep terminal
formatting, network transport, and model-provider code out of mechanical implementations.

Each mutating command has a stable request ID and expected state revision. Resolve
identity and permissions on the host, not from an actor name asserted by a client.
A retry must not repeat a payment, consume another item, or roll again. Reusing an
ID for different input is an error. Failed validation leaves no partial mutation.

Commit a command's state change, result, and history atomically before publishing
updates. A dropped connection after commit does not undo the action. Retrying or
reconnecting retrieves the committed outcome. A host establishes ordering for
concurrent mutations; the game module decides which intentions may become actions.

The standalone bootstrap dice utility is not yet a persisted game command and
must be labeled accordingly.

### State and history

SQLite is the authoritative live store on the host. Keep the resulting actions,
rolls, rulings, and resource changes together with enough provenance to explain
the current state. JSON is an import/export format, not a second live authority.

Do not call the system event-sourced unless reconstruction from events is actually
implemented and verified. Derived values should be recomputed through the owning
provider rather than edited independently.

### Player and referee views

Every player client receives an explicitly constructed player view, not a complete game
state with secrets hidden by CSS. Referee preparation and private source context
remain separate from player payloads. Modules own the disclosure of their fields.
Do not blindly broadcast full state or referee events to all connections. Apply
the recipient's visibility before sending snapshots, results, updates, or errors.

An external module can contain executable Python and should initially be treated
as trusted local code, not a sandboxed third-party package. Remote installation
and an untrusted extension security model are outside the first milestone.

### Transport and CLI proposal

Use WebSocket messages for the duplex connection and the Python `websockets`
library for server/client transport. This is a proposal pending owner selection,
not an installed dependency. The same application message contract can later be
used through a browser's WebSocket connection.

The library handles WebSocket handshakes, framing, ping/pong and closing. Tesserae
still owns authentication, command validation, permissions, persistence, replay,
and deciding which events each player receives. Transport reconnection alone is
not application recovery or permission to resend an action with a new ID.

Begin server probes with the library's included interactive client. Add a small
Tesserae wrapper when session joining, identity, structured errors, or pipe mode
requires it. This keeps the initial focus on command handling.

Proposed pipe mode: one JSON object per stdin line becomes one WebSocket message;
each incoming application message becomes one JSON line on stdout. Diagnostics
go to stderr. Results identify their request; unsolicited events are distinct
message types. No prompt, terminal control sequences, or local rules are needed
in pipe mode. Input and output run concurrently rather than waiting for a reply
after every input. Define EOF/disconnect handling when implementing the adapter.

For a human-friendly prompt later, consider `prompt_toolkit` asynchronous input
and `patch_stdout()` so incoming output does not overwrite text being edited.
This is optional and must not become a terminal UI project. The stock interactive
client is a development probe, not a complete authenticated campaign client or a
guaranteed machine-readable pipe implementation.

Start with a minimal host process and one campaign. Establish player identity
and visibility before enabling the friends-on-LAN acceptance check. Default to a
loopback listener during bootstrap and make LAN binding explicit. Defer the web
asset server/framework decision until the browser slice; keep handlers reusable
if that introduces another server adapter.

### Verification focus

Concentrate automated checks on a few meaningful boundaries:

- Command validation, authorization, and stale revisions leave state unchanged.
- A committed action survives restart and a repeated request returns the original
  outcome without another roll, resource change, or duplicate recorded event.
- Two connected player identities observe consistent state while each receives
  only their permitted information.
- Malformed application messages and a disconnected client do not damage the
  game or prevent another client from using it.
- The thin CLI can send input, receive an unsolicited event, and terminate
  cleanly. Pipe stdout remains parseable; diagnostics stay on stderr.

Test shared command semantics below the network layer, plus a small set of real
host/client integration checks. Do not test every terminal keystroke, mock every
library method, or duplicate WebSocket protocol tests. Libraries reduce the code
we own; they do not eliminate tests for our lifecycle and integration behavior.

### Import the private pilot deliberately

Do not relocate or rewrite the existing pilot to make the first CLI work. Introduce
an importer later, retain originals, and check resource/gear/scene equivalence.
Review inherited mechanical interpretations rather than treating past play as
proof that every rule implementation is correct.

## Open questions

- **Q-1. Conversation host:** keep conversation in an existing assistant with
  Tesserae alongside it, or embed the conversation and model calls in the app?
  Defer model integration until this is chosen; do not block shared commands.
- **Q-2. Persistence:** resolved by D-10: SQLite on the host, JSON import/export.
- **Q-3. First complete loop:** select the exact original demo scene and the bounded
  private-game mechanics used to validate it. Do not promise full game coverage.
- **Q-4. Module contract:** discover the first provider contracts from the demo and
  private module, then make required operations and compatibility checks explicit.
- **Q-5. Local documentation delivery:** resolved by D-9 for bootstrap and D-14
  for subsequent documentation edits until an origin exists. No remote is set.
- **Q-6. Public release:** select a license and review the public tree and tracked
  history before publication. No publishing is authorized by this design alone.
- **Q-7. Referee pacing:** when do several players' messages become committed
  actions, and when should the AI referee respond? Do not equate every incoming
  text message with advancing time or assume all games have combat-style turns.
- **Q-8. Transport selection:** `websockets` and optional later `prompt_toolkit`
  are the researched recommendation. Confirm before adding runtime dependencies;
  the server-first order itself is already settled by D-12.
- **Q-9. Joining and recovery:** choose the initial host-issued player credentials,
  session join flow, and snapshot/event recovery contract in the server slice.
  Caller-supplied names alone are not authentication. No cloud account system is
  required for local friends, but permission and disclosure checks are required.

## Technical references for the transport proposal

- [websockets features](https://websockets.readthedocs.io/en/stable/reference/features.html):
  transport handshake, messages, ping/pong and closing support.
- [websockets introductory tutorial](https://websockets.readthedocs.io/en/stable/intro/tutorial1.html):
  Python server, included interactive client, and browser message exchange.
- [websockets asyncio client](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html):
  send/receive API and transport reconnection behavior.
- [prompt_toolkit asynchronous prompts](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html#prompt-in-an-asyncio-application):
  asynchronous terminal input and output while editing a prompt.

## Scope discipline

- Every implementation slice produces a runnable behavior or closes a specific
  boundary required by the next slice.
- Do not build features solely because another game might eventually use them.
- Record proposals separately from approved direction; keep unresolved questions
  visible instead of silently making them requirements.
- Tests should verify real invariants and failure behavior, not mirror code.
- Pause for consequential product decisions; resolve ordinary implementation
  details within the agreed scope.

See [Milestone 1](milestone-1.md) for the proposed delivery order and acceptance
signals. This document is not yet a ready-to-file epic or a completed specification.
