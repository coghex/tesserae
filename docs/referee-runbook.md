# Running a game with the AI referee: operator runbook

This sets up and runs a game whose players talk to an AI referee instead of
typing commands. The referee is OpenAI's Codex CLI, run by the host once per
player input in a folder of its own. It reads and changes the game only
through Tesserae's bridge, and the host checks every call. The exact rules are
in the [referee contract](designs/referee_contract.md); the messages are in
[protocol.md](protocol.md#referee-mode). The host is the same foreground host
as always: for play across a LAN, combine this with the
[LAN runbook](lan-runbook.md).

Paths, names and addresses below are placeholders. Databases, tokens and the
referee folder stay outside any repository.

## 1. Install

- Python 3.11 or later, and Tesserae installed into a virtual environment:
  `python3 -m venv .venv && .venv/bin/python -m pip install .` (or `-e .` for
  development). The commands below assume `tesserae` is that environment's.
- The Codex CLI, on your `PATH` (`codex --version`). Tesserae does not pin a
  Codex version; it was verified with `codex-cli 0.166.0`. If a later Codex
  changes its options or events, `tesserae referee check` and the job
  diagnostics (section 7) are where it shows.

## 2. The referee folder and its one-time login

    tesserae referee setup
    CODEX_HOME=~/.local/share/tesserae/referee/codex-home codex login
    tesserae referee check

`setup` creates the referee folder (default `~/.local/share/tesserae/referee`,
mode 0700; another with `--referee-dir DIR` on every referee command and on
`serve`). It writes Codex's configuration there, which leaves Codex only the
Tesserae bridge (section 6), its standing instructions and the reply schema.
`login` signs that separate Codex home in once; your own `~/.codex` is never
used. `check` reports the Codex version, whether the configuration is current
and whether the login works.

The configuration names the Python that runs the bridge, the one `setup` ran
with. **Rerun `tesserae referee setup` whenever you reinstall or move
Tesserae**, or the bridge will not start; it rewrites only its own files and
keeps the login and saved sessions.

What is in the folder:

| Path | What it is |
|---|---|
| `codex-home/config.toml`, `AGENTS.md` | Written by `setup`; rerun it to restore them |
| `codex-home/auth.json` | The Codex login: a secret; never copy it anywhere shared |
| `codex-home/sessions/` | Every game's Codex conversation history |
| `games/<game_id>/jobs/` | Per-job diagnostics (section 7) |
| `run/` | The running host's sockets and lock files |

**Back up `codex-home/sessions/` together with the game database.** The
database holds the game, the whole conversation and every ruling; the session
holds Codex's own memory of it. If a session is lost, the game still works
after a rebuild (section 5), which starts a new session from the stored
conversation.

## 3. Create and serve a game

    tesserae game create --database game.sqlite3 --module relay
    tesserae participant add --database game.sqlite3 --name Alice --token-file alice.token
    tesserae participant add --database game.sqlite3 --name Bob --token-file bob.token
    tesserae referee enable --database game.sqlite3
    tesserae serve --authenticated --database game.sqlite3 --port 8765

Any module works; a trusted external package also takes `--module-dir` on
`game create` and `serve` ([external modules](external-modules.md)). A module
can give the referee a richer view and referee-only commands
([modules.md](modules.md)); without them the referee sees each player's view.
Referee mode stays on once enabled. `serve` refuses a referee game without
`--authenticated` or a set-up referee folder, and refuses to start while
another host serves the same game. `--referee-timeout SECONDS` changes the
300-second limit on one referee job.

Give each player their token file, as for any authenticated game.

## 4. Playing

    tesserae connect ws://127.0.0.1:8765/ --token-file alice.token

Players type plain words. Everything typed goes to the referee and the whole
table, even text that looks like a command or JSON. The referee interprets it,
runs the game's commands for the player who said it (never anyone else), and
narrates the results. It states the stakes before a roll and asks when a
consequential choice is missing; a question it asks you waits for your answer
while others keep playing.

| Type | To |
|---|---|
| any words | say them to the referee and the table |
| `/private WORDS` | say them to the referee only; its reply comes to you only |
| `//WORDS` | send words that start with `/` |
| `/status` | see whether the referee is busy, what is waiting for you, and your last input and its state |
| `/sync` | see your current view of the game |
| `/history` | see the whole conversation you may see |
| `/retry` | resend your last input exactly, if you never saw its reply |
| `/help` | list these |

- **One thing at a time.** While the referee answers someone, new input is
  refused (`[busy] The referee is still answering Alice.`), never queued: say
  it again when `[idle]` appears.
- **Retrying is safe.** `/retry` resends with the original request ID. The
  host returns the recorded reply or state; it never starts a second job or
  repeats an action.
- **Players cannot bypass the referee.** A game command sent directly is
  refused (`referee_mode`).
- `connect --json` prints JSON lines instead, for scripts.

## 5. Stopping, restarting, rebuilding

- **Stop the host** with Ctrl-C. A job in progress is stopped and ends
  `interrupted`; anything it had already done to the game stays done and is
  listed to the next job, so the referee can tell the players.
- **Restart** with the same `serve` command. The game continues from the
  stored state, and the next job resumes the same Codex session. If the
  previous host crashed while a job ran, the game stays busy until that job's
  Codex has exited, then the job ends `interrupted`.
- **Stop one job** without stopping the host:
  `tesserae referee stop --database game.sqlite3`. With no host running, it
  stops a leftover Codex only if it can prove it is the job's own, and
  otherwise says why not.
- **A stuck job.** If a job's processes survived even SIGKILL, the game stays
  busy and `stop` names the process group to check by hand
  (`ps -A -o pid,pgid,lstart,command`). Stop what belongs to the job, then
  `tesserae referee stop --database game.sqlite3 --clear-stuck`.
- **Rebuild.** If players get `referee_needs_rebuild`, Codex could not find the
  game's session. Stop the host, run
  `tesserae referee rebuild --database game.sqlite3`, and start it again. The
  next job starts a new session from the stored conversation and rulings; the
  game itself is unchanged.

## 6. What Codex can do

Codex runs with a read-only sandbox, no approval prompts, no web search and
every built-in tool that could run a command, read a file or browse turned off.
Its only tools are the bridge's: read the game as the referee sees it, run one
of the speaking player's commands, run one of the module's referee-only
commands, and record a ruling. The host authorizes each call against the one
running job, which ends with the job or the host. A player who asks the
referee to read a file or run something gets no such access.

## 7. Diagnostics

- **The host's output**: its listening line, then only failures (a job that
  failed inside the host, a store error, a connection closed for not reading),
  with tracebacks. It never contains tokens or what players typed.
- **Per job**, in `games/<game_id>/jobs/` of the referee folder:
  `<job>.jsonl` holds Codex's events and every bridge call with its result;
  `<job>.reply.json` is Codex's final reply. They can contain the
  conversation, so keep them private. They never contain tokens.
- **The job lock**, `run/<game_id>.lock`, records the current job's stage
  (`starting`, `running` with its process, `ended`, or `stuck`).
- A job that ended without a reply shows as `failed` or `interrupted` in the
  player's `/status`; its reason is in the database (`referee_jobs.error`):
  `runner_failed` (Codex would not start or
  exited badly), `malformed_reply`, `session_missing`, `cleanup_failed`, or
  `interrupted` for `timeout`, `shutdown`, `operator` or `host_stopped`.

## Scope

One referee conversation per game, one job at a time, no queue. No browser
client, direct model API, selective context loading or new game mechanics.
The [acceptance record](acceptance/referee.md) shows both demonstration
scenes played this way.
