# Milestone 1: one reliable playable loop

Status: implementation beginning. Server → CLI → browser order accepted by the
owner; individual slice details remain proposals where noted.

## Outcome

A locally hosted server supports multiple player clients. Players first connect
with a thin streaming CLI; a browser client follows using the same command contract.
A player can view an original small scene and a character, attempt an action,
have the AI referee use executable tools for supported mechanics, see the result
on the map and character view, and reload without losing or duplicating changes.

Validate equivalent workflows with one private game module. No private content
is required to run the public demonstration. Coverage is deliberately bounded:
character/equipment records, checks, movement/display, the selected combat actions,
resource tracking, and recorded rulings. Specify the exact demonstration before
calling the milestone complete.

Acceptance includes two distinct player clients connected to the same host, with
consistent saved state and correctly separated private information. Demonstrate
a friend joining from another computer once the LAN join/identity flow exists.

## Should the CLI come first?

Yes, as a thin streaming client to the minimal server, after host-side command
handling exists. It should send input and print responses/events concurrently,
including updates triggered by other clients. A pipe mode uses JSON lines with
diagnostics on stderr. The existing offline dice utility remains separate from
authoritative campaign commands.

Do not build a separate terminal RPG, interactive menu system, or text UI. The
browser remains the intended player interface. Both clients must use the same
server command handlers. Use transport libraries and an existing interactive
WebSocket client for initial server probes; add only the application-specific
CLI glue needed. Introduce the browser after the server/CLI loop works.

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

### M1-3. Connect a thin CLI and prove several clients

Depends on M1-2.

- Maintain a duplex connection: send stdin while displaying incoming messages.
- Keep stdout machine-readable in pipe mode and diagnostics on stderr.
- Join with a host-recognized player identity; select a campaign without direct
  database access. Do not trust a claimed actor name in a command payload.
- Show two clients receiving permitted updates from one authoritative state.
- Verify retry after disconnect does not roll or apply an action twice.
- Test a small number of real process/connection lifecycle boundaries; do not
  build a keystroke-by-keystroke test suite or duplicate library conformance tests.

### M1-4. Introduce the original demo and minimal module loading

Depends on M1-3.

- Load an explicitly selected local module and its action definitions.
- Give the public demo a small character and a simple uncertain action.
- Let the module define its fields and check rules; host assumptions stay small.
- Validate only the dependencies/provider selections actually required.
- Report missing providers or conflicting selections before play begins.
- Keep tags descriptive; no implicit execution based on tag names.

Do not add a plugin marketplace, dependency solver, or general override framework.

### M1-5. Connect the first browser slice

Depends on M1-4. Resolve the web UI/asset serving choices when needed. The command
server and client message contract must already work.

- Show the demo's map, token, character summary, and action result.
- Submit the same commands already exercised by the CLI.
- Provide a deliberate player projection, with tests for undisclosed fields.
- Refresh/reopen and recover the saved state correctly.
- Do not expose the state directory as a general static file server.
- Connect to the same host and identity/command boundaries as the CLI. Browser
  actions do not bypass validation or maintain another authoritative state copy.

Prove one click-to-command-to-saved-result loop before expanding mechanics.

### M1-6. Add the private module and extract the first reusable features

Depends on M1-5. Agree the exact private validation scene first.

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

### M1-7. Connect the chosen AI-referee workflow and play end to end

Depends on M1-6. Resolve design Q-1 and player/referee pacing Q-7 before the
corresponding model integration work.

- Use the shared command surface from the chosen conversation host.
- Preserve player control, declared stakes before rolling, and recorded rulings.
- Keep referee context separate from player payloads.
- Play the original public scene and the agreed private validation scene.
- Confirm displayed and persisted resources agree, retries do not duplicate
  actions/rolls, and closing/reopening recovers the correct point in play.
- Verify the agreed scene works for more than one player connection, with
  explicit action authority and information disclosure.

## Deferred

Additional complete rulesets, crafting, public internet hosting/cloud accounts,
matchmaking, elaborate party management, marketplaces,
untrusted plugin execution, elaborate override chains, and automatic rulebook-to-code
conversion. No need to settle a model vendor or frontend framework in M1-1.

## Progress and next decision

- Repository initialized; its default branch is now `master`.
- Agreed architecture captured in `tesserae_design.md`; remaining proposals marked.
- M1-1 implemented and verified with ten passing tests.
- SQLite live persistence and server → CLI → browser order accepted.
- Origin configured and local bootstrap published with GitHub's MIT license intact.
- Foreground lifecycle and `websockets` transport accepted for M1-2.
- Specify the first durable command contract in its issue; finish the multi-player
  join/recovery contract before M1-3 enables friend access over LAN.
- Version 1 command protocol merged (#3) and documented in `protocol.md`; the
  durable SQLite game store merged (#4), documented in `game_store.md`.
- The foreground, loopback-only WebSocket development host (#5) is in review.
  It uses one fixed development participant. Real participant joining and the
  streaming CLI come next, before LAN access.
