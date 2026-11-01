# Tesserae design

Design state: `exploring`

This document records the owner's agreed direction and distinguishes it from
implementation proposals. The public repository is now `coghex/tesserae` on
GitHub. The local bootstrap and GitHub's original MIT-license commit have been
reconciled without rewriting either history. A foreground, loopback-only
development host with SQLite persistence exists (tracker #1); authenticated
participants and the streaming CLI are complete for localhost under tracker #12.
Its [combined acceptance](acceptance/localhost-multiplayer.md) passed on master
at `a5f0a80`. Encrypted LAN transport is implemented and accepted for M1-3,
including a separately installed Ubuntu VM connecting to the Mac host; see the
[network acceptance](acceptance/encrypted-lan.md). Physical-device Wi-Fi
coverage is deferred under D-21.

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

- The repository began as a local Git bootstrap. It now tracks GitHub's public
  `coghex/tesserae`, default branch `master`, with the original MIT license preserved.
- A separate private pilot has demonstrated conversational play backed by Python
  dice tools, structured character/campaign records, and a browser map.
- That pilot has not been imported into this repository. Its original records
  and source materials must remain unchanged during bootstrap work.
- The initial standalone Python dice CLI had ten tests. Live SQLite storage,
  authenticated localhost sessions and the streaming client are now implemented,
  with 77 tests at tracker #12's acceptance. A small catalog of bundled modules
  is in place for M1-4. External module loading and bounded private validation
  completed in M1-5 through PR #62. M1-6's resumable Codex referee and both
  demonstrations completed through PR #80. The next agreed slice is the
  [localhost browser console](designs/browser_player_design.md).

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

Historical bootstrap record; current workflow is D-17.

The owner explicitly approved committing the README and these two design documents
locally and fast-forwarding the then-default branch to the completed bootstrap
work. This is a one-time exception because `docs-push` requires an origin and
the repository has none. It does not authorize remote publication or change future
delivery policy.

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

Historical policy, superseded by D-17. Do not apply its publication instruction.

The owner explicitly authorized local documentation commits and fast-forwarding
the then-default branch while this repository has no origin. This replaces the
need for another one-time documentation exception on each edit. Keep edits isolated
and the primary checkout clean. Once an origin exists, use the standing `docs-push`
workflow again.
This exception applies only to documentation; it does not authorize publication,
remote creation, or a bypass of code delivery/review requirements.

### D-15. Foreground server lifecycle

The owner's preferred foreground model is adopted for the first server. Start it
explicitly in a dedicated terminal; show startup, bound address, and useful errors.
Keep it running until the operator stops it. Ctrl-C stops accepting work, closes
connections, and leaves SQLite with either complete committed actions or no partial
change. Expected shutdown should not produce an unhandled-exception traceback.

Connecting a client does not implicitly start, daemonize, or install the server.
Closing a client does not stop the host. No service manager, launch agent, PID-file
daemon, or automatic reloader is required for this slice. Foreground execution
still permits concurrent client I/O and AI work inside the process.

### D-16. WebSocket transport library

The owner accepted the transport recommendation. Use Python `websockets` for
the initial host/client connection; begin manual probes with its included
interactive client. A custom CLI remains thin application-specific glue.
`prompt_toolkit` remains an optional later choice for interactive editing; pipe
mode does not require a terminal editing library.

### D-17. Casual manual development and project-scoped merge authorization

The owner explicitly directed that Tesserae not use the custom Kanban plugins,
their approval/review coordinators or gates, or `docs-push`. Those workflows belong
to the owner's professional projects, not this hobby project.

Use ordinary Git branches/PRs, with related code and documentation together.
Agents can draft and file issues autonomously. PR reviews are manual assessments
reported in conversation, not automatic review labels or posted verdicts. The owner
subsequently gave standing permission to merge PRs that are ready after manual
review and passing checks at the current head. A request to check a PR includes
merging it if ready, without another approval prompt. Unresolved blockers or failed
checks leave it open; explicit review-only or hold requests take precedence.
This permission applies only to Tesserae, not globally or to other projects.
Passing checks alone do not replace manual review.

Root `AGENTS.md` records this project-specific exception and overrides older
workflow instructions in this repository and issue descriptions. Earlier decisions
about product architecture, privacy, and issue scope are preserved.

### D-18. Default branch is master

At the owner's explicit request, GitHub's default branch and the local primary
branch were renamed to `master` after PR #7 merged. Preserve the existing commit
history; no rebase or force-push is needed. CI push filters, instructions, and
current repository links must use `master`.

### D-19. Encrypted LAN follows localhost acceptance

After tracker #12 closed, the owner selected encrypted connections for the first
LAN version. Tracker [#25](https://github.com/coghex/tesserae/issues/25) and the
[LAN delivery design](designs/encrypted_lan_design.md) describe the implemented
explicit authenticated listener, certificate-verified `wss`, personal tokens
and retained localhost defaults. Children #26–#29 are delivered, with combined
checks and the owner-selected full-VM network acceptance recorded at `632fb7e`.
D-21 supersedes the original physical second-computer gate. Public hosting,
router automation, browser and game modules remain outside this tracker.

### D-20. Browser work is beyond Milestone 1

The owner explicitly deferred the browser beyond Milestone 1. The first
playable loop uses the shared server/CLI surface and the selected referee
workflow. Module/private-game work and end-to-end play no longer depend on a
browser slice. [Milestone 1](milestone-1.md) records the revised order.
Preserve [browser-client-notes.md](browser-client-notes.md) for the later browser
milestone: the same personal token/join protocol can be reused, while browser
certificate trust needs its own operator/user experience. This does not add
browser serving or system certificate changes to the active LAN tracker.

### D-21. Accept a full VM for M1-3 network verification

A second physical computer was unavailable, so the owner chose Ubuntu in UTM
and authorized the installation and network test. The guest uses its own OS,
network interface, public checkout and Python venv. Real `connect` processes
inside it reach the Mac's explicit non-loopback TLS listener with a supplied
certificate and personal token. The [recorded result](acceptance/encrypted-lan.md#vm-network-check)
passed the runbook's multiplayer, recovery and credential checks.

Accept this VM result with the existing automated checks to complete M1-3 and
close #29/#25. Physical-device Wi-Fi isolation, device firewalls and independent
physical-link behavior remain deferred coverage. Record that limitation wherever
acceptance is claimed; the VM result does not establish physical-device coverage.

### D-22. Resumable Codex referee, with busy input rejection

For M1-6, the owner selected `codex exec` rather than a direct model-provider
API integration. Each invocation exits; a later invocation resumes the game's
saved Codex conversation by its explicit session ID. Keep one referee
conversation and one active job per game. Tesserae's durable game records remain
authoritative independently of Codex history.

Initially give the referee the complete context for the agreed bounded scene.
Custom selective context loading is a deferred stretch goal. Player-specific
privacy still applies. While the referee is busy, reject new player input;
input queues and steering are deferred. Read-only inspection and recovery
remain responsive. The owner also selected a host-controlled command bridge
within each invocation: all player gameplay goes through Codex. Unmarked
conversation broadcasts to the table; private routing is explicitly marked.
The
[M1-6 delivery plan](designs/codex_referee_design.md) records tracker
[#63](https://github.com/coghex/tesserae/issues/63), its filed children and the
engineering contracts the first child specifies before implementation.

### D-23. Localhost browser console and two-screen navigation

With M1-6 complete, the owner selected localhost-only browser delivery and
console first on 2026-11-01. The page has a collapsible left column with
exactly two navigation items for Milestone 2: Console and Settings. Console
is selected and open by default. Browser LAN access and interactive maps
follow this first browser tracker. The owner also selected memory-only browser
tokens initially; remembered login is a later design. The
[browser delivery plan](designs/browser_player_design.md), tracker
[#82](https://github.com/coghex/tesserae/issues/82), and its
[browser contract](designs/browser_contract.md) record the children and the
engineering boundaries, under [Milestone 2](milestone-2.md).

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

### Transport and CLI implementation direction

Use WebSocket messages for the duplex connection and the Python `websockets`
library for server/client transport. This direction is accepted under D-16, and
`websockets` is the declared runtime dependency. The same message contract can
later be used through a browser's WebSocket connection.

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

- **Q-1. Conversation host:** resolved for M1-6 by D-22: Tesserae launches
  `codex exec` and later resumes the saved session by explicit ID. No persistent
  Codex process or direct model-provider API integration in this slice.
- **Q-2. Persistence:** resolved by D-10: SQLite on the host, JSON import/export.
- **Q-3. First complete loop:** select the exact original demo scene and the bounded
  private-game mechanics used to validate it. Do not promise full game coverage.
- **Q-4. Module contract:** discover the first provider contracts from the demo and
  private module, then make required operations and compatibility checks explicit.
- **Q-5. Documentation delivery:** resolved by D-17. Use ordinary Git with related
  code/docs in the same branch/PR; do not use `docs-push` or a separate docs lane.
- **Q-6. Public release:** select a license and review the public tree and tracked
  history before publication. No publishing is authorized by this design alone.
- **Q-7. Referee pacing:** initially one active job per game; reject new player
  input while busy, with queuing deferred (D-22). Questions and clarification
  do not inherently advance fictional time. The draft's REF-1 child specifies
  exact job, pending-input and action boundaries before implementation.
- **Q-8. Transport selection:** resolved by D-16 for `websockets`.
  `prompt_toolkit` remains optional; the server-first order is settled by D-12.
- **Q-9. Joining and recovery:** resolved for localhost by
  [#13](https://github.com/coghex/tesserae/issues/13) under tracker
  [#12](https://github.com/coghex/tesserae/issues/12). The contract is in
  [protocol.md](protocol.md#authenticated-sessions). Decisions:
  - Host-issued, personal, revocable bearer tokens, provisioned by local
    operator commands into user-only token files. A token travels only in a
    first `join` message, never in a URL or command. Names are labels, not
    authority.
  - Authenticated mode is an explicit `--authenticated` serve option, on
    127.0.0.1 by default. Explicit non-loopback binding requires TLS and
    authentication (D-19). Fixed-identity development mode remains loopback-only;
    neither mode falls back to the other.
  - Joining has a 10-second timeout and happens once per connection. Nothing
    about the game is disclosed before success, and every credential failure
    looks the same.
  - The recipient's view is a metadata `snapshot`: game ID, own participant ID,
    and revision. A shared `revision` notification, carrying no participant or
    command details, goes to every joined connection. Notifications are
    monotonic per connection and may be coalesced. Subscribing before the
    snapshot closes the join race. Slow clients are closed (1013) rather than
    queued without bound.
  - Every message gets exactly one in-order reply, and notifications are never
    replies.
  - Credential validity is rechecked for each protected operation. For fresh
    actions the check happens inside the store's write transaction, so it is
    strictly ordered with revocation. Revocation never undoes committed work.
  - Recovery means reconnect, join, read the snapshot, and resend the uncertain
    command unchanged. There is no event log, automatic resubmission, or
    rewritten `expected_revision`.

  Participants and credentials (#14), the authenticated host (#15), snapshots
  and notifications (#16), and `tesserae connect` (#17) are merged. Tracker #12's
  localhost scope passed combined acceptance. Explicit encrypted LAN transport
  (#25) is also implemented and accepted under D-19/D-21; the linked acceptance
  records preserve the evidence and deferred physical-device coverage.

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
