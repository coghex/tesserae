"""Negotiated creation overrides (docs/designs/creation_overrides_contract.md
sections 5 to 10): a requester's request, the campaign host's offer,
counteroffer, denial or withdrawal, the requester's acceptance or decline,
the host's direct override, and their private, bounded reads.

A companion of GameStore, like tesserae.host_requests, whose table it
shares: every operation is one transaction on the store's connection.
Authority is the caller's to establish (the lobby's guard: the session's
participant, its campaign role and the campaign's host player); this module
records who decided. Verdicts change no game state. Only an acceptance of
the exact open offer, or the current host's direct override, runs the
module's `apply`, and each commits its effect, its command record, the
draft's new revision and its application record together, or nothing.
"""

import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from . import policy as policies
from .modules import (
    ActionRejected,
    ArgumentError,
    IntentError,
    UnsupportedOverride,
    preview_digest,
    result_bytes,
)
from .protocol import PROTOCOL_VERSION
from .store import Decider, GameStore, Refusal

logger = logging.getLogger(__name__)

KIND = "creation_override"
OPEN_STATES = ("pending_host", "awaiting_player")
TERMINAL_STATES = (
    "applied", "denied", "declined", "withdrawn", "cancelled", "superseded", "stale",
)  # fmt: skip
DECISIONS = ("offer", "counteroffer", "deny")
RESPONSES = ("accept", "decline")
STATE_FILTERS = ("open", "terminal", "all")
MAX_TEXT = 1000
TEXT_BYTES = 4096
NAME_EXCERPT = 120
SUMMARY_EXCERPT = 240
CONTEXT_EXCERPT = 120
PROBLEM_EXCERPT = 300
PROBLEMS_BYTES = 2048
SUMMARY_CATEGORIES = 4
PAGE_ITEMS = 20
PAGE_BYTES = 32 * 1024
RESULT_BYTES = 45056
REQUEST_SUMMARY_BYTES = 2048
OFFER_SUMMARY_BYTES = 1024
RECORD_SUMMARY_BYTES = 1024
CONTEXT_ENTRY_BYTES = 1024
EXPIRY_HEADLINE = "Unused creation allowances expired when the character was approved."
PREVIEW_FAILED = "The module could not preview this intent."
SUPERSEDED_LEGACY = "Negotiated creation overrides replace this request; ask again."
_CURSOR = re.compile(r"[0-9]{1,18}")


@dataclass(frozen=True)
class Viewer:
    """Who reads or acts, as the lobby's guard established it: the session's
    participant, whether it is the campaign host now, and the campaign's
    host player now (None with no campaign host). Never from a message."""

    participant_id: str
    host: bool = False
    campaign_host: str | None = None


class _Kept(Exception):
    """A refusal whose bookkeeping (an offer found stale) still commits."""

    def __init__(self, refusal: Refusal):
        super().__init__(refusal.text)
        self.refusal = refusal


class Overrides:
    def __init__(self, store: GameStore):
        self.store = store

    # --- Discovery and previews (reads) -------------------------------------

    def categories(self) -> list[dict]:
        """Every declared category with its request policy now."""
        runtime = self._runtime()
        _, document = self.store.active_document()
        policy = policies.category_policy(runtime, document)
        return [{**c, "policy": policy[c["category_id"]]}
                for c in runtime.override_categories()]  # fmt: skip

    def discover(self, viewer: Viewer, character_id, query) -> dict:
        runtime = self._runtime()
        self._draft_for(viewer, character_id)
        try:
            page = runtime.override_discover(
                self.store.inspect_state(), character_id, query
            )
        except ArgumentError as error:
            raise Refusal("invalid_request", error.message) from None
        except UnsupportedOverride as error:
            raise Refusal("override_unsupported", error.message) from None
        except IntentError as error:
            raise _invalid_intent(error.problems) from None
        return {"character_id": character_id, **page}

    def preview(self, viewer: Viewer, character_id, intent) -> dict:
        runtime = self._runtime()
        self._draft_for(viewer, character_id)
        preview, digest = self._preview(runtime, character_id, intent)
        return {"character_id": character_id, "preview": preview,
                "preview_digest": digest}  # fmt: skip

    # --- Request (section 7, step 1) ----------------------------------------

    def request(self, viewer: Viewer, request_id, character_id, intent, explanation,
                revises=None, *, check=None) -> tuple[dict, bool]:  # fmt: skip
        """A requester's request for one of its own drafts, replayed in its
        own namespace: `{"proposal_id", "state", "preview_digest"}`."""
        _check_text(explanation, "explanation")
        content = {"character_id": character_id, "intent": intent,
                   "explanation": explanation, "revises": revises}  # fmt: skip

        def apply(db):
            return self.store_request(viewer, character_id, intent, explanation,
                                      revises, origin="client")  # fmt: skip

        return self._mutation(viewer.participant_id, request_id, "override_request",
                              content, apply, check)  # fmt: skip

    def store_request(self, viewer: Viewer, character_id, intent, explanation,
                      revises=None, *, origin="client", job=None,
                      step_request_id=None) -> dict:  # fmt: skip
        """Inside the caller's write transaction: check, preview and store one
        request as `pending_host`, superseding the requester's earlier open
        request for the draft. Raises Refusal with nothing stored."""
        runtime = self._runtime()
        if viewer.host:
            raise Refusal(
                "invalid_request",
                "The campaign host does not ask itself: apply it directly.",
            )
        _check_text(explanation, "explanation")
        character = self._owned_draft(viewer.participant_id, character_id)
        if revises is not None:
            earlier = self._request_row(revises)
            if (
                earlier is None
                or earlier["requester_participant"] != viewer.participant_id
                or earlier["target"] != _target(character_id)
            ):
                raise _proposal_unavailable()
        preview, digest = self._preview(runtime, character_id, intent)
        _, document = self.store.active_document()
        blocked = policies.blocked_categories(
            runtime, policies.category_policy(runtime, document), intent, preview
        )
        if blocked:
            raise Refusal(
                "override_blocked",
                "This campaign does not take requests in "
                + ", ".join(b["label"] for b in blocked)
                + "; nothing was stored.",
                {"categories": blocked},
            )
        now = _now()
        for row in self._open_requests(
            "requester_participant = ? AND target = ?",
            (viewer.participant_id, _json(_target(character_id))),
        ):
            self._close_request(row, "superseded", now)
        proposal_id = "hp-" + secrets.token_hex(6)
        self.store.query_write(
            "INSERT INTO host_requests (proposal_id, job_id, step_request_id, "
            "requester_participant, requester_player, target, kind, payload, "
            "explanation, policy_revision, target_revision, state, created_at, "
            "revises, preview, preview_digest, origin, module) VALUES (?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, 'pending_host', ?, ?, ?, ?, ?, ?)",
            (proposal_id, job, step_request_id, viewer.participant_id,
             self._player_of(viewer.participant_id), _json(_target(character_id)),
             KIND, _json(intent), explanation, self.store.policy_revision(),
             character.draft_revision, now, revises, _json(preview), digest, origin,
             self._module(runtime)),
        )  # fmt: skip
        self.store.notify_proposal(proposal_id)
        return {"proposal_id": proposal_id, "state": "pending_host",
                "preview_digest": digest}  # fmt: skip

    # --- Verdicts and withdrawal (section 7, steps 2 and 4) -----------------

    def decide(self, by: Decider, viewer: Viewer, request_id, proposal_id, decision,
               preview_digest_=None, intent=None, note=None, *,
               check=None) -> tuple[dict, bool]:  # fmt: skip
        """The campaign host's verdict, replayed in its namespace: an offer
        or counteroffer (open, the request `awaiting_player`) or a denial.
        `{"proposal_id", "state", "offer_id", "offer_revision"}`."""
        if decision not in DECISIONS:
            raise Refusal("invalid_request", "decision is offer, counteroffer or deny.")
        if decision == "offer":
            if intent is not None:
                raise Refusal(
                    "invalid_request", "An offer offers the requested intent as it is."
                )
            if note is not None:
                _check_text(note, "note")
        else:
            _check_text(note, "note")
        if decision == "deny" and (intent is not None or preview_digest_ is not None):
            raise Refusal("invalid_request", "A denial has only a note.")
        if decision != "deny" and not isinstance(preview_digest_, str):
            raise Refusal(
                "invalid_request",
                "An offer names the preview_digest of the preview the host reviewed.",
            )
        if decision == "counteroffer" and intent is None:
            raise Refusal("invalid_request", "A counteroffer names its intent.")
        content = {"proposal_id": proposal_id, "decision": decision,
                   "preview_digest": preview_digest_, "intent": intent, "note": note}  # fmt: skip

        def apply(db):
            return self.store_decision(by, viewer, request_id, proposal_id, decision,
                                       preview_digest_, intent, note)  # fmt: skip

        return self._mutation(by.sender, request_id, "override_decide", content,
                              apply, check)  # fmt: skip

    def store_decision(self, by, viewer, request_id, proposal_id, decision,
                       named_digest, intent, note) -> dict:  # fmt: skip
        """Inside the caller's write transaction. Raises Refusal; a `_Kept`
        refusal keeps a staleness it found."""
        runtime = self._runtime()
        row = self._request_row(proposal_id)
        if row is None:
            raise _proposal_unavailable()
        found_stale = self._settle_open_offer(runtime, row, viewer.campaign_host)
        row = self._request_row(proposal_id)
        state = row["state"]
        if state in ("cancelled", "superseded", "stale"):
            raise self._keep(_proposal_unavailable(), found_stale)
        allowed = ("pending_host", "awaiting_player") if decision == "counteroffer" \
            else ("pending_host",)  # fmt: skip
        if state not in allowed:
            raise self._keep(
                Refusal(
                    "proposal_decided",
                    f"That request is {state} now.",
                    {"proposal_id": proposal_id, "state": state},
                ),
                found_stale,
            )
        now = _now()
        character_id = row["target"]["id"]
        offer_id = offer_revision = None
        if decision == "deny":
            self.store.query_write(
                "UPDATE host_requests SET state = 'denied', decided_by = ?, "
                "decided_at = ?, decision_note = ? WHERE proposal_id = ?",
                (by.player_id, now, note, proposal_id),
            )
            state = "denied"
        else:
            offered = row["payload"] if decision == "offer" else intent
            preview, digest = self._preview(runtime, character_id, offered)
            if digest != named_digest:
                raise self._keep(_stale_preview(preview, digest), found_stale)
            for open_offer in self._offers("proposal_id = ? AND state = 'open'",
                                           (proposal_id,)):  # fmt: skip
                self._close_offer(open_offer["offer_id"], "superseded", now)
            offer_revision = 1 + (self.store.query(
                "SELECT ifnull(max(offer_revision), 0) FROM override_offers "
                "WHERE proposal_id = ?", (proposal_id,))[0][0])  # fmt: skip
            offer_id = "ho-" + secrets.token_hex(6)
            draft = self.store.character(character_id)
            self.store.query_write(
                "INSERT INTO override_offers (offer_id, proposal_id, offer_revision, "
                "origin, intent, preview, preview_digest, module, host_player_id, "
                "note, draft_revision, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, 'open', ?)",
                (offer_id, proposal_id, offer_revision, decision, _json(offered),
                 _json(preview), digest, self._module(runtime), by.player_id, note,
                 draft.draft_revision, now),
            )  # fmt: skip
            self.store.query_write(
                "UPDATE host_requests SET state = 'awaiting_player', decided_by = ?, "
                "decided_at = ?, decision_note = ? WHERE proposal_id = ?",
                (by.player_id, now, note, proposal_id),
            )
            state = "awaiting_player"
        self.store.audit(
            by, "decision",
            {"proposal_id": proposal_id, "decision": decision, "outcome": state,
             "offer_id": offer_id, "request_id": request_id},
            target=character_id,
        )  # fmt: skip
        self.store.notify_proposal(proposal_id)
        return {"proposal_id": proposal_id, "state": state, "offer_id": offer_id,
                "offer_revision": offer_revision}  # fmt: skip

    def withdraw(self, by: Decider, viewer: Viewer, request_id, offer_id, note, *,
                 check=None) -> tuple[dict, bool]:  # fmt: skip
        """The campaign host withdraws an open offer, with a note; the
        request becomes `withdrawn`. An accepted offer cannot be withdrawn."""
        _check_text(note, "note")
        content = {"offer_id": offer_id, "note": note}

        def apply(db):
            return self.store_withdrawal(by, viewer, request_id, offer_id, note)

        return self._mutation(by.sender, request_id, "override_withdraw", content,
                              apply, check)  # fmt: skip

    def store_withdrawal(self, by, viewer, request_id, offer_id, note) -> dict:
        runtime = self._runtime()
        offer = self._offer_row(offer_id)
        if offer is None:
            raise _offer_unavailable()
        row = self._request_row(offer["proposal_id"])
        found_stale = self._settle_open_offer(runtime, row, viewer.campaign_host)
        offer = self._offer_row(offer_id)
        if offer["state"] != "open":
            raise self._keep(_offer_unavailable(), found_stale)
        now = _now()
        self._close_offer(offer_id, "withdrawn", now, by.player_id, note)
        self.store.query_write(
            "UPDATE host_requests SET state = 'withdrawn', decided_by = ?, "
            "decided_at = ?, decision_note = ? WHERE proposal_id = ?",
            (by.player_id, now, note, offer["proposal_id"]),
        )
        self.store.audit(
            by, "decision",
            {"proposal_id": offer["proposal_id"], "decision": "withdraw",
             "outcome": "withdrawn", "offer_id": offer_id, "request_id": request_id},
            target=row["target"]["id"],
        )  # fmt: skip
        self.store.notify_proposal(offer["proposal_id"])
        return {"proposal_id": offer["proposal_id"], "state": "withdrawn",
                "offer_id": offer_id}  # fmt: skip

    # --- The requester's response (section 7, step 3; section 8) ------------

    def respond(self, viewer: Viewer, request_id, offer_id, response,
                named_digest=None, note=None, *, check=None,
                job_id=None) -> tuple[dict, bool]:  # fmt: skip
        """Accept or decline an open offer of one's own request. A decline is
        replayed in `host_mutations`; an acceptance is a command in the
        requester's namespace and applies the plan, or applies nothing.
        `{"proposal_id", "state", "offer_id", "record_id"?, "revision"?}`."""
        if response not in RESPONSES:
            raise Refusal("invalid_request", "response is accept or decline.")
        if response == "decline":
            if named_digest is not None:
                raise Refusal("invalid_request", "A decline names no preview_digest.")
            if note is not None:
                _check_text(note, "note")
            content = {"offer_id": offer_id, "response": response, "note": note}

            def apply(db):
                return self._decline(viewer, offer_id, note)

            return self._mutation(viewer.participant_id, request_id, "override_respond",
                                  content, apply, check)  # fmt: skip
        if note is not None:
            raise Refusal("invalid_request", "An acceptance has no note.")
        if not isinstance(named_digest, str):
            raise Refusal(
                "invalid_request", "An acceptance names the offer's preview_digest."
            )
        content = {"version": PROTOCOL_VERSION, "type": "override_respond",
                   "offer_id": offer_id, "response": response,
                   "preview_digest": named_digest}  # fmt: skip

        def apply():
            return self._accept(viewer, offer_id, named_digest, job_id)

        return self._command(viewer.participant_id, request_id, content, apply, check)

    def _decline(self, viewer, offer_id, note) -> dict:
        offer = self._offer_row(offer_id)
        row = None if offer is None else self._request_row(offer["proposal_id"])
        if row is None or row["requester_participant"] != viewer.participant_id:
            raise _offer_unavailable()
        if offer["state"] != "open":
            raise _offer_unavailable()
        now = _now()
        self._close_offer(offer_id, "declined", now, viewer.participant_id, note)
        self.store.query_write(
            "UPDATE host_requests SET state = 'declined', decided_at = ? "
            "WHERE proposal_id = ?",
            (now, offer["proposal_id"]),
        )
        self.store.notify_proposal(offer["proposal_id"])
        return {"proposal_id": offer["proposal_id"], "state": "declined",
                "offer_id": offer_id}  # fmt: skip

    def _accept(self, viewer, offer_id, named_digest, job_id):
        """Inside the command's transaction: (state, record fields, reply)."""
        runtime = self._runtime()
        offer = self._offer_row(offer_id)
        row = None if offer is None else self._request_row(offer["proposal_id"])
        if row is None or row["requester_participant"] != viewer.participant_id:
            raise _offer_unavailable()
        character_id = row["target"]["id"]
        if offer["state"] != "open":
            raise _offer_unavailable()
        if offer["preview_digest"] != named_digest:
            raise _stale_preview(offer["preview"], offer["preview_digest"])
        if self.store.acting_job(character_id, except_job=job_id):
            raise Refusal(
                "character_busy",
                "The referee is acting for that character; try again when it is done.",
            )
        reason, preview = self._offer_problem(runtime, offer, row, viewer.campaign_host)
        if reason is not None:
            state = self._make_stale(runtime, offer, row, reason)
            raise _Kept(Refusal(
                "offer_stale",
                "That offer no longer holds; nothing was applied.",
                {"proposal_id": row["proposal_id"], "state": state,
                 "stale_reason": reason},
            ))  # fmt: skip
        authorization = {
            "kind": "accepted_offer", "record_id": "hr-" + secrets.token_hex(6),
            "host_player_id": offer["host_player_id"],
            "proposal_id": row["proposal_id"], "offer_id": offer_id,
            "requester_participant_id": viewer.participant_id,
            "reason": row["explanation"],
        }  # fmt: skip
        state, receipt = self._apply(runtime, character_id, preview, authorization)
        return state, {
            "authorization": authorization, "character_id": character_id,
            "intent": offer["intent"], "preview": preview,
            "preview_digest": offer["preview_digest"], "receipt": receipt,
            "row": row, "offer_id": offer_id,
        }  # fmt: skip

    # --- Direct override (section 8) ----------------------------------------

    def apply_direct(self, by: Decider, request_id, character_id, intent,
                     named_digest, reason, *, check=None) -> tuple[dict, bool]:  # fmt: skip
        """The campaign host's own change to any draft, a command in its
        namespace: `{"character_id", "record_id", "revision",
        "draft_revision"}`. No request, offer or category policy is involved."""
        _check_text(reason, "reason")
        if not isinstance(named_digest, str):
            raise Refusal(
                "invalid_request",
                "A direct override names the preview_digest it shows.",
            )
        content = {"version": PROTOCOL_VERSION, "type": "override_apply",
                   "character_id": character_id, "intent": intent,
                   "preview_digest": named_digest, "reason": reason}  # fmt: skip

        def apply():
            runtime = self._runtime()
            busy = self.store.busy_refusal(by.job_id)
            if busy is not None:
                raise busy
            character = self.store.character(character_id)
            if character is None:
                raise Refusal(
                    "character_unavailable", "There is no such character in this game."
                )
            if character.phase != "draft":
                raise Refusal("invalid_request", "Overrides change drafts only.")
            preview, digest = self._preview(runtime, character_id, intent)
            if digest != named_digest:
                raise _stale_preview(preview, digest)
            authorization = {
                "kind": "direct", "record_id": "hr-" + secrets.token_hex(6),
                "host_player_id": by.player_id, "proposal_id": None,
                "offer_id": None, "requester_participant_id": None, "reason": reason,
            }  # fmt: skip
            state, receipt = self._apply(runtime, character_id, preview, authorization)
            return state, {
                "authorization": authorization, "character_id": character_id,
                "intent": intent, "preview": preview, "preview_digest": digest,
                "receipt": receipt, "by": by,
            }  # fmt: skip

        return self._command(by.sender, request_id, content, apply, check)

    # --- Hooks inside other transactions ------------------------------------

    def recheck(self, character_id: str):
        """The eager check (section 7): re-preview the draft's open offer,
        if any, and record it stale if it no longer holds. Inside the
        caller's write transaction; a module that cannot preview stales it."""
        runtime = self.store.runtime()
        if runtime.creation_overrides is None:
            return
        for row in self._open_requests(
            "state = 'awaiting_player' AND target = ?", (_json(_target(character_id)),)
        ):
            for offer in self._offers("proposal_id = ? AND state = 'open'",
                                      (row["proposal_id"],)):  # fmt: skip
                reason, _ = self._offer_problem(runtime, offer, row, None)
                if reason is not None:
                    self._make_stale(runtime, offer, row, reason)

    def completed(self, character_id: str, expired: list, revision: int,
                  draft_revision: int):  # fmt: skip
        """Inside the completion's transaction: the expiry record of the
        allowances that ended, if any, and the draft's open requests made
        `stale` (completed), their open offers too."""
        now = _now()
        if expired:
            self.store.query_write(
                "INSERT INTO override_records (record_id, character_id, kind, "
                "receipt, revision, draft_revision, created_at) VALUES (?, ?, "
                "'expiry', ?, ?, ?, ?)",
                ("hr-" + secrets.token_hex(6), character_id, _json(expired),
                 revision, draft_revision, now),
            )  # fmt: skip
        for row in self._open_requests("target = ?", (_json(_target(character_id)),)):
            self._close_request(row, "stale", now, stale_reason="completed")

    def cancel_open(self, where: str, parameters: tuple) -> list[str]:
        """Inside the caller's transaction (archive, unlink): the matching
        open requests and their open offers `cancelled`."""
        now, ended = _now(), []
        for row in self._open_requests(where, parameters):
            self._close_request(row, "cancelled", now)
            ended.append(row["proposal_id"])
        return ended

    def cancel(self, participant_id: str, row: dict):
        """The requester's `request_cancel` of an open request, inside its
        transaction."""
        self._close_request(row, "cancelled", _now())

    # --- Reads (section 10) -------------------------------------------------

    def requests(self, viewer: Viewer, character_id=None, states="all",
                 after=None) -> dict:  # fmt: skip
        if states not in STATE_FILTERS:
            raise Refusal("invalid_request", "states is open, terminal or all.")
        self._runtime()
        where, parameters = ["kind = ?"], [KIND]
        if not viewer.host:
            where.append("requester_participant = ?")
            parameters.append(viewer.participant_id)
        if character_id is not None:
            where.append("target = ?")
            parameters.append(_json(_target(character_id)))
        if states != "all":
            listed = OPEN_STATES if states == "open" else TERMINAL_STATES
            where.append(f"state IN ({', '.join('?' * len(listed))})")
            parameters += listed
        rows = self._rows_after(
            "host_requests", " AND ".join(where), parameters, "rowid", after
        )
        names = self._names()
        return self._page(
            rows, lambda r: self._request_summary(r, viewer, names), "rowid",
            {"type": "override_requests"},
        )  # fmt: skip

    def detail(self, viewer: Viewer, proposal_id) -> dict:
        self._runtime()
        row = self._visible_request(viewer, proposal_id)
        names = self._names()
        request = self._request_summary(row, viewer, names, full=True)
        request.pop("offer")
        request.update({
            "intent": row["payload"], "preview": row["preview"],
            "preview_digest": row["preview_digest"],
            "offer_count": self.store.query(
                "SELECT count(*) FROM override_offers WHERE proposal_id = ?",
                (proposal_id,))[0][0],
            "current": self._current(row),
        })  # fmt: skip
        last = self._last_offer(proposal_id)
        reply = {
            "request": request,
            "offer": None if last is None else self._offer_summary(last, row, viewer),
        }
        return _bounded(reply, RESULT_BYTES, "override_detail")

    def offers(self, viewer: Viewer, proposal_id, after=None) -> dict:
        self._runtime()
        row = self._visible_request(viewer, proposal_id)
        rows = self._rows_after(
            "override_offers", "proposal_id = ?", [proposal_id], "offer_revision", after
        )
        return self._page(
            rows, lambda o: self._offer_summary(o, row, viewer), "offer_revision",
            {"type": "override_offers", "proposal_id": proposal_id},
        )  # fmt: skip

    def offer(self, viewer: Viewer, offer_id) -> dict:
        self._runtime()
        offer = self._offer_row(offer_id)
        row = None if offer is None else self._request_row(offer["proposal_id"])
        if row is None or not (
            viewer.host or row["requester_participant"] == viewer.participant_id
        ):
            raise _offer_unavailable()
        view = self._offer_summary(offer, row, viewer, full=True)
        view.update({
            "proposal_id": offer["proposal_id"], "note": offer["note"],
            "close_note": offer["close_note"],
            "host_player_id": offer["host_player_id"],
            "draft_revision": offer["draft_revision"], "intent": offer["intent"],
            "preview": offer["preview"], "cut": [],
        })  # fmt: skip
        return _bounded({"offer": view}, RESULT_BYTES, "override_offer")

    def history(self, viewer: Viewer, character_id, after=None) -> dict:
        self._runtime()
        self._visible_character(viewer, character_id)
        rows = self._rows_after(
            "override_records", "character_id = ?", [character_id], "rowid", after
        )
        return self._page(
            rows, self._record_summary, "rowid",
            {"type": "override_history", "character_id": character_id},
        )  # fmt: skip

    def record(self, viewer: Viewer, record_id) -> dict:
        self._runtime()
        found = self._record_row(record_id)
        if found is None:
            raise _character_unavailable()
        self._visible_character(viewer, found["character_id"])
        view = self._record_summary(found)
        view.update({
            "reason": found["reason"], "intent": found["intent"],
            "preview": found["preview"], "preview_digest": found["preview_digest"],
            "receipt": found["receipt"], "cut": [],
        })  # fmt: skip
        return _bounded({"record": view}, RESULT_BYTES, "override_record")

    def requester_of(self, proposal_id) -> str | None:
        row = self._request_row(proposal_id)
        return None if row is None else row["requester_participant"]

    def context_entry(self, viewer: Viewer, row: dict) -> dict:
        """One request's entry in the referee context (section 11)."""
        explanation, cut = _excerpt(row["explanation"], CONTEXT_EXCERPT)
        categories = self._categories_of(row)
        last = self._last_offer(row["proposal_id"])
        entry = {
            "proposal_id": row["proposal_id"], "character_id": row["target"]["id"],
            "requester": row["requester_participant"], "state": row["state"],
            "categories": categories[:2], "categories_more": max(0, len(categories) - 2),
            "explanation": explanation,
            "offer": None if last is None else self._offer_brief(last, viewer),
            "cut": ["explanation"] if cut else [],
        }  # fmt: skip
        return _bounded(entry, CONTEXT_ENTRY_BYTES, "a context entry")

    # --- Internals: steps ---------------------------------------------------

    def _runtime(self):
        runtime = self.store.runtime()
        if runtime.creation_overrides is None:
            raise Refusal(
                "override_unsupported", "This game's module has no creation overrides."
            )
        return runtime

    def _module(self, runtime) -> str:
        binding = self.store.binding()
        return _json({"id": binding.module_id, "version": binding.version,
                      "state_version": binding.state_version,
                      "capability": runtime.creation_overrides.version})  # fmt: skip

    def _preview(self, runtime, character_id, intent) -> tuple[dict, str]:
        try:
            preview = runtime.override_preview(
                self.store.inspect_state(), character_id, intent,
                self.store.effective_policy(character_id),
            )  # fmt: skip
        except IntentError as error:
            raise _invalid_intent(error.problems) from None
        except UnsupportedOverride as error:
            raise Refusal("override_unsupported", error.message) from None
        return preview, preview_digest(preview)

    def _apply(self, runtime, character_id, preview, authorization):
        """Run the module's `apply` on the draft now; check what it left."""
        try:
            outcome = runtime.override_apply(
                self.store.inspect_state(), character_id, preview["plan"],
                self.store.effective_policy(character_id), authorization,
            )  # fmt: skip
        except ActionRejected as rejection:
            raise Refusal("action_rejected", rejection.message) from None
        except IntentError as error:
            raise _invalid_intent(error.problems) from None
        except UnsupportedOverride as error:
            raise Refusal("override_unsupported", error.message) from None
        # The result must still be a sheet and a set of allowances the
        # module can describe, or nothing commits (a module failure).
        runtime.character_sheet(outcome.state, character_id)
        runtime.override_allowances(outcome.state, character_id)
        return outcome.state, outcome.result

    def _command(self, sender, request_id, content, apply, check):
        """A mutation recorded as a command in `sender`'s namespace: exact
        replay, then `apply()` -> (state, details), committed with its
        command record, the draft's revision, its application record and,
        for an acceptance, the offer and request closed."""
        kept = None
        with self.store.transaction():
            if check is not None:
                check()
            recorded = self.store.recorded_command(sender, request_id)
            if recorded is None and self._mutation_row(sender, request_id):
                raise _conflict()
            if recorded is not None:
                if recorded.content != content:
                    raise _conflict()
                return recorded.result, False
            try:
                state, details = apply()
            except _Kept as stale:
                kept = stale.refusal
            else:
                result = self._commit_application(sender, request_id, content,
                                                  state, details)  # fmt: skip
        if kept is not None:
            raise kept
        return result, True

    def _commit_application(self, sender, request_id, content, state, d) -> dict:
        character_id = d["character_id"]
        authorization = d["authorization"]
        record_id = authorization["record_id"]
        revision = self.store.revision() + 1
        draft = self.store.character(character_id)
        draft_revision = draft.draft_revision + 1
        accepted = authorization["kind"] == "accepted_offer"
        if accepted:
            row = d["row"]
            result = {"proposal_id": row["proposal_id"], "state": "applied",
                      "offer_id": d["offer_id"], "record_id": record_id,
                      "revision": revision}  # fmt: skip
        else:
            result = {"character_id": character_id, "record_id": record_id,
                      "revision": revision, "draft_revision": draft_revision}  # fmt: skip
        self.store.record_command(sender, request_id, content, result, state)
        self.store.bump_draft(character_id)
        now = _now()
        self.store.query_write(
            "INSERT INTO override_records (record_id, character_id, kind, "
            "proposal_id, offer_id, host_player_id, requester_participant, intent, "
            "preview, preview_digest, receipt, reason, revision, draft_revision, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record_id, character_id, authorization["kind"],
             authorization["proposal_id"], authorization["offer_id"],
             authorization["host_player_id"],
             authorization["requester_participant_id"], _json(d["intent"]),
             _json(d["preview"]), d["preview_digest"], _json(d["receipt"]),
             None if accepted else authorization["reason"], revision,
             draft_revision, now),
        )  # fmt: skip
        if accepted:
            self._close_offer(d["offer_id"], "accepted", now, sender)
            self.store.query_write(
                "UPDATE host_requests SET state = 'applied', decided_at = ?, "
                "effect = ? WHERE proposal_id = ?",
                (now, _json({"record_id": record_id, "revision": revision}),
                 row["proposal_id"]),
            )  # fmt: skip
            self.store.notify_proposal(row["proposal_id"])
        else:
            self.store.audit(
                d["by"], "override",
                {"intent": d["intent"], "preview_digest": d["preview_digest"],
                 "reason": authorization["reason"], "record_id": record_id,
                 "request_id": request_id},
                target=character_id,
            )  # fmt: skip
            self.recheck(character_id)
        return result

    def _mutation(self, sender, request_id, kind, content, apply, check):
        """One host-mutation-recorded step (governance contract section 8):
        exact replay in `sender`'s namespace, else `apply(db)`; a `_Kept`
        refusal commits what it found and is then raised."""
        recorded = policies.canonical({"kind": kind, **content})
        kept = None
        with self.store.transaction() as db:
            if check is not None:
                check()
            if request_id is not None:
                found = self._mutation_row(sender, request_id)
                if found is None and self.store.recorded_command(sender, request_id):
                    raise _conflict()
                if found is not None:
                    if found[0] != recorded:
                        raise _conflict()
                    return json.loads(found[1]), False
            try:
                result = apply(db)
            except _Kept as stale:
                kept = stale.refusal
            else:
                if request_id is not None:
                    db.execute(
                        "INSERT INTO host_mutations (sender, request_id, kind, "
                        "content, result, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            sender,
                            request_id,
                            kind,
                            recorded,
                            json.dumps(result),
                            _now(),
                        ),
                    )
        if kept is not None:
            raise kept
        return result, True

    def _mutation_row(self, sender, request_id):
        rows = self.store.query(
            "SELECT content, result FROM host_mutations WHERE sender = ? AND "
            "request_id = ?",
            (sender, request_id),
        )
        return rows[0] if rows else None

    @staticmethod
    def _keep(refusal: Refusal, found_stale: bool):
        return _Kept(refusal) if found_stale else refusal

    # --- Internals: staleness (section 7) -----------------------------------

    def _settle_open_offer(self, runtime, row, campaign_host) -> bool:
        """At use: record the request's open offer stale if it no longer
        holds. Whether it did."""
        if row["state"] != "awaiting_player":
            return False
        for offer in self._offers("proposal_id = ? AND state = 'open'",
                                  (row["proposal_id"],)):  # fmt: skip
            reason, _ = self._offer_problem(runtime, offer, row, campaign_host)
            if reason is not None:
                self._make_stale(runtime, offer, row, reason)
                return True
        return False

    def _offer_problem(self, runtime, offer, row, campaign_host):
        """(reason, None) if the open offer no longer holds, else (None, its
        fresh preview): the draft, the module, the host, the preview."""
        character = self.store.character(row["target"]["id"])
        if (
            character is None
            or character.phase != "draft"
            or character.participant_id != row["requester_participant"]
        ):
            return "completed", None
        if _canonical(offer["module"]) != self._module(runtime):
            return "module_changed", None
        if campaign_host is not None and offer["host_player_id"] != campaign_host:
            return "host_changed", None
        try:
            preview, digest = self._preview(runtime, row["target"]["id"],
                                            offer["intent"])  # fmt: skip
        except Refusal:
            return "intent_invalid", None
        except Exception:
            logger.exception("Could not preview an open offer; it is stale")
            return "intent_invalid", None
        if digest != offer["preview_digest"]:
            return "preview_changed", None
        return None, preview

    def _make_stale(self, runtime, offer, row, reason) -> str:
        """Record an open offer stale, and its request back to `pending_host`
        (with a fresh preview) while its intent still previews, else
        `stale`. The request's new state."""
        now = _now()
        self._close_offer(offer["offer_id"], "stale", now, stale_reason=reason)
        state, fresh = "stale", None
        if reason != "completed":
            try:
                fresh = self._preview(runtime, row["target"]["id"], row["payload"])
            except Refusal:
                reason = "intent_invalid"
            except Exception:
                logger.exception("Could not preview a request; it is stale")
                reason = "intent_invalid"
        if fresh is not None:
            state = "pending_host"
            self.store.query_write(
                "UPDATE host_requests SET state = 'pending_host', preview = ?, "
                "preview_digest = ?, module = ?, stale_reason = ? "
                "WHERE proposal_id = ?",
                (_json(fresh[0]), fresh[1], self._module(runtime), reason,
                 row["proposal_id"]),
            )  # fmt: skip
        else:
            self.store.query_write(
                "UPDATE host_requests SET state = 'stale', decided_at = ?, "
                "stale_reason = ? WHERE proposal_id = ?",
                (now, reason, row["proposal_id"]),
            )
        self.store.notify_proposal(row["proposal_id"])
        return state

    def _close_request(self, row, state, now, *, stale_reason=None):
        for offer in self._offers("proposal_id = ? AND state = 'open'",
                                  (row["proposal_id"],)):  # fmt: skip
            self._close_offer(offer["offer_id"], state, now, stale_reason=stale_reason)
        self.store.query_write(
            "UPDATE host_requests SET state = ?, decided_at = ?, stale_reason = "
            "ifnull(?, stale_reason) WHERE proposal_id = ?",
            (state, now, stale_reason, row["proposal_id"]),
        )
        self.store.notify_proposal(row["proposal_id"])

    def _close_offer(self, offer_id, state, now, by=None, note=None, *,
                     stale_reason=None):  # fmt: skip
        self.store.query_write(
            "UPDATE override_offers SET state = ?, closed_at = ?, closed_by = ?, "
            "close_note = ?, stale_reason = ? WHERE offer_id = ?",
            (state, now, by, note, stale_reason, offer_id),
        )

    # --- Internals: rows ----------------------------------------------------

    def _owned_draft(self, participant_id, character_id):
        character = self.store.character(character_id)
        if (
            character is None
            or character.participant_id != participant_id
            or character.phase == "archived"
        ):
            raise _character_unavailable()
        if character.phase != "draft":
            raise Refusal("invalid_request", "Overrides change drafts only.")
        return character

    def _draft_for(self, viewer, character_id):
        """A draft the viewer may discover and preview on: its own, or any
        for the campaign host."""
        if viewer.host:
            character = self.store.character(character_id)
            if character is None or character.phase == "archived":
                raise _character_unavailable()
            if character.phase != "draft":
                raise Refusal("invalid_request", "Overrides change drafts only.")
            return character
        return self._owned_draft(viewer.participant_id, character_id)

    def _visible_request(self, viewer, proposal_id) -> dict:
        row = self._request_row(proposal_id)
        if row is None or not (
            viewer.host or row["requester_participant"] == viewer.participant_id
        ):
            raise _proposal_unavailable()
        return row

    def _visible_character(self, viewer, character_id):
        character = self.store.character(character_id)
        if character is None or not (
            viewer.host or character.participant_id == viewer.participant_id
        ):
            raise _character_unavailable()
        return character

    def _open_requests(self, where, parameters) -> list[dict]:
        ids = self.store.query(
            "SELECT proposal_id FROM host_requests WHERE kind = ? AND state IN "
            f"('pending_host', 'awaiting_player') AND {where} ORDER BY rowid",
            (KIND, *parameters),
        )
        return [self._request_row(i[0]) for i in ids]

    _REQUEST_COLUMNS = (
        "rowid", "proposal_id", "job_id", "requester_participant", "requester_player",
        "target", "kind", "payload", "explanation", "target_revision", "state",
        "created_at", "decided_by", "decided_at", "decision_note", "effect",
        "revises", "preview", "preview_digest", "stale_reason", "origin", "module",
    )  # fmt: skip

    def _request_row(self, proposal_id) -> dict | None:
        if not isinstance(proposal_id, str):
            return None
        rows = self.store.query(
            f"SELECT {', '.join(self._REQUEST_COLUMNS)} FROM host_requests "
            "WHERE proposal_id = ? AND kind = ?",
            (proposal_id, KIND),
        )
        return self._request_dict(rows[0]) if rows else None

    def _request_dict(self, values) -> dict:
        row = dict(zip(self._REQUEST_COLUMNS, values))
        for key in ("target", "payload", "effect", "preview"):
            row[key] = None if row[key] is None else json.loads(row[key])
        return row

    _OFFER_COLUMNS = (
        "offer_id", "proposal_id", "offer_revision", "origin", "intent", "preview",
        "preview_digest", "module", "host_player_id", "note", "draft_revision",
        "state", "created_at", "closed_at", "closed_by", "close_note", "stale_reason",
    )  # fmt: skip

    def _offers(self, where, parameters) -> list[dict]:
        rows = self.store.query(
            f"SELECT {', '.join(self._OFFER_COLUMNS)} FROM override_offers "
            f"WHERE {where} ORDER BY offer_revision",
            parameters,
        )
        return [self._offer_dict(r) for r in rows]

    def _offer_dict(self, values) -> dict:
        offer = dict(zip(self._OFFER_COLUMNS, values))
        for key in ("intent", "preview"):
            offer[key] = json.loads(offer[key])
        return offer

    def _offer_row(self, offer_id) -> dict | None:
        if not isinstance(offer_id, str):
            return None
        found = self._offers("offer_id = ?", (offer_id,))
        return found[0] if found else None

    def _last_offer(self, proposal_id) -> dict | None:
        found = self._offers("proposal_id = ?", (proposal_id,))
        return found[-1] if found else None

    _RECORD_COLUMNS = (
        "rowid", "record_id", "character_id", "kind", "proposal_id", "offer_id",
        "host_player_id", "requester_participant", "intent", "preview",
        "preview_digest", "receipt", "reason", "revision", "draft_revision",
        "created_at",
    )  # fmt: skip

    def _record_dict(self, values) -> dict:
        found = dict(zip(self._RECORD_COLUMNS, values))
        for key in ("intent", "preview", "receipt"):
            found[key] = None if found[key] is None else json.loads(found[key])
        return found

    def _record_row(self, record_id) -> dict | None:
        if not isinstance(record_id, str):
            return None
        rows = self.store.query(
            f"SELECT {', '.join(self._RECORD_COLUMNS)} FROM override_records "
            "WHERE record_id = ?",
            (record_id,),
        )
        return self._record_dict(rows[0]) if rows else None

    def _rows_after(self, table, where, parameters, order, after) -> list[dict]:
        if after is not None and (
            not isinstance(after, str) or not _CURSOR.fullmatch(after)
        ):
            raise Refusal(
                "invalid_request", "'after' is a cursor from an earlier page."
            )
        columns = {
            "host_requests": self._REQUEST_COLUMNS,
            "override_offers": ("rowid", *self._OFFER_COLUMNS),
            "override_records": self._RECORD_COLUMNS,
        }[table]
        rows = self.store.query(
            f"SELECT {', '.join(columns)} FROM {table} WHERE {where} AND {order} > ? "
            f"ORDER BY {order} LIMIT ?",
            (*parameters, -1 if after is None else int(after), PAGE_ITEMS + 1),
        )
        if table == "host_requests":
            return [self._request_dict(r) for r in rows]
        if table == "override_records":
            return [self._record_dict(r) for r in rows]
        return [{"rowid": r[0], **self._offer_dict(r[1:])} for r in rows]

    def _page(self, rows, summary, order, head) -> dict:
        """Up to 20 summaries in at most 32,768 result bytes for the whole
        reply, always one when any remain, and the next page's cursor."""
        page = {**head, "items": [], "next": None}
        for index, row in enumerate(rows[:PAGE_ITEMS]):
            item = summary(row)
            trial = {**page, "items": [*page["items"], item], "next": "0" * 18}
            if page["items"] and result_bytes(trial) > PAGE_BYTES:
                page["next"] = str(rows[index - 1][order])
                return page
            page["items"].append(item)
        if len(rows) > PAGE_ITEMS:
            page["next"] = str(rows[PAGE_ITEMS - 1][order])
        return _bounded(page, PAGE_BYTES, "a page")

    def _names(self) -> dict:
        return {p.participant_id: p.name for p in self.store.participants()}

    def _player_of(self, participant_id: str) -> str | None:
        rows = self.store.query(
            "SELECT player_id FROM host_links WHERE participant_id = ? "
            "AND unlinked_at IS NULL",
            (participant_id,),
        )
        return rows[0][0] if rows else None

    # --- Internals: views (section 10) --------------------------------------

    def _categories_of(self, row) -> list[str]:
        """The sorted categories of the qualities the request's intent names."""
        named = {c["quality_id"] for c in row["payload"]["changes"]}
        return sorted({
            a["category_id"] for a in (row["preview"] or {}).get("affected", [])
            if a["quality_id"] in named
        })  # fmt: skip

    def _request_summary(self, row, viewer, names, full=False) -> dict:
        cut = []
        requester = row["requester_participant"]
        name = names.get(requester)
        explanation, decision_note = row["explanation"], row["decision_note"]
        categories = self._categories_of(row)
        shown = categories if full else categories[:SUMMARY_CATEGORIES]
        if not full:
            if name is not None:
                name, was_cut = _excerpt(name, NAME_EXCERPT)
                cut += ["requester"] if was_cut else []
            explanation, was_cut = _excerpt(explanation, SUMMARY_EXCERPT)
            cut += ["explanation"] if was_cut else []
            if decision_note is not None:
                decision_note, was_cut = _excerpt(decision_note, SUMMARY_EXCERPT)
                cut += ["decision_note"] if was_cut else []
        last = self._last_offer(row["proposal_id"])
        summary = {
            "proposal_id": row["proposal_id"], "target": row["target"],
            "requester": {"participant_id": requester, "name": name},
            "state": row["state"], "revises": row["revises"],
            "categories": shown, "categories_more": len(categories) - len(shown),
            "explanation": explanation, "decision_note": decision_note,
            "stale_reason": row["stale_reason"], "created_at": row["created_at"],
            "decided_at": row["decided_at"],
            "record_id": (row["effect"] or {}).get("record_id"),
            "offer": None if last is None else self._offer_brief(last, viewer),
            "cut": cut,
        }  # fmt: skip
        if full:
            return summary
        return _bounded(summary, REQUEST_SUMMARY_BYTES, "a request summary")

    def _currency(self, offer, viewer) -> tuple[bool, str | None]:
        """(current, stale_reason): an open offer is current while its host
        is the campaign host, as every read reports at once (section 7)."""
        if offer["state"] != "open":
            return False, offer["stale_reason"]
        if viewer.campaign_host is not None and (
            offer["host_player_id"] != viewer.campaign_host
        ):
            return False, "host_changed"
        return True, None

    def _offer_brief(self, offer, viewer) -> dict:
        headline, cut = _excerpt(offer["preview"]["headline"], CONTEXT_EXCERPT)
        current, stale_reason = self._currency(offer, viewer)
        return {
            "offer_id": offer["offer_id"], "offer_revision": offer["offer_revision"],
            "origin": offer["origin"], "state": offer["state"], "current": current,
            "stale_reason": stale_reason, "headline": headline,
            "preview_digest": offer["preview_digest"],
            "cut": ["headline"] if cut else [],
        }  # fmt: skip

    def _offer_summary(self, offer, row, viewer, full=False) -> dict:
        headline, note = offer["preview"]["headline"], offer["note"]
        cut = []
        if not full:
            headline, was_cut = _excerpt(headline, SUMMARY_EXCERPT)
            cut += ["headline"] if was_cut else []
            if note is not None:
                note, was_cut = _excerpt(note, SUMMARY_EXCERPT)
                cut += ["note"] if was_cut else []
        current, stale_reason = self._currency(offer, viewer)
        summary = {
            "offer_id": offer["offer_id"], "offer_revision": offer["offer_revision"],
            "origin": offer["origin"], "state": offer["state"], "current": current,
            "stale_reason": stale_reason, "headline": headline,
            "preview_digest": offer["preview_digest"], "note": note,
            "created_at": offer["created_at"], "closed_at": offer["closed_at"],
            "cut": cut,
        }  # fmt: skip
        if full:
            return summary
        return _bounded(summary, OFFER_SUMMARY_BYTES, "an offer summary")

    def _record_summary(self, found) -> dict:
        headline = EXPIRY_HEADLINE if found["preview"] is None \
            else found["preview"]["headline"]  # fmt: skip
        headline, cut = _excerpt(headline, SUMMARY_EXCERPT)
        summary = {
            "record_id": found["record_id"], "character_id": found["character_id"],
            "kind": found["kind"], "proposal_id": found["proposal_id"],
            "offer_id": found["offer_id"], "revision": found["revision"],
            "draft_revision": found["draft_revision"],
            "created_at": found["created_at"], "headline": headline,
            "cut": ["headline"] if cut else [],
        }  # fmt: skip
        return _bounded(summary, RECORD_SUMMARY_BYTES, "a record summary")

    def _current(self, row) -> dict:
        """What the request's intent would do now (section 10), from module
        code only while the target is a draft under the request's module."""
        current = {"status": "not_checked", "reason": None, "preview": None,
                   "preview_digest": None, "problems": None, "problems_more": 0}  # fmt: skip
        runtime = self.store.runtime()
        character = self.store.character(row["target"]["id"])
        if character is None or character.phase != "draft":
            return {**current, "reason": "target_not_draft"}
        if row["module"] is None or _canonical(row["module"]) != self._module(runtime):
            return {**current, "reason": "module_changed"}
        try:
            preview, digest = self._preview(runtime, character.character_id,
                                            row["payload"])  # fmt: skip
        except Refusal as refusal:
            problems = refusal.extra.get("problems") or [refusal.text]
            shown, more = _problems(problems)
            return {**current, "status": "invalid", "problems": shown,
                    "problems_more": more}  # fmt: skip
        except Exception:
            logger.exception("Could not preview a request for a read")
            return {**current, "status": "invalid", "problems": [PREVIEW_FAILED]}
        status = "same" if digest == row["preview_digest"] else "changed"
        return {**current, "status": status, "preview": preview,
                "preview_digest": digest}  # fmt: skip


# --- Messages -------------------------------------------------------------


def message(kind: str, body: dict, request_id: str | None = None) -> dict:
    """A reply of type `kind` with `body`, and its request ID if any."""
    reply = {"version": PROTOCOL_VERSION, "type": kind, **body}
    if request_id is not None:
        reply["request_id"] = request_id
    return reply


def legacy_draft_refusal(store: GameStore, target) -> Refusal | None:
    """`override_required` for a legacy exception or operation request on a
    draft in an adopting game (section 6), else None."""
    if store.runtime().creation_overrides is None:
        return None
    if not isinstance(target, dict) or target.get("kind") != "character":
        return None
    character = store.character(target.get("id"))
    if character is None or character.phase != "draft":
        return None
    return Refusal(
        "override_required",
        "Changes to a draft are negotiated: ask with kind creation_override.",
    )


def legacy_draft_requests(store: GameStore) -> list[str]:
    """Legacy pending exception or operation requests whose target is a
    draft: what adoption supersedes."""
    found = []
    for proposal_id, target in store.query(
        "SELECT proposal_id, target FROM host_requests WHERE state = 'pending' "
        "AND kind IN ('exception', 'operation') ORDER BY rowid"
    ):
        target = None if target is None else json.loads(target)
        if isinstance(target, dict) and target.get("kind") == "character":
            character = store.character(target.get("id"))
            if character is not None and character.phase == "draft":
                found.append(proposal_id)
    return found


def adopt(store: GameStore) -> list[str]:
    """Inside the caller's write transaction, in an adopting game: every
    legacy pending exception or operation request on a draft `superseded`
    (section 6). Nothing is applied or converted. Their proposal IDs."""
    found = legacy_draft_requests(store)
    for proposal_id in found:
        store.query_write(
            "UPDATE host_requests SET state = 'superseded', decided_at = ?, "
            "decision_note = ? WHERE proposal_id = ?",
            (_now(), SUPERSEDED_LEGACY, proposal_id),
        )
    return found


# --- Helpers ---------------------------------------------------------------


def _target(character_id) -> dict:
    return {"kind": "character", "id": character_id}


def _json(value) -> str | None:
    return None if value is None else policies.canonical(value)


def _canonical(text: str) -> str:
    return policies.canonical(json.loads(text))


def _check_text(value, name: str):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_TEXT
        or result_bytes(value) > TEXT_BYTES
    ):
        raise Refusal(
            "invalid_request",
            f"'{name}' is 1 to {MAX_TEXT} characters and at most {TEXT_BYTES} "
            "bytes; shorten it.",
        )


def _excerpt(text: str, limit: int) -> tuple[str, bool]:
    """The longest prefix of `text`, in whole characters, of at most `limit`
    result bytes, and whether it was cut."""
    if result_bytes(text) <= limit:
        return text, False
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if result_bytes(text[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low], True


def _problems(problems: list) -> tuple[list, int]:
    """Whole problems, each excerpted, in at most 2,048 result bytes, and
    how many were left out."""
    shown = []
    for problem in problems:
        excerpt, _ = _excerpt(problem, PROBLEM_EXCERPT)
        if result_bytes([*shown, excerpt]) > PROBLEMS_BYTES:
            break
        shown.append(excerpt)
    return shown, len(problems) - len(shown)


def _bounded(view: dict, limit: int, what: str) -> dict:
    """`view`, or a host failure if it is over its bound: never cut."""
    if result_bytes(view) > limit:
        raise RuntimeError(f"{what} is over its {limit}-byte bound")
    return view


def _invalid_intent(problems) -> Refusal:
    shown, more = _problems(list(problems))
    return Refusal(
        "invalid_intent",
        "The module cannot do that as asked; nothing was stored.",
        {"problems": shown, "problems_more": more},
    )


def _stale_preview(preview, digest) -> Refusal:
    return Refusal(
        "stale_preview",
        "What that would do has changed; review the current preview.",
        {"preview": preview, "preview_digest": digest},
    )


def _conflict() -> Refusal:
    return Refusal(
        "request_conflict", "This request_id was already used for a different request."
    )


def _proposal_unavailable() -> Refusal:
    return Refusal(
        "proposal_unavailable", "That request is not available: unknown or not yours."
    )


def _offer_unavailable() -> Refusal:
    return Refusal(
        "offer_unavailable",
        "That offer is not available: unknown, not yours, or no longer open.",
    )


def _character_unavailable() -> Refusal:
    return Refusal("character_unavailable", "That character is not available to you.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
