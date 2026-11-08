"""Guest requests and host decisions through the lobby host (GOV-5, #136):
a guest's creation job proposes an exception and ends; the requester and the
campaign host are told; others play meanwhile; only the current campaign host
decides, once; the requester can withdraw; the host's own job uses host
tools, a guest's job cannot. Over raw WebSocket messages with Trail
(Trailhead) and scripted jobs driving the real bridge tools, given the
lobby's own host authority; no model. docs/designs/governance_contract.md
sections 9, 10 and 13."""

import unittest

from tesserae.referee.tools import BridgeTools
from tesserae.store import GameStore

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_campaign_host import CampaignHost
    from test_characters import use_legacy_trailhead
    from test_lobby_characters import answer, say
    from test_lobby_host import message
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_campaign_host import CampaignHost
    from tests.test_characters import use_legacy_trailhead
    from tests.test_lobby_characters import answer, say
    from tests.test_lobby_host import message


class RequestCase(CampaignHost):
    def setUp(self):
        use_legacy_trailhead(self)  # its requests are legacy draft requests
        super().setUp()

    def scripted(self, *calls, then):
        """A job that runs bridge calls with this lobby's host authority and
        proposal notices, then replies."""
        service = self.host._services[self.campaigns["trail"]]

        def outcome(job):
            tools = BridgeTools(
                lambda: GameStore(self.trail_path, randint=lambda low, high: 2),
                **service.bridge_hooks(),
            )
            token = tools.grant(job.job_id)
            self.results = [
                tools.handle({"token": token, "tool": tool, "args": args})
                for tool, args in calls
            ]
            tools.revoke()
            return then

        return outcome

    async def drafting(self, connection, name="Ash") -> str:
        draft = await self.create(connection, "c-1", name)
        await self.ask(
            connection, "character_select", character_id=draft["character_id"]
        )
        return draft["character_id"]

    async def revision_of(self, connection, character_id) -> int:
        listed = (await self.ask(connection, "characters"))["characters"]
        return next(c for c in listed if c["character_id"] == character_id)[
            "draft_revision"
        ]

    async def proposal_notice(self, connection) -> dict:
        notice = await self.until(connection, "proposal")
        self.assertNotIn("request_id", notice)
        return notice

    async def asked_for(self, alice, ash, bonus=1) -> str:
        """Alice's creation job asks the host for a starting bonus for Ash."""
        await alice.send(
            say("a-1", f"Could Ash start with {bonus} more?", ash, "creation")
        )
        self.assertEqual((await self.reply(alice))["type"], "accepted")
        await self.job("trail")
        self.finish("trail", self.scripted(
            ("propose_host_decision", {
                "step_id": "p-1", "kind": "exception", "scope": "target", "target": None,
                "payload": {"starting_stamina_bonus": bonus},
                "explanation": "Ash grew up guiding walkers."}),
            then=answer(("sender", "I have asked the campaign host.")),
        ))  # fmt: skip
        notice = await self.proposal_notice(alice)
        self.assertEqual(notice["state"], "pending")
        await self.until(alice, "referee", state="idle")
        self.assertTrue(self.results[0]["ok"], self.results)
        return notice["proposal_id"]


class RequestTests(RequestCase):
    async def test_a_guest_request_waits_while_others_play_and_is_decided_once(self):
        with self.registry() as registry:  # Dora is a guest with no part in it
            registry.grant(self.campaigns["trail"], self.players["dora"])
        await self.start_host()
        bob, _ = await self.enter("bob")
        alice, _ = await self.enter("alice")
        dora, _ = await self.enter("dora")
        ash = await self.drafting(alice)
        proposal_id = await self.asked_for(alice, ash)
        self.assertEqual((await self.proposal_notice(bob))["proposal_id"], proposal_id)

        # Nothing is busy while the host thinks: Bob plays a turn of his own.
        await bob.send(say("b-1", "What is the weather?", None, "setup"))
        self.assertEqual((await self.reply(bob))["type"], "accepted")
        await self.job("trail")
        self.finish("trail", answer(("table", "Clouds over the pass.")))
        await self.until(bob, "referee", state="idle")

        [mine] = (await self.ask(alice, "requests"))["requests"]
        self.assertEqual((mine["proposal_id"], mine["state"]), (proposal_id, "pending"))
        [theirs] = (await self.ask(bob, "requests"))["requests"]
        self.assertEqual(theirs["payload"], {"starting_stamina_bonus": 1})
        status = await self.ask(alice, "referee_status")
        self.assertEqual(status["requests"][0]["state"], "pending")

        # Only the campaign host decides.
        refused = await self.ask(alice, "request_decide", request_id="d-1",
                                 proposal_id=proposal_id, decision="approve", note=None)  # fmt: skip
        self.assertEqual(
            (refused["code"], refused["request_id"]), ("not_campaign_host", "d-1")
        )
        before = await self.revision_of(alice, ash)
        approved = await self.ask(bob, "request_decide", request_id="d-1",
                                  proposal_id=proposal_id, decision="approve",
                                  note="Welcome, guide.")  # fmt: skip
        self.assertEqual(
            (approved["type"], approved["state"], approved["request_id"]),
            ("proposal", "approved", "d-1"),
        )
        self.assertEqual(approved["effect"]["policy_revision"], 1)
        self.assertEqual((await self.proposal_notice(alice))["state"], "approved")
        self.assertEqual(
            await self.revision_of(alice, ash), before + 1
        )  # a shown approval is stale now
        mine = await self.ask(alice, "rules_view", character_id=ash)
        self.assertEqual(mine["settings"], {"starting_stamina_bonus": 1})

        # A retry is the recorded reply; a competing decision is refused.
        self.assertEqual(
            await self.ask(bob, "request_decide", request_id="d-1", proposal_id=proposal_id,
                           decision="approve", note="Welcome, guide."),
            approved,
        )  # fmt: skip
        second = await self.ask(bob, "request_decide", request_id="d-2",
                                proposal_id=proposal_id, decision="deny", note=None)  # fmt: skip
        self.assertEqual(second["code"], "proposal_decided")
        late = await self.ask(
            alice, "request_cancel", request_id="c-9", proposal_id=proposal_id
        )
        self.assertEqual(late["code"], "proposal_unavailable")
        # Dora was told nothing of it, and cannot read it.
        await dora.send(message("sync"))
        while (received := await self.next(dora))["type"] != "snapshot":
            self.assertNotEqual(received["type"], "proposal", received)
        self.assertEqual((await self.ask(dora, "requests"))["requests"], [])

    async def test_the_requester_withdraws_and_a_denial_changes_nothing(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        alice, _ = await self.enter("alice")
        ash = await self.drafting(alice)
        first = await self.asked_for(alice, ash)
        refused = await self.ask(
            bob, "request_cancel", request_id="c-1", proposal_id=first
        )
        self.assertEqual(refused["code"], "proposal_unavailable")  # not Bob's own
        withdrawn = await self.ask(
            alice, "request_cancel", request_id="c-1", proposal_id=first
        )
        self.assertEqual(
            (withdrawn["state"], withdrawn["request_id"]), ("cancelled", "c-1")
        )
        self.assertEqual((await self.proposal_notice(bob))["state"], "cancelled")
        with self.trail() as store:
            self.assertEqual(store.policy_revision(), 0)

    async def test_after_a_transfer_only_the_new_host_decides(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        alice, _ = await self.enter("alice")
        ash = await self.drafting(alice)
        proposal_id = await self.asked_for(alice, ash)
        await self.transfer(bob, "alice")
        self.assertEqual(await self.role_notice(alice), "host")
        refused = await self.ask(bob, "request_decide", request_id="d-1",
                                 proposal_id=proposal_id, decision="approve", note=None)  # fmt: skip
        self.assertEqual(refused["code"], "not_campaign_host")
        denied = await self.ask(alice, "request_decide", request_id="d-1",
                                proposal_id=proposal_id, decision="deny", note="Not now.")  # fmt: skip
        self.assertEqual(denied["state"], "denied")
        with self.trail() as store:
            self.assertEqual(store.policy_revision(), 0)


class HostJobTests(RequestCase):
    async def test_a_host_unlinked_during_its_job_can_change_nothing(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        await bob.send(say("b-1", "Set the scene.", None, "setup"))
        self.assertEqual((await self.reply(bob))["type"], "accepted")
        await self.job("trail")
        alice, _ = await self.enter("alice")  # to watch the job end
        with self.trail() as store:  # Bob's participant link ends mid-job
            store.unlink_host_player(self.host_id, self.players["bob"])
        self.finish("trail", self.scripted(
            ("get_context", {}),
            ("host_operations", {}),
            ("host_act", {"step_id": "h-1", "command": "trailhead.scene_set",
                          "args": {"description": "Bob's scene."}}),
            then=answer(("table", "Nothing changed.")),
        ))  # fmt: skip
        await self.until(alice, "referee", state="idle")
        context, offered, acted = self.results
        self.assertNotIn("host", context["result"])
        self.assertEqual(offered["error"]["code"], "not_campaign_host")
        self.assertEqual(acted["error"]["code"], "not_campaign_host")
        with self.trail() as store:
            self.assertNotIn("scene", store.inspect_state())
            self.assertEqual(store.host_audit(0)[0], [])

    async def test_a_transfer_during_the_hosts_job_ends_its_host_tools(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        alice, _ = await self.enter("alice")
        await bob.send(say("b-1", "Hand over, then set the scene.", None, "setup"))
        self.assertEqual((await self.reply(bob))["type"], "accepted")
        await self.job("trail")
        await self.transfer(bob, "alice")  # while Bob's job runs
        self.assertEqual(await self.role_notice(alice), "host")
        self.finish("trail", self.scripted(
            ("host_act", {"step_id": "h-1", "command": "trailhead.scene_set",
                          "args": {"description": "Bob's scene."}}),
            then=answer(("table", "The host has changed.")),
        ))  # fmt: skip
        await self.until(bob, "referee", state="idle")
        self.assertEqual(self.results[0]["error"]["code"], "not_campaign_host")
        with self.trail() as store:
            self.assertNotIn("scene", store.inspect_state())

    async def test_the_hosts_own_job_has_host_tools_and_a_guests_has_none(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        alice, _ = await self.enter("alice")
        await bob.send(say("b-1", "Make it snow.", None, "setup"))
        self.assertEqual((await self.reply(bob))["type"], "accepted")
        await self.job("trail")
        self.finish("trail", self.scripted(
            ("host_act", {"step_id": "h-1", "command": "trailhead.scene_set",
                          "args": {"description": "Snow on the pass."}}),
            then=answer(("table", "Snow falls.")),
        ))  # fmt: skip
        await self.until(bob, "referee", state="idle")
        self.assertTrue(self.results[0]["ok"], self.results)
        self.assertEqual(
            (await self.ask(alice, "sync"))["view"]["scene"]["description"],
            "Snow on the pass.",
        )
        ash = await self.drafting(alice)
        await alice.send(say("a-1", "Make it sunny.", ash, "creation"))
        self.assertEqual((await self.reply(alice))["type"], "accepted")
        await self.job("trail")
        self.finish("trail", self.scripted(
            ("host_act", {"step_id": "h-1", "command": "trailhead.scene_set",
                          "args": {"description": "Sun."}}),
            then=answer(("sender", "Only the host can do that.")),
        ))  # fmt: skip
        await self.until(alice, "referee", state="idle")
        self.assertEqual(self.results[0]["error"]["code"], "not_campaign_host")
        with self.trail() as store:
            self.assertEqual(
                store.inspect_state()["scene"]["description"], "Snow on the pass."
            )
            [audit] = store.host_audit(0)[0]
            self.assertEqual(
                (audit["via"], audit["player_id"]), ("referee", self.players["bob"])
            )


if __name__ == "__main__":
    unittest.main()
