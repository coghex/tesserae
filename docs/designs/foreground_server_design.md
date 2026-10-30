# Foreground server delivery design

Design state: `exploring`

Owner: `coghex/tesserae`; default branch: `main`.

The owner approved the first server scope and requested that it become a tracker
with easily accomplishable children. The approved contract is preserved in
[tracker #1](https://github.com/coghex/tesserae/issues/1). This document proposes
delivery boundaries; the child split has not yet been approved or filed.

Status legend: `[ ]` unprocessed · `[#N]` linked to issue N · `[no-issue]`
reviewed without a separate issue · `[deferred]` blocked on a concrete precondition.

## Processing status

- [#1] EPIC. Run and persist commands on a foreground local server.
- [ ] HOST-1. Make the package installable and run existing tests in CI.
- [ ] HOST-2. Define and validate the command protocol.
- [ ] HOST-3. Execute and record commands atomically in SQLite.
- [ ] HOST-4. Expose commands through a foreground WebSocket server.

## Epic contract

- **Goal:** A foreground local host accepts one recorded dice command through a
  library-provided WebSocket client, persists the outcome, and safely handles retry,
  restart, invalid input, and shutdown.
- **Done when:** Every requirement and acceptance condition of tracker #1 is met
  by accepted child PRs. A persistent result is returned once for repeated requests,
  and the documented installation and manual connection procedures work.
- **Users and operators:** Initially a developer running one loopback-only session.
  This is the foundation for the future streaming CLI and player-facing browser.
- **Arc label:** Existing `enhancement`; no new label proposed.

## Current state and evidence

- `tesserae/cli.py:22–39` registers only the standalone `roll` command.
- `tesserae/dice.py` contains the reusable arithmetic/validation implementation.
- `tests/test_dice.py` supplies ten passing tests. No server, live database,
  dependency configuration, or GitHub Actions workflow exists yet.
- Root documentation records host-owned SQLite, WebSocket transport, foreground
  lifecycle, and server → CLI → browser order as accepted decisions.
- The open tracker scan and searches for server, websocket, and command found no
  existing issue before tracker #1 was created. Children still need dedup checks
  when their final specifications are prepared.

## Desired experience

The operator installs the package, starts an explicitly foreground host, and sees
its bound loopback address and database location. A library-provided interactive
client connects, discovers the current revision, submits a roll, and receives its
committed result. Restarting the host with the same database and repeating the
request returns the original result. Ctrl-C stops the host cleanly.

## Scope

### In scope

- Installable package and a minimal CI check for the existing unittest suite.
- One versioned command/result/error contract with request identity and revision.
- Validation independent of transport and persistence.
- One registered operation backed by the existing dice evaluator.
- Atomic SQLite result/history/revision storage, idempotent replay, and conflicts.
- A foreground, loopback-only WebSocket host with startup/disconnect/shutdown behavior.
- Focused tests and documentation in each child.

### Out of scope

Custom streaming CLI, terminal editing/history, LAN access, player credentials,
browser/HTTP UI, AI integration, full game modules, private game materials,
inventory/combat systems, background services, automatic reloaders, and library
conformance or exhaustive terminal tests. These exclusions match tracker #1.

## Design

The proposed dependency chain is `HOST-1 → HOST-2 → HOST-3 → HOST-4`.

The command contract is defined before persistence and network adapters depend on
it. HOST-2 validates requests but does not expose an unrecorded campaign execution
path. HOST-3 coordinates actual command execution and persistence. HOST-4 adapts
the transport to that existing service rather than recreating mechanics in socket
handlers.

The host is not considered complete until HOST-4 verifies the seams through real
connections and a process lifecycle check. Acceptance testing belongs to the slice
that introduces each behavior; there is no separate omnibus testing ticket.

Keep the parent open while children are delivered. Each PR closes its child issue
and references tracker #1 as its parent, rather than accidentally closing the
tracker with the first implementation PR. Link the created children as sub-issues
or a parent checklist when their individual specifications have been approved.

## Decisions

### D-1. The approved first server scope is a tracking issue

The owner approved the previously presented requirements, then explicitly requested
a tracker and manageable children. Tracker #1 preserves that contract and says not
to implement the parent as one PR. This tracker is a subset of Milestone 1, not
the entire game/application project.

### D-2. Retain accepted technical constraints

Use the existing dice arithmetic, host-owned SQLite, and `websockets`. Run the host
in the foreground with an explicit start and clean Ctrl-C shutdown. Begin with a
loopback-only development session. Root design decisions D-10 through D-16 and
tracker #1 remain authoritative; the proposed split does not alter their behavior.

## Proposals and open questions

### Q-1. Approve the four-child split

The four proposed slices below divide packaging, the command contract, durable
execution, and transport/lifecycle into separately reviewable changes. The owner
has not yet signed off on these boundaries. Keep the design exploring until that
approval and any needed refinements are complete.

### Q-2. Settle the exact wire contract before filing HOST-2

Tracker #1 already requires a versioned JSON contract, request IDs, expected
revision, deterministic replay of recorded results, and explicit errors. Its exact
message shape and validation/error cases should be presented with the HOST-2 issue
specification. Do not silently implement competing shapes in HOST-3 or HOST-4.
This does not block the packaging/CI slice.

## Verification strategy

- Every child retains `python3 -m unittest discover -s tests -v` as the test entry point.
- HOST-1 proves clean-environment installation and runs the existing ten tests in CI.
- HOST-2 proves valid/invalid application contracts without sockets or a database;
  malformed or unsupported input must not consume randomness.
- HOST-3 uses temporary real databases and controlled random sources to verify
  atomic commit/failure, stale revisions, replay, conflicting ID reuse, and restart.
- HOST-4 adds a small number of real connection/process checks for command flow,
  malformed-message isolation, disconnects, occupied ports, and Ctrl-C recovery.
- Use library transport behavior rather than duplicating WebSocket conformance tests.
- Documentation lands through `docs-push`; child PRs link the associated doc commits.

## Delivery plan

### HOST-1. Make the package installable and run existing tests in CI

- **Outcome:** A clean virtual environment can install the project and run the
  existing utility/tests; GitHub runs that same check on pull requests.
- **Scope:** Packaging/dependency metadata, minimal CI, and installation instructions.
  Add runtime dependencies only when their owning slice uses them.
- **Phase:** Foundation.
- **Depends on:** `none`.
- **Ordering:** `critical path`.
- **Relevant decisions:** D-1, D-2; tracker #1 requirement 6.
- **Acceptance signals:** `python3 -m pip install -e .` succeeds in a clean environment;
  `python3 -m unittest discover -s tests -v` retains all ten tests; the PR check passes.
- **Out of scope:** Server, database, protocol implementation, and refactoring dice.
- **Open questions:** None for this proposed scope.
- **Estimate:** Easy; configuration-sized PR.

### HOST-2. Define and validate the command protocol

- **Outcome:** One documented request/result/error contract can be reused by every
  client and the host's execution layer.
- **Scope:** Versioning, supported command/arguments, request identity, revision
  fields, result/error representation, and validation using existing expression
  parsing. Accept only the first registered dice operation for this slice.
- **Phase:** Command boundary.
- **Depends on:** `HOST-1`.
- **Ordering:** `critical path`.
- **Relevant decisions:** D-2; tracker #1 requirements 2 and 4.
- **Acceptance signals:** Focused tests distinguish valid and rejected requests,
  including unsupported versions/commands and malformed fields. Invalid requests
  do not roll. Contract examples match the implementation.
- **Out of scope:** Actual persisted execution, request replay, sockets, and CLI UI.
- **Open questions:** Q-2, to be resolved in the child specification before filing.
- **Estimate:** Easy–Medium; small application contract PR.

### HOST-3. Execute and record commands atomically in SQLite

- **Outcome:** The command service produces a recorded result and can return that
  same result after retry or restart without another roll.
- **Scope:** SQLite-backed revision, outcomes/history, transaction boundaries,
  command execution using the existing evaluator, stale-request rejection,
  conflicting request identity, and replay semantics.
- **Phase:** Durable execution.
- **Depends on:** `HOST-2`.
- **Ordering:** `critical path`.
- **Relevant decisions:** D-2; tracker #1 requirements 3 and 4.
- **Acceptance signals:** Tests using temporary databases and controlled randomness
  prove once-only execution, preserved results after reopen, atomic result/history/
  revision changes, and unchanged state/history on invalid, stale, or failed requests.
- **Out of scope:** Network handlers, terminal clients, game schemas, and event-sourcing
  reconstruction beyond the approved result/history requirements.
- **Open questions:** Q-2 must have been resolved by HOST-2; no competing protocol.
- **Estimate:** Medium; the most substantial child, kept focused on one operation.

### HOST-4. Expose commands through a foreground WebSocket server

- **Outcome:** The documented foreground host can be used through an existing
  interactive WebSocket client and meets the tracker's end-to-end acceptance.
- **Scope:** `serve` entry point, `websockets` dependency, loopback binding,
  configured database and port (including port 0), reporting address/revision,
  forwarding to the durable command service, and connection/process lifecycle.
- **Phase:** Operator-visible host.
- **Depends on:** `HOST-3`.
- **Ordering:** `critical path`.
- **Relevant decisions:** D-2; tracker #1 requirements 1, 2, 5, and documentation in 6.
- **Acceptance signals:** Real WebSocket/process checks cover successful/replayed
  requests, malformed messages, disconnect isolation, restart, occupied-port errors,
  and clean Ctrl-C with usable persisted state. Manual README procedure works with
  the library's interactive client. All checks remain green.
- **Out of scope:** Custom CLI, LAN/credentials, HTTP/browser serving, background
  installation, auto-reload, and application logic duplicated in socket handlers.
- **Open questions:** Use the contract and service delivered by HOST-2/HOST-3.
- **Estimate:** Medium; thin transport adapter with focused lifecycle checks.
