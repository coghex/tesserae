"""Negotiated creation overrides through the lobby host (OVR-3, #175): the
messages of section 10 over raw WebSocket frames, with the guard's roles,
the `proposal` notices sent when each step commits, the revision an
acceptance publishes, privacy between guests, host transfer and a former
host's refusal. Trail is an original Trailhead campaign; nothing calls a
model. docs/designs/creation_overrides_contract.md sections 5 to 10."""

import json
import unittest

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_campaign_host import CampaignHost
    from test_lobby_host import message
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_campaign_host import CampaignHost
    from tests.test_lobby_host import message

MODIFIER = {"changes": [{"quality_id": "max_stamina_modifier", "action_id": "set",
                         "parameters": {"amount": 1}}]}  # fmt: skip
ROPE = {"changes": [{"quality_id": "starting_gear", "action_id": "grant",
                     "parameters": {"item": "climbing rope"}}]}  # fmt: skip


class OverrideCase(CampaignHost):
    """Trail, with Bob its campaign host (no character), Alice a guest and
    Dora a guest with no part in Alice's requests."""

    def setUp(self):
        super().setUp()
        with self.registry() as registry:
            registry.grant(self.campaigns["trail"], self.players["dora"])

    async def notice(self, connection, proposal_id, state) -> dict:
        found = await self.until(connection, "proposal", proposal_id=proposal_id,
                                 state=state)  # fmt: skip
        self.assertNotIn("request_id", found)
        return found

    async def both(self, connection, *wanted):
        """Skip messages until each (type, fields) has arrived, in any order."""
        waiting = list(wanted)
        while waiting:
            received = await self.next(connection)
            for kind, fields in list(waiting):
                if received["type"] == kind and all(
                    received.get(k) == v for k, v in fields.items()
                ):
                    waiting.remove((kind, fields))

    async def digest(self, connection, character_id, intent) -> str:
        preview = await self.ask(connection, "override_preview",
                                 character_id=character_id, intent=intent)  # fmt: skip
        self.assertEqual(preview["type"], "override_preview", preview)
        return preview["preview_digest"]

    async def requested(self, alice, bob, ash, intent=MODIFIER) -> str:
        asked = await self.ask(
            alice, "override_request", request_id="q-1", character_id=ash,
            intent=intent, explanation="A mountain guide by trade.",
        )  # fmt: skip
        self.assertEqual(
            (asked["type"], asked["state"], asked["request_id"]),
            ("proposal", "pending_host", "q-1"),
        )
        await self.notice(bob, asked["proposal_id"], "pending_host")
        return asked["proposal_id"]

    async def offered(self, bob, ash, proposal_id, intent=MODIFIER, request_id="o-1"):
        offered = await self.ask(
            bob, "override_decide", request_id=request_id, proposal_id=proposal_id,
            decision="offer", preview_digest=await self.digest(bob, ash, intent),
        )  # fmt: skip
        self.assertEqual(offered["state"], "awaiting_player", offered)
        return offered


class NegotiationTests(OverrideCase):
    async def test_a_request_is_offered_accepted_and_applied_once(self):
        await self.start_host()
        bob, snapshot = await self.enter("bob")
        self.assertEqual(
            [(c["category_id"], c["policy"]) for c in snapshot["override_categories"]],
            [("endurance", "approval_required"), ("supplies", "approval_required")],
        )
        alice, _ = await self.enter("alice")
        dora, _ = await self.enter("dora")
        ash = (await self.create(alice, "c-1", "Ash"))["character_id"]
        found = await self.ask(alice, "override_discover", character_id=ash,
                               query={"category_id": "endurance"})  # fmt: skip
        self.assertEqual([q["quality_id"] for q in found["items"]],
                         ["stamina_roll", "max_stamina_modifier"])  # fmt: skip

        proposal_id = await self.requested(alice, bob, ash)
        # Dora learns nothing of it; unknown and foreign look alike.
        self.assertEqual((await self.ask(dora, "override_requests"))["items"], [])
        for pid in (proposal_id, "hp-000000000000"):
            refused = await self.ask(dora, "override_detail", proposal_id=pid)
            self.assertEqual(refused["code"], "proposal_unavailable")
        [listed] = (await self.ask(bob, "override_requests"))["items"]
        self.assertEqual((listed["proposal_id"], listed["categories"]),
                         (proposal_id, ["endurance"]))  # fmt: skip
        # Only the campaign host gives a verdict or applies.
        for kind, fields in (
            ("override_decide", {"proposal_id": proposal_id, "decision": "deny",
                                 "note": "No."}),
            ("override_apply", {"character_id": ash, "intent": ROPE,
                                "preview_digest": "0" * 64, "reason": "Mine."}),
        ):  # fmt: skip
            refused = await self.ask(alice, kind, request_id="x-1", **fields)
            self.assertEqual((refused["code"], refused["request_id"]),
                             ("not_campaign_host", "x-1"))  # fmt: skip

        offered = await self.offered(bob, ash, proposal_id)
        await self.notice(alice, proposal_id, "awaiting_player")
        offer = (await self.ask(alice, "override_offer",
                                offer_id=offered["offer_id"]))["offer"]  # fmt: skip
        self.assertEqual((offer["state"], offer["current"]), ("open", True))
        self.assertNotIn("overrides", self.trail().inspect_state()["walkers"][ash])

        revision = self.trail().revision()
        accepted = await self.ask(
            alice, "override_respond", request_id="a-1", offer_id=offer["offer_id"],
            response="accept", preview_digest=offer["preview_digest"],
        )  # fmt: skip
        self.assertEqual(
            (accepted["type"], accepted["state"], accepted["revision"]),
            ("proposal", "applied", revision + 1),
        )
        await self.both(bob, ("revision", {"revision": revision + 1}),
                        ("proposal", {"proposal_id": proposal_id, "state": "applied"}))  # fmt: skip
        walker = self.trail().inspect_state()["walkers"][ash]
        self.assertEqual(walker["overrides"]["max_stamina_modifier"]["amount"], 1)
        retried = await self.ask(
            alice, "override_respond", request_id="a-1", offer_id=offer["offer_id"],
            response="accept", preview_digest=offer["preview_digest"],
        )  # fmt: skip
        self.assertEqual(retried, accepted)
        self.assertEqual(self.trail().revision(), revision + 1)

        # The host, with no character, applies a change directly.
        applied = await self.ask(
            bob, "override_apply", request_id="o-2", character_id=ash, intent=ROPE,
            preview_digest=await self.digest(bob, ash, ROPE), reason="The hut's gift.",
        )  # fmt: skip
        self.assertEqual((applied["type"], applied["request_id"]),
                         ("override_applied", "o-2"))  # fmt: skip
        history = await self.ask(alice, "override_history", character_id=ash)
        self.assertEqual([r["kind"] for r in history["items"]],
                         ["accepted_offer", "direct"])  # fmt: skip
        refused = await self.ask(dora, "override_history", character_id=ash)
        self.assertEqual(refused["code"], "character_unavailable")
        # The legacy listing keeps its legacy content.
        self.assertEqual((await self.ask(bob, "requests"))["requests"], [])

    async def test_transfer_keeps_the_request_and_stales_the_offer(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        alice, _ = await self.enter("alice")
        dora, _ = await self.enter("dora")
        ash = (await self.create(alice, "c-1", "Ash"))["character_id"]
        birch = (await self.create(alice, "c-2", "Birch"))["character_id"]
        proposal_id = await self.requested(alice, bob, ash)
        offered = await self.offered(bob, ash, proposal_id)
        asked = await self.ask(
            alice, "override_request", request_id="q-2", character_id=birch,
            intent=MODIFIER, explanation="Also a guide.",
        )  # fmt: skip
        waiting = asked["proposal_id"]
        transferred = await self.transfer(bob, "dora")
        self.assertEqual(transferred["type"], "role", transferred)
        # The new host hears its role and each request it inherits, with no
        # guest doing anything.
        await self.both(
            dora, ("role", {"role": "host"}),
            ("proposal", {"proposal_id": proposal_id}),
            ("proposal", {"proposal_id": waiting, "state": "pending_host"}),
        )  # fmt: skip
        # Reads report it at once.
        detail = await self.ask(alice, "override_detail", proposal_id=proposal_id)
        self.assertEqual(
            (detail["offer"]["current"], detail["offer"]["stale_reason"]),
            (False, "host_changed"),
        )
        stale = await self.ask(
            alice, "override_respond", request_id="a-1", offer_id=offered["offer_id"],
            response="accept", preview_digest=detail["offer"]["preview_digest"],
        )  # fmt: skip
        self.assertEqual(
            (stale["code"], stale["state"], stale["stale_reason"]),
            ("offer_stale", "pending_host", "host_changed"),
        )
        # The former host reads as a guest and decides nothing.
        refused = await self.ask(bob, "override_detail", proposal_id=proposal_id)
        self.assertEqual(refused["code"], "proposal_unavailable")
        refused = await self.ask(
            bob, "override_decide", request_id="o-1", proposal_id=proposal_id,
            decision="offer", preview_digest=detail["offer"]["preview_digest"],
        )  # fmt: skip
        self.assertEqual(refused["code"], "not_campaign_host")
        # The new host offers again; the guest never asked twice.
        fresh = await self.offered(dora, ash, proposal_id, request_id="o-9")
        accepted = await self.ask(
            alice, "override_respond", request_id="a-2", offer_id=fresh["offer_id"],
            response="accept", preview_digest=await self.digest(alice, ash, MODIFIER),
        )  # fmt: skip
        self.assertEqual(accepted["state"], "applied", accepted)

    async def test_bad_messages_and_unsupported_games(self):
        await self.start_host()
        alice, _ = await self.enter("alice")
        refused = await self.ask(alice, "override_request", request_id="q-1",
                                 character_id="c_1", intent=[], explanation="x")  # fmt: skip
        self.assertEqual((refused["code"], refused["request_id"]),
                         ("invalid_request", "q-1"))  # fmt: skip
        refused = await self.ask(alice, "override_requests", states="some")
        self.assertEqual(refused["code"], "invalid_request")
        await alice.send(message("leave"))
        await self.until(alice, "lobby")
        await alice.send(message("select", campaign_id=self.campaigns["harbor"]))
        snapshot = await self.until(alice, "snapshot")
        self.assertNotIn("override_categories", snapshot)
        refused = await self.ask(alice, "override_categories")
        self.assertEqual(refused["code"], "override_unsupported", json.dumps(refused))


if __name__ == "__main__":
    unittest.main()
