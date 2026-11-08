"""Negotiated creation overrides in a game's own store (OVR-3, #175): the
schema 10 records and their upgrade, every step of the negotiation,
acceptance's atomicity, exact retries and conflicts, the races the write
transaction serializes, staleness by preview rather than revision, host
transfer, withdrawal, archive, unlink and completion, and the private,
bounded reads. The original Trailhead module, no network, no model.
docs/designs/creation_overrides_contract.md sections 5 to 10."""

import json
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from tesserae import overrides as overrides_module
from tesserae.builtin import DICE_PROVIDER
from tesserae.host_requests import HostRequests
from tesserae.modules import Catalog, Outcome, result_bytes
from tesserae.overrides import Overrides, Viewer
from tesserae.referee.store import RefereeStore
from tesserae.store import Actor, Decider, GameStore, Refusal
from tesserae.trailhead import TRAILHEAD_MODULE

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_characters import (
        LEGACY_TRAILHEAD,
        TRAILHEAD,
        CharacterCase,
        as_schema_9,
        command,
        new_request,
    )
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_characters import (
        LEGACY_TRAILHEAD,
        TRAILHEAD,
        CharacterCase,
        as_schema_9,
        command,
        new_request,
    )

HOST = Decider("u_0123456789abcdef", "h_0123456789abcdef")
NEW_HOST = Decider("u_fedcba9876543210", "h_0123456789abcdef")
ASTRAL = "\U0001f600"  # 12 result bytes once escaped


def change(quality_id, action_id, **parameters) -> dict:
    return {"quality_id": quality_id, "action_id": action_id, "parameters": parameters}


def intent(*changes) -> dict:
    return {"changes": list(changes)}


ROLL_2 = intent(change("stamina_roll", "replace", value=2))
ROLL_3 = intent(change("stamina_roll", "replace", value=3))
ROPE = intent(change("starting_gear", "grant", item="climbing rope"))
PICKS = intent(change("starting_gear", "allow", count=2))
GUIDE = "A mountain guide by trade."
TABLES = ("host_requests", "override_offers", "override_records", "host_mutations",
          "host_audit", "commands", "characters", "module", "game")  # fmt: skip


def with_overrides(**hooks):
    capability = replace(TRAILHEAD_MODULE.creation_overrides, **hooks)
    module = replace(TRAILHEAD_MODULE, creation_overrides=capability)
    return Catalog([module], [DICE_PROVIDER]).resolve("trailhead")


class NegotiationCase(CharacterCase):
    """Alice's drafts in a Trailhead game whose campaign host is HOST (a
    lobby player with no participant here); the guard's answers are
    stood in by Viewers."""

    def setUp(self):
        super().setUp()
        self.notices = []
        self.store.on_proposal = self.notices.append
        self.overrides = Overrides(self.store)
        self.campaign_host = HOST.player_id

    # Who acts, as the guard would say.

    def guest(self, context=None) -> Viewer:
        context = context or self.alice
        return Viewer(context.participant_id, False, self.campaign_host)

    def host(self) -> Viewer:
        return Viewer(self.bob.participant_id, True, self.campaign_host)

    # Steps.

    def request(self, character_id, wanted=ROLL_2, *, request_id=None,
                context=None, revises=None) -> dict:  # fmt: skip
        result, applied = self.overrides.request(
            self.guest(context), request_id or new_request(), character_id, wanted,
            GUIDE, revises,
        )  # fmt: skip
        self.assertTrue(applied)
        return result

    def digest(self, character_id, wanted) -> str:
        return self.overrides.preview(self.host(), character_id, wanted)[
            "preview_digest"
        ]

    def offer(self, proposal_id, wanted=None, *, by=HOST, note=None,
              request_id=None) -> dict:  # fmt: skip
        row = self.overrides._request_row(proposal_id)
        offered = row["payload"] if wanted is None else wanted
        decision = "offer" if wanted is None else "counteroffer"
        if decision == "counteroffer" and note is None:
            note = "Three is what the trail allows."
        result, _ = self.overrides.decide(
            by, self.host(), request_id or new_request(), proposal_id, decision,
            self.digest(row["target"]["id"], offered), wanted, note,
        )  # fmt: skip
        return result

    def accept(self, offer_id, *, request_id=None, context=None, digest=None):
        offer = self.overrides._offer_row(offer_id)
        return self.overrides.respond(
            self.guest(context), request_id or new_request(), offer_id, "accept",
            digest or offer["preview_digest"],
        )  # fmt: skip

    def refused(self, call, *args, **kwargs) -> Refusal:
        with self.assertRaises(Refusal) as caught:
            call(*args, **kwargs)
        return caught.exception

    def state_of(self, proposal_id) -> str:
        return self.overrides._request_row(proposal_id)["state"]

    def offer_state(self, offer_id) -> str:
        return self.overrides._offer_row(offer_id)["state"]

    def walker(self, character_id) -> dict:
        return self.store.inspect_state()["walkers"][character_id]

    def tables(self) -> dict:
        """Every record an operation might touch."""
        return {
            t: self.store.query(f"SELECT * FROM {t} ORDER BY rowid") for t in TABLES
        }

    def host_command(self, name, args, by=HOST):
        raw = json.dumps({"version": 1, "type": "command", "request_id": new_request(),
                          "expected_revision": self.store.revision(), "command": name,
                          "args": args})  # fmt: skip
        reply, committed = self.store.execute_host_command(by, raw)
        self.assertTrue(committed, reply)
        return reply

    def creation_step(self, character_id, name, args, context=None):
        reply, committed = self.run_command(
            context or self.alice, new_request(), name, args,
            Actor(character_id, "creation"),
        )  # fmt: skip
        self.assertTrue(committed, reply)


class StepTests(NegotiationCase):
    def test_a_verdict_changes_nothing_and_acceptance_applies_once(self):
        ash = self.draft()  # keen, rolled 4
        before = self.store.inspect_state()
        requested = self.request(ash)
        self.assertEqual(requested["state"], "pending_host")
        self.assertEqual(self.notices, [requested["proposal_id"]])
        offered = self.offer(requested["proposal_id"])
        self.assertEqual(
            (offered["state"], offered["offer_revision"]), ("awaiting_player", 1)
        )
        # Neither the request nor the verdict changed the draft or the game.
        self.assertEqual(self.store.inspect_state(), before)
        draft_revision = self.record(ash).draft_revision
        revision = self.store.revision()
        draws = self.draws.call_count

        result, applied = self.accept(offered["offer_id"], request_id="a-1")
        self.assertTrue(applied)
        self.assertEqual(
            result,
            {"proposal_id": requested["proposal_id"], "state": "applied",
             "offer_id": offered["offer_id"], "record_id": result["record_id"],
             "revision": revision + 1},
        )  # fmt: skip
        walker = self.walker(ash)
        self.assertEqual((walker["rolled"], walker["overrides"]["stamina_roll"]["value"]),
                         (4, 2))  # fmt: skip
        self.assertEqual(
            walker["overrides"]["stamina_roll"]["by"],
            {"kind": "accepted_offer", "record_id": result["record_id"]},
        )
        self.assertEqual(self.store.revision(), revision + 1)
        self.assertEqual(self.record(ash).draft_revision, draft_revision + 1)
        self.assertEqual(self.draws.call_count, draws)  # nothing rolled
        [history] = self.store.inspect_history()[-1:]
        self.assertEqual(
            (history.participant_id, history.content["type"]),
            (self.alice.participant_id, "override_respond"),
        )
        self.assertEqual(self.state_of(requested["proposal_id"]), "applied")
        self.assertEqual(self.offer_state(offered["offer_id"]), "accepted")
        # An exact retry is the recorded reply, even later; nothing twice.
        tables = self.tables()
        self.assertEqual(self.accept(offered["offer_id"], request_id="a-1"),
                         (result, False))  # fmt: skip
        self.assertEqual(self.tables(), tables)
        # The same ID for anything else is a conflict.
        conflict = self.refused(
            self.overrides.respond, self.guest(), "a-1", offered["offer_id"], "decline"
        )
        self.assertEqual(conflict.code, "request_conflict")
        # Whole-sheet approval stays separate: a shown revision is stale.
        reply, _ = self.store.complete_character(self.alice, "k-1", ash, draft_revision)
        self.assertEqual(reply["code"], "stale_draft")

    def test_a_counteroffer_replaces_the_open_offer(self):
        ash = self.draft()
        requested = self.request(ash)
        first = self.offer(requested["proposal_id"])
        counter = self.offer(requested["proposal_id"], ROLL_3)
        self.assertEqual(counter["offer_revision"], 2)
        self.assertEqual(self.offer_state(first["offer_id"]), "superseded")
        self.assertEqual(self.refused(self.accept, first["offer_id"]).code,
                         "offer_unavailable")  # fmt: skip
        self.accept(counter["offer_id"])
        self.assertEqual(self.walker(ash)["overrides"]["stamina_roll"]["value"], 3)
        offers = self.overrides.offers(self.guest(), requested["proposal_id"])
        self.assertEqual(
            [(o["offer_revision"], o["origin"], o["state"]) for o in offers["items"]],
            [(1, "offer", "superseded"), (2, "counteroffer", "accepted")],
        )

    def test_denial_decline_withdrawal_cancel_and_revision(self):
        ash = self.draft()
        denied = self.request(ash)
        result, _ = self.overrides.decide(HOST, self.host(), "d-1",
                                          denied["proposal_id"], "deny", None, None,
                                          "Not on this trail.")  # fmt: skip
        self.assertEqual(result["state"], "denied")
        again = self.refused(self.offer, denied["proposal_id"])
        self.assertEqual(
            (again.code, again.extra["state"]), ("proposal_decided", "denied")
        )
        # A deliberate revision of the denied request.
        revised = self.request(ash, ROLL_3, revises=denied["proposal_id"])
        self.assertEqual(
            self.overrides.detail(self.guest(), revised["proposal_id"])["request"][
                "revises"
            ],
            denied["proposal_id"],
        )
        offered = self.offer(revised["proposal_id"])
        result, _ = self.overrides.respond(self.guest(), "n-1", offered["offer_id"],
                                           "decline", None, "Not that.")  # fmt: skip
        self.assertEqual(result["state"], "declined")
        withdrawn = self.request(ash)
        offered = self.offer(withdrawn["proposal_id"])
        result, _ = self.overrides.withdraw(HOST, self.host(), "w-1",
                                            offered["offer_id"], "I misread it.")  # fmt: skip
        self.assertEqual(result["state"], "withdrawn")
        self.assertEqual(self.refused(self.accept, offered["offer_id"]).code,
                         "offer_unavailable")  # fmt: skip
        # Cancel, waiting for the host and with an open offer.
        for offered_first in (False, True):
            asked = self.request(ash)
            if offered_first:
                offer_id = self.offer(asked["proposal_id"])["offer_id"]
            result, _ = HostRequests(self.store).cancel(
                self.alice.participant_id, new_request(), asked["proposal_id"]
            )
            self.assertEqual(result["state"], "cancelled")
            if offered_first:
                self.assertEqual(self.offer_state(offer_id), "cancelled")
        self.assertNotIn("overrides", self.walker(ash))  # nothing ever applied

    def test_a_new_request_supersedes_the_open_one_and_its_offer(self):
        ash = self.draft()
        first = self.request(ash)
        offer_id = self.offer(first["proposal_id"])["offer_id"]
        second = self.request(ash, ROLL_3)
        self.assertEqual(self.state_of(first["proposal_id"]), "superseded")
        self.assertEqual(self.offer_state(offer_id), "superseded")
        self.assertEqual(self.state_of(second["proposal_id"]), "pending_host")

    def test_refusals_store_nothing(self):
        ash = self.draft()
        mara = self.draft(self.bob, name="Mara")
        tables = self.tables()
        cases = {
            "the host asking itself": (
                lambda: self.overrides.request(self.host(), "q-1", ash, ROLL_2, GUIDE),
                "invalid_request",
            ),
            "another's draft": (lambda: self.request(mara), "character_unavailable"),
            "an invalid intent": (
                lambda: self.request(ash, intent(change("stamina_roll", "replace",
                                                        value=9))),
                "invalid_intent",
            ),
            "an unknown quality": (
                lambda: self.request(ash, intent(change("level", "set"))),
                "override_unsupported",
            ),
            "no explanation": (
                lambda: self.overrides.request(self.guest(), "q-2", ash, ROLL_2, " "),
                "invalid_request",
            ),
            "a stranger's revision": (
                lambda: self.request(ash, revises="hp-000000000000"),
                "proposal_unavailable",
            ),
        }  # fmt: skip
        for name, (call, code) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.refused(call).code, code)
        self.assertEqual(self.tables(), tables)
        self.assertEqual(self.notices, [])

    def test_category_policy_limits_requests_never_the_host(self):
        from tesserae.campaign_rules import CampaignRules

        rules = CampaignRules(self.store)
        document = {"document_version": 2, "module": {"id": "trailhead", "version": "1",
                    "state_version": 1}, "settings": {}, "exceptions": [],
                    "guidance": None, "overrides": {"categories": {"supplies": "blocked"}}}  # fmt: skip
        imported, _ = rules.import_document(document, HOST)
        ash = self.draft()
        requested = self.request(ash)  # endurance: still allowed
        offer_id = self.offer(requested["proposal_id"])["offer_id"]
        before = self.overrides._offer_row(offer_id)
        rules.activate(imported["revision"], None, HOST)  # never stales an offer
        self.assertEqual(self.overrides._offer_row(offer_id), before)
        tables = self.tables()
        blocked = self.refused(self.request, ash, ROPE)
        self.assertEqual(
            (blocked.code, blocked.extra["categories"]),
            ("override_blocked", [{"category_id": "supplies", "label": "Supplies"}]),
        )
        self.assertEqual(self.tables(), tables)
        # The host counteroffers, and applies, in the blocked category.
        counter = self.offer(requested["proposal_id"], ROPE)
        self.accept(counter["offer_id"])
        self.assertEqual(self.walker(ash)["gear"], ["climbing rope"])
        self.direct(ash, intent(change("starting_gear", "grant", item="compass")))
        self.assertEqual(self.walker(ash)["gear"], ["climbing rope", "compass"])

    def direct(self, character_id, wanted, request_id=None, by=HOST):
        result, applied = self.overrides.apply_direct(
            by, request_id or new_request(), character_id, wanted,
            self.digest(character_id, wanted), "Agreed at the table.",
        )  # fmt: skip
        self.assertTrue(applied)
        return result

    def test_a_named_digest_that_moved_is_stale_preview(self):
        ash = self.draft()
        requested = self.request(ash, ROPE)
        reviewed = self.digest(ash, ROPE)
        state = self.store.inspect_state()
        self.host_command("trailhead.set_gear", {"character_id": ash, "gear": ["map"]})
        refused = self.refused(
            self.overrides.decide, HOST, self.host(), "o-1", requested["proposal_id"],
            "offer", reviewed, None, None,
        )  # fmt: skip
        self.assertEqual(refused.code, "stale_preview")
        self.assertNotEqual(refused.extra["preview_digest"], reviewed)
        self.assertEqual(self.state_of(requested["proposal_id"]), "pending_host")
        self.assertNotEqual(self.store.inspect_state(), state)  # only the host's edit


class DirectTests(NegotiationCase):
    def direct(self, character_id, wanted, request_id=None, by=HOST, digest=None):
        result, applied = self.overrides.apply_direct(
            by, request_id or new_request(), character_id, wanted,
            digest or self.digest(character_id, wanted), "Agreed at the table.",
        )  # fmt: skip
        return result, applied

    def test_the_host_applies_directly_with_no_request(self):
        ash = self.draft()
        revision = self.store.revision()
        digest = self.digest(ash, ROPE)
        result, applied = self.direct(ash, ROPE, request_id="o-1", digest=digest)
        self.assertTrue(applied)
        self.assertEqual(
            result,
            {"character_id": ash, "record_id": result["record_id"],
             "revision": revision + 1,
             "draft_revision": self.record(ash).draft_revision},
        )  # fmt: skip
        self.assertEqual(self.walker(ash)["gear"], ["climbing rope"])
        last = self.store.inspect_history()[-1]
        self.assertEqual((last.participant_id, last.content["type"]),
                         (HOST.sender, "override_apply"))  # fmt: skip
        [audit] = [a for a in self.store.host_audit(0)[0] if a["kind"] == "override"]
        self.assertEqual(
            (audit["target"], audit["detail"]["reason"], audit["detail"]["record_id"]),
            (ash, "Agreed at the table.", result["record_id"]),
        )
        self.assertEqual(
            self.store.query("SELECT count(*) FROM host_requests")[0][0], 0
        )
        tables = self.tables()
        self.assertEqual(
            self.direct(ash, ROPE, request_id="o-1", digest=digest), (result, False)
        )
        self.assertEqual(self.tables(), tables)
        conflict = self.refused(self.direct, ash, ROLL_2, request_id="o-1")
        self.assertEqual(conflict.code, "request_conflict")
        history = self.overrides.history(self.guest(), ash)
        self.assertEqual([r["kind"] for r in history["items"]], ["direct"])

    def test_direct_refusals_change_nothing(self):
        ash = self.draft()
        ready = self.ready(name="Wren")
        tables = self.tables()
        self.assertEqual(self.refused(self.direct, ready, ROPE).code, "invalid_request")
        self.assertEqual(self.refused(self.direct, "c_ffffffffffffffff", ROPE).code,
                         "character_unavailable")  # fmt: skip
        stale = self.refused(
            self.overrides.apply_direct, HOST, "o-2", ash, ROPE, "0" * 64, "Agreed."
        )
        self.assertEqual(stale.code, "stale_preview")
        self.assertEqual(self.tables(), tables)

    def test_a_direct_override_rechecks_the_drafts_open_offer(self):
        ash = self.draft()
        requested = self.request(ash, ROPE)
        offer_id = self.offer(requested["proposal_id"])["offer_id"]
        self.direct(ash, intent(change("starting_gear", "grant", item="compass")))
        self.assertEqual(self.offer_state(offer_id), "stale")
        self.assertEqual(self.state_of(requested["proposal_id"]), "pending_host")
        self.assertIn(requested["proposal_id"], self.notices)


class StalenessTests(NegotiationCase):
    def test_unrelated_steps_keep_the_offer_and_relevant_ones_stale_it(self):
        ash = self.draft(trait="steady")
        requested = self.request(ash, ROPE)
        offer_id = self.offer(requested["proposal_id"])["offer_id"]
        self.notices.clear()
        for name, args in (("trailhead.trait", {"trait": "quick"}),
                           ("character.name", {"name": "Ash Vale"})):  # fmt: skip
            self.creation_step(ash, name, args)
        self.assertEqual(self.offer_state(offer_id), "open")  # revision moved twice
        self.assertEqual(self.notices, [])
        self.accept(offer_id)
        walker = self.walker(ash)
        self.assertEqual(
            (walker["trait"], walker["gear"]), ("quick", ["climbing rope"])
        )
        self.assertEqual(self.record(ash).name, "Ash Vale")  # unrelated edits kept

        # A relevant change: the gear the grant depends on.
        requested = self.request(ash, intent(change("starting_gear", "grant",
                                                    item="compass")))  # fmt: skip
        offer_id = self.offer(requested["proposal_id"])["offer_id"]
        self.host_command("trailhead.set_gear",
                          {"character_id": ash, "gear": ["climbing rope", "map"]})  # fmt: skip
        self.assertEqual(self.offer_state(offer_id), "stale")
        row = self.overrides._request_row(requested["proposal_id"])
        self.assertEqual((row["state"], row["stale_reason"]),
                         ("pending_host", "preview_changed"))  # fmt: skip
        self.assertEqual(
            row["preview"]["dependencies"]["gear"], ["climbing rope", "map"]
        )
        self.assertIn(requested["proposal_id"], self.notices)
        # The host reviews the fresh preview and offers again; no resubmission.
        offer_id = self.offer(requested["proposal_id"])["offer_id"]
        self.accept(offer_id)
        self.assertEqual(self.walker(ash)["gear"], ["climbing rope", "map", "compass"])

    def test_an_intent_that_no_longer_previews_makes_its_request_stale(self):
        ash = self.draft()
        self.host_command("trailhead.set_gear", {"character_id": ash, "gear": ["map"]})
        wanted = intent(change("starting_gear", "remove", carried="map"))
        requested = self.request(ash, wanted)
        offer_id = self.offer(requested["proposal_id"])["offer_id"]
        self.host_command("trailhead.set_gear", {"character_id": ash, "gear": []})
        row = self.overrides._request_row(requested["proposal_id"])
        self.assertEqual(
            (row["state"], row["stale_reason"]), ("stale", "intent_invalid")
        )
        self.assertEqual(self.offer_state(offer_id), "stale")
        # History stays readable: the stored preview, and why it fails now.
        detail = self.overrides.detail(self.guest(), requested["proposal_id"])
        self.assertEqual(detail["request"]["preview"]["plan"], wanted)
        current = detail["request"]["current"]
        self.assertEqual(
            (current["status"], current["preview"], current["problems"]),
            ("invalid", None, ["The walker does not carry that item."]),
        )
        offer = self.overrides.offer(self.guest(), offer_id)["offer"]
        self.assertEqual((offer["state"], offer["stale_reason"]),
                         ("stale", "intent_invalid"))  # fmt: skip
        self.assertEqual(self.refused(self.accept, offer_id).code, "offer_unavailable")

    def test_transfer_keeps_pending_requests_and_stales_former_host_offers(self):
        ash, birch = self.draft(), self.draft(name="Birch")
        waiting = self.request(birch)
        offered = self.request(ash)
        offer_id = self.offer(offered["proposal_id"])["offer_id"]
        self.campaign_host = NEW_HOST.player_id  # the registry's transfer
        # Reads say so at once, before any record changes.
        entry = self.overrides.detail(self.guest(), offered["proposal_id"])["offer"]
        self.assertEqual(
            (entry["current"], entry["stale_reason"]), (False, "host_changed")
        )
        self.assertEqual(self.offer_state(offer_id), "open")
        # Accepting finds it; the staleness commits with the refusal.
        refused = self.refused(self.accept, offer_id, request_id="a-1")
        self.assertEqual(
            (refused.code, refused.extra["state"], refused.extra["stale_reason"]),
            ("offer_stale", "pending_host", "host_changed"),
        )
        self.assertEqual(self.offer_state(offer_id), "stale")
        self.assertNotIn("overrides", self.walker(ash))
        # The new host offers both, without the guest asking again.
        self.assertEqual(self.state_of(waiting["proposal_id"]), "pending_host")
        fresh = self.offer(offered["proposal_id"], by=NEW_HOST)
        self.assertEqual(fresh["offer_revision"], 2)
        result, applied = self.accept(fresh["offer_id"], request_id="a-1")
        self.assertTrue(applied)  # a refusal is not recorded: the ID is reusable
        self.offer(waiting["proposal_id"], by=NEW_HOST)
        # A former host's verdict namespace replays only its own record.
        history = self.overrides.offers(self.host(), offered["proposal_id"])["items"]
        self.assertEqual([o["state"] for o in history], ["stale", "accepted"])

    def test_an_acceptance_retried_after_a_transfer_returns_its_success(self):
        ash = self.draft()
        offer_id = self.offer(self.request(ash)["proposal_id"])["offer_id"]
        result, _ = self.accept(offer_id, request_id="a-1")
        self.campaign_host = NEW_HOST.player_id
        self.assertEqual(self.accept(offer_id, request_id="a-1"), (result, False))

    def test_a_changed_module_stales_the_offer(self):
        ash = self.draft()
        requested = self.request(ash)
        offer_id = self.offer(requested["proposal_id"])["offer_id"]
        with self.store.transaction():
            self.store.query_write(
                "UPDATE override_offers SET module = ? WHERE offer_id = ?",
                ('{"capability":0,"id":"trailhead","state_version":1,"version":"1"}',
                 offer_id),
            )  # fmt: skip
        refused = self.refused(self.accept, offer_id)
        self.assertEqual((refused.code, refused.extra["stale_reason"]),
                         ("offer_stale", "module_changed"))  # fmt: skip
        self.assertEqual(self.state_of(requested["proposal_id"]), "pending_host")


class RaceTests(NegotiationCase):
    """The campaign write transaction serializes each pair: whichever
    commits first wins, and the other is judged against its result."""

    def test_acceptance_against_withdrawal_decline_cancel_and_counteroffer(self):
        for loser in ("withdraw", "decline", "cancel", "counteroffer"):
            ash = self.draft(name=f"First {loser}")
            with self.subTest(first="accept", then=loser):
                requested = self.request(ash)
                offer_id = self.offer(requested["proposal_id"])["offer_id"]
                self.accept(offer_id)
                refused = self.refused(self.lose, loser, requested, offer_id)
                self.assertIn(refused.code, ("offer_unavailable", "proposal_unavailable",
                                             "proposal_decided"))  # fmt: skip
                self.assertEqual(self.state_of(requested["proposal_id"]), "applied")
            with self.subTest(first=loser, then="accept"):
                requested = self.request(self.draft(name=f"Then {loser}"))
                offer_id = self.offer(requested["proposal_id"])["offer_id"]
                self.lose(loser, requested, offer_id)
                self.assertEqual(self.refused(self.accept, offer_id).code,
                                 "offer_unavailable")  # fmt: skip

    def lose(self, step, requested, offer_id):
        if step == "withdraw":
            return self.overrides.withdraw(HOST, self.host(), new_request(), offer_id,
                                           "Withdrawn.")  # fmt: skip
        if step == "decline":
            return self.overrides.respond(self.guest(), new_request(), offer_id,
                                          "decline")  # fmt: skip
        if step == "cancel":
            return HostRequests(self.store).cancel(
                self.alice.participant_id, new_request(), requested["proposal_id"]
            )
        return self.offer(requested["proposal_id"],
                          intent(change("max_stamina_modifier", "set", amount=1)))  # fmt: skip

    def test_two_verdicts_on_one_request(self):
        ash = self.draft()
        requested = self.request(ash)
        self.offer(requested["proposal_id"])
        second = self.refused(self.offer, requested["proposal_id"])
        self.assertEqual(second.code, "proposal_decided")
        deny = self.refused(self.overrides.decide, HOST, self.host(), "d-9",
                            requested["proposal_id"], "deny", None, None, "No.")  # fmt: skip
        self.assertEqual(deny.code, "proposal_decided")
        self.offer(requested["proposal_id"], ROLL_3)  # a counteroffer may replace

    def test_acceptance_against_completion_and_archive(self):
        ash = self.draft()
        offer_id = self.offer(self.request(ash)["proposal_id"])["offer_id"]
        revision = self.record(ash).draft_revision
        reply, committed = self.store.complete_character(
            self.alice, "k-1", ash, revision
        )
        self.assertTrue(committed, reply)
        self.assertEqual(self.offer_state(offer_id), "stale")
        self.assertEqual(self.refused(self.accept, offer_id).code, "offer_unavailable")
        birch = self.draft(name="Birch")
        requested = self.request(birch)
        offer_id = self.offer(requested["proposal_id"])["offer_id"]
        self.store.archive_character(self.alice, "x-1", birch)
        self.assertEqual(
            (self.state_of(requested["proposal_id"]), self.offer_state(offer_id)),
            ("cancelled", "cancelled"),
        )
        self.assertEqual(self.refused(self.accept, offer_id).code, "offer_unavailable")

    def test_unlinking_the_requester_cancels_its_open_requests(self):
        ash = self.draft()
        requested = self.request(ash)
        offer_id = self.offer(requested["proposal_id"])["offer_id"]
        self.store.link_host_player("h-1", "u-alice", self.alice.participant_id)
        self.assertTrue(self.store.unlink_host_player("h-1", "u-alice"))
        self.assertEqual(
            (self.state_of(requested["proposal_id"]), self.offer_state(offer_id)),
            ("cancelled", "cancelled"),
        )

    def test_unrelated_players_never_touch_an_offer(self):
        ash = self.draft()
        offer_id = self.offer(self.request(ash)["proposal_id"])["offer_id"]
        wren = self.ready(self.bob, name="Wren")
        before = self.overrides._offer_row(offer_id)
        self.run_command(self.bob, new_request(), "trailhead.push", {},
                         Actor(wren, "play"))  # fmt: skip
        self.assertEqual(self.overrides._offer_row(offer_id), before)
        self.accept(offer_id)


class RequestIdTests(NegotiationCase):
    """One sender's request IDs span commands and host mutations: an ID
    used in either is a conflict in the other, in both directions."""

    def conflict(self, call):
        result = call()
        if isinstance(result, tuple) and isinstance(result[0], dict):
            self.assertEqual(result[0].get("code"), "request_conflict", result)
            return
        raise AssertionError(f"not refused: {result!r}")

    def test_requester_namespace_both_ways(self):
        ash = self.draft()

        def creation_step(request_id):
            raw = command(request_id, "trailhead.trait", {"trait": "quick"},
                          self.store.revision())  # fmt: skip
            return self.store.execute_command(
                self.alice, raw, actor=Actor(ash, "creation")
            )

        self.request(ash, request_id="x-1")  # a host mutation, then a command
        tables = self.tables()
        self.conflict(lambda: creation_step("x-1"))
        self.conflict(lambda: self.store.create_character(self.alice, "x-1", "Fir"))
        self.assertEqual(self.tables(), tables)
        self.assertTrue(creation_step("x-2")[1])  # a command, then mutations
        tables = self.tables()
        for call in (
            lambda: self.request(ash, request_id="x-2"),
            lambda: HostRequests(self.store).cancel(
                self.alice.participant_id, "x-2", "hp-000000000000"
            ),
        ):
            self.assertEqual(self.refused(call).code, "request_conflict")
        self.assertEqual(self.tables(), tables)

    def test_an_acceptance_and_a_cancel_never_share_an_id(self):
        ash, birch = self.draft(), self.draft(name="Birch")
        offer_id = self.offer(self.request(ash)["proposal_id"])["offer_id"]
        other = self.request(birch)
        self.accept(offer_id, request_id="a-1")
        refused = self.refused(HostRequests(self.store).cancel,
                               self.alice.participant_id, "a-1", other["proposal_id"])  # fmt: skip
        self.assertEqual(refused.code, "request_conflict")
        self.assertEqual(self.state_of(other["proposal_id"]), "pending_host")
        HostRequests(self.store).cancel(self.alice.participant_id, "c-1",
                                        other["proposal_id"])  # fmt: skip
        third = self.request(ash, ROLL_3)
        offer_id = self.offer(third["proposal_id"])["offer_id"]
        self.assertEqual(self.refused(self.accept, offer_id, request_id="c-1").code,
                         "request_conflict")  # fmt: skip

    def test_host_namespace_both_ways(self):
        from tesserae.campaign_rules import CampaignRules

        ash = self.draft()
        rules = CampaignRules(self.store)
        document = {"document_version": 1, "module": {"id": "trailhead", "version": "1",
                    "state_version": 1}, "settings": {}, "exceptions": [],
                    "guidance": None}  # fmt: skip

        def host_command(request_id):
            raw = json.dumps({"version": 1, "type": "command", "request_id": request_id,
                              "expected_revision": self.store.revision(),
                              "command": "trailhead.describe",
                              "args": {"character_id": ash, "description": "Tall."}})  # fmt: skip
            return self.store.execute_host_command(HOST, raw)

        self.assertTrue(host_command("h-1")[1])
        tables = self.tables()
        for call in (
            lambda: rules.import_document(document, HOST, request_id="h-1"),
            lambda: self.overrides.decide(HOST, self.host(), "h-1", "hp-000000000000",
                                          "deny", None, None, "No."),
        ):  # fmt: skip
            self.assertEqual(self.refused(call).code, "request_conflict")
        self.assertEqual(self.tables(), tables)
        rules.import_document(document, HOST, request_id="h-2")
        tables = self.tables()
        self.conflict(lambda: host_command("h-2"))
        self.assertEqual(
            self.refused(self.overrides.apply_direct, HOST, "h-2", ash, ROPE,
                         self.digest(ash, ROPE), "Mine.").code,
            "request_conflict",
        )  # fmt: skip
        self.assertEqual(self.tables(), tables)


class AtomicityTests(NegotiationCase):
    def test_a_failure_at_any_step_of_acceptance_applies_nothing(self):
        ash = self.draft()
        offer_id = self.offer(self.request(ash, PICKS)["proposal_id"])["offer_id"]
        tables = self.tables()
        steps = {
            "the module's apply": "tesserae.modules.Runtime.override_apply",
            "the sheet check": "tesserae.modules.Runtime.character_sheet",
            "the command record": "tesserae.store.GameStore.record_command",
            "the draft revision": "tesserae.store.GameStore.bump_draft",
            "the offer's close": "tesserae.overrides.Overrides._close_offer",
            "the notice": "tesserae.store.GameStore.notify_proposal",
        }
        for name, target in steps.items():
            with self.subTest(name), patch(target, side_effect=RuntimeError(name)):
                with self.assertRaises(RuntimeError):
                    self.accept(offer_id, request_id="a-1")
            self.assertEqual(self.tables(), tables)
        result, applied = self.accept(offer_id, request_id="a-1")  # then it works
        self.assertTrue(applied)

    def test_a_module_refusal_keeps_the_offer_open(self):
        ash = self.draft()
        offer_id = self.offer(self.request(ash)["proposal_id"])["offer_id"]
        tables = self.tables()
        from tesserae.modules import ActionRejected

        with patch("tesserae.modules.Runtime.override_apply",
                   side_effect=ActionRejected("Not now.")):  # fmt: skip
            refused = self.refused(self.accept, offer_id)
        self.assertEqual(refused.code, "action_rejected")
        self.assertEqual(self.tables(), tables)
        self.assertEqual(self.offer_state(offer_id), "open")


class CompletionTests(NegotiationCase):
    def test_completion_expires_unused_allowances_and_keeps_history(self):
        ash = self.draft()
        result, _ = self.overrides.apply_direct(
            HOST, "o-1", ash, PICKS, self.digest(ash, PICKS), "A gift from the hut."
        )
        self.creation_step(ash, "trailhead.pick", {"item": "headlamp"})
        requested = self.request(ash, ROLL_3)
        offer_id = self.offer(requested["proposal_id"])["offer_id"]
        revision = self.record(ash).draft_revision
        reply, committed = self.store.complete_character(
            self.alice, "k-1", ash, revision
        )
        self.assertTrue(committed, reply)
        walker = self.walker(ash)
        self.assertEqual(walker["gear"], ["headlamp"])  # used: kept
        self.assertNotIn("gear_picks", walker["overrides"])
        history = self.overrides.history(self.guest(), ash)["items"]
        self.assertEqual([r["kind"] for r in history], ["direct", "expiry"])
        expiry = self.overrides.record(self.guest(), history[1]["record_id"])["record"]
        self.assertEqual(
            (expiry["receipt"][0]["remaining"], expiry["intent"], expiry["revision"]),
            (1, None, self.store.revision()),
        )
        row = self.overrides._request_row(requested["proposal_id"])
        self.assertEqual((row["state"], row["stale_reason"]), ("stale", "completed"))
        self.assertEqual(self.offer_state(offer_id), "stale")
        # A ready walker cannot spend a pick in play.
        reply, committed = self.run_command(
            self.alice, new_request(), "trailhead.pick", {"item": "compass"},
            Actor(ash, "play"),
        )  # fmt: skip
        self.assertEqual((committed, reply["code"]), (False, "mode_forbidden"))
        # Requests on it are refused now.
        self.assertEqual(self.refused(self.request, ash).code, "invalid_request")

    def test_a_completion_leaving_allowances_live_is_a_module_failure(self):
        ash = self.draft()
        self.overrides.apply_direct(HOST, "o-1", ash, PICKS, self.digest(ash, PICKS),
                                    "A gift.")  # fmt: skip
        tables = self.tables()
        keep = replace(TRAILHEAD_MODULE.characters,
                       complete=lambda state, cid, policy: {**state, **{}})  # fmt: skip
        runtime = Catalog(
            [replace(TRAILHEAD_MODULE, characters=keep)], [DICE_PROVIDER]
        ).resolve("trailhead")
        with GameStore(self.path, runtime=runtime) as other:
            with self.assertRaises(RuntimeError):
                other.complete_character(self.alice, "k-1", ash,
                                         self.record(ash).draft_revision)  # fmt: skip
        self.assertEqual(self.tables(), tables)


class ReadTests(NegotiationCase):
    def test_every_read_is_private_to_the_requester_and_the_host(self):
        ash = self.draft()
        requested = self.request(ash)
        offer_id = self.offer(requested["proposal_id"])["offer_id"]
        record_id = self.accept(offer_id)[0]["record_id"]
        pid = requested["proposal_id"]
        stranger = self.guest(self.bob)
        former_host = Viewer(self.bob.participant_id, False, NEW_HOST.player_id)
        for viewer in (stranger, former_host):
            with self.subTest(viewer=viewer):
                self.assertEqual(self.overrides.requests(viewer)["items"], [])
                for call, code in (
                    (lambda: self.overrides.detail(viewer, pid), "proposal_unavailable"),
                    (lambda: self.overrides.offers(viewer, pid), "proposal_unavailable"),
                    (lambda: self.overrides.offer(viewer, offer_id), "offer_unavailable"),
                    (lambda: self.overrides.history(viewer, ash), "character_unavailable"),
                    (lambda: self.overrides.record(viewer, record_id),
                     "character_unavailable"),
                ):  # fmt: skip
                    self.assertEqual(self.refused(call).code, code)
                # Unknown and foreign look the same.
                self.assertEqual(
                    self.refused(self.overrides.detail, viewer, "hp-000000000000").code,
                    "proposal_unavailable",
                )
        for viewer in (self.guest(), self.host()):
            self.assertEqual([r["proposal_id"] for r in
                              self.overrides.requests(viewer)["items"]], [pid])  # fmt: skip
            self.overrides.detail(viewer, pid)
            self.overrides.offer(viewer, offer_id)
            self.overrides.record(viewer, record_id)
        # The legacy listing leaves negotiated requests out.
        self.assertEqual(HostRequests(self.store).listing(), [])

    def test_paging_lists_every_request_once_while_new_ones_arrive(self):
        drafts = [self.draft(name=f"W{i}") for i in range(3)]
        made = [self.request(drafts[i % 3], ROLL_2 if i % 2 else ROLL_3)["proposal_id"]
                for i in range(30)]  # fmt: skip
        seen, after, pages = [], None, 0
        while True:
            page = self.overrides.requests(self.guest(), states="all", after=after)
            self.assertLessEqual(len(page["items"]), 20)
            self.assertLessEqual(result_bytes(page), 32768)
            seen += [r["proposal_id"] for r in page["items"]]
            pages += 1
            if pages == 1:  # new ones join the end
                made.append(self.request(drafts[0], ROLL_3)["proposal_id"])
            if page["next"] is None:
                break
            after = page["next"]
        self.assertEqual(seen, made)
        opened = self.overrides.requests(self.guest(), states="open")["items"]
        self.assertEqual({r["state"] for r in opened}, {"pending_host"})
        self.assertEqual(len(opened), 3)
        self.assertEqual(
            len(self.overrides.requests(self.guest(), drafts[1])["items"]), 10
        )
        bad = self.refused(self.overrides.requests, self.guest(), after="x")
        self.assertEqual(bad.code, "invalid_request")

    def test_completed_and_archived_targets_stay_readable_without_module_code(self):
        ash, birch = self.draft(), self.draft(name="Birch")
        done = self.request(ash)
        self.offer(done["proposal_id"])
        archived = self.request(birch)
        self.store.complete_character(self.alice, "k-1", ash,
                                      self.record(ash).draft_revision)  # fmt: skip
        self.store.archive_character(self.alice, "x-1", birch)
        calls = []

        def counted(*args):
            calls.append(args)
            raise AssertionError("no module code on a read of a non-draft")

        runtime = with_overrides(preview=counted, discover=counted, apply=counted)
        with GameStore(self.path, runtime=runtime) as other:
            reads = Overrides(other)
            for proposal_id in (done["proposal_id"], archived["proposal_id"]):
                for viewer in (self.guest(), self.host()):
                    detail = reads.detail(viewer, proposal_id)
                    self.assertEqual(
                        (detail["request"]["current"]["status"],
                         detail["request"]["current"]["reason"]),
                        ("not_checked", "target_not_draft"),
                    )  # fmt: skip
                    reads.offers(viewer, proposal_id)
            reads.history(self.guest(), birch)
        self.assertEqual(calls, [])

    def test_a_preview_hook_that_fails_on_a_read_reads_as_invalid(self):
        ash = self.draft()
        requested = self.request(ash)

        def broken(*args):
            raise ZeroDivisionError("module bug")

        with GameStore(self.path, runtime=with_overrides(preview=broken)) as other:
            with self.assertLogs("tesserae.overrides", "ERROR"):
                current = Overrides(other).detail(self.guest(),
                                                  requested["proposal_id"])["request"]["current"]  # fmt: skip
        self.assertEqual((current["status"], current["problems"]),
                         ("invalid", [overrides_module.PREVIEW_FAILED]))  # fmt: skip


class WorstCaseTests(NegotiationCase):
    """Every view at its worst: 12-byte characters, every part at its
    limit, measured with the bridge's own serialization."""

    def setUp(self):
        super().setUp()
        self.store.close()
        self.store = GameStore(self.path, randint=self.draws,
                               runtime=with_overrides(preview=self.big_preview,
                                                      apply=self.big_apply))  # fmt: skip
        self.overrides = Overrides(self.store)

    def big_preview(self, state, character_id, wanted, policy):
        preview = {
            "plan": wanted, "headline": ASTRAL * 49,
            "affected": [{"quality_id": c["quality_id"], "category_id": "endurance"}
                         for c in wanted["changes"]],
            "effects": [], "waivers": [], "allowances": [], "later_steps": [],
            "consequences": [], "limitations": [], "dependencies": {"x": ""},
        }  # fmt: skip
        preview["dependencies"]["x"] = "a" * (12288 - result_bytes(preview))
        return preview

    def big_apply(self, action):
        receipt = {"x": ""}
        receipt["x"] = "a" * (8192 - result_bytes(receipt))
        return Outcome(action.state, receipt)

    def test_every_view_fits_its_bound_at_its_worst(self):
        long_name = ASTRAL * 100
        token = "t" * 40
        self.store.add_participant(long_name, token)
        guest = self.store.authenticate(token)
        ash = self.draft(guest, name="Ash")
        text = overrides_module._excerpt(ASTRAL * 1000, 4096)[0]
        quality = "q" * 48
        wanted = {"changes": []}
        while True:
            trial = {"changes": [*wanted["changes"],
                                 change(quality, "a", v=ASTRAL * 5)]}  # fmt: skip
            if result_bytes(trial) > 4096 or len(trial["changes"]) > 16:
                break
            wanted = trial
        for _ in range(3):
            requested, _ = self.overrides.request(self.guest(guest), new_request(), ash,
                                                  wanted, text)  # fmt: skip
        pid = requested["proposal_id"]
        offered = None
        for _ in range(25):
            digest = self.digest(ash, wanted)
            offered, _ = self.overrides.decide(HOST, self.host(), new_request(), pid,
                                               "counteroffer", digest, wanted, text)  # fmt: skip
        digest = self.overrides._offer_row(offered["offer_id"])["preview_digest"]
        record = self.overrides.respond(self.guest(guest), new_request(),
                                        offered["offer_id"], "accept", digest)[0]  # fmt: skip
        viewer = self.guest(guest)
        sizes = {
            "detail": (self.overrides.detail(viewer, pid), 45056),
            "offer": (self.overrides.offer(viewer, offered["offer_id"]), 45056),
            "record": (self.overrides.record(viewer, record["record_id"]), 45056),
            "requests page": (self.overrides.requests(viewer), 32768),
            "offers page": (self.overrides.offers(viewer, pid), 32768),
            "history page": (self.overrides.history(viewer, ash), 32768),
        }
        for name, (view, bound) in sizes.items():
            with self.subTest(name):
                self.assertLessEqual(result_bytes(view), bound)
        offers = self.overrides.offers(viewer, pid)
        self.assertIsNotNone(offers["next"])  # 25 revisions: a second page
        summary = self.overrides.requests(viewer)["items"][0]
        self.assertEqual(summary["cut"], ["requester", "explanation"])
        self.assertLessEqual(result_bytes(summary), 2048)
        entry = self.overrides.context_entry(viewer, self.overrides._request_row(pid))
        self.assertLessEqual(result_bytes(entry), 1024)


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        from tesserae.credentials import new_token

        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.directory / "old.sqlite3"
        token = new_token()
        with GameStore.create(
            self.path, LEGACY_TRAILHEAD, randint=lambda a, b: 2
        ) as store:
            store.add_participant("Alice", token)
            alice = store.authenticate(token)
            ids = []
            for name in ("Ash", "Wren"):
                reply, _ = store.create_character(alice, f"c-{name}", name)
                ids.append(reply["result"]["character"]["character_id"])
            for step, args in (("trailhead.trait", {"trait": "keen"}),
                               ("trailhead.stamina", {})):  # fmt: skip
                store.execute_command(
                    alice,
                    command(new_request(), step, args, store.revision()),
                    actor=Actor(ids[1], "creation"),
                )
            store.complete_character(alice, "k-1", ids[1], 3)
            self.ash, self.wren = ids
            job = SimpleNamespace(job_id="j-1", participant_id=alice.participant_id,
                                  character_id=self.ash)  # fmt: skip
            requests = HostRequests(store)
            for step, target in (("s-1", self.ash), ("s-2", self.wren)):
                proposal = requests.check(job, "exception", "target",
                                          {"kind": "character", "id": target},
                                          {"starting_stamina_bonus": 1}, "A guide.")  # fmt: skip
                requests.propose(job, step, proposal)
            store.close()
        self.alice = alice
        with closing(sqlite3.connect(self.path)) as connection:
            self.rows = connection.execute(
                "SELECT * FROM host_requests ORDER BY rowid"
            ).fetchall()
        as_schema_9(self.path)

    def test_schema_9_rows_are_kept_and_adoption_supersedes_draft_requests(self):
        with GameStore(self.path, runtime=LEGACY_TRAILHEAD) as store:  # not adopting
            rows = store.query("SELECT state FROM host_requests ORDER BY rowid")
            self.assertEqual(rows, [("pending",), ("pending",)])
        with closing(sqlite3.connect(self.path)) as connection:
            kept = connection.execute(
                "SELECT * FROM host_requests ORDER BY rowid"
            ).fetchall()
        self.assertEqual([row[:18] for row in kept], [row[:18] for row in self.rows])
        with GameStore(self.path, runtime=TRAILHEAD) as store:
            rows = store.query(
                "SELECT target, state, decision_note FROM host_requests ORDER BY rowid"
            )
            self.assertEqual(
                [(json.loads(t)["id"], s) for t, s, _ in rows],
                [(self.ash, "superseded"), (self.wren, "pending")],  # ready: legacy
            )
            self.assertEqual(rows[0][2], overrides_module.SUPERSEDED_LEGACY)
            job = SimpleNamespace(job_id="j-2", participant_id=self.alice.participant_id,
                                  character_id=self.ash)  # fmt: skip
            with self.assertRaises(Refusal) as refused:
                HostRequests(store).check(job, "exception", "target", None,
                                          {"starting_stamina_bonus": 1}, "Again.")  # fmt: skip
            self.assertEqual(refused.exception.code, "override_required")
        with GameStore(self.path, runtime=TRAILHEAD) as store:  # once is enough
            self.assertEqual(
                [r[0] for r in store.query("SELECT state FROM host_requests")],
                ["superseded", "pending"],
            )

    def test_request_decide_refuses_negotiated_requests(self):
        with GameStore(self.path, runtime=TRAILHEAD, randint=lambda a, b: 2) as store:
            viewer = Viewer(self.alice.participant_id, False, HOST.player_id)
            result, _ = Overrides(store).request(viewer, "q-1", self.ash, ROPE, GUIDE)
            with self.assertRaises(Refusal) as refused:
                HostRequests(store).decide(HOST, "d-1", result["proposal_id"],
                                           "approve", None)  # fmt: skip
            self.assertEqual(refused.exception.code, "invalid_request")


class AcceptanceBusyTests(unittest.TestCase):
    """A running referee job acting for the draft holds acceptance off."""

    def test_acceptance_waits_for_a_job_acting_for_the_draft(self):
        import tempfile
        from pathlib import Path

        from tesserae.credentials import new_token

        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        token = new_token()
        store = self.enterContext(
            GameStore.create(directory / "t.sqlite3", TRAILHEAD, randint=lambda a, b: 2)
        )
        store.add_participant("Alice", token)
        alice = store.authenticate(token)
        reply, _ = store.create_character(alice, "c-1", "Ash")
        ash = reply["result"]["character"]["character_id"]
        viewer = Viewer(alice.participant_id, False, HOST.player_id)
        host = Viewer("p_ffffffffffffffff", True, HOST.player_id)
        overrides = Overrides(store)
        requested, _ = overrides.request(viewer, "q-1", ash, ROPE, GUIDE)
        digest = overrides.preview(host, ash, ROPE)["preview_digest"]
        offered, _ = overrides.decide(HOST, host, "o-1", requested["proposal_id"],
                                      "offer", digest, None, None)  # fmt: skip
        referee = RefereeStore(store)
        referee.enable()
        job = referee.submit(alice, "s-1", "Hello", False,
                             actor=Actor(ash, "creation")).job  # fmt: skip
        with self.assertRaises(Refusal) as refused:
            overrides.respond(viewer, "a-1", offered["offer_id"], "accept", digest)
        self.assertEqual(refused.exception.code, "character_busy")
        result, applied = overrides.respond(viewer, "a-1", offered["offer_id"],
                                            "accept", digest, job_id=job.job_id)  # fmt: skip
        self.assertTrue(applied)


if __name__ == "__main__":
    unittest.main()
