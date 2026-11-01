"""The referee loop (#69, REF-6): one accepted input becomes one Codex job.

This wires the pieces together for the host (tesserae.server): the store's
jobs (store.py), the supervised Codex run (runner.py) and the bridge
(tools.py). For each job it takes the game's job lock, settles earlier steps,
grants the bridge a token, writes the short prompt, starts or resumes the
game's session, and turns how the run ended into a reply for the host to
publish, or RefereeJobFailed. It also finishes a job an earlier host run left
`running`, and answers the operator's `tesserae referee stop`.

It is a bounded loop, not a planner: Codex decides what to say and which
commands to run, through the bridge only. See
docs/designs/referee_contract.md sections 4, 6 and 10.
"""

import asyncio
import fcntl
import json
import os
import threading
from dataclasses import dataclass

from ..store import GameStore
from .runner import (
    DEFAULT_TIMEOUT,
    STOP_GRACE,
    CodexRun,
    JobLock,
    RefereeDir,
    RunnerError,
    RunResult,
)
from .store import Job, RefereeStore
from .tools import BridgeServer, BridgeTools

LOCK_RETRY = 5.0  # seconds between tries of a lock an earlier run still holds


class RefereeJobFailed(Exception):
    """A job ends `state` (`failed` or `interrupted`) with `error`, and
    publishes nothing."""

    def __init__(self, state: str, error: str):
        super().__init__(error)
        self.state = state
        self.error = error


def job_prompt(job: Job, sender: str, revision: int, *, new_session: bool,
               previous: Job | None) -> str:  # fmt: skip
    """The prompt for one job, on Codex's stdin (contract section 4). The
    player's words are quoted as a JSON string, so they stay one value."""
    where = "privately to you" if job.private else "to the table"
    lines = [
        f"New input (job {job.job_id}) from {sender} ({job.participant_id}), {where}:",
        json.dumps(job.text, ensure_ascii=False),
        f"Call get_context before anything else. Game revision now: {revision}.",
    ]
    if new_session:
        lines.append(
            "This is the start of the game's referee conversation. Read every page "
            "of the transcript with get_transcript before replying."
        )
    if previous is not None and previous.state in ("interrupted", "failed"):
        lines.append(
            f"The previous job ended without a reply ({previous.state}); get_context "
            "lists what it committed. Do not repeat those steps."
        )
    return "\n".join(lines) + "\n"


@dataclass
class _Attempt:
    """One job's run, so a stop reaches the right one whenever it comes."""

    reason: str | None = None
    run: CodexRun | None = None
    wake: threading.Event = None

    def __post_init__(self):
        self.wake = threading.Event()


class CodexReferee:
    """The host's referee for one game: call it with each accepted Job.

    The host calls `open(host_run, open_store, on_commit)` before serving,
    `recover()` (blocking) until it stops returning "waiting", and `close()`
    after its jobs have ended. `open_store()` returns a new GameStore;
    `on_commit(revision)` may be called from any thread.
    """

    def __init__(self, folder: RefereeDir, game_id: str, *, codex=("codex",),
                 timeout: float = DEFAULT_TIMEOUT, grace: float = STOP_GRACE,
                 lock_retry: float = LOCK_RETRY):  # fmt: skip
        self.folder, self.game_id = folder, game_id
        self.codex, self.timeout, self.grace = tuple(codex), timeout, grace
        self.lock_retry = lock_retry
        self.host_run = None
        self._open_store = None
        self._on_commit = None
        self._guard = threading.Lock()
        self._attempt = None
        self._host_lock = None  # this host's claim on the game's endpoints
        self.tools = BridgeTools(self._open, on_commit=self._committed, log=self._note)
        self._bridge = BridgeServer(folder.socket_path(game_id), self.tools)
        self._control = BridgeServer(folder.control_path(game_id), _Control(self))

    # --- The host's side ---------------------------------------------------------

    def open(self, host_run: str, open_store, on_commit):
        """Claim the game for this host, then open its sockets. A second host
        for the same game fails here (RunnerError), before it touches the
        first host's sockets."""
        self.host_run, self._open_store = host_run, open_store
        self._on_commit = on_commit
        path = self.folder.host_lock_path(self.game_id)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RunnerError(
                "another host is already serving this game's referee; "
                "nothing was changed"
            ) from None
        self._host_lock = fd  # never inherited by Codex (only the job lock is)
        self._bridge.start()
        self._control.start()

    def close(self):
        self.tools.revoke()
        self._control.close()
        self._bridge.close()
        if self._host_lock is not None:  # last: the sockets are gone
            os.close(self._host_lock)
            self._host_lock = None

    def recover(self) -> str:
        """Finish a job an earlier host run left `running`, if nothing of it is
        still alive (contract section 4): "finished", "none" (nothing to do),
        or "waiting" (its Codex may still be running: the game stays busy, and
        the host asks again later)."""
        with self._open() as store:
            referee = RefereeStore(store)
            job = referee.running_job()
            if job is not None and job.host_run == self.host_run:
                return "none"  # this host's own
            lock = JobLock(self.folder.lock_path(self.game_id))
            if not lock.try_acquire():
                return "waiting" if job is not None else "none"
            try:
                referee.reconcile_steps()
                if job is None:
                    return "none"
                referee.finish(job.job_id, "interrupted", "host_stopped")
                return "finished"
            finally:
                lock.release()

    async def __call__(self, job: Job) -> dict:
        """Run one job; the reply to publish, or RefereeJobFailed. If the host
        cancels it (shutdown), the run is stopped and waited for, so the lock
        is free before the job is marked interrupted."""
        attempt = _Attempt()
        with self._guard:
            self._attempt = attempt
        loop = asyncio.get_running_loop()
        work = loop.run_in_executor(None, self._run_job, job, attempt)
        try:
            result = await asyncio.shield(work)
        except asyncio.CancelledError:
            self.stop("shutdown")
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
            raise
        finally:
            with self._guard:
                if self._attempt is attempt:
                    self._attempt = None
        if result.outcome == "completed":
            return result.reply
        raise RefereeJobFailed(result.outcome, result.error)

    def stop(self, reason: str) -> bool:
        """Stop the running job's Codex, if any: its own process group, which
        this host started. Returns at once; the job then ends `interrupted`."""
        with self._guard:
            attempt = self._attempt
            if attempt is None:
                return False
            attempt.reason = attempt.reason or reason
            attempt.wake.set()
            run = attempt.run
        if run is not None and run.process is not None:
            threading.Thread(target=run.stop, args=(reason,), daemon=True).start()
        return True

    # --- One job ------------------------------------------------------------------

    def _run_job(self, job: Job, attempt: _Attempt) -> RunResult:
        lock = JobLock(self.folder.lock_path(self.game_id))
        # An earlier run's process group may survive it (a `stuck` record):
        # this job stays `running`, so the game stays busy, until it is gone.
        while not lock.try_acquire():
            if attempt.wake.wait(self.lock_retry):
                return RunResult("interrupted", attempt.reason)
        try:
            with self._open() as store:
                referee = RefereeStore(store)
                referee.reconcile_steps()
                session_id, _ = referee.session()
                names = {p.participant_id: p.name for p in store.participants()}
                prompt = job_prompt(
                    job, names.get(job.participant_id, job.participant_id),
                    store.revision(), new_session=session_id is None,
                    previous=referee.previous_job(job.job_id),
                )  # fmt: skip
            run = CodexRun(self.folder, game_id=self.game_id, job_id=job.job_id,
                           host_run=self.host_run, lock=lock, codex=self.codex,
                           timeout=self.timeout, grace=self.grace)  # fmt: skip
            with self._guard:
                if attempt.reason is not None:
                    return RunResult("interrupted", attempt.reason)
                attempt.run = run
            token = self.tools.grant(job.job_id)
            run.start(prompt, session_id=session_id, token=token,
                      on_session=self._save_session)  # fmt: skip
            with self._guard:
                reason = attempt.reason  # a stop that came while it started
            if reason is not None:
                run.stop(reason)
            result = run.wait()
        finally:
            self.tools.revoke()  # waits for an action already committing
            lock.release()
        if result.error == "session_missing":
            with self._open() as store:
                RefereeStore(store).mark_needs_rebuild()
        return result

    def _open(self) -> GameStore:
        return self._open_store()

    def _save_session(self, thread_id: str):
        with self._open() as store:
            RefereeStore(store).set_session(thread_id)

    def _committed(self, revision: int):
        if self._on_commit is not None:
            self._on_commit(revision)

    def _note(self, entry: dict):
        with self._guard:
            run = None if self._attempt is None else self._attempt.run
        if run is not None:
            run.note(entry)


class _Control:
    """The running host's answer to `tesserae referee stop` (one JSON line)."""

    def __init__(self, referee: CodexReferee):
        self.referee = referee

    def handle(self, request) -> dict:
        if request != {"stop": True}:
            return {"ok": False, "error": {"code": "invalid_request",
                                           "message": 'Send {"stop": true}.'}}  # fmt: skip
        return {"ok": True, "result": {"stopping": self.referee.stop("operator")}}
