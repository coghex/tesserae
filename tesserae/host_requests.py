"""Guest requests for host decisions (docs/designs/governance_contract.md
section 9): a referee job proposes an exact exception, campaign setting or
host operation; the campaign host approves or denies it; the requester may
cancel it.

A companion of GameStore, like tesserae.campaign_rules: every operation is
one transaction on the store's connection. Authority is the caller's to
establish (the lobby's guard for a campaign host, the job's frozen sender for
a proposal, the requester's session for a cancel). An approval applies its
exact effect once, against the state it was proposed for, and never grants
anything further: no standing permission, no next action.
"""

import json
import secrets
from datetime import datetime, timezone

from . import policy as policies
from .campaign_rules import CampaignRules, mutation
from .modules import ArgumentError
from .protocol import PROTOCOL_VERSION, ProtocolError, parse_request
from .store import Decider, GameStore, Refusal

KINDS = ("exception", "campaign_setting", "operation")
DECISIONS = ("approve", "deny")
MAX_EXPLANATION = 1000
MAX_NOTE = 1000
RECENT = 10  # decided requests a requester's projection keeps showing


class HostRequests:
    def __init__(self, store: GameStore):
        self.store = store

    # --- Proposing (a referee job's `propose_host_decision`) -----------------

    def check(self, job, kind, scope, target, payload, explanation) -> dict:
        """The proposal a job may make, normalized: {kind, target, payload,
        explanation}. Raises Refusal for anything the host would not decide
        on, before anything is stored. Read inside the caller's state."""
        if kind not in KINDS:
            raise Refusal(
                "invalid_request", "kind is exception, campaign_setting or operation."
            )
        if scope not in ("target", "campaign"):
            raise Refusal("invalid_request", "scope is target or campaign.")
        if (scope == "campaign") != (kind == "campaign_setting"):
            raise Refusal(
                "invalid_request",
                "A campaign setting is campaign-wide (scope campaign); anything "
                "else names its target.",
            )
        if (
            not isinstance(explanation, str)
            or not explanation.strip()
            or len(explanation) > MAX_EXPLANATION
        ):
            raise Refusal(
                "invalid_request",
                f"explanation is 1 to {MAX_EXPLANATION} characters.",
            )
        runtime = self.store.runtime()
        if kind == "campaign_setting":
            if target is not None:
                raise Refusal("invalid_request", "A campaign setting has no target.")
            self._settings_problems(runtime, payload, None)
            return {"kind": kind, "target": None, "payload": payload,
                    "explanation": explanation}  # fmt: skip
        if kind == "exception":
            character_id = job.character_id if target is None else _character_of(target)
            if character_id is None or not self._requester_owns(job, character_id):
                raise Refusal(
                    "invalid_request",
                    "An exception is for one of the sender's own characters; this "
                    "job has none to default to.",
                )
            self._settings_problems(runtime, payload, character_id)
            return {"kind": kind, "target": {"kind": "character", "id": character_id},
                    "payload": payload, "explanation": explanation}  # fmt: skip
        # An operation: exactly one of the module's host operations.
        if not isinstance(payload, dict) or set(payload) != {"command", "args"}:
            raise Refusal(
                "invalid_request", "An operation's payload is exactly command and args."
            )
        request = self._parsed_operation(payload, "check")
        # The target is what the operation changes, by the module's rules;
        # the request must name exactly that, so its digest guards it.
        changed = runtime.for_host().operation_target(request.command, request.args)
        if changed is None:
            raise Refusal(
                "invalid_request",
                "This module cannot say what that operation changes, so it cannot "
                "be requested.",
            )
        if target != changed:
            raise Refusal(
                "invalid_request",
                "The target must be exactly what the operation changes.",
            )
        try:
            self.store.target_digest(target)
        except ArgumentError as error:
            raise Refusal("invalid_request", error.message) from None
        return {"kind": kind, "target": target, "payload": payload,
                "explanation": explanation}  # fmt: skip

    def propose(self, job, step_request_id: str, proposal: dict, *, check=None):
        """Store a proposal (as `check` normalized it, checked again here) as
        pending, in one transaction, with the policy revision, a draft
        target's draft revision and the target's digest it was made against;
        supersede the requester's older pending one for the same target and
        kind. The same step returns the same proposal: (view, created)."""
        with self.store.transaction() as db:
            if check is not None:
                check()
            # The step journal holds one content per step ID, so the step's
            # proposal, if it was stored, is this one.
            row = db.execute(
                "SELECT proposal_id FROM host_requests WHERE step_request_id = ?",
                (step_request_id,),
            ).fetchone()
            if row is not None:
                return self._view(row[0]), False
            proposal = self.check(job, proposal["kind"],
                                  "campaign" if proposal["target"] is None else "target",
                                  proposal["target"], proposal["payload"],
                                  proposal["explanation"])  # fmt: skip
            target = proposal["target"]
            target_revision = digest = None
            if target is not None:
                digest = self.store.target_digest(target)
                if target["kind"] == "character":
                    character = self.store.character(target["id"])
                    target_revision = character.draft_revision
            target_json = _json(target)
            superseded = db.execute(
                "SELECT proposal_id FROM host_requests WHERE state = 'pending' AND "
                "requester_participant = ? AND kind = ? AND target IS ?",
                (job.participant_id, proposal["kind"], target_json),
            ).fetchall()
            db.executemany(
                "UPDATE host_requests SET state = 'superseded', decided_at = ? "
                "WHERE proposal_id = ?",
                [(_now(), r[0]) for r in superseded],
            )
            proposal_id = "hp-" + secrets.token_hex(6)
            db.execute(
                "INSERT INTO host_requests (proposal_id, job_id, step_request_id, "
                "requester_participant, requester_player, target, kind, payload, "
                "explanation, policy_revision, target_revision, target_digest, state, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (proposal_id, job.job_id, step_request_id, job.participant_id,
                 self._player_of(job.participant_id), target_json, proposal["kind"],
                 _json(proposal["payload"]), proposal["explanation"],
                 self.store.policy_revision(), target_revision, digest, _now()),
            )  # fmt: skip
            return self._view(proposal_id), True

    # --- Deciding and cancelling ---------------------------------------------

    def decide(self, by: Decider, request_id, proposal_id, decision, note, *,
               check=None) -> tuple[dict, bool]:  # fmt: skip
        """The campaign host's decision, exactly replayed in its namespace:
        `{"proposal_id", "state", "effect"}`. An approval revalidates against
        the game now: if the policy revision, a draft target's revision or the
        target's digest has moved, the proposal becomes `stale` and nothing
        else changes; otherwise its exact effect, its state, the replay
        record and the audit row commit together. Refused (nothing changed)
        while a referee job other than the host's own runs."""
        if decision not in DECISIONS:
            raise Refusal("invalid_request", "decision is approve or deny.")
        if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE):
            raise Refusal(
                "invalid_request", f"note is null or up to {MAX_NOTE} characters."
            )
        content = {"proposal_id": proposal_id, "decision": decision, "note": note}

        def apply(db):
            row = self._row(proposal_id)
            if row is None or row["state"] in ("cancelled", "superseded", "stale"):
                raise _unavailable()
            if row["state"] != "pending":
                raise Refusal(
                    "proposal_decided",
                    f"That proposal was already {row['state']}.",
                    {"proposal_id": proposal_id, "state": row["state"]},
                )
            busy = self.store.busy_refusal(by.job_id)
            if busy is not None:
                raise busy
            effect, state = None, "denied"
            if decision == "approve":
                state = "stale" if self._moved(row) else "approved"
                if state == "approved":
                    effect = self._apply(db, row, by)
            db.execute(
                "UPDATE host_requests SET state = ?, decided_by = ?, decided_at = ?, "
                "decision_note = ?, effect = ? WHERE proposal_id = ?",
                (state, by.player_id, _now(), note, _json(effect), proposal_id),
            )
            self.store.audit(
                by,
                "decision",
                {"proposal_id": proposal_id, "decision": decision, "outcome": state,
                 "effect": effect, "request_id": request_id},
                target=None if row["target"] is None else row["target"]["id"],
            )  # fmt: skip
            return {"proposal_id": proposal_id, "state": state, "effect": effect}

        return mutation(self.store, by.sender, request_id, "request_decide", content,
                        apply, check)  # fmt: skip

    def cancel(self, participant_id: str, request_id, proposal_id, *, check=None):
        """The requester withdraws its own pending proposal, replayed in its
        own namespace; anyone else's, or one no longer pending, is
        `proposal_unavailable`."""
        content = {"proposal_id": proposal_id}

        def apply(db):
            row = self._row(proposal_id)
            if (
                row is None
                or row["requester_participant"] != participant_id
                or row["state"] != "pending"
            ):
                raise _unavailable()
            db.execute(
                "UPDATE host_requests SET state = 'cancelled', decided_at = ? "
                "WHERE proposal_id = ?",
                (_now(), proposal_id),
            )
            return {"proposal_id": proposal_id, "state": "cancelled"}

        return mutation(self.store, participant_id, request_id, "request_cancel",
                        content, apply, check)  # fmt: skip

    # --- Reads -----------------------------------------------------------------

    def listing(self, participant_id: str | None = None, *, recent=None) -> list[dict]:
        """Every proposal (the host's view), or one requester's own; with
        `recent`, pending ones and only that many decided ones, newest last."""
        where, parameters = "", ()
        if participant_id is not None:
            where, parameters = "WHERE requester_participant = ?", (participant_id,)
        ids = [row[0] for row in self.store.query(
            f"SELECT proposal_id, state FROM host_requests {where} "
            "ORDER BY created_at, rowid", parameters)]  # fmt: skip
        views = [self._view(proposal_id) for proposal_id in ids]
        if recent is None:
            return views
        decided = [v for v in views if v["state"] != "pending"][-recent:]
        return [v for v in views if v["state"] == "pending" or v in decided]

    def requester_of(self, proposal_id: str) -> str | None:
        row = self._row(proposal_id)
        return None if row is None else row["requester_participant"]

    # --- Internals -----------------------------------------------------------

    def _settings_problems(self, runtime, payload, character_id):
        if runtime.settings is None:
            raise Refusal("invalid_request", "This game's module has no settings.")
        supported = frozenset(runtime.settings_defaults())
        if (
            not isinstance(payload, dict)
            or not payload
            or not set(payload) <= supported
        ):
            raise Refusal(
                "invalid_request",
                "The payload is the exact settings to change, among: "
                + ", ".join(sorted(supported))
                + ".",
            )
        settings = self.store.effective_policy(character_id)["settings"]
        problems = runtime.settings_problems({**settings, **payload},
                                             self.store.inspect_state())  # fmt: skip
        if problems:
            raise Refusal(
                "invalid_document",
                "The rules do not allow that: " + " ".join(problems),
                {"problems": problems},
            )

    def _parsed_operation(self, payload, request_id: str):
        runtime = self.store.runtime().for_host()
        raw = json.dumps({"version": PROTOCOL_VERSION, "type": "command",
                          "request_id": request_id,
                          "expected_revision": self.store.revision(),
                          "command": payload["command"], "args": payload["args"]})  # fmt: skip
        try:
            return parse_request(raw, runtime)
        except ProtocolError as error:
            raise Refusal(error.code, error.message) from None

    def _requester_owns(self, job, character_id) -> bool:
        character = self.store.character(character_id)
        return (
            character is not None
            and character.participant_id == job.participant_id
            and character.phase != "archived"
        )

    def _moved(self, row) -> bool:
        """Whether the game has moved since the proposal (section 9's stale
        rule): the policy revision, a draft target's revision, its digest."""
        if self.store.policy_revision() != row["policy_revision"]:
            return True
        target = row["target"]
        if target is None:
            return False
        if target["kind"] == "character":
            character = self.store.character(target["id"])
            if character is None or character.draft_revision != row["target_revision"]:
                return True
        try:
            return self.store.target_digest(target) != row["target_digest"]
        except ArgumentError:
            return True

    def _apply(self, db, row, by: Decider) -> dict:
        """The approved proposal's exact effect, inside the decision's
        transaction."""
        if row["kind"] == "operation":
            request = self._parsed_operation(row["payload"], row["proposal_id"])
            record = self.store.run_host_operation(by, request)
            return {"revision": record.revision, "result": record.result}
        rules = CampaignRules(self.store)
        _, active = self.store.active_document()
        binding = self.store.binding()
        document = active or {
            "document_version": policies.DOCUMENT_VERSION,
            "module": {"id": binding.module_id, "version": binding.version,
                       "state_version": binding.state_version},
            "settings": {}, "exceptions": [], "guidance": None,
        }  # fmt: skip
        document = json.loads(json.dumps(document))
        if row["kind"] == "campaign_setting":
            document["settings"].update(row["payload"])
        else:
            character_id = row["target"]["id"]
            exceptions = document["exceptions"]
            found = policies.own_exception(document, character_id)
            settings = {} if found is None else dict(found["settings"])
            settings.update(row["payload"])
            replacement = {
                "exception_id": found["exception_id"] if found
                else "x-" + row["proposal_id"][3:],
                "character_id": character_id,
                "settings": settings,
                "reason": row["explanation"],
                "proposal_id": row["proposal_id"],
            }  # fmt: skip
            document["exceptions"] = [
                e for e in exceptions if e["character_id"] != character_id
            ] + [replacement]
        revision, _ = rules.store_candidate(db, document, "request")
        drafts = rules.make_active(db, revision, document, by)
        return {"policy_revision": revision, "drafts": drafts}

    def _row(self, proposal_id) -> dict | None:
        if not isinstance(proposal_id, str):
            return None
        rows = self.store.query(
            "SELECT proposal_id, job_id, requester_participant, requester_player, "
            "target, kind, payload, explanation, policy_revision, target_revision, "
            "target_digest, state, created_at, decided_by, decided_at, "
            "decision_note, effect FROM host_requests WHERE proposal_id = ?",
            (proposal_id,),
        )
        if not rows:
            return None
        keys = ("proposal_id", "job_id", "requester_participant", "requester_player",
                "target", "kind", "payload", "explanation", "policy_revision",
                "target_revision", "target_digest", "state", "created_at",
                "decided_by", "decided_at", "decision_note", "effect")  # fmt: skip
        row = dict(zip(keys, rows[0]))
        for key in ("target", "payload", "effect"):
            row[key] = None if row[key] is None else json.loads(row[key])
        return row

    def _view(self, proposal_id: str) -> dict:
        """A proposal as the people involved see it (no digests or IDs of
        infrastructure)."""
        row = self._row(proposal_id)
        names = {p.participant_id: p.name for p in self.store.participants()}
        requester = row["requester_participant"]
        return {
            "proposal_id": row["proposal_id"],
            "kind": row["kind"],
            "target": row["target"],
            "payload": row["payload"],
            "explanation": row["explanation"],
            "state": row["state"],
            "policy_revision": row["policy_revision"],
            "requester": {"participant_id": requester, "name": names.get(requester)},
            "created_at": row["created_at"],
            "decided_at": row["decided_at"],
            "decision_note": row["decision_note"],
            "effect": row["effect"],
        }

    def _player_of(self, participant_id: str) -> str | None:
        rows = self.store.query(
            "SELECT player_id FROM host_links WHERE participant_id = ? "
            "AND unlinked_at IS NULL",
            (participant_id,),
        )
        return rows[0][0] if rows else None


def proposal_message(result: dict, request_id: str | None = None) -> dict:
    """`proposal {proposal_id, state, effect?}`: a decision's or a cancel's
    reply (with its request ID), or the unsolicited notice."""
    message = {"version": PROTOCOL_VERSION, "type": "proposal", **result}
    if request_id is not None:
        message["request_id"] = request_id
    return message


def requests_message(requests: list) -> dict:
    return {"version": PROTOCOL_VERSION, "type": "requests", "requests": requests}


def _character_of(target) -> str | None:
    if isinstance(target, dict) and target.get("kind") == "character":
        return target.get("id") if isinstance(target.get("id"), str) else None
    return None


def _unavailable() -> Refusal:
    return Refusal(
        "proposal_unavailable",
        "That proposal is not available: unknown, not yours, or no longer pending.",
    )


def _json(value) -> str | None:
    return None if value is None else policies.canonical(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
