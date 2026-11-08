"""The host side of the referee bridge (#67, REF-4): the whitelisted tools,
the job token that authorizes them, and the game's Unix socket.

Codex reaches these only through `python -m tesserae.referee.bridge`, which
forwards each MCP tool call here as one JSON line. Every read of the game and
every action goes through `BridgeTools.handle`; there is no other path. A
call is authorized by the token of the one running job, and acts only as
that job's sender (`act`) or in the `referee` context (`referee_act`). Every
mutation is journaled in `referee_steps` before it runs and executed by
`GameStore.execute_command`, which validates it at the current revision and
commits state, result and history atomically. See
docs/designs/referee_contract.md sections 6 and 9.

In a character game each job acts as its frozen actor and only as its mode
allows (docs/designs/character_contract.md sections 5 and 7): nothing in
setup, its own draft's creation steps in creation, and its own ready
character's commands, and referee-only commands, in play.

Any job may propose an exact host decision (`propose_host_decision`). Only a
job of the campaign host has the host tools, and each call takes the
registry guard for that host's session again: a transfer or a revocation
ends them mid-job (docs/designs/governance_contract.md section 10).
"""

import hmac
import json
import logging
import os
import secrets
import socketserver
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .. import overrides as negotiated
from ..campaign_rules import CampaignRules, requirements, rules_message
from ..host_requests import RECENT, HostRequests, proposal_message
from ..modules import result_bytes
from ..protocol import PROTOCOL_VERSION
from ..store import GameStore, ParticipantContext, Refusal
from .store import (
    REFEREE,
    RESULT_BYTES,
    TRANSCRIPT_PAGE_BYTES,
    Job,
    RefereeError,
    RefereeStore,
    Step,
)

MAX_REQUEST = 256 * 1024  # one request line from the bridge
TOOL_ARGUMENTS = {
    "get_context": frozenset(),
    "get_transcript": frozenset({"after_seq"}),
    "get_view": frozenset({"participant_id"}),
    "list_actions": frozenset(),
    "act": frozenset({"step_id", "command", "args"}),
    "referee_act": frozenset({"step_id", "command", "args"}),
    "record_ruling": frozenset({"text", "scope"}),
    # Governance (docs/designs/governance_contract.md section 10).
    "propose_host_decision": frozenset(
        {"step_id", "kind", "scope", "target", "payload", "explanation"}
    ),
    "host_operations": frozenset(),
    "host_act": frozenset({"step_id", "command", "args"}),
    "host_decide": frozenset({"step_id", "proposal_id", "decision", "note"}),
    "host_propose_rules": frozenset({"step_id", "content"}),
    "host_activate_rules": frozenset(
        {"step_id", "revision", "expected_policy_revision"}
    ),
    # Negotiated creation overrides (creation overrides contract section 11).
    "override_discover": frozenset({"character_id", "query"}),
    "override_preview": frozenset({"character_id", "intent"}),
    "override_requests": frozenset({"character_id", "states", "after"}),
    "override_detail": frozenset({"proposal_id"}),
    "override_offers": frozenset({"proposal_id", "after"}),
    "override_offer": frozenset({"offer_id"}),
    "override_history": frozenset({"character_id", "after"}),
    "override_record": frozenset({"record_id"}),
    "override_respond": frozenset(
        {"step_id", "offer_id", "response", "preview_digest", "note"}
    ),
    "host_override_decide": frozenset(
        {"step_id", "proposal_id", "decision", "preview_digest", "intent", "note"}
    ),
    "host_override_withdraw": frozenset({"step_id", "offer_id", "note"}),
    "host_override_apply": frozenset(
        {"step_id", "character_id", "intent", "preview_digest", "reason"}
    ),
}
HOST_TOOLS = frozenset(
    {"host_operations", "host_act", "host_decide", "host_propose_rules",
     "host_activate_rules", "host_override_decide", "host_override_withdraw",
     "host_override_apply"}
)  # fmt: skip
OVERRIDE_READS = frozenset(
    {"override_discover", "override_preview", "override_requests", "override_detail",
     "override_offers", "override_offer", "override_history", "override_record"}
)  # fmt: skip
# The context's `overrides` block (creation overrides contract section 11).
MINE, RECENT_OVERRIDES, PENDING_HOST = 3, 2, 3
OVERRIDES_BLOCK_BYTES = 10240
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


class NotCampaignHost(Exception):
    """Raised by a host authority: this job's sender is not (or no longer)
    the campaign host."""


class BridgeTools:
    """The tools for one game. `open_store()` returns a new GameStore for the
    calling thread; `on_commit(revision)` is called after each fresh commit, so
    the host can notify players; `log(entry)` receives every call and result
    (never a token). Safe to call from several threads.

    `campaign_host(job)`, given by a lobby host, returns the campaign's
    host player now, read under the registry guard of the job's session (None
    with none), so an offer by a former host is found stale.

    `host_authority(job)`, given by a lobby host, returns a context manager
    that takes the registry guard for the job's sender's session and yields
    its `Decider` while it is the campaign host, else raises
    NotCampaignHost; without it no job has host tools. `on_proposal(id)` is
    called after a proposal is made or decided, so the host can tell the
    people involved.

    An action is authorized again inside its own write transaction, and
    revoke() waits for one already committing, so once revoke() returns no
    action of the revoked job can commit."""

    def __init__(self, open_store, *, on_commit=None, log=None,
                 host_authority=None, on_proposal=None,
                 campaign_host=None):  # fmt: skip
        self._open = open_store
        self._on_commit = on_commit
        self._log = log
        self.host_authority = host_authority
        self.campaign_host = campaign_host
        self.on_proposal = on_proposal
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
        except Refusal as refusal:
            response = _error(refusal.code, refusal.text)
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
        if tool == "propose_host_decision":
            if args["kind"] == negotiated.KIND:
                return self._propose_override(store, referee, grant, job, args)
            return self._propose(store, referee, grant, job, args)
        if tool == "override_respond":
            return self._respond(store, referee, grant, job, args)
        if tool in OVERRIDE_READS:
            with self._host(job, optional=True) as by:
                with store.snapshot():
                    if by is not None and not _participant_holds(store, by):
                        by = None
                    viewer = self._viewer(job, by)
                    return _override_read(store, job, viewer, tool, args)
        if tool in HOST_TOOLS:
            with self._host(job) as by:
                if tool == "host_operations":
                    with store.snapshot():
                        _check_participant(store, by)
                        return {"revision": store.revision(),
                                "operations": store.runtime().host_operations(
                                    store.inspect_state())}  # fmt: skip
                return self._host_step(store, referee, grant, job, by, tool, args)
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
        if tool == "get_context":  # whether this is a host job, held while read
            with self._host(job, optional=True) as by:
                with store.snapshot():
                    if by is not None and not _participant_holds(store, by):
                        by = None  # no longer in this game: a guest's view
                    return self._get_context(store, referee, job, args, by)
        with store.snapshot():  # one committed state for every read below
            return getattr(self, f"_{tool}")(store, referee, job, args)

    def _viewer(self, job: Job, by) -> "negotiated.Viewer":
        """Who a job reads and acts as (section 11): the host while its
        sender is the campaign host at this call, else its own sender."""
        campaign_host = None
        if by is not None:
            campaign_host = by.player_id
        elif self.campaign_host is not None:
            campaign_host = self.campaign_host(job)
        return negotiated.Viewer(job.participant_id, by is not None, campaign_host)

    def _propose_override(self, store, referee, grant, job: Job, args: dict) -> dict:
        """`propose_host_decision` of kind `creation_override`: checked before
        it is journaled (refused, never stored, if unsupported, invalid or
        blocked), then stored once per step. It never waits."""
        if args["scope"] != "target":
            raise ToolError("invalid_request", "A creation override's scope is target.")
        target, payload = args["target"], args["payload"]
        character_id = job.character_id
        if target is not None:
            if (
                not isinstance(target, dict)
                or set(target) != {"kind", "id"}
                or (target["kind"] != "character")
            ):
                raise ToolError(
                    "invalid_request", 'A target is {"kind": "character", "id": ID}.'
                )
            character_id = target["id"]
        if character_id is None:
            raise ToolError("invalid_request", "This job has no draft to default to.")
        if not isinstance(payload, dict) or not (
            {"intent"} <= set(payload) <= {"intent", "revises"}
        ):
            raise ToolError(
                "invalid_request", 'The payload is {"intent", "revises" (optional)}.'
            )
        intent, revises = payload["intent"], payload.get("revises")
        with self._host(job, optional=True) as by:
            viewer = self._viewer(job, by)
        requests = negotiated.Overrides(store)
        with store.snapshot():
            requests.check_request(viewer, character_id, intent, args["explanation"],
                                   revises)  # fmt: skip
        content = {k: v for k, v in args.items() if k != "step_id"}
        step = referee.intend_step(job.job_id, args["step_id"], "propose",
                                   job.participant_id, "propose_host_decision", content)  # fmt: skip
        recorded = _recorded(step)
        if recorded is not None:
            return {
                "step_id": step.step_id,
                "status": "committed",
                "proposal": recorded,
            }
        with self._acting:
            view, _ = requests.request_from_step(
                viewer, step.request_id, job.job_id, character_id, intent,
                args["explanation"], revises,
                check=self._still_running(referee, grant, job),
            )  # fmt: skip
            referee.record_step(job.job_id, step.step_id, "committed", view)
        return {"step_id": step.step_id, "status": "committed", "proposal": view}

    def _respond(self, store, referee, grant, job: Job, args: dict) -> dict:
        """`override_respond`: the owner's accept or decline of an exact
        offer, a step of kind `respond` in the sender's namespace, from any
        of its jobs. It never selects the draft."""
        viewer = self._viewer(job, None)  # a requester's own act, never a host's
        content = {k: v for k, v in args.items() if k != "step_id"}
        step = referee.intend_step(job.job_id, args["step_id"], "respond",
                                   job.participant_id, "override_respond", content)  # fmt: skip
        recorded = _recorded(step)
        if recorded is not None:
            return {"step_id": step.step_id, "status": "committed", "result": recorded}
        with self._acting:
            try:
                result, applied = negotiated.Overrides(store).respond(
                    viewer, step.request_id, args["offer_id"], args["response"],
                    args["preview_digest"], args["note"],
                    check=self._still_running(referee, grant, job), job_id=job.job_id,
                )  # fmt: skip
            except Refusal as refusal:
                reply = refusal.message(step.request_id, None)
                referee.record_step(job.job_id, step.step_id, "refused", reply)
                raise ToolError(refusal.code, refusal.text) from None
            reply = proposal_message(result, step.request_id)
            if applied and "revision" in result and self._on_commit is not None:
                self._on_commit(result["revision"])
            referee.record_step(job.job_id, step.step_id, "committed", reply)
        return {"step_id": step.step_id, "status": "committed", "result": reply}

    @contextmanager
    def _host(self, job: Job, *, optional=False):
        """The job's campaign host authority for one call (a Decider via the
        referee), held for the call; with `optional`, None for a guest's job
        instead of `not_campaign_host`."""
        with ExitStack() as stack:
            by = None
            if self.host_authority is not None:
                try:
                    by = stack.enter_context(self.host_authority(job))
                except NotCampaignHost:
                    pass
            if by is None and not optional:
                raise _not_host()
            yield by

    def _still_running(self, referee, grant, job, store=None, by=None):
        """A precondition for a step's own transaction: the job may have been
        interrupted, or its access revoked, since the step was intended; and
        for a host step, the host's own session in this game must still hold
        (its registry authority is held for the whole call)."""

        def check():
            with self._guard:
                current = self._grant
            running = referee.running_job()
            if current is not grant or running is None or running.job_id != job.job_id:
                raise ToolError(
                    "job_ended", "This job's access has ended; nothing ran."
                )
            if by is not None:
                _check_participant(store, by)

        return check

    def _propose(self, store, referee, grant, job: Job, args: dict) -> dict:
        """`propose_host_decision`: checked before it is journaled (an
        unsupported request is refused, not stored), then stored once per
        step in the sender's namespace. It never waits: the job replies and
        ends, and the proposal stays pending."""
        requests = HostRequests(store)
        with store.snapshot():
            proposal = requests.check(job, args["kind"], args["scope"], args["target"],
                                      args["payload"], args["explanation"])  # fmt: skip
        content = {k: v for k, v in args.items() if k != "step_id"}
        step = referee.intend_step(job.job_id, args["step_id"], "propose",
                                   job.participant_id, "propose_host_decision", content)  # fmt: skip
        recorded = _recorded(step)
        if recorded is not None:
            return {
                "step_id": step.step_id,
                "status": "committed",
                "proposal": recorded,
            }
        with self._acting:
            view, created = requests.propose(
                job, step.request_id, proposal,
                check=self._still_running(referee, grant, job),
            )  # fmt: skip
            referee.record_step(job.job_id, step.step_id, "committed", view)
        if created and self.on_proposal is not None:
            self.on_proposal(view["proposal_id"])
        return {"step_id": step.step_id, "status": "committed", "proposal": view}

    def _host_step(self, store, referee, grant, job, by, tool, args) -> dict:
        """One mutating host tool, journaled as a step of kind `host` in the
        campaign host's namespace and run inside the registry guard taken for
        this call. The busy rule does not apply to the host's own job."""
        content = {k: v for k, v in args.items() if k != "step_id"}
        step = referee.intend_step(job.job_id, args["step_id"], "host", by.sender,
                                   tool, content)  # fmt: skip
        recorded = _recorded(step)
        if recorded is not None:
            return {"step_id": step.step_id, "status": "committed", "result": recorded}
        check = self._still_running(referee, grant, job, store, by)
        with self._acting:
            try:
                reply, committed = self._run_host_tool(store, by, step, tool, content,
                                                       check)  # fmt: skip
            except Refusal as refusal:
                reply, committed = refusal.message(step.request_id, None), False
            if reply["type"] == "error":
                if reply["code"] in RETRYABLE:  # nothing ran; the step stays intended
                    raise ToolError(reply["code"],
                                    reply["message"] + " Retry with the same step_id.")  # fmt: skip
                referee.record_step(job.job_id, step.step_id, "refused", reply)
                raise ToolError(reply["code"], reply["message"])
            referee.record_step(job.job_id, step.step_id, "committed", reply)
        if committed:
            effect = reply.get("effect") or {}
            if reply["type"] in ("result", "override_applied") and (
                self._on_commit is not None
            ):
                self._on_commit(reply["revision"])
            elif reply["type"] == "proposal":
                if "revision" in effect and self._on_commit is not None:
                    self._on_commit(effect["revision"])
                if self.on_proposal is not None:
                    self.on_proposal(reply["proposal_id"])
        return {"step_id": step.step_id, "status": "committed", "result": reply}

    def _run_host_tool(self, store, by, step, tool, content, check):
        if tool == "host_act":
            if not isinstance(content["command"], str) or not isinstance(
                content["args"], dict
            ):
                raise ToolError(
                    "invalid_request", "command is a string and args an object."
                )
            raw = json.dumps({
                "version": PROTOCOL_VERSION, "type": "command",
                "request_id": step.request_id, "expected_revision": store.revision(),
                "command": content["command"], "args": content["args"],
            })  # fmt: skip
            return store.execute_host_command(by, raw, precondition=check)
        if tool in ("host_override_decide", "host_override_withdraw",
                    "host_override_apply"):  # fmt: skip
            return _host_override(store, by, step, tool, content, check)
        if tool == "host_decide":
            result, applied = HostRequests(store).decide(
                by, step.request_id, content["proposal_id"], content["decision"],
                content["note"], check=check,
            )  # fmt: skip
            return proposal_message(result, step.request_id), applied
        rules = CampaignRules(store)
        if tool == "host_propose_rules":
            result, applied = rules.import_document(
                content["content"], by, request_id=step.request_id, origin="referee",
                check=check,
            )  # fmt: skip
        else:
            result, applied = rules.activate(
                content["revision"], content["expected_policy_revision"], by,
                request_id=step.request_id, check=check,
            )  # fmt: skip
        return rules_message(result, step.request_id), applied

    def _get_context(self, store, referee, job, args, by=None) -> dict:
        state, runtime = store.inspect_state(), store.runtime()
        names = _names(store, job)
        context = runtime.referee_context(state)
        if context is None:  # the minimal default: each participant's own view
            context = {"views": {pid: runtime.view(state, pid) for pid in names}}
        result = {
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
            "pending_choices": referee.all_choices(job),
            "rulings": referee.rulings(),
            "earlier_results": [
                _step(s, store)
                for s in referee.unnarrated_steps(
                    job, None if by is None else by.sender
                )
            ],
            "transcript": {"messages": [], "next_after_seq": None},
        }
        if job.mode is not None:
            result.update(_character_context(store, runtime, state, job))
            result["input"]["actor"] = _actor(store, job)
        result.update(_policy_context(store, runtime, state, job))
        requests = HostRequests(store)
        mine = requests.listing(job.participant_id, recent=RECENT)
        if mine:  # the sender's own requests (section 9)
            result["requests"] = mine
        if by is not None:  # a job of the campaign host (section 10)
            result["host"] = True
            result["pending_requests"] = [
                r for r in requests.listing() if r["state"] == "pending"
            ]
        if runtime.creation_overrides is not None:
            result["overrides"] = _overrides_block(store, job, self._viewer(job, by))
        return _with_first_page(result, referee)

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
        if job.mode is not None:
            return _character_actions(store, runtime, job)
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
        if job.mode is not None:  # before anything is journaled
            _check_mode(store.runtime(), job, tool, args["command"])
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
                actor=job.actor if tool == "act" else None,
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


def _override_read(store, job: Job, viewer, tool: str, args: dict) -> dict:
    """One of section 11's read tools, under section 10's privacy rule for
    the job's sender (`viewer`). `character_id` defaults to the job's own."""
    reads = negotiated.Overrides(store)
    if "character_id" in args:
        character_id = args["character_id"] or job.character_id
        if character_id is None and (tool != "override_requests"):
            raise ToolError("invalid_request", "Name the character: this job has none.")
    if tool == "override_discover":
        return reads.discover(viewer, character_id, args["query"] or {})
    if tool == "override_preview":
        return reads.preview(viewer, character_id, args["intent"])
    if tool == "override_requests":
        page = reads.requests(viewer, args["character_id"], args["states"] or "all",
                              args["after"])  # fmt: skip
        return {"items": page["items"], "next": page["next"]}
    if tool == "override_detail":
        return reads.detail(viewer, args["proposal_id"])
    if tool == "override_offers":
        page = reads.offers(viewer, args["proposal_id"], args["after"])
        return {k: page[k] for k in ("proposal_id", "items", "next")}
    if tool == "override_offer":
        return reads.offer(viewer, args["offer_id"])
    if tool == "override_history":
        page = reads.history(viewer, character_id, args["after"])
        return {k: page[k] for k in ("character_id", "items", "next")}
    return reads.record(viewer, args["record_id"])


def _host_override(store, by, step, tool, content, check):
    """A host override tool's step, run with the store's own replay in the
    campaign host's namespace: (reply message, applied)."""
    reads = negotiated.Overrides(store)
    viewer = negotiated.Viewer(
        by.participant.participant_id if by.participant is not None else by.sender,
        True, by.player_id,
    )  # fmt: skip
    if tool == "host_override_decide":
        result, applied = reads.decide(
            by, viewer, step.request_id, content["proposal_id"], content["decision"],
            content["preview_digest"], content["intent"], content["note"], check=check,
        )  # fmt: skip
        return proposal_message(result, step.request_id), applied
    if tool == "host_override_withdraw":
        result, applied = reads.withdraw(by, viewer, step.request_id,
                                         content["offer_id"], content["note"],
                                         check=check)  # fmt: skip
        return proposal_message(result, step.request_id), applied
    result, applied = reads.apply_direct(
        by, step.request_id, content["character_id"], content["intent"],
        content["preview_digest"], content["reason"], check=check,
    )  # fmt: skip
    return negotiated.message("override_applied", result, step.request_id), applied


def _overrides_block(store: GameStore, job: Job, viewer) -> dict:
    """The context's `overrides` block (creation overrides contract section
    11): the categories' policy; the sender's open requests (newest first)
    and terminal ones (most recently decided first); in a host job, the
    requests waiting for the host (oldest first). Each list is short, and
    its `…_more` counts the rest: `override_requests` pages them all."""
    reads = negotiated.Overrides(store)
    categories = [{"category_id": c["category_id"], "policy": c["policy"]}
                  for c in reads.categories()]  # fmt: skip

    def listed(where, parameters, order, limit):
        rows = store.query(
            "SELECT proposal_id FROM host_requests WHERE kind = ? AND "
            f"{where} ORDER BY {order}",
            (negotiated.KIND, *parameters),
        )
        entries = [reads.context_entry(viewer, reads._request_row(r[0]))
                   for r in rows[:limit]]  # fmt: skip
        return entries, len(rows) - len(entries)

    open_states = "state IN ('pending_host', 'awaiting_player')"
    mine, mine_more = listed(f"requester_participant = ? AND {open_states}",
                             (job.participant_id,), "rowid DESC", MINE)  # fmt: skip
    recent, recent_more = listed(
        f"requester_participant = ? AND NOT ({open_states})", (job.participant_id,),
        "decided_at DESC, rowid DESC", RECENT_OVERRIDES,
    )  # fmt: skip
    block = {"categories": categories, "mine": mine, "mine_more": mine_more,
             "recent": recent, "recent_more": recent_more}  # fmt: skip
    if viewer.host:
        block["pending_host"], block["pending_host_more"] = listed(
            "state = 'pending_host'", (), "rowid", PENDING_HOST
        )
    if result_bytes(block) > OVERRIDES_BLOCK_BYTES:
        raise RuntimeError("The context's overrides block is over its bound.")
    return block


def _check_mode(runtime, job: Job, tool: str, command: str):
    """Refuse, before journaling, what the job's mode never allows
    (character contract section 5). The store checks the actor again when
    the command runs."""
    step = runtime.is_step(command) if isinstance(command, str) else False
    if tool == "referee_act":
        allowed = job.mode == "play"
        why = "Referee-only commands run only in play, not in setup or creation."
    elif job.mode == "setup":
        allowed = False
        why = "This job's sender has no character: nothing in the game may change."
    elif job.mode == "creation":
        allowed = step
        why = "A creation job takes only its draft's creation steps."
    else:
        allowed = not step
        why = "Creation steps are only for drafts."
    if not allowed:
        raise ToolError("mode_forbidden", why)


def _actor(store: GameStore, job: Job) -> dict:
    """The job's frozen actor, as `get_context` names it."""
    character = None if job.character_id is None else store.character(job.character_id)
    return {
        "mode": job.mode,
        "character": None
        if character is None
        else {
            "character_id": character.character_id,
            "name": character.name,
            "phase": character.phase,
            "draft_revision": character.draft_revision,
        },
    }


def _with_first_page(result: dict, referee: RefereeStore) -> dict:
    """`get_context` with as much of the transcript's first page (up to
    TRANSCRIPT_PAGE_BYTES) as fits in one tool result (RESULT_BYTES, referee
    contract section 7), possibly none: `next_after_seq` says where `get_transcript` continues, so nothing
    is lost. Everything else is never shortened: a module whose context
    alone takes the result over the budget is refused, rather than letting
    Codex cut the middle out of it."""
    rest = len(json.dumps(result))
    if rest > RESULT_BYTES:
        raise ToolError(
            "context_too_large",
            f"This game's context is {rest} bytes, over the {RESULT_BYTES}-byte limit "
            "for one tool result; the module must keep its context and sheets smaller "
            "(governance contract section 11).",
        )
    room = min(TRANSCRIPT_PAGE_BYTES, RESULT_BYTES - rest)  # a page is still a page
    page, next_after = referee.transcript(0, max_bytes=room, minimum=0)
    while True:  # the page's own separators and next_after_seq count too
        result["transcript"] = {"messages": page, "next_after_seq": next_after}
        if len(json.dumps(result)) <= RESULT_BYTES or not page:
            return result
        page = page[:-1]
        next_after = page[-1]["seq"] if page else 0


def _character_context(store: GameStore, runtime, state: dict, job: Job) -> dict:
    """What a character game adds to `get_context`: every ready character
    (never another player's draft) and the job's own character's sheet."""
    ready = [
        {"character_id": c.character_id, "name": c.name,
         "participant_id": c.participant_id, "introduced": c.introduced_at is not None}
        for c in store.inspect_characters()
        if c.phase == "ready"
    ]  # fmt: skip
    sheet = None
    if job.character_id is not None:
        sheet = runtime.character_sheet(state, job.character_id)
    return {"characters": ready, "sheet": sheet}


def _policy_context(store: GameStore, runtime, state: dict, job: Job) -> dict:
    """What campaign settings add to `get_context` (governance contract
    section 10): the job's character's effective settings with the
    campaign's guidance, and what that character needs. Computed by the
    host; never another character's exception. Guidance reaches the referee
    whether or not the module has settings; a game with neither settings
    nor a document gets nothing here, as before."""
    revision, document = store.active_document()
    if runtime.settings is None and document is None:
        return {}
    policy = store.effective_policy(job.character_id)
    if policy is None:  # guidance only: the module has no settings
        policy = {"revision": revision, "settings": {}}
    return {
        "policy": {
            "revision": policy["revision"],
            "settings": policy["settings"],
            "guidance": None if document is None else document["guidance"],
        },
        "requirements": requirements(store, runtime, policy, state, job.character_id),
    }


def _character_actions(store: GameStore, runtime, job: Job) -> dict:
    """`list_actions` in a character game: what the job's mode allows."""
    state = store.inspect_state()
    policy = store.effective_policy(job.character_id)
    referee_commands = []
    if job.mode == "setup":
        actions = []
    elif job.mode == "creation":
        actions = runtime.creation_actions(state, job.character_id, policy)
        if actions is None:
            steps = [c.name for c in runtime.characters.steps]
            actions = [{"command": name} for name in ("character.name", *steps)]
    else:
        actions = runtime.character_actions(state, job.character_id, policy)
        if actions is None:
            actions = [{"command": c.name} for c in runtime.module.commands]
        referee_commands = [c.name for c in runtime.module.referee_commands]
    return {
        "for": {"participant_id": job.participant_id, "character_id": job.character_id,
                "mode": job.mode},
        "actions": actions,
        "referee_commands": referee_commands,
    }  # fmt: skip


def _names(store: GameStore, job: Job) -> dict:
    """Participant ID to name; a development game's sender has no record."""
    names = {p.participant_id: p.name for p in store.participants()}
    names.setdefault(job.participant_id, job.participant_id)
    return names


def _step(step: Step, store: GameStore | None = None) -> dict:
    """A step as the referee sees it. In a character game it also names the
    actor its job was accepted for, so a recovered result is told about the
    right character, whoever's job reports it."""
    shown = {"job_id": step.job_id, "step_id": step.step_id, "kind": step.kind,
             "by": step.context, "command": step.command, "args": step.args,
             "result": step.result}  # fmt: skip
    if step.mode is not None:
        character = None
        if step.character_id is not None and store is not None:
            record = store.character(step.character_id)
            character = {"character_id": step.character_id,
                         "name": None if record is None else record.name}  # fmt: skip
        shown["actor"] = {"mode": step.mode, "character": character}
    return shown


def _recorded(step: Step):
    """An intended step's outcome so far: its recorded result if it
    committed (an exact retry), else None; raises for a settled refusal or a
    step that never ran."""
    if step.status == "committed":
        return step.result
    if step.status == "refused":
        raise ToolError(step.result["code"], step.result["message"])
    if step.status == "not_committed":
        raise ToolError("request_conflict", "That step did not run; use a new step_id.")
    return None


def _participant_holds(store: GameStore, by) -> bool:
    return by.participant is None or store.session_revision(by.participant) is not None


def _check_participant(store: GameStore, by):
    """Inside the caller's transaction: refuse if the host's own session in
    this game has ended (its link, or the game in its file)."""
    if not _participant_holds(store, by):
        raise _not_host()


def _not_host() -> ToolError:
    return ToolError(
        "not_campaign_host",
        "Only a job of the campaign host has host tools, and only while it is host.",
    )


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
