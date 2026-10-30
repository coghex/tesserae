# Foreground server delivery design

Design state: `complete` (tracker #1 closed 2026-10-30)

Owner: `coghex/tesserae`; default branch: `master`.

The owner approved the first server scope and requested that it become a tracker
with easily accomplishable children. The approved contract is preserved in
[tracker #1](https://github.com/coghex/tesserae/issues/1). The owner approved the
four-child split and explicitly authorized drafting and filing all four without
further approval pauses. All child issues are now filed; HOST-1 (#2) was completed
by merged PR #6 with the owner's approval.

Status legend: `[ ]` unprocessed · `[#N]` linked to issue N · `[no-issue]`
reviewed without a separate issue · `[deferred]` blocked on a concrete precondition.

## Processing status

- [#1] EPIC. Run and persist commands on a foreground local server.
- [#2] HOST-1. Make the package installable and run existing tests in CI.
- [#3] HOST-2. Define and validate the command protocol.
- [#4] HOST-3. Execute and record commands atomically in SQLite.
- [#5] HOST-4. Expose commands through a foreground WebSocket server.

Issue processing is complete for this tracker. HOST-1 (#2) is complete through
merged PR #6, HOST-2 (#3) through merged PR #8 (contract:
[docs/protocol.md](../protocol.md)), and HOST-3 (#4) through merged PR #9
(service: [docs/game_store.md](../game_store.md)), and HOST-4 (#5), the
foreground WebSocket development host, through merged PR #10. Tracker #1's
combined acceptance was verified on `master` at 93a58a5, and the tracker is
closed.

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

## Starting state and evidence

- `tesserae/cli.py` registers only the standalone `roll` command.
- `tesserae/dice.py` contains the reusable arithmetic/validation implementation.
- `tests/test_dice.py` supplies ten passing tests. PR #6 added installable packaging
  and a GitHub Actions unittest check. No server or live database exists yet.
- Root documentation records host-owned SQLite, WebSocket transport, foreground
  lifecycle, and server → CLI → browser order as accepted decisions.
- Deduplication before filing found only the existing parent tracker. All four
  children were then filed and linked; no duplicate child was found.

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

The approved dependency chain is `HOST-1 → HOST-2 → HOST-3 → HOST-4`
(`issue #2 → #3 → #4 → #5`). GitHub sub-issue and blocked-by relationships are set.

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
tracker with the first implementation PR. All four children are linked as native
sub-issues. Their specifications were filed using the owner's delegated authority.

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
tracker #1 remain authoritative; the child split does not alter their behavior.

### D-3. Four children and autonomous issue filing

The owner approved this four-part split and said to draft and file all four. The
owner also explicitly waived global issue-approval pauses for this hobby project
and authorized agents to file issues they see fit. The project-specific exception
is persisted in root `AGENTS.md`. Issue-filing autonomy remains in effect. The
owner's subsequent manual-workflow direction is recorded below and supersedes
the earlier review/delivery machinery; private-content boundaries still apply.

### D-4. Manual review and owner merge approval

The owner explicitly excluded custom Kanban plugins, their automated approval/
review requirements, and `docs-push` from this hobby project. Use ordinary Git and
keep documentation with related code. Reviews are discussed with the owner; the
owner gives final merge approval, followed by the agreed checks. Root `AGENTS.md`
and root design D-17 supersede earlier delivery clauses in the tracker/children.

## Resolved planning questions

### Q-1. Approve the four-child split

Resolved by D-3. The owner approved the four boundaries and authorized batch filing.

### Q-2. Settle the exact wire contract before filing HOST-2

Resolved in the filed specification for [HOST-2 / #3](https://github.com/coghex/tesserae/issues/3)
under the owner's delegated issue-authoring authority. It defines version-1 command,
result, error, and welcome messages, validation categories, and replay content
identity. This is the implementation contract; it is not a claim that the owner
personally selected each field name. HOST-3 and HOST-4 must consume the same contract.

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
- Documentation accompanies the relevant changes through ordinary Git/PR review.
  Do not use `docs-push` or custom Kanban review/approval tooling.

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
- **Open questions:** None.
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
- **Open questions:** None; Q-2 is resolved by issue #3's specification.
- **Contract:** [docs/protocol.md](../protocol.md). `request_id` may not contain
  whitespace anywhere (owner's choice, 2026-10-30).
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
- **Open questions:** None; consume the protocol specified by issue #3.
- **Service:** [docs/game_store.md](../game_store.md). Replay is namespaced by
  trusted participant context; all participants share one game revision.
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
