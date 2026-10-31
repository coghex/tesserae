# M1-6 referee contract (REF-1)

Status: specification for [#64](https://github.com/coghex/tesserae/issues/64)
under tracker [#63](https://github.com/coghex/tesserae/issues/63). Nothing here
is implemented yet. It turns the [M1-6 delivery plan](codex_referee_design.md)
and the owner's decisions into exact contracts for REF-2 to REF-8. Section 12
lists the one product choice still open.

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
  stops the process group it started (SIGTERM, then SIGKILL after 10 s) and
  never touches any other process.

### Prompts

`AGENTS.md` (written by setup) holds the standing instructions, the same for
every game: you are the referee for a tabletop game; the bridge tools are your
only way to see or change it, and their results are the truth; never invent
rolls or outcomes; call `act` only for what the sender has clearly chosen, and
ask (with `awaiting`) when a consequential choice is missing; state stakes and
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
of a session adds "This is the start of the game's referee conversation."; a
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
| `referee_jobs` | `job_id`, `input_id`, `state`, `revision_at_start`, `started_at`, `ended_at`, `error`, `awaiting` (participant ID and question, or null) |
| `referee_steps` | `job_id`, `step_id`, `kind` (`act` or `referee_act`), the command, its arguments, the mapped `request_id`, `status`, `result` |
| `referee_messages` | `seq`, `job_id`, `sender` (`referee` or a participant ID), `audience` (`table` or a list of participant IDs), `text`, `created_at` |
| `referee_rulings` | `id`, `job_id`, `text`, `scope`, `created_at` |

`tesserae referee enable --database DB` turns referee mode on for a game; it
stays on. In referee mode, players' raw gameplay commands are refused (section
8) whether a job is running or not.

### Job states

| State | Busy? | Next |
|---|---|---|
| `running` | yes | `completed`, `awaiting_input`, `failed`, `interrupted` |
| `completed` | no | (final) |
| `awaiting_input` | no | (final for this job; the answer starts a new job) |
| `failed` | no | (final; `error` says why) |
| `interrupted` | no | (final; see section 10) |

The game is busy exactly while one job is `running`.

A pending choice (`awaiting`) releases busy. The named participant's next input
is their answer and starts a new job; anyone else's input also starts a job,
and `get_context` still shows the pending choice. A new reply replaces it.

### Accepting input

One transaction:

1. If (participant, request ID) already exists: same text and privacy → return
   that input's job state and, if finished, its reply; different → refuse with
   `request_conflict`. An exact retry never starts a run, busy or not.
2. Otherwise, if a job is `running` → refuse with `referee_busy`. Nothing is
   stored.
3. Otherwise insert the input, the player's message (section 7) and a `running`
   job, and return `accepted`.

Two simultaneous submissions serialize on the store's write lock: one gets
`accepted`, the other `referee_busy`.

## 6. The bridge: the whitelisted scripts

`python -m tesserae.referee.bridge` is an MCP stdio server that Codex starts.
It holds no game logic: each tool is a request over the host's Unix socket
(`run/<game_id>.sock`, mode 0600), carrying the job token. The host checks the
token against the `running` job; when the job ends, the token stops working.
These are the only tools, and the only way Codex sees or changes the game.

| Tool | Arguments | Result |
|---|---|---|
| `get_context` | none | The module's complete referee context for the bounded scene (`Module.referee_context(state)`), the participants (ID, name), this job's input and sender, any pending choice, the last 50 conversation messages, recorded rulings, and committed steps of any interrupted job |
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
- **Step identity.** `step_id` is chosen by Codex, 1 to 64 characters. The host
  maps it to the request ID `ref:<job_id>:<step_id>` in the acting context's
  namespace, so the store's existing exact replay applies: the same step returns
  its recorded result; the same step ID with other content is
  `request_conflict`.
- **Errors** are tool results, not crashes: `action_rejected`,
  `unsupported_command`, `invalid_request` and `request_conflict` come back with
  their messages so the referee can explain them.
- Every call and result is recorded in `referee_steps` (for mutations) and the
  diagnostics file.

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
| `{"type": "referee_status", "state": "idle" or "busy", "awaiting": {"participant_id", "name"} or null, "last_seq"}` | Reply to `referee_status` |
| `{"type": "messages", "messages": [...]}` | Reply to `messages`: the caller's own audience only |
| `{"type": "message", "seq", "from": {"kind": "referee"} or {"kind": "player", "name"}, "audience": "table" or "private", "text"}` | Unsolicited, to each recipient as it is stored |
| `{"type": "referee", "state": "busy" or "idle"}` | Unsolicited, to everyone, when a job starts or ends |

The snapshot gains `"referee": {"state", "awaiting", "last_seq"}` in referee
mode. A retry of a `say` gets the same `accepted`, plus any stored reply
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
  "required": ["messages", "awaiting"],
  "properties": {
    "messages": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
      "type": "object", "additionalProperties": false, "required": ["audience", "text"],
      "properties": {
        "audience": {"enum": ["table", "sender"]},
        "text": {"type": "string", "minLength": 1, "maxLength": 4000}}}},
    "awaiting": {"anyOf": [{"type": "null"}, {
      "type": "object", "additionalProperties": false, "required": ["participant_id", "question"],
      "properties": {"participant_id": {"type": "string"}, "question": {"type": "string", "maxLength": 1000}}}]}
  }
}
```

`sender` means "privately to this job's sender". `awaiting` names whose choice
is pending; the host checks it is a participant. A reply that does not parse
or validate fails the job (`malformed_reply`), and nothing from it is published.

### Limits

Player text: 1 to 2,000 characters. Messages per reply: 8, each up to 4,000.
`get_context` returns at most the last 50 messages.

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
| Codex will not start, or exits non-zero before `thread.started` | exit status | job `failed` (`runner_failed`); no session change; busy released |
| Exit with no valid final reply | missing or invalid `LAST_MESSAGE` | job `failed` (`malformed_reply`); committed steps stay committed and are listed to the next job |
| Timeout | the 300 s limit | process group stopped; job `interrupted`; committed steps stay |
| Host stops or crashes during a job | at startup, a `running` job | that job becomes `interrupted`; its process, if still alive, is the host's own child and was stopped with it |
| A step committed, then the run died before replying | `referee_steps` | the commit stands; the sender sees `interrupted`; the next job's `get_context` lists the committed steps so the referee narrates them instead of repeating them |
| `resume` finds no session | runner error ("no rollout found") | job `failed` (`session_missing`); `referee.state = needs_rebuild`; later input is refused with `referee_needs_rebuild` until the operator rebuilds |
| Bridge call with a stale token | the host | refused; nothing runs |

- **Rebuild.** `tesserae referee rebuild --database DB` (host stopped) ends the
  current session in `referee_sessions` and clears `session_id`. The next job
  starts a new session, and `get_context` supplies the stored conversation and
  rulings. No game state changes.
- **Retries.** A player resending an input exactly gets its recorded state
  (`interrupted`, `failed`, or the reply). It never reruns. They say something
  new to continue.
- There is never more than one Codex process per game: a new job starts only
  after the previous one's process has exited or been stopped.

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
   repair against 5, one effort either way) and asks whether to try now:
   `awaiting` Alice. Busy is released.
6. Bob says something while Alice decides: accepted as table conversation (no
   action for Alice).
7. Alice: "Yes, go." `act` with `relay.repair {}`; the referee narrates the
   actual roll and result.
8. While that job runs, Bob's new input gets `referee_busy`; his exact retry
   after it finishes returns his first input's state, not a new job.

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

## 12. Open product choice

- **O-1. How the CLI prints conversation.** Input is plain text (below). For
  output, either keep JSON lines on stdout, or print `message` events as
  readable lines such as `[table] Referee: ...` and `[private] Referee: ...`.
  Recommendation: readable lines in referee mode, with `--json` keeping JSON
  lines for scripts and tests. The owner decides before REF-5.

The input convention (REF-5), in referee mode: each stdin line is a `say` with
a client-generated request ID. Lines starting with `/` are plumbing:
`/private TEXT`, `/status`, `/sync`, `/history`, `/retry` (resend the last
unanswered input unchanged). `//TEXT` sends text that starts with `/`. Anything
else, including text that looks like JSON or code, is sent as the player's
words.

## 13. Feasibility evidence (2026-10-31)

Five short `codex exec` probes on `codex-cli 0.166.0`, ephemeral, with the
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
alice< (snapshot field) "referee": {"state": "idle", "awaiting": {"participant_id": "p_a…", "name": "Alice"}, "last_seq": 6}
alice> {"version": 1, "type": "say", "request_id": "a-2", "text": "Yes, go.", "private": false}
         (Codex: act {"step_id": "repair-1", "command": "relay.repair", "args": {}} -> revision 3, roll 5, success)
all  < {"version": 1, "type": "revision", "game_id": "…", "revision": 3}
all  < {"version": 1, "type": "message", "seq": 8, "from": {"kind": "referee"}, "audience": "table", "text": "Wren rolls a 5: 6 against 5. The relay hums back to life. Effort 3 → 2."}
```
