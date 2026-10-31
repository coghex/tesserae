"""Run and resume supervised Codex referee subprocesses (#66, REF-3).

Implements docs/designs/referee_contract.md sections 2 to 4: the referee
folder and its own Codex home, the config that leaves Codex only the Tesserae
bridge, the per-game job lock, and one `codex exec` (or `exec resume`) per
job with the prompt on stdin. It knows nothing about game rules, WebSockets
or the store; the host wires those together (#69).
"""

import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DIR = Path.home() / ".local" / "share" / "tesserae" / "referee"
SESSION_MISSING = "no rollout found"  # what `codex exec resume` says (0.166.0)
MAX_LINE = 1_000_000  # bytes of one JSONL event kept in diagnostics
MAX_STDERR = 200_000
DEFAULT_TIMEOUT = 300.0
STOP_GRACE = 10.0

# Every built-in tool that could run a command, read a file, browse, or start
# another agent is off (contract section 3; verified on codex-cli 0.166.0).
FEATURES_OFF = (
    "shell_tool", "unified_exec", "view_image", "image_generation", "browser_use",
    "in_app_browser", "computer_use", "apps", "plugins", "goals", "multi_agent",
    "sleep_tool", "tool_suggest",
)  # fmt: skip

REPLY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["messages", "choices"],
    "properties": {
        "messages": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False, "required": ["audience", "text"],
            "properties": {
                "audience": {"enum": ["table", "sender"]},
                "text": {"type": "string", "minLength": 1, "maxLength": 4000}}}},
        "choices": {
            "type": "object", "additionalProperties": False, "required": ["ask", "resolve"],
            "properties": {
                "ask": {"type": "array", "maxItems": 4, "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["participant_id", "question"],
                    "properties": {"participant_id": {"type": "string"},
                                   "question": {"type": "string", "maxLength": 1000}}}},
                "resolve": {"type": "array", "maxItems": 4, "items": {"type": "string"}}}},
    },
}  # fmt: skip

AGENTS_TEXT = """\
# Referee instructions

You are the referee for a tabletop game played by friends over Tesserae.

- The `tesserae` tools are your only way to see or change the game. Their
  results are the truth: never invent a roll, a result or a character's state.
- Start every job with `get_context`. On a new session, read every page of the
  transcript with `get_transcript` before replying.
- Use `act` only for something the sender has clearly chosen. When a
  consequential choice is missing, ask for it with `choices.ask`, and resolve the
  choice (`choices.resolve`) once it has been answered.
- Before any roll, state what is rolled, against what, and any declared resource
  the player is spending. Questions and talk need no tool step and do not move
  time on.
- Give every mutating call a new, short `step_id`; reuse one only to retry that
  exact call.
- Keep hidden preparation and other players' private details out of table
  messages. Reply privately (`sender`) only to private input.
- Do not start other agents.
- Reply only in the required JSON shape.
"""


class RunnerError(RuntimeError):
    """The referee folder or a stop request cannot be used; nothing was run."""


def config_text(python: str = sys.executable) -> str:
    """The referee Codex home's `config.toml` (contract section 3)."""
    features = "\n".join(f"{name} = false" for name in FEATURES_OFF)
    return (
        "# Written by `tesserae referee setup`; rerun setup to restore it.\n"
        'sandbox_mode = "read-only"\n'
        'approval_policy = "never"\n'
        'web_search = "disabled"\n\n'
        f"[features]\n{features}\n\n"
        "[mcp_servers.tesserae]\n"
        f"command = {json.dumps(python)}\n"
        'args = ["-m", "tesserae.referee.bridge"]\n'
        'env_vars = ["TESSERAE_BRIDGE_SOCKET", "TESSERAE_BRIDGE_TOKEN"]\n'
        "tool_timeout_sec = 60\n"
    )


@dataclass(frozen=True)
class RefereeDir:
    """The referee folder's layout (contract section 2)."""

    root: Path

    @property
    def codex_home(self) -> Path:
        return self.root / "codex-home"

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def run(self) -> Path:
        return self.root / "run"

    @property
    def schema(self) -> Path:
        return self.root / "reply.schema.json"

    def lock_path(self, game_id: str) -> Path:
        return self.run / f"{game_id}.lock"

    def socket_path(self, game_id: str) -> Path:
        return self.run / f"{game_id}.sock"

    def jobs(self, game_id: str) -> Path:
        return self.root / "games" / game_id / "jobs"

    def login_command(self) -> str:
        return f"CODEX_HOME={self.codex_home} codex login"


def setup(root: Path = DEFAULT_DIR, python: str = sys.executable) -> RefereeDir:
    """Create the folder (mode 0700) and write the Codex home's config and
    instructions and the reply schema. Rerunning rewrites only those files."""
    folder = RefereeDir(Path(root).expanduser())
    for path in (
        folder.root,
        folder.codex_home,
        folder.workspace,
        folder.run,
        folder.root / "games",
    ):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    for path, text in (
        (folder.codex_home / "config.toml", config_text(python)),
        (folder.codex_home / "AGENTS.md", AGENTS_TEXT),
        (folder.schema, json.dumps(REPLY_SCHEMA, indent=1) + "\n"),
    ):
        path.write_text(text)
        path.chmod(0o600)
    return folder


def check(
    root: Path = DEFAULT_DIR, codex=("codex",), python: str = sys.executable
) -> dict:
    """What `tesserae referee check` reports: the Codex version, whether its
    login works in the referee home, and whether the config is current."""
    folder = RefereeDir(Path(root).expanduser())
    config = folder.codex_home / "config.toml"
    report = {
        "folder": str(folder.root),
        "codex_version": codex_version(codex),
        "config_current": config.is_file()
        and config.read_text() == config_text(python),
        "logged_in": False,
    }
    if folder.codex_home.is_dir() and report["codex_version"] is not None:
        try:
            status = subprocess.run(
                [*codex, "login", "status"], capture_output=True, text=True,
                env={**os.environ, "CODEX_HOME": str(folder.codex_home)}, timeout=60,
            )  # fmt: skip
        except (OSError, subprocess.TimeoutExpired):
            return report
        report["logged_in"] = status.returncode == 0
    return report


def codex_version(codex=("codex",)) -> str | None:
    try:
        result = subprocess.run(
            [*codex, "--version"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


# --- The job lock (contract section 4) ------------------------------------------


class JobLock:
    """The per-game lock that says whether anything from a job may be alive.

    The host takes it before a job and hands its descriptor to Codex, so the
    kernel holds it until the host *and* Codex have both closed it. Its file
    records the job's stage for operators; it never proves which process is
    alive by itself (see `stop_orphan`).
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd = None

    def try_acquire(self) -> bool:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def write(self, record: dict):
        """Replace the record. Only the holder writes."""
        if self.fd is None:
            raise RunnerError("The job lock is not held.")
        data = json.dumps(record, sort_keys=True).encode()
        os.ftruncate(self.fd, 0)
        os.pwrite(self.fd, data, 0)
        os.fsync(self.fd)

    def release(self):
        if self.fd is not None:
            os.close(self.fd)  # the kernel frees the lock once no holder remains
            self.fd = None

    def __enter__(self):
        if not self.try_acquire():
            raise RunnerError("A referee job for this game may still be running.")
        return self

    def __exit__(self, *exc_info):
        self.release()

    @staticmethod
    def held(path: Path) -> bool:
        """Whether anyone holds the lock right now (the caller must not)."""
        probe = JobLock(path)
        if not Path(path).exists():
            return False
        if probe.try_acquire():
            probe.release()
            return False
        return True

    @staticmethod
    def read(path: Path) -> dict | None:
        try:
            return json.loads(Path(path).read_text() or "null")
        except (OSError, ValueError):
            return None


def process_identity(pid: int) -> dict | None:
    """The process's start time and command as `ps` reports them, or None."""
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-o", "comm=", "-p", str(pid)],
        capture_output=True,
        text=True,
    )
    line = result.stdout.strip()
    if result.returncode != 0 or not line:
        return None
    # lstart is five fields: weekday, month, day, time, year.
    parts = line.split(None, 5)
    return {
        "started_at": " ".join(parts[:5]),
        "command": parts[5] if len(parts) > 5 else "",
    }


def settled_identity(pid: int, tries: int = 20) -> dict | None:
    """The identity once two reads 0.1 s apart agree: a launcher (a venv's
    `python`, or a wrapper around `codex`) may re-exec just after spawning."""
    previous = process_identity(pid)
    for _ in range(tries):
        time.sleep(0.1)
        current = process_identity(pid)
        if current == previous:
            return current
        previous = current
    return previous


def stop_orphan(lock_path: Path) -> str:
    """Stop a crashed host's leftover Codex, only if it is provably the job's
    (contract section 4). Returns what happened; raises RunnerError and signals
    nothing if the process cannot be identified."""
    if not JobLock.held(lock_path):
        return "Nothing is running for this game."
    record = JobLock.read(lock_path) or {}
    state = record.get("state")
    if state == "starting":
        raise RunnerError(
            "The job is still starting; there is no process to identify yet. Try again."
        )
    if state != "running":
        raise RunnerError(
            f"The lock is held but its record is {state!r}; not stopping anything."
        )
    pid, pgid = record.get("pid"), record.get("pgid")
    if not isinstance(pid, int) or pid != pgid:
        raise RunnerError(
            "The record has no usable process group; not stopping anything."
        )
    identity = process_identity(pid)
    if identity is None:
        raise RunnerError(
            f"Process {pid} is gone, but the lock is still held; not stopping anything."
        )
    if identity != {
        "started_at": record.get("started_at"),
        "command": record.get("command"),
    }:
        raise RunnerError(
            f"Process {pid} is not the job's Codex (start time or command differ); not stopping it."
        )
    try:
        if os.getpgid(pid) != pgid:
            raise RunnerError(
                f"Process {pid} no longer leads its group; not stopping it."
            )
    except ProcessLookupError:
        return "The job's Codex has already exited."
    _stop_group(pgid, lambda: not JobLock.held(lock_path))
    return f"Stopped the job's Codex (process group {pgid})."


def _stop_group(pgid: int, gone, grace: float = STOP_GRACE):
    """SIGTERM a process group we own, then SIGKILL it after `grace` seconds."""
    for signum in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, signum)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if gone():
                return
            time.sleep(0.05)


# --- One Codex run per job (contract section 4) ----------------------------------


@dataclass
class RunResult:
    """How a job's Codex run ended. `outcome` is the job state to record:
    `completed` (with `reply`), `failed` or `interrupted` (with `error`)."""

    outcome: str
    error: str | None = None
    session_id: str | None = None
    reply: dict | None = None
    exit_code: int | None = None
    codex_version: str | None = None
    details: list = field(default_factory=list)


class CodexRun:
    """One supervised `codex exec` for one job, holding the game's job lock.

    `start()` launches it and returns; `wait()` blocks until it ends, times
    out or is stopped; `stop()` (from any thread) ends it for host shutdown.
    The caller holds `lock` for the whole job and releases it afterwards.
    """

    def __init__(self, folder: RefereeDir, *, game_id: str, job_id: str, host_run: str,
                 lock: JobLock, codex=("codex",), timeout: float = DEFAULT_TIMEOUT):  # fmt: skip
        self.folder, self.game_id, self.job_id, self.host_run = (
            folder,
            game_id,
            job_id,
            host_run,
        )
        self.lock, self.codex, self.timeout = lock, tuple(codex), timeout
        self.process = None
        self.session_id = None
        self._stopped = None
        self._stderr = []

    @property
    def diagnostics(self) -> Path:
        return self.folder.jobs(self.game_id) / f"{self.job_id}.jsonl"

    @property
    def reply_path(self) -> Path:
        return self.folder.jobs(self.game_id) / f"{self.job_id}.reply.json"

    def argv(self, session_id: str | None) -> list[str]:
        resume = ["resume", session_id] if session_id else []
        return [*self.codex, "exec", *resume, "--json", "--skip-git-repo-check", "--ignore-rules",
                "--output-schema", str(self.folder.schema), "-o", str(self.reply_path), "-"]  # fmt: skip

    def start(
        self, prompt: str, *, session_id: str | None, token: str, on_session=None
    ):
        """Launch Codex; `on_session(thread_id)` is called as soon as the
        `thread.started` event is read, before anything else is trusted. It runs
        on a reader thread: hand off to the store's own thread if needed."""
        if self.lock.fd is None:
            raise RunnerError("Take the game's job lock before starting a run.")
        self.version = codex_version(self.codex)
        self.diagnostics.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.reply_path.unlink(missing_ok=True)
        self._log = self.diagnostics.open("a")
        self._log.write(json.dumps({"tesserae": "start", "job_id": self.job_id, "resume": session_id,
                                    "codex_version": self.version, "argv": self.argv(session_id)}) + "\n")  # fmt: skip
        self.session_id = session_id
        self._on_session = on_session
        self.lock.write(
            {"state": "starting", "job_id": self.job_id, "host_run": self.host_run}
        )
        env = {**os.environ, "CODEX_HOME": str(self.folder.codex_home),
               "TESSERAE_BRIDGE_SOCKET": str(self.folder.socket_path(self.game_id)),
               "TESSERAE_BRIDGE_TOKEN": token}  # fmt: skip
        self.process = subprocess.Popen(
            self.argv(session_id), cwd=self.folder.workspace, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            pass_fds=(self.lock.fd,),
        )  # fmt: skip
        identity = settled_identity(self.process.pid) or {}
        self.lock.write({"state": "running", "job_id": self.job_id, "host_run": self.host_run,
                         "pid": self.process.pid, "pgid": self.process.pid, **identity})  # fmt: skip
        self._readers = [threading.Thread(target=self._read_events, daemon=True),
                         threading.Thread(target=self._read_stderr, daemon=True)]  # fmt: skip
        for reader in self._readers:
            reader.start()
        try:
            self.process.stdin.write(prompt.encode())
            self.process.stdin.close()
        except BrokenPipeError:
            pass  # it exited early; wait() reports why

    def _read_events(self):
        for raw in self.process.stdout:
            line = raw[:MAX_LINE].decode("utf-8", "replace").rstrip("\n")
            self._log.write(line + "\n")
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if (
                isinstance(event, dict)
                and event.get("type") == "thread.started"
                and self.session_id is None
            ):
                thread_id = event.get("thread_id")
                if isinstance(thread_id, str) and thread_id:
                    self.session_id = thread_id
                    if self._on_session is not None:
                        self._on_session(thread_id)

    def _read_stderr(self):
        total = 0
        for raw in self.process.stderr:
            if total < MAX_STDERR:
                text = raw[: MAX_STDERR - total].decode("utf-8", "replace")
                total += len(text)
                self._stderr.append(text)

    def stop(self, reason: str = "shutdown"):
        """End the run: SIGTERM, then SIGKILL, to this run's own process group."""
        self._stopped = reason
        if self.process is not None and self.process.poll() is None:
            _stop_group(self.process.pid, lambda: self.process.poll() is not None)

    def wait(self) -> RunResult:
        try:
            self.process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.stop("timeout")
            self.process.wait()
        for reader in self._readers:
            reader.join(timeout=5)
        stderr = "".join(self._stderr)
        self._log.write(json.dumps({"tesserae": "end", "exit_code": self.process.returncode,
                                    "stderr": stderr}) + "\n")  # fmt: skip
        self._log.close()
        self.lock.write({"state": "ended", "job_id": self.job_id})
        result = self._result(stderr)
        result.codex_version = self.version
        return result

    def _result(self, stderr: str) -> RunResult:
        code, session = self.process.returncode, self.session_id
        if self._stopped is not None:
            return RunResult("interrupted", self._stopped, session, exit_code=code)
        if SESSION_MISSING in stderr:
            return RunResult("failed", "session_missing", session, exit_code=code)
        if code != 0 or session is None:
            return RunResult("failed", "runner_failed", session, exit_code=code,
                             details=[stderr.strip()[-500:]])  # fmt: skip
        try:
            reply = json.loads(self.reply_path.read_text())
        except (OSError, ValueError):
            return RunResult("failed", "malformed_reply", session, exit_code=code)
        if not _reply_shape(reply):
            return RunResult("failed", "malformed_reply", session, exit_code=code)
        return RunResult("completed", None, session, reply, code)


def _reply_shape(reply) -> bool:
    """The reply schema's structure; the store checks audiences and names."""
    return (
        isinstance(reply, dict)
        and set(reply) == {"messages", "choices"}
        and isinstance(reply["messages"], list)
        and isinstance(reply["choices"], dict)
        and set(reply["choices"]) == {"ask", "resolve"}
    )
