# M1-6 referee contract (REF-1)

Status: specification for [#64](https://github.com/coghex/tesserae/issues/64)
under tracker [#63](https://github.com/coghex/tesserae/issues/63). Implemented
so far: the store (section 5, with the step journal of section 6 and the reply
validation and audiences of section 7) in `tesserae/referee/store.py` (#65), and
`tesserae referee enable`; the referee folder, its config, the job lock and the
Codex runner (sections 2 to 4) in `tesserae/referee/runner.py` (#66), with
`tesserae referee setup` and `check`; the host's referee mode and the CLI
(sections 7, 8 and 12) in `tesserae/server.py` and `client.py` (#68), with a
pluggable executor for each accepted job. The bridge, the Codex executor and
recovery (#69), and `referee stop`/`rebuild` are not. It turns the [M1-6 delivery plan](codex_referee_design.md)
and the owner's decisions into exact contracts for REF-2 to REF-8. Section 12
records how the CLI shows conversation (owner decision O-1).

Tested against `codex-cli 0.166.0` on macOS (arm64). The runner records the
version it ran with; it does not pin or refuse other versions.

## 1. Owner decisions this contract follows

- `codex exec`, resumed by explicit session ID; one referee conversation and one
  active job per game. No `--last`, no `--ephemeral`, no direct model API.
- Codex can run **only** Tesserae's whitelisted Python bridge, even to read.
- Referee data lives in its own local folder, with its own Codex home.
- Players type plain text; Codex interprets it, including anything that looks
  like JSON or code. All player gameplay goes through Codex.
- New input while busy is refused, never queued.
- Unmarked conversation and narration go to the table; only explicitly private
  exchanges are private.
- A personal tool for the owner and friends on the local network: no version
  pinning, and no hardening beyond the ordinary authority and privacy checks.

## 2. The referee folder

Default `~/.local/share/tesserae/referee/`; every referee command and `serve`
take `--referee-dir DIR` to use another. Created with mode 0700.

```text
referee/
  codex-home/           CODEX_HOME for every referee run
    config.toml         written by `tesserae referee setup` (section 3)
    AGENTS.md           the referee's standing instructions, written by setup
    auth.json           created by the one-time `codex login` below
    sessions/ ...       Codex's own saved sessions (back these up)
  workspace/            empty; the working directory of every run
  run/                  the host's bridge sockets, one per serving game
  games/<game_id>/jobs/<job_id>.jsonl   private diagnostics: raw Codex events
```

- `tesserae referee setup [--referee-dir DIR]` creates the folder, writes
  `config.toml` and `AGENTS.md`, and prints the one-time login command:
  `CODEX_HOME=~/.local/share/tesserae/referee/codex-home codex login`. Rerunning
  setup rewrites those two files and touches nothing else.
- `tesserae referee check` reports the Codex version, whether the login works
  (`codex login status` with that `CODEX_HOME`), and whether `config.toml`
  matches what setup would write.
- The owner's own `~/.codex` is never read: its config, plugins, MCP servers and
  personal `AGENTS.md` instructions stay out of referee runs (the probes in
  section 13 showed a run without a separate home loading them).
- Back up `codex-home/sessions/` with the game database: a game's Codex history
  lives there. Neither belongs in any repository.

## 3. How Codex is restricted

Everything is in `codex-home/config.toml`, because `codex exec resume` accepts
no `--sandbox` or `--cd` option:

```toml
sandbox_mode = "read-only"
approval_policy = "never"
web_search = "disabled"

[features]
shell_tool = false
unified_exec = false
view_image = false
image_generation = false
browser_use = false
in_app_browser = false
computer_use = false
apps = false
plugins = false
goals = false
multi_agent = false
sleep_tool = false
tool_suggest = false

[mcp_servers.tesserae]
command = "/path/to/python"          # the host's own interpreter, written by setup
args = ["-m", "tesserae.referee.bridge"]
env_vars = ["TESSERAE_BRIDGE_SOCKET", "TESSERAE_BRIDGE_TOKEN"]
tool_timeout_sec = 60
```

The result, verified on 0.166.0:

- No shell, no command execution, no file or image reading, no web, no browser.
- Code mode (`functions.exec`) stays on, because Codex reaches MCP tools only
  through it; its runtime has no file system, network or process access.
- `apply_patch` remains listed but cannot write: the read-only sandbox rejects
  it. A spawned sub-agent, if Codex ever starts one, inherits the same config.
- The only tools that touch the game are the bridge's (section 6).

## 4. Running Codex

The host starts one subprocess per job, with environment `CODEX_HOME`,
`TESSERAE_BRIDGE_SOCKET` and `TESSERAE_BRIDGE_TOKEN` (a fresh random secret for
this job), working directory `workspace/`, and the prompt on stdin:

```text
first job:   codex exec --json --skip-git-repo-check --ignore-rules \
               --output-schema SCHEMA -o LAST_MESSAGE -
later jobs:  codex exec resume SESSION_ID --json --skip-git-repo-check \
               --ignore-rules --output-schema SCHEMA -o LAST_MESSAGE -
```

- **Session ID.** The first `thread.started` event's `thread_id` is written to
  the store as soon as it is read, before anything else is trusted.
- **Final reply.** `SCHEMA` is the reply schema in section 7. The host reads
  `LAST_MESSAGE`, validates it, and only then stores and publishes it.
- **Diagnostics.** Every stdout line and stderr goes to the job's diagnostics
  file. None of it is ever sent to a player.
- **Limits.** A job ends after 300 s (operator-configurable). The host then
  stops the process group it started (SIGTERM, then SIGKILL after 10 s to
  whatever is left) and never touches any other process. A run counts as over
  only once every member of that group is gone, not just Codex itself, so a
  helper that ignores SIGTERM cannot outlive its job.

### Who is still running: the job lock

A host crash or SIGKILL does not stop a child in its own process group, so the
host never assumes a job's Codex died with it. Liveness comes from a lock the
kernel releases only when every holder has exited:

- Before starting a job, the host opens `run/<game_id>.lock` and takes an
  exclusive `flock` on it, then starts Codex in a new process group with that
  file descriptor inherited (`pass_fds`). The lock is held while the host *or*
  the Codex process tree still holds it. The lock decides *whether* anything
  from a job may be alive; it never says *which* process that is.
- **The lock file's record.** The host rewrites the file's JSON contents, under
  the lock, at each stage, so it never describes an earlier job:
  1. `{"state": "starting", "job_id", "host_run"}` before spawning (no process
     yet);
  2. `{"state": "running", "job_id", "host_run", "pgid", "pid",
     "started_at", "command"}` right after spawning, where `pid` (= `pgid`) is
     the Codex process, `started_at` its start time as `ps -o lstart= -p PID`
     reports it, and `command` its executable path;
  3. `{"state": "ended", "job_id"}` after the process exits, before the host
     closes its descriptor; or
  4. `{"state": "stuck", ...}`, keeping the `running` fields, if the run's
     process group survived SIGKILL. While any process has that group ID, the
     lock counts as held even once every descriptor is closed, so no new job
     starts. When the group is gone, the next attempt to take the lock clears
     the record to `ended`. The ID may by then belong to an unrelated group,
     so a stuck record only ever blocks: nothing is signalled on its word.
- After Codex exits normally, the host writes `ended` and closes its own
  descriptor; the lock is free again.
- **At host startup**, for a job still marked `running`, the host tries the lock
  without blocking:
  - Acquired: nothing from that job is alive. The job becomes `interrupted`
    (section 10) and its steps are reconciled (section 6).
  - Not acquired: the old Codex is still running. The game stays busy, players
    get `referee_busy`, and the host retries the lock every 5 s. When it gets
    it, it finishes the job the same way.
- **Bridge access ends with the host run.** Each host run has a new run ID, and
  a job token is valid only for the run that issued it. An orphaned Codex's
  bridge calls are refused (its socket is gone, and its token is stale), so it
  can change nothing more and soon stops.
- **`tesserae referee stop --database DB`** (operator) stops a job's Codex:
  - **With the host running** (the usual case), it asks the host over its local
    socket, and the host stops the process group of the child it supervises
    (its own `Popen` object): SIGTERM, then SIGKILL after 10 s. The host knows
    its child; no recorded number is trusted.
  - **With no host running** (an orphan after a crash), it reads the record and
    signals only if *all* of these hold: the lock is still held, the record is
    `running`, a process `pid` exists, it leads process group `pgid`, its
    current start time equals `started_at`, and its executable equals
    `command`. Any mismatch or missing field means the job's process cannot be
    identified: `stop` changes nothing and says why (the operator can wait, or
    stop the process by hand after checking it).
  - For a `stuck` record, it signals nothing: the leader is gone, so whoever
    has that group ID now cannot be verified as the job's. It says which group
    to check by hand (`ps -A -o pid,pgid,lstart,command`). The record clears
    itself once the group is gone; if the group turns out to be unrelated, the
    operator clears it (`clear_stuck`, `tesserae referee stop --clear-stuck`
    with #69), which also signals nothing and refuses while the lock itself is
    still held.
  - Stop during startup (record `starting`) refuses: there is no process to
    identify yet; retry in a moment. Stop after the process exited (record
    `ended`, or the lock is free) reports that nothing is running.
- No new job starts until the lock is free: there is never a second Codex
  process for a game.

### Prompts

`AGENTS.md` (written by setup) holds the standing instructions, the same for
every game: you are the referee for a tabletop game; the bridge tools are your
only way to see or change it, and their results are the truth; never invent
rolls or outcomes; call `act` only for what the sender has clearly chosen, and
ask (with `choices.ask`) when a consequential choice is missing, and resolve a
choice (`choices.resolve`) once it is answered; state stakes and
any declared resource before a roll; questions and talk need no step; keep
hidden preparation and other players' private details out of table messages;
reply only in the required JSON shape.

Each job's prompt, on stdin, is short, because `get_context` supplies the game:

```text
New input (job j-7) from Alice (p_…), to the table:
"Yes, go."
Call get_context before anything else. Game revision now: 2.
```

A private input says "privately to you" instead of "to the table". The first job
of a session adds "This is the start of the game's referee conversation. Read
every page of the transcript with get_transcript before replying."; a
job after an interruption adds "The previous job was interrupted; get_context
lists what it committed. Do not repeat those steps."

## 5. Store

New tables (REF-2). The game's `revision` stays mechanical; conversation has its
own sequence.

| Table | Contents |
|---|---|
| `referee` | One row: `enabled`, `session_id` (null before the first job), `state` (`ok` or `needs_rebuild`) |
| `referee_sessions` | Every session ID the game has used, with `started_at` and `ended_reason` (kept after a rebuild) |
| `referee_inputs` | `participant_id`, `request_id`, `text`, `private`, `received_at`; unique on (participant, request) |
| `referee_jobs` | `job_id`, `input_id`, `state`, `host_run`, `revision_at_start`, `started_at`, `ended_at`, `error`, `narrated` (whether its reply was published) |
| `referee_steps` | `job_id`, `step_id` (unique per job, across both kinds), `kind` (`act` or `referee_act`), the acting context, the command, its arguments, the mapped `request_id`, `status` (`intended`, `committed`, `refused`, `not_committed`), `result` |
| `referee_choices` | At most one pending choice per participant: `participant_id`, `question`, `asked_in_job`, `asked_at` |
| `referee_messages` | `seq`, `job_id`, `sender` (`referee` or a participant ID), `audience` (`table` or a list of participant IDs), `text`, `created_at` |
| `referee_rulings` | `id`, `job_id`, `text`, `scope`, `created_at` |

`tesserae referee enable --database DB` turns referee mode on for a game; it
stays on. In referee mode, players' raw gameplay commands are refused (section
8) whether a job is running or not.

### Job states

| State | Busy? | Next |
|---|---|---|
| `running` | yes | `completed`, `failed`, `interrupted` |
| `completed` | no | (final) |
| `failed` | no | (final; `error` says why) |
| `interrupted` | no | (final; see section 10) |

The game is busy exactly while one job is `running`, or while the job lock is
still held after a crash (section 4).

### Pending choices

A pending choice belongs to the game, not to a job, so it survives other
players' conversation:

- The referee sets one with the reply's `choices.ask` (section 7): at most one
  per participant; asking that participant again replaces it.
- It ends only when the referee lists that participant in `choices.resolve`,
  normally after acting on their answer, or when it asks them something new.
- Anything else leaves it alone: another player's talk, a question from the
  addressed player, or a reply that sets or resolves nothing.
- Any input from anyone starts a job once the game is idle; `get_context` shows
  every pending choice, with who it is for.
- Example (a test): Alice is asked "Try the repair now?"; Bob asks the referee a
  question and gets an answer (Alice's choice is unchanged); Alice asks "What
  happens if it fails?" and is answered (still unchanged); Alice says "Yes, go":
  the referee acts and resolves her choice.

### Accepting input

One transaction:

0. The sender's credential must still be active, checked inside the write lock
   as gameplay commands do; otherwise `session_revoked`, with no replay and no
   new job.
1. If (participant, request ID) already exists: same text and privacy → return
   that input's job state and, if finished, its reply; different → refuse with
   `request_conflict`. An exact retry never starts a run, busy or not.
2. Otherwise, if a job is `running` → refuse with `referee_busy`. Nothing is
   stored.
3. Otherwise insert the input, the player's message (section 7) and a `running`
   job, and return `accepted`.

Two simultaneous submissions serialize on the store's write lock: one gets
`accepted`, the other `referee_busy`.

Two cases that look alike, and stay different:

- **An accepted input, retried** (even while busy): the same `request_id`
  returns its recorded state and any reply. It never starts a second job.
- **A busy-refused input, sent again later:** it was never stored, so once the
  game is idle it is a fresh acceptance and starts a job. Nothing is queued in
  between, and no job is invented for the refused attempt.

**The sender's last input.** `referee_status` also returns the caller's own most
recent accepted input: its `request_id`, text, privacy, job ID and job state.
A newly started client uses it to recover (section 12), so a reply lost with the
old client never forces the player to retype, which would be a new request.

## 6. The bridge: the whitelisted scripts

`python -m tesserae.referee.bridge` is an MCP stdio server that Codex starts.
It holds no game logic: each tool is a request over the host's Unix socket
(`run/<game_id>.sock`, mode 0600), carrying the job token. The host checks the
token against the `running` job; when the job ends, the token stops working.
These are the only tools, and the only way Codex sees or changes the game.

| Tool | Arguments | Result |
|---|---|---|
| `get_context` | none | The module's complete referee context for the bounded scene (`Module.referee_context(state)`), the participants (ID, name), this job's input and sender, every pending choice, all recorded rulings, the committed steps of every earlier job whose reply was never published, and the first page of the conversation transcript |
| `get_transcript` | `after_seq` | The next page of the complete conversation transcript (up to 200 messages and `next_after_seq`), so nothing is ever cut off |
| `get_view` | `participant_id` | That participant's own snapshot view, so the referee knows what they can see |
| `list_actions` | none | The commands the job's sender may use now, with descriptions and stakes (`Module.referee_actions(state, participant_id)`), and any referee-only commands |
| `act` | `step_id`, `command`, `args` | Runs a player command **as the job's sender** (never anyone else), through the normal store path, at the current revision |
| `referee_act` | `step_id`, `command`, `args` | Runs one of the module's referee-only commands in the `referee` context (section 9) |
| `record_ruling` | `text`, `scope` | Stores an adopted ruling; it changes no mechanics |

- **Socket protocol.** One JSON object per line each way. Request:
  `{"token": "...", "tool": "act", "args": {...}}`. Response:
  `{"ok": true, "result": {...}}` or `{"ok": false, "error": {"code": "...",
  "message": "..."}}`. The host runs mutations in its normal worker path, so
  other players still get their revision notifications.
- **Step identity.** `step_id` is chosen by Codex, 1 to 64 characters with no
  whitespace (so the mapped request ID is always valid), and is
  unique within the job across `act` and `referee_act`. The host maps it to the
  request ID `ref:<job_id>:<step_id>` in the acting context's namespace, so the
  store's existing exact replay applies within a context. The `referee_steps`
  row decides first: the same step ID with another kind, command or arguments
  is `request_conflict` even if the other kind would act in another context.
- **The step journal and its recovery boundary.** For `act` and `referee_act`:
  1. Insert the step as `intended` (with its kind, context, command, arguments
     and mapped request ID) and commit. A conflicting row stops here.
  2. Run the command through `GameStore.execute`, which commits state, result,
     history and revision atomically, or refuses with nothing changed.
  3. Record `committed` with the result, or `refused` with the error.

  A crash between 2 and 3 leaves an `intended` row. Reconciliation (at startup,
  and before any new job) looks up its mapped request ID in the acting context's
  command history: found means `committed`, with the recorded result; absent
  means `not_committed`. So a committed action is always found, and one that
  never committed is never reported as done.
- **Errors** are tool results, not crashes: `action_rejected`,
  `unsupported_command`, `invalid_request` and `request_conflict` come back with
  their messages so the referee can explain them.
- Every call and result is recorded in the diagnostics file, and every mutation
  in `referee_steps` as above.

## 7. Messages and the reply schema

### Player input on the wire (REF-5)

Client to host, all with `"version": 1`:

```json
{"type": "say", "request_id": "c-1", "text": "Wren will try to fix the relay.", "private": false}
{"type": "referee_status"}
{"type": "messages", "after_seq": 12}
```

Host to client:

| Message | When |
|---|---|
| `{"type": "accepted", "request_id", "job_id", "seq"}` | Reply to a new `say` |
| `{"type": "error", "request_id", "code": "referee_busy", "message", "job": {"state", "for"}}` | Reply while another job runs |
| `{"type": "error", "code": "referee_mode", ...}` | Reply to a raw gameplay `command` in referee mode |
| `{"type": "referee_status", "state": "idle" or "busy", "choices": [{"question"}], "last_seq", "your_last_input": {"request_id", "text", "private", "job_id", "job_state"} or null}` | Reply to `referee_status`: the caller's referee projection (below) |
| `{"type": "messages", "messages": [...]}` | Reply to `messages`: the caller's own audience only |
| `{"type": "message", "seq", "from": {"kind": "referee"} or {"kind": "player", "name"}, "audience": "table" or "private", "text"}` | Unsolicited, to each recipient as it is stored |
| `{"type": "referee", "state": "busy" or "idle"}` | Unsolicited, to everyone, when a job starts or ends |

**The referee projection.** `referee_status`, the join snapshot and every sync
snapshot carry the same recipient-scoped object, read from one committed state
(one read transaction), `"referee": {"state",
"choices", "last_seq", "your_last_input"}`:

- `choices` lists only the pending choice addressed to the viewer (at most one),
  never another participant's, so a privately asked question cannot reach
  anyone else through status or a snapshot.
- `your_last_input` is always the viewer's own.
- `state` and `last_seq` are table information.

Only the referee (through `get_context`) sees the full list of pending choices. A retry of a `say` gets the same `accepted`, plus any stored reply
messages. Sync and joins never wait for a job.

### Audience

- A player's `say` is stored as a message. Unmarked, it goes to the whole table.
  Private, it goes only to that player (and the referee sees it).
- The referee's reply lists messages, each `table` or private. A private message
  may go only to the sender of a private input in this job: a reply to private
  input is private unless the referee writes a separate table message. The host
  refuses any other recipient and fails the job rather than guess.
- Before delivering, the host rechecks each recipient's session (a revoked
  credential gets nothing).

### Reply schema (`--output-schema`)

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["messages", "choices", "narrated"],
  "properties": {
    "messages": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
      "type": "object", "additionalProperties": false, "required": ["audience", "text"],
      "properties": {
        "audience": {"enum": ["table", "sender"]},
        "text": {"type": "string", "minLength": 1, "maxLength": 4000}}}},
    "choices": {
      "type": "object", "additionalProperties": false, "required": ["ask", "resolve"],
      "properties": {
        "ask": {"type": "array", "maxItems": 4, "items": {
          "type": "object", "additionalProperties": false, "required": ["participant_id", "question"],
          "properties": {"participant_id": {"type": "string"}, "question": {"type": "string", "maxLength": 1000}}}},
        "resolve": {"type": "array", "maxItems": 4, "items": {"type": "string"}}}},
    "narrated": {"type": "array", "maxItems": 16, "items": {
      "type": "object", "additionalProperties": false, "required": ["job_id", "step_id"],
      "properties": {"job_id": {"type": "string"}, "step_id": {"type": "string"}}}}
  }
}
```

`sender` means "privately to this job's sender". `choices.ask` sets or replaces
pending choices and `choices.resolve` ends them (section 5); both may be empty,
and empty changes nothing. The host checks every named participant exists.
`narrated` lists the earlier, unnarrated results (from `get_context`) that this
reply tells their players about; a reply always narrates its own job's steps.
Each listed step's sender must be able to see at least one of the reply's
messages, or the reply is refused: a result is only acknowledged once it can
have reached its player, and anything not listed stays waiting. A
reply that does not parse, or does not match this schema everywhere (types,
required keys and limits, checked by the runner), fails the job
(`malformed_reply`), and nothing from it is published.

### Limits

Player text: 1 to 2,000 characters. Messages per reply: 8, each up to 4,000.
The transcript is never truncated: `get_context` returns its first page and
`get_transcript` the rest. The page size limits one tool result, not what the
referee can read. A rebuilt session (section 10) has only this transcript, so it
must be complete; the referee is told to read every page on a new session.

## 8. Referee mode on the host

- Every gameplay `command` from a player is refused with `referee_mode`. Joins,
  `sync`, `referee_status`, `messages` and the operator's local commands work.
- The referee's `act` and `referee_act` are the only gameplay writers, through
  the same `GameStore.execute` (validation, atomic state/result/history, exact
  replay). No model process holds a transaction open.
- Mechanical-only hosting (no referee mode) is unchanged.

## 9. Referee authority

- `act` always runs as the job's sender. The model cannot name another player.
- The `referee` context (participant ID `referee`, which no token can
  authenticate as) runs only the module's referee-only commands, declared as
  `Module.referee_commands`. A module checks `action.participant_id ==
  "referee"`; the host never offers those commands to a player context.
- New optional module hooks: `referee_context(state) -> dict`,
  `referee_actions(state, participant_id) -> list`, `referee_commands`. A module
  without them gets a minimal default (its view for the participant and its
  command names) and no referee-only commands.
- Rulings are text with a scope, stored and shown in `get_context`; they never
  change state or add executable rules.

## 10. Failures and recovery

| What fails | Detected by | Result |
|---|---|---|
| Codex will not start, or exits non-zero before `thread.started` | launch error or exit status | job `failed` (`runner_failed`); no session change; busy released |
| The host cannot save the session ID from `thread.started` | the save raises, or has not returned by the deadline | the run is stopped; job `failed` (`runner_failed`), even if Codex exited cleanly; the ID is kept in the run's result and diagnostics |
| The run's process group survives SIGKILL | cleanup is not confirmed | job `failed` (`cleanup_failed`), nothing published; lock record `stuck`, so the game stays busy until the group is gone or the operator, after checking by hand, clears it |
| Exit with no valid final reply | missing or invalid `LAST_MESSAGE` | job `failed` (`malformed_reply`), `narrated` false; its committed steps stay and are listed to the next job |
| Timeout | the 300 s limit | process group stopped; job `interrupted`; committed steps stay |
| Host stops cleanly during a job (SIGINT) | shutdown | the host stops the job's process group, waits for the lock, and marks the job `interrupted` |
| Host is killed or crashes during a job | at startup, a `running` job | the job lock decides (section 4): if Codex is gone, the job becomes `interrupted`; if it is still alive, the game stays busy and its bridge calls are refused until it exits |
| A step committed, then the host died before journaling it | an `intended` step at reconciliation | the mapped request ID is found in the command history: `committed`, with its result |
| A step was intended, but never ran | an `intended` step at reconciliation | not in the history: `not_committed`; nothing to narrate or undo |
| Any job ends without publishing a reply (`failed`, `interrupted`) | `narrated` false | its committed steps are listed by every later `get_context` until a reply narrates them; the sender sees the job's state |
| `resume` finds no session | runner error ("no rollout found") | job `failed` (`session_missing`); `referee.state = needs_rebuild`; later input is refused with `referee_needs_rebuild` until the operator rebuilds |
| Bridge call with a stale token | the host | refused; nothing runs |

- **Rebuild.** `tesserae referee rebuild --database DB` (host stopped, lock
  free) ends the current session in `referee_sessions` and clears `session_id`.
  The next job starts a new session; `get_context` and `get_transcript` supply
  the complete stored conversation and rulings. No game state changes.
- **Retries.** A player resending an input exactly gets its recorded state
  (`interrupted`, `failed`, or the reply). It never reruns. They say something
  new to continue.
- There is never more than one Codex process per game: a new job starts only
  when the job lock is free (section 4).

## 11. Demonstrations

### The original Relay conversation (REF-6)

Two players, a Relay game in referee mode. The referee has no NPC; Relay needs
only `referee_context` (the scene, the rules text, both workers) and
`referee_actions`. The walkthrough must show, in order:

1. Alice: "What is this place?" No step; the referee describes the scene to
   the table. No revision change.
2. Alice: "I'd like to sign up a worker called Wren." The referee calls
   `act` with `relay.enlist {"name": "Wren"}` and narrates the result.
3. Bob, private: "Can I see what Alice's worker is like?" A private reply to
   Bob only: the referee may describe what is publicly visible, not Alice's
   sheet.
4. Bob: "Odo signs up too." `act` with `relay.enlist {"name": "Odo"}`.
5. Alice: "Can Wren fix the relay?" The referee explains the stakes (1d6 plus
   repair against 5, one effort either way) and asks whether to try now
   (`choices.ask` for Alice). The job ends; busy is released.
6. Bob asks the referee something while Alice decides: a job answers him, and
   Alice's pending choice is unchanged.
7. Alice: "Yes, go." `act` with `relay.repair {}`; the referee narrates the
   actual roll and result, and resolves Alice's choice.
8. While that job runs, Bob sends a new input: `referee_busy`, and nothing is
   stored. Bob's earlier, accepted input (step 6), resent with its request ID,
   returns its recorded reply without a job. Bob's refused input, sent again
   after the game is idle, is a fresh acceptance and starts a job.
9. Privacy of choices: the referee privately asks Alice a choice. Bob's
   `/status`, join snapshot and sync show no choice and none of its question;
   Alice's show hers.
10. Recovery: Alice's client exits right after an input is accepted and before
   its reply arrives. A new client shows her last input and its state from
   `referee_status`; `/retry` resends it with the same request ID and gets the
   recorded reply, with no second job or action.

### The private scene (REF-7), criteria only

- The private module adds `referee_context` (complete bounded context, including
  hidden preparation for the referee only) and `referee_actions`.
- The fixed opponent policy is replaced by referee-only commands for the
  opponent's choices. Automatic saves, action costs and turn bookkeeping stay in
  the module and still happen exactly once; the opponent's turn waits for the
  referee's `referee_act`.
- Before a consequential roll, the referee states the stakes and asks for any
  declared resource; none declared means zero.
- The approved mechanics, bindings and fixed-draw tests stay; only the
  opponent's decision-maker changes.

## 12. The CLI in referee mode (REF-5)

This is a human-friendly interface (owner decision O-1, 2026-10-31).

**Input.** Each stdin line is a `say` with a client-generated request ID. Lines
starting with `/` are plumbing: `/private TEXT`, `/status`, `/sync`,
`/history`, `/retry`. `//TEXT` sends text that starts with `/`. Anything else,
including text that looks like JSON or code, is sent as the player's words.

**Recovery.** On every join, the client asks `referee_status` and prints the
player's last accepted input and its state (for example `[your last input]
"Yes, go." completed`). `/retry` resends an input exactly, with its original
request ID, text and privacy: the last unanswered input of this client if there
is one, or else the `your_last_input` the host reported. Retrying an accepted
input returns its recorded state and reply, never a second job. An input the
host refused as busy is never in `your_last_input`; to send it again the player
types it again, as a fresh input.

**Output.** Readable lines on stdout, not JSON:

```text
[table] Alice: Can Wren fix the relay?
[table] Referee: Wren can try: 1d6 plus repair 1, needing 5. It costs one effort either way. Try now?
[private] Referee: From where you stand you can see Wren's tools, nothing more.
[waiting for Alice]
[busy] The referee is still answering Bob. Try again when it finishes.
[status] idle; waiting for Alice; last message 6
```

Messages show their audience and sender; status, busy, pending choices and errors get
short bracketed lines; `/sync` prints a short summary of the player's own view.
Diagnostics stay on stderr. `connect --json` keeps the JSON-line stream (and
sends input as `say` messages the same way) for scripts and tests.

## 13. Feasibility evidence (2026-10-31)

Six short `codex exec` probes on `codex-cli 0.166.0`, ephemeral, with the
section 3 settings and a one-tool stub bridge:

1. With the shell features off, Codex could not run `ls` or read `/etc/hosts`.
2. Turning `code_mode_host` off also removed MCP access, so code mode stays on.
3. The stub tool was called with no approval prompt. Inside `functions.exec`,
   `require`, `fs`, `fetch`, `process`, `Deno`, `File` and `Bun` were all
   undefined. Without a separate `CODEX_HOME`, the owner's personal agent
   instructions were loaded.
4. `apply_patch` was rejected by the read-only sandbox, and nothing was written.
   `agents.max_threads` cannot be 0 in this version.
5. The section 7 reply schema was accepted by `--output-schema`, and the reply
   written to `-o` matched it.
6. After review, the revised schema (with `choices.ask` and `choices.resolve`)
   was accepted too; Codex set a pending choice for the addressed player and
   resolved nothing, as asked.

The probes say nothing about crash recovery, the step journal or client
reconnects; those are specified above and tested by REF-2 to REF-5.

REF-3's real probe (2026-10-31, `codex-cli 0.166.0`, through `CodexRun` with
the referee folder set up by `tesserae referee setup` and logged in): a first
run captured session `01a1937b-…` from `thread.started` and returned a valid
reply; a second run resumed that session by ID and recalled a word given only
in the first. The session file was saved in the referee's own Codex home, not
`~/.codex`. With the bridge module not yet present (#67), runs that call no
tool were unaffected.

## 14. An illustrative exchange

This shows the shapes only; it is not a recorded run.

```text
alice> {"version": 1, "type": "say", "request_id": "a-1", "text": "Can Wren fix the relay?", "private": false}
alice< {"version": 1, "type": "accepted", "request_id": "a-1", "job_id": "j-3", "seq": 5}
all  < {"version": 1, "type": "message", "seq": 5, "from": {"kind": "player", "name": "Alice"}, "audience": "table", "text": "Can Wren fix the relay?"}
all  < {"version": 1, "type": "referee", "state": "busy"}
         (Codex: get_context, list_actions; no step)
all  < {"version": 1, "type": "message", "seq": 6, "from": {"kind": "referee"}, "audience": "table", "text": "Wren can try: 1d6 plus repair 1, needing 5. It costs one effort either way. Try now?"}
all  < {"version": 1, "type": "referee", "state": "idle"}
alice< (snapshot field) "referee": {"state": "idle", "choices": [{"participant_id": "p_a…", "name": "Alice", "question": "Try now?"}], "last_seq": 6, "your_last_input": {"request_id": "a-1", "text": "Can Wren fix the relay?", "private": false, "job_id": "j-3", "job_state": "completed"}}
alice> {"version": 1, "type": "say", "request_id": "a-2", "text": "Yes, go.", "private": false}
         (Codex: act {"step_id": "repair-1", "command": "relay.repair", "args": {}} -> revision 3, roll 5, success)
all  < {"version": 1, "type": "revision", "game_id": "…", "revision": 3}
all  < {"version": 1, "type": "message", "seq": 8, "from": {"kind": "referee"}, "audience": "table", "text": "Wren rolls a 5: 6 against 5. The relay hums back to life. Effort 3 → 2."}
         (the reply's choices.resolve lists Alice: her pending choice ends)
```
