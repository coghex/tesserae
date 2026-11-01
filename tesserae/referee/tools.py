"""The host side of the referee bridge (#67, REF-4): the seven whitelisted
tools, the job token that authorizes them, and the game's Unix socket.

Codex reaches these only through `python -m tesserae.referee.bridge`, which
forwards each MCP tool call here as one JSON line. Every read of the game and
every action goes through `BridgeTools.handle`; there is no other path. A
call is authorized by the token of the one running job, and acts only as
that job's sender (`act`) or in the `referee` context (`referee_act`). Every
mutation is journaled in `referee_steps` before it runs and executed by
`GameStore.execute_command`, which validates it at the current revision and
commits state, result and history atomically. See
docs/designs/referee_contract.md sections 6 and 9.
"""

import hmac
import json
import logging
import os
import secrets
import socketserver
import threading
from pathlib import Path

from ..protocol import PROTOCOL_VERSION
from ..store import GameStore, ParticipantContext
from .store import REFEREE, Job, RefereeError, RefereeStore, Step

MAX_REQUEST = 256 * 1024  # one request line from the bridge
TOOL_ARGUMENTS = {
    "get_context": frozenset(),
    "get_transcript": frozenset({"after_seq"}),
    "get_view": frozenset({"participant_id"}),
    "list_actions": frozenset(),
    "act": frozenset({"step_id", "command", "args"}),
    "referee_act": frozenset({"step_id", "command", "args"}),
    "record_ruling": frozenset({"text", "scope"}),
}
# A refusal that settles a step. A stale revision does not: nothing ran, and
# the same step ID may be retried at the new revision.
RETRYABLE = frozenset({"stale_revision"})

logger = logging.getLogger("tesserae.referee.tools")


class ToolError(Exception):
    """A tool call that did nothing, or was refused; returned, not raised, to
    the bridge as `{"ok": false, "error": {"code", "message"}}`."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class BridgeTools:
    """The tools for one game. `open_store()` returns a new GameStore for the
    calling thread; `on_commit(revision)` is called after each fresh commit, so
    the host can notify players; `log(entry)` receives every call and result
    (never a token). Safe to call from several threads.

    An action is authorized again inside its own write transaction, and
    revoke() waits for one already committing, so once revoke() returns no
    action of the revoked job can commit."""

    def __init__(self, open_store, *, on_commit=None, log=None):
        self._open = open_store
        self._on_commit = on_commit
        self._log = log
        self._grant = None  # (job_id, token) of the running job
        self._guard = threading.Lock()
        # Held from an action's command transaction until its step is
        # recorded; revoke() takes it first. Order: _acting, then _guard.
        self._acting = threading.Lock()

    def grant(self, job_id: str) -> str:
        """A new token for this job; any earlier job's token stops working."""
        token = secrets.token_urlsafe(32)
        with self._guard:
            self._grant = (job_id, token)
        return token

    def revoke(self):
        """End all bridge access, as when the job ends or the host stops.
        Waits for an action already committing to finish recording."""
        with self._acting, self._guard:
            self._grant = None

    def handle(self, request) -> dict:
        """Answer one request, `{"token", "tool", "args"}`, with `{"ok": true,
        "result"}` or `{"ok": false, "error"}`."""
        tool = request.get("tool") if isinstance(request, dict) else None
        args = request.get("args") if isinstance(request, dict) else None
        try:
            grant = self._authorize(request)
            with self._open() as store:
                result = self._run(store, grant, tool, args)
            response = {"ok": True, "result": result}
        except ToolError as error:
            response = _error(error.code, error.message)
        except RefereeError as error:
            response = _error(error.code, str(error))
        except Exception:
            logger.exception("A referee tool call failed")
            response = _error(
                "internal_error",
                "The host failed; nothing was acknowledged. Retry the same call.",
            )
        if self._log is not None:
            self._log({"tool": tool, "args": args, "response": response})
        return response

    def _authorize(self, request) -> tuple:
        if not isinstance(request, dict) or set(request) != {"token", "tool", "args"}:
            raise ToolError(
                "invalid_request", "A request has exactly token, tool and args."
            )
        tool, args, token = request["tool"], request["args"], request["token"]
        with self._guard:
            grant = self._grant
        if (
            grant is None
            or not isinstance(token, str)
            or not hmac.compare_digest(token.encode(), grant[1].encode())
        ):
            raise ToolError("job_ended", "This job's access has ended; nothing ran.")
        if tool not in TOOL_ARGUMENTS:
            raise ToolError(
                "unsupported_tool", "That is not one of the referee's tools."
            )
        if not isinstance(args, dict) or set(args) != TOOL_ARGUMENTS[tool]:
            expected = ", ".join(sorted(TOOL_ARGUMENTS[tool])) or "no arguments"
            raise ToolError("invalid_request", f"{tool} takes exactly: {expected}.")
        return grant

    def _run(self, store: GameStore, grant: tuple, tool: str, args: dict) -> dict:
        referee = RefereeStore(store)
        job = referee.running_job()
        if job is None or job.job_id != grant[0]:
            raise ToolError("job_ended", "This job's access has ended; nothing ran.")
        if tool in ("act", "referee_act"):
            return self._act(store, referee, grant, job, tool, args)
        if tool == "record_ruling":
            referee.record_ruling(job.job_id, args["text"], args["scope"])
            return {"recorded": True}
        if tool == "get_transcript":
            after = args["after_seq"]
            if not isinstance(after, int) or isinstance(after, bool) or after < 0:
                raise ToolError(
                    "invalid_request", "after_seq is a whole number, 0 or more."
                )
            page, next_after = referee.transcript(after)
            return {"messages": page, "next_after_seq": next_after}
        with store.snapshot():  # one committed state for every read below
            return getattr(self, f"_{tool}")(store, referee, job, args)

    def _get_context(self, store, referee, job, args) -> dict:
        state, runtime = store.inspect_state(), store.runtime()
        names = _names(store, job)
        context = runtime.referee_context(state)
        if context is None:  # the minimal default: each participant's own view
            context = {"views": {pid: runtime.view(state, pid) for pid in names}}
        page, next_after = referee.transcript(0)
        return {
            "revision": store.revision(),
            "module": {"id": runtime.module.id, "version": runtime.module.version},
            "context": context,
            "participants": [
                {"participant_id": p, "name": n} for p, n in names.items()
            ],
            "input": {
                "job_id": job.job_id,
                "from": {
                    "participant_id": job.participant_id,
                    "name": names[job.participant_id],
                },
                "text": job.text,
                "private": job.private,
            },
            "pending_choices": referee.all_choices(),
            "rulings": referee.rulings(),
            "earlier_results": [_step(step) for step in referee.unnarrated_steps()],
            "transcript": {"messages": page, "next_after_seq": next_after},
        }

    def _get_view(self, store, referee, job, args) -> dict:
        participant_id = args["participant_id"]
        if participant_id not in _names(store, job):
            raise ToolError(
                "invalid_request", "There is no such participant in this game."
            )
        view = store.runtime().view(store.inspect_state(), participant_id)
        return {"participant_id": participant_id, "view": view}

    def _list_actions(self, store, referee, job, args) -> dict:
        runtime = store.runtime()
        actions = runtime.referee_actions(store.inspect_state(), job.participant_id)
        if actions is None:  # the minimal default: the command names
            actions = [{"command": name} for name in runtime.command_names]
        return {
            "for": job.participant_id,
            "actions": actions,
            "referee_commands": [c.name for c in runtime.module.referee_commands],
        }

    def _act(self, store, referee, grant, job: Job, tool: str, args: dict) -> dict:
        if not isinstance(args["command"], str) or not isinstance(args["args"], dict):
            raise ToolError(
                "invalid_request", "command is a string and args an object."
            )
        who = job.participant_id if tool == "act" else REFEREE
        step = referee.intend_step(
            job.job_id, args["step_id"], tool, who, args["command"], args["args"]
        )
        if step.status == "committed":  # an exact retry: the recorded result
            return {
                "step_id": step.step_id,
                "status": "committed",
                "result": step.result,
            }
        if step.status == "refused":
            raise ToolError(step.result["code"], step.result["message"])
        if step.status == "not_committed":
            raise ToolError(
                "request_conflict", "That step did not run; use a new step_id."
            )
        raw = json.dumps({
            "version": PROTOCOL_VERSION, "type": "command", "request_id": step.request_id,
            "expected_revision": store.revision(), "command": step.command, "args": step.args,
        })  # fmt: skip

        def still_authorized():
            # Inside the command's transaction: the job may have been
            # interrupted, or its access revoked, since the step was intended.
            with self._guard:
                current = self._grant
            running = referee.running_job()
            if current is not grant or running is None or running.job_id != job.job_id:
                raise ToolError(
                    "job_ended", "This job's access has ended; nothing ran."
                )

        with self._acting:
            reply, committed = store.execute_command(
                ParticipantContext(who), raw, referee=tool == "referee_act",
                precondition=still_authorized,
            )  # fmt: skip
            if reply["type"] == "error":
                if reply["code"] in RETRYABLE:  # nothing ran; the step stays intended
                    raise ToolError(
                        reply["code"],
                        reply["message"] + " Retry with the same step_id.",
                    )
                referee.record_step(job.job_id, step.step_id, "refused", reply)
                raise ToolError(reply["code"], reply["message"])
            if committed and self._on_commit is not None:
                # First: players hear of the commit even if journaling it fails.
                self._on_commit(reply["revision"])
            referee.record_step(job.job_id, step.step_id, "committed", reply)
        return {"step_id": step.step_id, "status": "committed", "result": reply}


def _names(store: GameStore, job: Job) -> dict:
    """Participant ID to name; a development game's sender has no record."""
    names = {p.participant_id: p.name for p in store.participants()}
    names.setdefault(job.participant_id, job.participant_id)
    return names


def _step(step: Step) -> dict:
    return {"job_id": step.job_id, "step_id": step.step_id, "kind": step.kind,
            "by": step.context, "command": step.command, "args": step.args,
            "result": step.result}  # fmt: skip


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


class BridgeServer:
    """Serves one game's BridgeTools on its Unix socket (mode 0600), one JSON
    line per request and response, on background threads. Use it as a
    context manager, or call start() and close()."""

    def __init__(self, path: Path, tools: BridgeTools):
        self.path = Path(path)
        self.tools = tools
        self._server = None

    def start(self) -> "BridgeServer":
        if self.path.is_socket():
            self.path.unlink()  # a crashed host's; the run folder is ours alone
        previous = os.umask(0o177)
        try:
            self._server = _Server(str(self.path), _Handler)
        finally:
            os.umask(previous)
        self._server.tools = self.tools
        os.chmod(self.path, 0o600)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def close(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self.path.unlink(missing_ok=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.close()


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        while True:
            line = self.rfile.readline(MAX_REQUEST + 1)
            if not line:
                return
            if len(line) > MAX_REQUEST:
                self._send(_error("invalid_request", "The request is too long."))
                return
            try:
                request = json.loads(line)
            except ValueError:
                response = _error(
                    "invalid_request", "A request is one JSON object per line."
                )
            else:
                response = self.server.tools.handle(request)
            self._send(response)

    def _send(self, response: dict):
        self.wfile.write(json.dumps(response).encode() + b"\n")
        self.wfile.flush()
