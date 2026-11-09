"""Negotiated creation overrides through the referee's bridge (OVR-4, #176):
a guest asks and its job ends, the host decides in its own job while others
play, the guest accepts in a later job, denial and revision, the host's
direct override with no character, steps recovered after a crash, a fresh
session finding every request, the context's `overrides` block at its worst,
the prompt's rules, and one job through the real runner and bridge process.
The original Trailhead module and a scripted stand-in for Codex; no model.
docs/designs/creation_overrides_contract.md section 11."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tesserae.builtin import DICE_PROVIDER
from tesserae.campaign_rules import CampaignRules
from tesserae.credentials import new_token
from tesserae.host_requests import proposal_message
from tesserae.modules import Catalog, Category, result_bytes
from tesserae.overrides import Overrides
from tesserae.overrides import message as override_message
from tesserae.referee import runner
from tesserae.referee.bridge import TOOLS
from tesserae.referee.orchestrator import CodexReferee, job_prompt
from tesserae.referee.store import RESULT_BYTES, RefereeStore
from tesserae.referee.tools import TOOL_ARGUMENTS, NotCampaignHost
from tesserae.store import Actor, Decider, GameStore, ParticipantContext
from tesserae.trailhead import TRAILHEAD_MODULE

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_referee_characters import SCRIPTED, TRAILHEAD, Table, reply
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_referee_characters import SCRIPTED, TRAILHEAD, Table, reply

HOST = Decider("u_0123456789abcdef", "h_0123456789abcdef")
NEW_HOST = "u_fedcba9876543210"
ASTRAL = "\U0001f600"
MODIFIER = {"changes": [{"quality_id": "max_stamina_modifier", "action_id": "set",
                         "parameters": {"amount": 1}}]}  # fmt: skip
MODIFIER_2 = {"changes": [{"quality_id": "max_stamina_modifier", "action_id": "set",
                           "parameters": {"amount": 2}}]}  # fmt: skip
ROPE = {"changes": [{"quality_id": "starting_gear", "action_id": "grant",
                     "parameters": {"item": "climbing rope"}}]}  # fmt: skip
NOTHING = {"query": None}


def ask(step_id, intent, explanation="A mountain guide by trade.", revises=None,
        target=None) -> dict:  # fmt: skip
    payload = {"intent": intent}
    if revises is not None:
        payload["revises"] = revises
    return {"step_id": step_id, "kind": "creation_override", "scope": "target",
            "target": target, "payload": payload, "explanation": explanation}  # fmt: skip


class OverrideTable(Table):
    """Bob is the campaign host while `self.host_participant` says so; the
    stand-in registry answers each host tool call and who the host is."""

    def setUp(self):
        super().setUp()
        self.host_participant = self.bob.participant_id
        self.host_player = HOST.player_id
        self.tools.host_authority = self.authority
        self.tools.campaign_host = self.designation
        self.held = 0  # how many steps hold the registry guard now
        self.deferred = []  # transfers waiting for the guard, as the registry's do

    @contextmanager
    def designation(self, job):
        """The stand-in registry guard: while held, a transfer waits."""
        self.held += 1
        try:
            yield self.host_player
        finally:
            self.held -= 1
            if not self.held:
                for transfer in self.deferred:
                    transfer()
                self.deferred.clear()

    def transfer_to(self, participant, player):
        def transfer():
            self.host_participant, self.host_player = participant, player

        if self.held:
            self.deferred.append(transfer)
        else:
            transfer()

    @contextmanager
    def authority(self, job):
        if job.participant_id != self.host_participant:
            raise NotCampaignHost
        yield Decider(self.host_player, HOST.host_id, "referee", job.job_id)

    def job(self, who, character_id=None, mode="setup", text="Hello"):
        actor = Actor(character_id, mode) if character_id else Actor(None, "setup")
        return self.start(who, actor, text, private=True)

    def digest(self, token, character_id, intent) -> str:
        return self.ok(token, "override_preview", character_id=character_id,
                       intent=intent)["preview_digest"]  # fmt: skip

    def asked(self, character_id, intent=MODIFIER, step="p-1", **options) -> str:
        """Alice's creation job asks the host, and ends."""
        job, token = self.job(self.alice, character_id, "creation")
        proposal = self.ok(token, "propose_host_decision",
                           **ask(step, intent, **options))["proposal"]  # fmt: skip
        self.answer(job, ("sender", "I have asked the host; it waits for them."))
        return proposal["proposal_id"]

    def offered(self, proposal_id, character_id, intent=None, step="o-1"):
        """Bob's job, with no character, offers (or counteroffers)."""
        job, token = self.job(self.bob)
        offered_intent = intent or MODIFIER
        result = self.ok(
            token, "host_override_decide", step_id=step, proposal_id=proposal_id,
            decision="offer" if intent is None else "counteroffer",
            preview_digest=self.digest(token, character_id, offered_intent),
            intent=intent, note=None if intent is None else "Two is too many.",
        )["result"]  # fmt: skip
        self.answer(job, ("sender", "Offered; it is not applied until accepted."))
        return result


class ConversationTests(OverrideTable):
    def test_ask_wait_counteroffer_and_accept_in_a_later_job(self):
        ash = self.draft(name="Ash")
        wren = self.ready(self.bob, name="Wren")
        job, token = self.job(self.alice, ash, "creation", "Can Ash be tougher?")
        context = self.ok(token, "get_context")
        self.assertEqual(
            context["overrides"],
            {"categories": [{"category_id": "endurance", "policy": "approval_required"},
                            {"category_id": "supplies", "policy": "approval_required"}],
             "mine": [], "mine_more": 0, "recent": [], "recent_more": 0},
        )  # fmt: skip
        found = self.ok(token, "override_discover", character_id=None, query=None)
        self.assertEqual(len(found["items"]), 3)
        proposal = self.ok(token, "propose_host_decision",
                           **ask("p-1", MODIFIER_2))["proposal"]  # fmt: skip
        self.assertEqual(proposal["state"], "pending_host")
        # The job ends; nothing waits, nothing has changed.
        self.answer(job, ("sender", "Asked; it waits for the host."))
        self.assertIsNone(self.referee.running_job())
        self.assertNotIn("overrides", self.walker(ash))

        # Bob plays his own walker meanwhile.
        job, token = self.job(self.bob, wren, "play", "I push on.")
        self.assertTrue(self.act(token, "b-1", "trailhead.push")["ok"])
        context = self.ok(token, "get_context")
        [waiting] = context["overrides"]["pending_host"]  # a host job sees it
        self.assertEqual(waiting["proposal_id"], proposal["proposal_id"])
        self.answer(job, ("table", "Wren pushes on."))

        offer = self.offered(proposal["proposal_id"], ash, MODIFIER)  # a counteroffer
        self.assertEqual(offer["state"], "awaiting_player")
        self.assertNotIn("overrides", self.walker(ash))  # still not applied

        job, token = self.job(self.alice, ash, "creation", "Yes, I accept one.")
        [entry] = self.ok(token, "get_context")["overrides"]["mine"]
        self.assertEqual(
            (entry["state"], entry["offer"]["origin"], entry["offer"]["current"]),
            ("awaiting_player", "counteroffer", True),
        )
        exact = self.ok(token, "override_offer", offer_id=offer["offer_id"])["offer"]
        self.assertEqual(exact["note"], "Two is too many.")
        args = {"step_id": "r-1", "offer_id": offer["offer_id"], "response": "accept",
                "preview_digest": exact["preview_digest"], "note": None}  # fmt: skip
        accepted = self.ok(token, "override_respond", **args)
        self.assertEqual(accepted["result"]["state"], "applied")
        self.assertEqual(self.ok(token, "override_respond", **args), accepted)  # retry
        self.assertEqual(
            self.walker(ash)["overrides"]["max_stamina_modifier"]["amount"], 1
        )
        self.answer(job, ("sender", "Applied: Ash's maximum stamina modifier is +1."))

        # Her reply narrated it; her next job finds the verdict in recent.
        job, token = self.job(self.alice)
        context = self.ok(token, "get_context")
        [recent] = context["overrides"]["recent"]
        self.assertEqual(recent["state"], "applied")

    def test_denial_and_a_deliberate_revision(self):
        ash = self.draft(name="Ash")
        first = self.asked(ash)
        job, token = self.job(self.bob)
        denied = self.ok(token, "host_override_decide", step_id="d-1",
                         proposal_id=first, decision="deny", preview_digest=None,
                         intent=None, note="Not on this trail.")["result"]  # fmt: skip
        self.assertEqual(denied["state"], "denied")
        self.answer(job, ("sender", "Denied."))
        job, token = self.job(self.alice, ash, "creation")
        [recent] = self.ok(token, "get_context")["overrides"]["recent"]
        self.assertEqual(recent["state"], "denied")
        revised = self.ok(token, "propose_host_decision",
                          **ask("p-2", ROPE, revises=first))["proposal"]  # fmt: skip
        detail = self.ok(token, "override_detail", proposal_id=revised["proposal_id"])
        self.assertEqual(detail["request"]["revises"], first)

    def test_unsupported_and_blocked_requests_are_never_stored(self):
        rules = CampaignRules(self.store)
        imported, _ = rules.import_document(
            {"document_version": 2, "module": {"id": "trailhead", "version": "1",
             "state_version": 1}, "settings": {}, "exceptions": [], "guidance": None,
             "overrides": {"categories": {"supplies": "blocked"}}}, HOST,
        )  # fmt: skip
        rules.activate(imported["revision"], None, HOST)
        ash = self.draft(name="Ash")
        job, token = self.job(self.alice, ash, "creation")
        before = (self.store.query("SELECT count(*) FROM host_requests"), self.steps())
        cases = {
            "blocked": (ask("p-1", ROPE), "override_blocked"),
            "unknown": (ask("p-2", {"changes": [{"quality_id": "level", "action_id": "set",
                                                 "parameters": {}}]}),
                        "override_unsupported"),
            "invalid": (ask("p-3", {"changes": []}), "invalid_intent"),
            "a legacy exception on a draft": (
                {"step_id": "p-4", "kind": "exception", "scope": "target", "target": None,
                 "payload": {"starting_stamina_bonus": 1}, "explanation": "Please."},
                "override_required",
            ),
        }  # fmt: skip
        for name, (args, code) in cases.items():
            with self.subTest(name):
                self.refused(code, token, "propose_host_decision", **args)
        self.assertEqual(
            (self.store.query("SELECT count(*) FROM host_requests"), self.steps()),
            before,
        )

    def test_the_host_applies_directly_and_guests_have_no_host_tools(self):
        ash = self.draft(name="Ash")
        job, token = self.job(self.alice, ash, "creation")
        for tool, args in (
            ("host_override_apply", {"step_id": "h-1", "character_id": ash,
                                     "intent": ROPE, "preview_digest": "0" * 64,
                                     "reason": "Mine."}),
            ("host_override_withdraw", {"step_id": "h-2", "offer_id": "ho-000000000000",
                                        "note": "No."}),
        ):  # fmt: skip
            self.refused("not_campaign_host", token, tool, **args)
        self.answer(job, ("sender", "Only the host can."))
        job, token = self.job(self.bob)  # no character selected
        applied = self.ok(
            token, "host_override_apply", step_id="h-1", character_id=ash, intent=ROPE,
            preview_digest=self.digest(token, ash, ROPE), reason="The hut's gift.",
        )["result"]  # fmt: skip
        self.assertEqual(applied["type"], "override_applied")
        self.assertEqual(self.walker(ash)["gear"], ["climbing rope"])
        self.answer(job, ("sender", "Applied directly."))
        job, token = self.job(self.alice, ash, "creation")
        context = self.ok(token, "get_context")
        self.assertEqual(context["overrides"]["mine"], [])  # nothing awaits anyone
        history = self.ok(token, "override_history", character_id=None, after=None)
        self.assertEqual([r["kind"] for r in history["items"]], ["direct"])

    def test_withdrawal_and_transfer_reach_the_next_job(self):
        ash, birch = self.draft(name="Ash"), self.draft(name="Birch")
        first = self.asked(ash)
        offer = self.offered(first, ash)
        job, token = self.job(self.bob)
        withdrawn = self.ok(token, "host_override_withdraw", step_id="w-1",
                            offer_id=offer["offer_id"], note="I misread it.")["result"]  # fmt: skip
        self.assertEqual(withdrawn["state"], "withdrawn")
        self.answer(job, ("sender", "Withdrawn."))
        second = self.asked(birch, step="p-2")
        offer = self.offered(second, birch, step="o-2")
        # The registry moves the campaign to Alice.
        self.host_participant, self.host_player = self.alice.participant_id, NEW_HOST
        job, token = self.job(self.alice, birch, "creation")
        context = self.ok(token, "get_context")
        self.assertEqual(
            (context["overrides"]["mine"][0]["offer"]["current"],
             context["overrides"]["mine"][0]["offer"]["stale_reason"]),
            (False, "host_changed"),
        )  # fmt: skip
        self.refused("offer_stale", token, "override_respond", step_id="r-1",
                     offer_id=offer["offer_id"], response="accept",
                     preview_digest=offer_digest(self.store, offer["offer_id"]),
                     note=None)  # fmt: skip
        self.assertEqual(
            Overrides(self.store)._request_row(second)["state"], "pending_host"
        )
        self.answer(job, ("sender", "That offer is no longer good."))
        # Bob, no longer host, has no host tools.
        job, token = self.job(self.bob)
        self.refused("not_campaign_host", token, "host_override_decide", step_id="o-9",
                     proposal_id=second, decision="deny", preview_digest=None,
                     intent=None, note="No.")  # fmt: skip


def offer_digest(store, offer_id) -> str:
    return Overrides(store)._offer_row(offer_id)["preview_digest"]


class RecoveryTests(OverrideTable):
    def crash_recording(self, token, tool, **args):
        """The step commits, but its journal entry is never settled."""
        with mock.patch.object(RefereeStore, "record_step", side_effect=OSError):
            response = self.call(token, tool, **args)
        self.assertEqual(response["error"]["code"], "internal_error")

    def test_respond_and_host_steps_are_recovered_where_they_are_recorded(self):
        ash, birch = self.draft(name="Ash"), self.draft(name="Birch")
        cedar, dove = self.draft(name="Cedar"), self.draft(name="Dove")
        accepted_offer = self.offered(self.asked(ash), ash)
        declined_offer = self.offered(self.asked(birch, step="p-2"), birch, step="o-2")
        withdrawn_offer = self.offered(self.asked(cedar, step="p-3"), cedar, step="o-3")
        waiting = self.asked(dove, step="p-4")
        job, token = self.job(self.alice)
        guest_job = job.job_id
        self.crash_recording(token, "override_respond", step_id="r-1",
                             offer_id=accepted_offer["offer_id"], response="accept",
                             preview_digest=offer_digest(self.store,
                                                         accepted_offer["offer_id"]),
                             note=None)  # fmt: skip
        self.crash_recording(token, "override_respond", step_id="r-2",
                             offer_id=declined_offer["offer_id"], response="decline",
                             preview_digest=None, note="Not now.")  # fmt: skip
        self.fail(job)
        job, token = self.job(self.bob)
        host_job = job.job_id
        self.crash_recording(token, "host_override_apply", step_id="h-1",
                             character_id=birch, intent=ROPE,
                             preview_digest=self.digest(token, birch, ROPE),
                             reason="A gift.")  # fmt: skip
        self.crash_recording(token, "host_override_withdraw", step_id="h-2",
                             offer_id=withdrawn_offer["offer_id"], note="Withdrawn.")  # fmt: skip
        self.crash_recording(token, "host_override_decide", step_id="h-3",
                             proposal_id=waiting, decision="offer",
                             preview_digest=self.digest(token, dove, MODIFIER),
                             intent=None, note=None)  # fmt: skip
        self.fail(job)
        settled = {s.step_id: s for s in self.referee.reconcile_steps()}
        self.assertEqual({s.status for s in settled.values()}, {"committed"})
        reads = Overrides(self.store)
        record = reads._request_row(reads._offer_row(accepted_offer["offer_id"])
                                    ["proposal_id"])["effect"]  # fmt: skip
        [direct] = [r for r in reads.history(reads_host(self), birch)["items"]]
        [new_offer] = reads.offers(reads_host(self), waiting)["items"]
        # Each result is the whole reply the tool would have given.
        expected = {
            "r-1": proposal_message(
                {"proposal_id": reads._offer_row(accepted_offer["offer_id"])["proposal_id"],
                 "state": "applied", "offer_id": accepted_offer["offer_id"],
                 "record_id": record["record_id"], "revision": record["revision"]},
                f"ref:{guest_job}:r-1"),
            "r-2": proposal_message(
                {"proposal_id": reads._offer_row(declined_offer["offer_id"])["proposal_id"],
                 "state": "declined", "offer_id": declined_offer["offer_id"]},
                f"ref:{guest_job}:r-2"),
            "h-1": override_message(
                "override_applied",
                {"character_id": birch, "record_id": direct["record_id"],
                 "revision": direct["revision"], "draft_revision": direct["draft_revision"]},
                f"ref:{host_job}:h-1"),
            "h-2": proposal_message(
                {"proposal_id": reads._offer_row(withdrawn_offer["offer_id"])["proposal_id"],
                 "state": "withdrawn", "offer_id": withdrawn_offer["offer_id"]},
                f"ref:{host_job}:h-2"),
            "h-3": proposal_message(
                {"proposal_id": waiting, "state": "awaiting_player",
                 "offer_id": new_offer["offer_id"], "offer_revision": 1},
                f"ref:{host_job}:h-3"),
        }  # fmt: skip
        self.assertEqual({k: settled[k].result for k in expected}, expected)
        # Each later job of its owner is told; nothing ran twice.
        job, token = self.job(self.alice)
        results = {
            r["step_id"]: r for r in self.ok(token, "get_context")["earlier_results"]
        }
        self.assertEqual(set(results), {"r-1", "r-2"})
        self.answer(job, ("sender", "Ash's change applied; Birch's offer declined."))
        job, token = self.job(self.bob)
        results = [
            r["step_id"] for r in self.ok(token, "get_context")["earlier_results"]
        ]
        self.assertEqual(results, ["h-1", "h-2", "h-3"])
        self.assertEqual(
            self.store.query("SELECT count(*) FROM override_records")[0][0], 2
        )


def reads_host(case):
    from tesserae.overrides import Viewer

    return Viewer(case.bob.participant_id, True, case.host_player)


class RaceAndReplayTests(OverrideTable):
    def test_an_acceptance_holds_the_designation_through_its_commit(self):
        ash, birch = self.draft(name="Ash"), self.draft(name="Birch")
        first = self.offered(self.asked(ash), ash)
        second = self.offered(self.asked(birch, step="p-2"), birch, step="o-2")
        original = Overrides._offer_problem

        def transfer_midway(reads, runtime, offer, row, campaign_host):
            # The registry tries to move the campaign to Alice right here.
            self.assertTrue(self.held)
            self.transfer_to(self.alice.participant_id, NEW_HOST)
            return original(reads, runtime, offer, row, campaign_host)

        job, token = self.job(self.alice)
        with mock.patch.object(Overrides, "_offer_problem", transfer_midway):
            accepted = self.ok(token, "override_respond", step_id="r-1",
                               offer_id=first["offer_id"], response="accept",
                               preview_digest=offer_digest(self.store, first["offer_id"]),
                               note=None)  # fmt: skip
        # The acceptance was ordered first: it applied, then the transfer.
        self.assertEqual(accepted["result"]["state"], "applied")
        self.assertEqual(self.host_player, NEW_HOST)
        # The next acceptance sees the new designation: the former host's
        # offer is stale.
        self.refused("offer_stale", token, "override_respond", step_id="r-2",
                     offer_id=second["offer_id"], response="accept",
                     preview_digest=offer_digest(self.store, second["offer_id"]),
                     note=None)  # fmt: skip

    def test_a_retried_request_returns_its_record_whatever_has_changed(self):
        ash = self.draft(name="Ash")
        job, token = self.job(self.alice)  # setup: names its target
        args = ask("p-1", MODIFIER, target={"kind": "character", "id": ash})
        proposal = self.ok(token, "propose_host_decision", **args)
        archived, committed = self.store.archive_character(self.alice, self.rid(), ash)
        self.assertTrue(committed, archived)
        self.assertEqual(self.ok(token, "propose_host_decision", **args), proposal)
        changed = dict(args, explanation="Something else.")
        self.refused("request_conflict", token, "propose_host_decision", **changed)


class FreshSessionTests(OverrideTable):
    def test_a_fresh_session_finds_every_request_and_verdict(self):
        drafts = [self.draft(name=f"W{i}") for i in range(7)]
        asked = [self.asked(d, step=f"p-{i}") for i, d in enumerate(drafts)]
        job, token = self.job(self.bob)
        for i, proposal_id in enumerate(asked[:3]):
            self.ok(token, "host_override_decide", step_id=f"d-{i}",
                    proposal_id=proposal_id, decision="deny", preview_digest=None,
                    intent=None, note=f"Denied {i}.")  # fmt: skip
        self.answer(job, ("sender", "Three denied."))
        job, token = self.job(self.alice)
        block = self.ok(token, "get_context")["overrides"]
        self.assertEqual((len(block["mine"]), block["mine_more"]), (3, 1))
        self.assertEqual((len(block["recent"]), block["recent_more"]), (2, 1))
        listed, after = [], None
        while True:
            page = self.ok(token, "override_requests", character_id=None,
                           states=None, after=after)  # fmt: skip
            listed += page["items"]
            if page["next"] is None:
                break
            after = page["next"]
        self.assertEqual([r["proposal_id"] for r in listed], asked)
        denied = [r for r in listed if r["state"] == "denied"]
        self.assertEqual([r["decision_note"] for r in denied],
                         ["Denied 0.", "Denied 1.", "Denied 2."])  # fmt: skip

    def test_a_request_that_no_longer_previews_is_explained_from_its_stored_preview(
        self,
    ):
        ash = self.draft(name="Ash")
        raw = json.dumps({"version": 1, "type": "command", "request_id": "g-1",
                          "expected_revision": self.store.revision(),
                          "command": "trailhead.set_gear",
                          "args": {"character_id": ash, "gear": ["map"]}})  # fmt: skip
        self.store.execute_host_command(HOST, raw)
        remove = {"changes": [{"quality_id": "starting_gear", "action_id": "remove",
                               "parameters": {"carried": "map"}}]}  # fmt: skip
        proposal_id = self.asked(ash, remove)
        raw = json.dumps({"version": 1, "type": "command", "request_id": "g-2",
                          "expected_revision": self.store.revision(),
                          "command": "trailhead.set_gear",
                          "args": {"character_id": ash, "gear": []}})  # fmt: skip
        self.store.execute_host_command(HOST, raw)
        job, token = self.job(self.alice)
        detail = self.ok(token, "override_detail", proposal_id=proposal_id)
        self.assertEqual(detail["request"]["preview"]["plan"], remove)
        self.assertEqual(detail["request"]["current"]["status"], "invalid")
        self.assertEqual(detail["request"]["state"], "pending_host")
        self.assertIsNone(detail["offer"])  # nothing to accept


class WorstContextTests(OverrideTable):
    """The `overrides` block at its worst in a composed get_context: 16
    categories, every list full, 12-byte characters everywhere."""

    RUNTIME = None  # set below

    def test_the_block_fits_its_bound_inside_get_context(self):
        drafts = [self.draft(name=f"W{i}") for i in range(8)]
        text = ASTRAL * 341  # 4,094 result bytes
        intent = {"changes": [{"quality_id": f"q{i}", "action_id": "a",
                               "parameters": {}} for i in range(16)]}  # fmt: skip
        asked = [self.asked(d, intent, step=f"p-{i}", explanation=text)
                 for i, d in enumerate(drafts)]  # fmt: skip
        job, token = self.job(self.bob)
        for i, proposal_id in enumerate(asked[:3]):
            self.ok(token, "host_override_decide", step_id=f"d-{i}",
                    proposal_id=proposal_id, decision="deny", preview_digest=None,
                    intent=None, note="No.")  # fmt: skip
        for i, (proposal_id, character_id) in enumerate(zip(asked[3:6], drafts[3:6])):
            self.ok(token, "host_override_decide", step_id=f"o-{i}",
                    proposal_id=proposal_id, decision="offer",
                    preview_digest=self.digest(token, character_id, intent),
                    intent=None, note=None)  # fmt: skip
        self.answer(job, ("sender", "Decided."))
        # Alice, the requester of them all, becomes the campaign host.
        self.host_participant, self.host_player = self.alice.participant_id, NEW_HOST
        job, token = self.job(self.alice, drafts[0], "creation", ASTRAL * 1000)
        context = self.ok(token, "get_context")
        block = context["overrides"]
        self.assertEqual(
            [len(block[k]) for k in ("categories", "mine", "recent", "pending_host")],
            [16, 3, 2, 2],
        )
        self.assertLessEqual(result_bytes(block), 10240)
        self.assertLessEqual(result_bytes(context), RESULT_BYTES)
        for entry in block["mine"] + block["recent"] + block["pending_host"]:
            self.assertLessEqual(result_bytes(entry), 1024)


def _worst_runtime():
    categories = tuple(
        Category(f"c{i:02d}" + "c" * 45, "C" * 80, "") for i in range(16)
    )

    def preview(state, character_id, intent, policy):
        return {
            "plan": intent, "headline": ASTRAL * 49,
            "affected": [{"quality_id": c["quality_id"], "category_id": categories[i].id}
                         for i, c in enumerate(intent["changes"])],
            "effects": [], "waivers": [], "allowances": [], "later_steps": [],
            "consequences": [], "limitations": [], "dependencies": {},
        }  # fmt: skip

    capability = replace(TRAILHEAD_MODULE.creation_overrides, categories=categories,
                         preview=preview)  # fmt: skip
    module = replace(TRAILHEAD_MODULE, creation_overrides=capability)
    return Catalog([module], [DICE_PROVIDER]).resolve("trailhead")


WorstContextTests.RUNTIME = _worst_runtime()


class SchemaAndPromptTests(unittest.TestCase):
    def test_every_tool_schema_names_exactly_its_arguments(self):
        for tool in TOOLS:
            with self.subTest(tool["name"]):
                self.assertEqual(set(tool["inputSchema"]["required"]),
                                 set(TOOL_ARGUMENTS[tool["name"]]))  # fmt: skip

    def test_the_prompt_says_how_overrides_are_asked_offered_and_accepted(self):
        from tesserae.referee.store import Job

        job = Job("j-1", "p_1", "r-1", "Hi", False, "running", "h", 0, None, False,
                  "c_0123456789abcdef", "creation")  # fmt: skip
        character = mock.Mock(name="Ash", draft_revision=2)
        character.name = "Ash"
        guest = job_prompt(job, "Alice", 3, new_session=False, previous=None,
                           character=character, governed=True, overrides=True)  # fmt: skip
        for phrase in ("propose_host_decision (kind creation_override)",
                       "never say approved or granted", "override_respond",
                       "page override_requests", "expire when the character is approved"):  # fmt: skip
            self.assertIn(phrase, guest)
        self.assertNotIn("host_override_apply", guest)
        host = job_prompt(job, "Alice", 3, new_session=False, previous=None,
                          character=character, governed=True, host=True,
                          overrides=True)  # fmt: skip
        self.assertIn("host_override_apply", host)
        self.assertIn("former host", host)
        plain = job_prompt(job, "Alice", 3, new_session=False, previous=None,
                           character=character, governed=True)  # fmt: skip
        self.assertNotIn("creation_override", plain)


class ScriptedRunnerTests(unittest.IsolatedAsyncioTestCase):
    """One request through the real runner and bridge process, with the
    scripted stand-in for Codex."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir="/tmp", prefix="to")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.folder = runner.setup(root / "r")
        self.fake = root / "f"
        self.fake.mkdir()
        patch = mock.patch.dict(os.environ, {"FAKE_REFEREE_DIR": str(self.fake)})
        patch.start()
        self.addCleanup(patch.stop)
        self.path = root / "trail.sqlite3"
        token = new_token()
        with GameStore.create(self.path, TRAILHEAD) as store:
            self.game_id = store.game_id
            store.add_participant("Alice", token)
            RefereeStore(store).enable()
            self.alice = store.authenticate(token)
            created, _ = store.create_character(self.alice, "c-1", "Ash")
            self.draft = created["result"]["character"]["character_id"]

    def open(self):
        return GameStore(self.path, randint=lambda low, high: 2, runtime=TRAILHEAD)

    async def test_a_creation_job_asks_and_ends(self):
        script = {
            "calls": [
                {"tool": "override_discover", "args": {"character_id": None,
                                                       "query": {"category_id": "supplies"}}},
                {"tool": "propose_host_decision", "args": ask("p-1", ROPE)},
            ],
            "reply": reply(("sender", "Asked; it waits for the host. {results}"),
                           ask=[(self.draft, "Anything else?")]),
        }  # fmt: skip
        (self.fake / "script.json").write_text(json.dumps([script]))
        referee = CodexReferee(self.folder, self.game_id,
                               codex=(sys.executable, str(SCRIPTED)), timeout=30,
                               grace=1, lock_retry=0.1)  # fmt: skip
        referee.open("run-1", self.open, None)
        self.addCleanup(referee.close)
        with self.open() as store:
            job = RefereeStore(store).submit(
                ParticipantContext(self.alice.participant_id,
                                   credential_id=self.alice.credential_id),
                "a-1", "Can Ash carry a rope?", False, "run-1",
                actor=Actor(self.draft, "creation"),
            ).job  # fmt: skip
        answer = await asyncio.wait_for(referee(job), 30)
        with self.open() as store:
            RefereeStore(store).publish_reply(job.job_id, answer)
            [row] = store.query("SELECT state, origin, job_id FROM host_requests")
            self.assertEqual(row, ("pending_host", "referee", job.job_id))
            self.assertNotIn("gear", store.inspect_state()["walkers"][self.draft])
        [run] = [json.loads(line)
                 for line in (self.fake / "log.jsonl").read_text().splitlines()]  # fmt: skip
        self.assertIn("propose_host_decision (kind creation_override)", run["prompt"])
        self.assertIn('"state": "pending_host"', answer["messages"][0]["text"])


if __name__ == "__main__":
    unittest.main()
