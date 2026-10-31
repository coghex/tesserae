# M1-6: resumable Codex referee through the server and CLI

Status: planned; tracker [#63](https://github.com/coghex/tesserae/issues/63)
and children #64–#71 are filed. This document implements no runtime. Baseline:
M1-5 completed through PR #62 at `dec450f`.
The owner's choices below supersede the earlier direct-model-API proposal.
REF-1's exact contract is [referee_contract.md](referee_contract.md).

## Accepted direction

- The foreground Tesserae host coordinates a referee using `codex exec`.
  It does not implement a model-provider API client or require a persistent
  Codex process. One invocation exits after its work; later invocations resume
  the saved conversation by explicit session ID.
- One saved referee conversation belongs to one game, shared across its
  players. Capture the `thread.started` ID from JSON events and persist the
  mapping on the host. Do not use `--last` or `--ephemeral`.
- Initially supply the complete context for the agreed bounded demonstration.
  Include fresh authoritative game state on continuation. Custom selective
  context loading, retrieval and history reduction are deferred stretch goals.
  This does not defer participant-specific privacy.
- Only one referee job runs per game. Players cannot submit new input while
  it is busy. Reject that input clearly rather than silently queueing it.
  Queuing, steering and concurrent referee jobs are deferred.
- SQLite remains the authority for mechanics, accepted messages, results,
  pending decisions and rulings. Codex history supplies conversational
  continuity; it does not own another character sheet or database.
- All player gameplay requests go through Codex, which uses a narrow
  host-controlled command bridge within its invocation. Players cannot invoke
  raw gameplay commands directly in referee mode. Authentication, sync,
  job-status/recovery and operator administration remain host plumbing.
- Table conversation and referee narration broadcast by default, without a
  public marker. Only explicitly private messages/replies use private routing.
  Keep the user-facing convention simple; the host still validates recipients.
- CLI first, using the existing authenticated host and thin streaming client.
  Browser work remains beyond Milestone 1.
- Keep the original public demonstration and the already accepted private
  bounded scene. No complete ruleset, new combat modes or new campaign arc.

Owner decisions of 2026-10-31, after the plan was filed:

- Codex may run **only** the included, whitelisted Tesserae Python scripts that
  make up the command bridge. No other shell commands, file reads or edits:
  even reading game state goes through a whitelisted script.
- Referee data lives in a dedicated local folder on the host, by default
  `~/.local/share/tesserae/referee/` (operator-overridable): its own Codex home,
  with its own login and saved sessions, and the referee workspace. It stays
  separate from the owner's own Codex sessions and outside every repository.
- Players type plain natural language, never JSON. Codex interprets what they
  want, including anything they paste that looks like code or JSON.
- This is a personal tool, played with friends on the local network: no Codex
  version pinning (record the version used and fix breakage when it happens),
  and no adversarial hardening beyond the ordinary authority and privacy checks.

Codex capabilities were checked against local CLI 0.166.0 and the official
[noninteractive documentation](https://learn.chatgpt.com/docs/non-interactive-mode).
The runner records the CLI version it ran with but does not pin or refuse other
versions; if a release changes the event format, the runner is fixed then.

## Tracker #63

Title: **M1-6: play through a resumable Codex referee**

### Outcome

Two authenticated players can send natural-language input to the foreground
Tesserae host. The host runs/resumes a Codex referee, supplies game context,
resolves supported actions through authoritative module commands, and delivers
the appropriate narration and updated views. Codex exits between invocations.
Restarting and retrying preserve committed actions and the point in play.

### Boundaries

- Use `codex exec` and explicit session resumption; no direct model API
  integration, SDK/app-server dependency or provider-selection framework.
- Accept player intent without allowing a client to assert another character,
  referee role, NPC authority, difficulty, damage or arbitrary state patch.
- A question, clarification or hypothetical plan need not advance game time
  or mechanical revision. Consequential actions follow module rules and
  declared stakes/resources. Preserve player agency.
- Use a complete initial context for the bounded scenes. Never publish private
  module content or forward raw referee context/events to player connections.
- One job per game, server-enforced busy rejection, no input queue. Existing
  join, inspection/sync, status/recovery and disconnect remain responsive.
- Retain the existing transaction, expected-revision and exact-command-replay
  guarantees. Model work never holds a SQLite transaction open.
- Public tests use original fixtures and a scripted runner. Private adaptation,
  rules, logs and detailed acceptance remain external and locally reviewed.

### Completion

Close only after both agreed demonstrations have been played through the
actual authenticated CLI with real Codex runs, and failure/retry/privacy
checks pass. Publish a generic runbook and sanitized acceptance. Retain the
accepted VM/network coverage; no new physical-device gate. Do not call M1
complete from a mock-only conversation or a terminal-only restart.

## Contracts to settle before implementation

These are proposed engineering defaults, not additional owner decisions.
REF-1 specifies exact envelopes, state transitions and tool exchange before
the dependent children start.

### Busy handling and durable input

Claim the game's single active job atomically with acceptance of a new input.
Two submissions arriving together cannot both start work. Record trusted sender,
input request ID/content, an ordered conversation sequence, job state and the
mechanical revision used to prepare context. Conversation/job progress has
its own sequence; it does not consume a mechanical revision for every sentence.

Suggested states: accepted, running, awaiting player input, completed,
interrupted and failed. Waiting for the player means the process has exited
and the busy gate is released; retain the pending choice and its addressee.
Do not leave the game permanently busy while waiting for a declaration.

Check an authenticated exact retry before rejecting it as busy: a retry returns
the existing job/status/reply and never starts another invocation. A new busy
submission is refused without storing a queued intention. Refuse reused IDs
with different content. Define bounded input/output and timeout limits.

Enforce this on the host, not merely in a client UI. In referee mode, direct
player gameplay commands must not bypass the referee or mutate the state
under an active job. Legacy mechanical-only hosting remains compatible.

### Context, continuity and recovery

Use a dedicated referee workspace/configuration and record the tested runner
settings. Capture the session ID as soon as it is emitted, before relying on
the final response. Keep the ID, local session history and private diagnostics
host-side. Persist final player-visible output before announcing completion.

Each continuation identifies the new authenticated input, pending declarations,
current game revision/state and committed results. It must not assume that a
remembered inventory or NPC state is current. Module-provided context and
explicit local operator configuration supply rules/preparation; a player
message cannot choose a filesystem path or replace referee instructions.

Codex's session files and SQLite cannot commit atomically. An interrupted job
therefore requires reconciliation against recorded commands before continuing.
Do not blindly rerun the original player prompt with fresh command IDs.
Record issued steps/results sufficiently to discover an already committed
action when the subprocess or connection dies before the final reply.

Supervise and reap only the job's own processes. On timeout, shutdown or
startup recovery, preserve committed effects and mark uncertainty explicitly.
Never start a second invocation against the same conversation while the first
may still be running. A missing/unresumable session produces an explicit
recoverable state; provide an operator-triggered rebuild from Tesserae's
records, retain the old mapping/history, and do not reset game resources.
Rebuild is a basic recovery operation, not selective-context optimization.

### Referee authority and mechanical execution

The host authorizes a command for an initiating participant or a bounded
referee/NPC operation. The model does not choose its own privilege by writing
an owner/role field. Optional module hooks expose referee context, supported
actions, explanations/stakes and module-specific NPC operations without adding
universal inventory, turn or combat fields to the host.

The owner selected a narrow local command bridge within each invocation.
Codex can inspect state, call supported actions and narrate their returned
results without exiting between individual tool calls. REF-1 specifies that
bridge's concrete transport and capability contract; do not build an alternate
between-invocation action-proposal loop. Supported mechanical effects use the
same authoritative execution service, validation, expected revision and atomic
state/result/history storage. No direct model database edits or unrestricted
"set state" command. Stable step IDs belong to the job, survive retries and
cannot authorize a different action after recovery.

The referee can interpret a natural-language request, explain stakes, ask for
missing consequential choices, and narrate actual tool results. A roll's stated
mechanics and declared spend precede execution. Record a material adopted
ruling with its scope; it does not silently rewrite previous outcomes or add
unsupported executable mechanics. Do not ask for approval of every routine
action the player has already authorized.

The private fixed opponent policy was scaffolding for M1-5. The private child
must explicitly identify how bounded referee NPC decisions replace it without
running both policies or skipping required automatic saves/turn bookkeeping.
Keep the existing original campaign unchanged.

### Player delivery

Keep the CLI as a JSON-line pipe. Define accepted/busy/status/completed and
awaiting-input responses plus reconnect inspection. Read-only sync and other
clients remain responsive while a subprocess is running.

Unmarked table messages and referee narration broadcast to the table. Only
messages explicitly marked private are routed privately. A reply to private
input inherits its private audience unless a separate public message is
explicitly produced; never accidentally publish a private reply by dropping
its marker. Choose a small concrete private-message convention in REF-1, not
an elaborate player-facing reply format. The host validates private recipients
and keeps audience metadata in durable messages. Recheck identity/access before
delivery, including credential revocation.
Player snapshots still use module projections. Store recipient-safe messages
so reconnect/retry can retrieve them without invoking Codex again.

Raw Codex JSONL may contain secrets, tool output and internal instructions;
it is operator diagnostics, never a player stream. Validate final output shape
and recipients before publishing. A schema checks routing/structure, not the
truth or secrecy of arbitrary generated prose: use original secrecy fixtures
and manual acceptance to test narrative disclosure as well as state projection.

## Children and dependency order

All children are **filed and open**; no implementation is claimed.
Use ordinary manual Tesserae development; no custom Kanban or docs-push.

| Key | Deliverable | Depends on |
|---|---|---|
| [#64](https://github.com/coghex/tesserae/issues/64) / REF-1 | Specify referee input, job, tool and delivery contracts | Accepted direction |
| [#65](https://github.com/coghex/tesserae/issues/65) / REF-2 | Persist referee jobs/conversation and enforce one active job | #64 (REF-1) |
| [#66](https://github.com/coghex/tesserae/issues/66) / REF-3 | Run and resume supervised Codex subprocesses | #64 (REF-1) |
| [#67](https://github.com/coghex/tesserae/issues/67) / REF-4 | Expose module context and authorized referee action execution | #64 (REF-1), #65 (REF-2) |
| [#68](https://github.com/coghex/tesserae/issues/68) / REF-5 | Carry referee submissions, busy/status and replies over host/CLI | #64 (REF-1), #65 (REF-2) |
| [#69](https://github.com/coghex/tesserae/issues/69) / REF-6 | Orchestrate the original playable referee loop and recovery | #65 (REF-2), #66 (REF-3), #67 (REF-4), #68 (REF-5) |
| [#70](https://github.com/coghex/tesserae/issues/70) / REF-7 | Adapt the bounded private scene for referee-controlled play | #69 (REF-6) |
| [#71](https://github.com/coghex/tesserae/issues/71) / REF-8 | Verify both complete workflows and publish the runbook | #70 (REF-7) |

### REF-1: concrete contracts and demonstration

Document exact messages, audience rules, job transitions, session mapping,
busy/retry precedence, interruption reconciliation and the in-invocation bridge:
its whitelisted scripts and how Codex is restricted to them, the referee data
folder and its login, and the plain-text input convention.
Specify a short original Relay conversation and private scene criteria without
publishing private content. Resolve the bounded NPC authority and adopted-ruling
storage contract. Capture runnable example exchanges and a small failure matrix.
This child ends at an implementation-ready specification; no full runner here.

### REF-2: durable jobs, messages and busy gate

Add the minimal store schema/migration for accepted inputs, job status,
conversation/session linkage, pending choices and recipient-safe replies.
Atomically arbitrate simultaneous submissions; test exact retries, conflicts,
busy rejection, busy release and recovery of an interrupted job. Mechanical
revisions remain mechanical. Existing games and mechanical replay still work.
No model process or transport implementation belongs in this child.

### REF-3: supervised Codex runner

Implement the subprocess adapter against the tested CLI version: initial
JSONL session ID, explicit resume, structured completion, errors/timeouts and
clean shutdown. Use the selected explicit configuration/workspace; preserve
private diagnostics. A scripted executable tests event parsing and process
lifecycle without a model account in CI. A small real Codex probe confirms
session capture/resume; it is not a model quality benchmark. No general SDK
abstraction or session discovery.

### REF-4: module context and action authority

Implement the contract's minimal optional context/action hooks and chosen
mechanical exchange with original fixtures. Bind initiating-player/NPC
permissions on the host, preserve stable step IDs and use atomic command
execution. Test forged authority, hidden context separation, stale attempts,
duplicate steps and committed-step discovery after a lost result. Provide
only the original demonstration's needed hooks, not a generic GM console.

### REF-5: host and thin CLI conversation surface

Add authenticated input submission, job status/response retrieval and
recipient-scoped delivery. Enforce busy and raw-command bypass rules on the
server; preserve responsive joins/sync/disconnects. Update client reply pairing
for the additive messages without a terminal UI. Use a small number of real
connections to prove simultaneous submit and reconnect behavior.

### REF-6: original end-to-end orchestration

Connect the store, runner, context/actions and delivery for the agreed Relay
conversation. Use real supported module commands, pending player declarations
and recorded outcomes through Codex and its command bridge. Follow the specified
interruption/rebuild path and publish completed replies once. Scripted referee tests cover questions without
time advancement, unauthorized action attempts, missing choices, malformed
output and failures around a committed tool step. One real Codex walkthrough
proves the original loop before private adaptation begins.

### REF-7: bounded private adaptation

Work in the approved external local-only repository. Add private context,
action guidance/ruling records and the bounded NPC interface; replace the
practice policy explicitly. Preserve the approved mechanics, exact bindings,
player agency and original input fingerprints. Review named private/public
commits and fixed tests before closing. Publish only sanitized status; no
sourcebook text, game names, paths, raw logs or private examples.

### REF-8: combined acceptance and operator runbook

Use clean pinned installs and record actual CLI/runtime versions. Run the
original and private scenarios through foreground serve, two distinct
authenticated players and real initial/resumed Codex invocations. Demonstrate
busy refusal, a pending player choice, actual mechanics/narration, correct
recipient views, exact retry, and a host restart while play is active followed
by fresh play. Reconcile displayed/stored resources and command history.

Use scripted faults for subprocess death after a command commits, malformed
output, missing session history and safe rebuild; do not force live dice to
reach exceptional outcomes. Verify credentials and process cleanup. Keep
private full evidence local and publish a sanitized record plus generic setup,
normal play, busy/retry, restart and recovery instructions. Close the tracker
only when all children and both combined workflows pass.

## Deferred

Input queues, steering, multiple simultaneous referee jobs, custom context
selection/retrieval/compaction, multiple model backends, direct provider APIs,
browser UI, richer terminal interfaces, general role administration, arbitrary
state-edit tools, full rulesets and new private game mechanics. Built-in Codex
history behavior is not a promise that unlimited history remains verbatim in
the active model context.
