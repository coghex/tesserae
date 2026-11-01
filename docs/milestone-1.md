# Milestone 1: one reliable playable loop

Status: complete through M1-6 / PR #80. Milestone 1 targets the server and streaming CLI. At the
owner's direction, the browser is deferred beyond this milestone; its notes are
saved separately. Individual slice details remain proposals where noted.

The next agreed slice is the [localhost browser console](designs/browser_player_design.md),
with Console and Settings in a collapsible left navigation. Its design is a
draft; browser implementation is not part of this completed milestone.

## Outcome

A locally hosted server supports multiple player clients through a thin
streaming CLI. A later browser client will reuse the same command contract.
A player can view an original small scene and a character, attempt an action,
have the AI referee use executable tools for supported mechanics, receive the
narrated result and updated structured state, and reload without losing or
duplicating changes. Browser UI and interactive map rendering are not acceptance
criteria for Milestone 1.

Validate equivalent workflows with one private game module. No private content
is required to run the public demonstration. Coverage is deliberately bounded:
character/equipment records, checks, movement/state inspection, the selected combat
actions, resource tracking, and recorded rulings. Specify the exact demonstration
before calling the milestone complete.

Acceptance includes two distinct player clients connected to the same host, with
consistent saved state and correctly separated private information. Demonstrate
a separately installed client joining over the network. The owner accepted
an Ubuntu VM for M1-3's network verification; physical-device Wi-Fi testing
is deferred in the [acceptance record](acceptance/encrypted-lan.md).

## Should the CLI come first?

Yes, as a thin streaming client to the minimal server, after host-side command
handling exists. It should send input and print responses/events concurrently,
including updates triggered by other clients. A pipe mode uses JSON lines with
diagnostics on stderr. The existing offline dice utility remains separate from
authoritative campaign commands.

Do not build a separate terminal RPG, interactive menu system, or text UI. The
browser remains the intended later player interface. Both clients must use the
same server command handlers. Use transport libraries and an existing interactive
WebSocket client for initial server probes; add only the application-specific
CLI glue needed. Browser work starts in a later milestone; preserve the
[browser notes](browser-client-notes.md) without implementing that scope now.

## Proposed implementation order

### M1-1. Standalone dice and CLI bootstrap — implemented and verified

- Create a small Python package and `python3 -m tesserae` entry point.
- Support dice arithmetic and inspectable individual results without game rules.
- Supply plain and JSON output; reject malformed expressions safely.
- Keep rolling logic independent of terminal formatting and argument parsing.
- Do not imply that these standalone rolls are persisted campaign actions.
- Verify arithmetic, malformed inputs, absence of implicit critical rules, and
  the actual command-line boundary.

This is a bootstrap utility, not completion of the state/command architecture.

Verification: `python3 -m unittest discover -s tests -v` passes all ten tests,
including command-line subprocess checks. Utility resource limits are 4,096 input
characters, 1,000 total dice, and 1,000,000 sides per die; these are implementation
limits, not rules imposed on all games. Dice support `NdS` (or `dS`), signed
integer terms, and addition/subtraction. No implicit criticals or fumbles.

### M1-2. Implement one durable command on a minimal server

Depends on M1-1. Storage is settled: host-owned SQLite, JSON import/export.
Transport is settled: `websockets`. Run the server explicitly in the foreground;
show startup/error logs and shut down cleanly with Ctrl-C.

- Establish one authoritative state representation and versioned records.
- Implement a command envelope and a state repository independently of the CLI.
- Prove one mutation survives restart and has an auditable event/result.
- Make a repeated request ID return the same result without applying twice.
- Check invalid requests and stale revisions without partial writes.
- Choose and test the failure boundary for updating state and recording history.
- Expose that command through a minimal server transport adapter. Keep command
  handlers and persistence independent of socket code.
- Define identity/join and per-player projections before enabling LAN access.
- Probe with an existing interactive client before writing terminal machinery.
- Start with a documented loopback-only development session. The first registered
  command can reuse the existing dice evaluator and persist its result/history.
- Do not implicitly launch the server from a client or install a background service.

### M1-3. Connect a thin CLI and prove several clients — implemented and verified

Depends on M1-2. The localhost portion is tracker
[#12](https://github.com/coghex/tesserae/issues/12). Its session contract (#13)
is in [protocol.md](protocol.md#authenticated-sessions). Participants and
credentials (#14) are merged, documented in
[game_store.md](game_store.md#participants-and-credentials). The authenticated
host (#15), snapshots and notifications (#16), and streaming `tesserae connect`
CLI (#17) are merged. The localhost portion of M1-3 is complete: tracker #12's
[combined acceptance](acceptance/localhost-multiplayer.md) passed on master at
`a5f0a80`. Encrypted LAN access, tracker
[#25](https://github.com/coghex/tesserae/issues/25), is implemented: TLS setup
(#26), the host (#27), the client (#28), and combined checks/runbook (#29)
are merged. The [acceptance record](acceptance/encrypted-lan.md) includes the
passed Mac-host/Ubuntu-VM flow at `632fb7e`. M1-3 is complete under the
owner-selected VM verification; physical-device Wi-Fi coverage is deferred.

- Maintain a duplex connection: send stdin while displaying incoming messages.
- Keep stdout machine-readable in pipe mode and diagnostics on stderr.
- Join with a host-recognized player identity; select a campaign without direct
  database access. Do not trust a claimed actor name in a command payload.
- Show two clients receiving permitted updates from one authoritative state.
- Verify retry after disconnect does not roll or apply an action twice.
- Test a small number of real process/connection lifecycle boundaries; do not
  build a keystroke-by-keystroke test suite or duplicate library conformance tests.

### M1-4. Introduce the original demo and minimal module loading

Depends on M1-3. Tracker [#37](https://github.com/coghex/tesserae/issues/37)
and the [demo/module delivery contract](designs/demo_modules_design.md) specify
the working original relay scene, minimal bundled module/provider contracts,
durable state and scoped views. Start with #38; #39 and #40 can follow
independently, then #41 and #42. The module contract and `dice` adapter
(#38, [modules.md](modules.md)), persisted module state with `game create`
(#39), the [Relay module](relay.md) (#40), and hosting module games with
per-player views (#41) are in place. The combined two-player demo passed (#42):
see the [runbook](relay-demo.md) and [acceptance record](acceptance/relay-demo.md).

- Load an explicitly selected local module and its action definitions.
- Give the public demo a small character and a simple uncertain action.
- Let the module define its fields and check rules; host assumptions stay small.
- Validate only the dependencies/provider selections actually required.
- Report missing providers or conflicting selections before play begins.
- Keep tags descriptive; no implicit execution based on tag names.

Do not add a plugin marketplace, dependency solver, or general override framework.

### M1-5. Add the private module and extract the first reusable features

Depends on M1-4. Tracker [#49](https://github.com/coghex/tesserae/issues/49)
and the [private-module delivery contract](designs/private_module_design.md)
split this work into eight children, all delivered. The selected scene covers
equipment, a skill check with a declared spendable resource, and a short ranged
encounter. The public support is the external package loader (#51), `game
create`/`serve --module-dir` (#52) and seeded creation (#53); the
specification, converter and module (#50, #53-#56) are private. The combined
workflow passed: see the [acceptance record](acceptance/private-module.md) and
the [external module runbook](external-modules.md).

- Load the private module/content from a configured directory outside the repo.
- Implement a bounded rules subset with explicit recorded interpretations.
- Inspect existing tools before reuse; do not copy game-specific data publicly.
- Import a copy of pilot state without changing originals; compare key resources,
  quantities, worn/carried gear, and scene position.
- Introduce inventory, equipment, and action/combat providers only as the selected
  loop needs them. Extract shared components where both demos support the boundary.
- Verify provider replacement explicitly instead of relying on load order.

The private module may initially contain most mechanics. Do not manufacture a
large generic abstraction just to make that module small.

### M1-6. Connect the chosen AI-referee workflow and play end to end

Depends on M1-5. The owner selected resumable `codex exec` invocations, one
referee conversation per game, and rejection of new input while busy. This
resolves the initial direction for Q-1/Q-7; direct API integration, queuing
and custom selective context loading are deferred. The
[M1-6 delivery plan](designs/codex_referee_design.md) records tracker
[#63](https://github.com/coghex/tesserae/issues/63) and children #64–#71, all
delivered: the [referee contract](designs/referee_contract.md), the store, runner,
bridge, host and CLI, the playable loop with recovery, and the private scene's
adaptation. Both scenes passed; see the [acceptance record](acceptance/referee.md)
and the [referee runbook](referee-runbook.md).

- Use the shared command surface from the chosen conversation host.
- Preserve player control, declared stakes before rolling, and recorded rulings.
- Keep referee context separate from player payloads.
- Play the original public scene and the agreed private validation scene.
- Confirm displayed and persisted resources agree, retries do not duplicate
  actions/rolls, and closing/reopening recovers the correct point in play.
- Verify the agreed scene works for more than one player connection, with
  explicit action authority and information disclosure.

## Deferred

Browser UI, click-to-command interactions, browser asset serving and browser
certificate-trust UX are beyond Milestone 1. The original browser-slice goals
and the token/TLS discussion are retained in
[browser-client-notes.md](browser-client-notes.md).

Additional complete rulesets, crafting, public internet hosting/cloud accounts,
matchmaking, elaborate party management, marketplaces,
untrusted plugin execution, elaborate override chains, and automatic rulebook-to-code
conversion. No need to settle a model vendor or frontend framework in M1-1.

## Progress and next decision

- Repository initialized; its default branch is now `master`.
- Agreed architecture captured in `tesserae_design.md`; remaining proposals marked.
- M1-1 implemented and verified with ten passing tests.
- SQLite live persistence and server-first/CLI-first delivery accepted. Browser
  delivery is now explicitly beyond Milestone 1.
- Origin configured and local bootstrap published with GitHub's MIT license intact.
- Foreground lifecycle and `websockets` transport accepted for M1-2.
- Specify the first durable command contract in its issue; finish the multi-player
  join/recovery contract before M1-3 enables friend access over LAN.
- Version 1 command protocol merged (#3) and documented in `protocol.md`; the
  durable SQLite game store merged (#4), documented in `game_store.md`.
- The foreground, loopback-only WebSocket development host merged (#5), which
  completes M1-2's first server scope (tracker #1, closed). It uses one fixed
  development participant.
- M1-3's localhost tracker #12 is complete: the authenticated session and recovery
  contract (#13) and persisted participants with personal tokens (#14) are
  merged, as are authenticated loopback hosting (#15) and shared revision
  notifications (#16) and the streaming CLI (#17). Combined acceptance passed;
  see the linked record above.
- Encrypted LAN play (#25) is implemented and verified (#26–#29 delivered).
  Automated checks and the separately installed Ubuntu VM's network check
  passed; see the [acceptance record](acceptance/encrypted-lan.md). M1-3 is
  complete under the agreed VM substitution. Physical-device Wi-Fi testing
  is deferred.
- M1-4's original demo and minimal module loading (#37) are complete
  (#38–#42 delivered). Two real CLI players passed the Relay demo, one of
  them on the Ubuntu VM; see the [acceptance record](acceptance/relay-demo.md).
  M1-5 then completed through tracker #49 and PR #62, including private
  acceptance and the external-module runbook.
- M1-6's AI referee (#63, #64–#71) is complete: both scenes passed with real
  Codex and two CLI players; see the [acceptance record](acceptance/referee.md).
