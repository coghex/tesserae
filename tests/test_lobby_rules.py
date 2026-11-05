"""Campaign settings through the lobby host (GOV-3, #134): the campaign host
imports, lists, exports, activates and rejects documents with exact replay;
guests read the rules and their own character's requirements, never another
character's exception, and cannot change anything; a host authorizes its own
exception directly; a transfer ends the old host's authority. Over raw
WebSocket messages, with Trail (Trailhead) and a scripted executor; no model.
docs/designs/governance_contract.md sections 5, 7 and 13."""

import json
import unittest

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_campaign_host import CampaignHost
    from test_campaign_rules import document, exception
    from test_lobby_characters import MARA
    from test_referee_host import HostCase
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_campaign_host import CampaignHost
    from tests.test_campaign_rules import document, exception
    from tests.test_lobby_characters import MARA
    from tests.test_referee_host import HostCase


class RulesTests(CampaignHost):
    async def rules(self, connection, kind, **fields) -> dict:
        return await self.ask(connection, kind, **fields)

    async def test_the_host_configures_and_guests_only_read(self):
        await self.start_host()
        bob, snapshot = await self.enter("bob")
        self.assertEqual(snapshot["policy_revision"], 0)
        alice, _ = await self.enter("alice")

        imported = await self.rules(bob, "rules_import", request_id="i-1",
                                    content=document(bonus=1))  # fmt: skip
        self.assertEqual(
            imported,
            {"version": 1, "type": "rules", "revision": 1, "state": "candidate",
             "request_id": "i-1"},
        )  # fmt: skip
        # A lost reply retried is the same candidate; other content conflicts.
        self.assertEqual(
            await self.rules(bob, "rules_import", request_id="i-1", content=document(bonus=1)),
            imported,
        )  # fmt: skip
        conflict = await self.rules(bob, "rules_import", request_id="i-1",
                                    content=document(bonus=2))  # fmt: skip
        self.assertEqual(conflict["code"], "request_conflict")
        invalid = await self.rules(bob, "rules_import", request_id="i-2",
                                   content=document(bonus=7))  # fmt: skip
        self.assertEqual(
            (invalid["code"], invalid["request_id"]), ("invalid_document", "i-2")
        )
        self.assertTrue(invalid["problems"])

        # Guests can change nothing and read no host data.
        for kind, fields in (
            ("rules_list", {}), ("rules_export", {"revision": 1}),
            ("rules_import", {"request_id": "g-1", "content": document(bonus=2)}),
            ("rules_activate", {"request_id": "g-2", "revision": 1,
                                "expected_policy_revision": 0}),
            ("rules_reject", {"request_id": "g-3", "revision": 1}),
        ):  # fmt: skip
            with self.subTest(kind=kind):
                refused = await self.rules(alice, kind, **fields)
                self.assertEqual(
                    (refused["code"], refused["request_id"]),
                    ("not_campaign_host", fields.get("request_id")),
                )

        listed = await self.rules(bob, "rules_list")
        self.assertEqual(
            (listed["policy_revision"], [r["state"] for r in listed["revisions"]]),
            (0, ["candidate"]),
        )
        exported = await self.rules(bob, "rules_export", revision=1)
        self.assertEqual(
            (exported["state"], exported["content"]), ("candidate", document(bonus=1))
        )
        stale = await self.rules(bob, "rules_activate", request_id="a-1", revision=1,
                                 expected_policy_revision=3)  # fmt: skip
        self.assertEqual((stale["code"], stale["policy_revision"]), ("stale_policy", 0))
        active = await self.rules(bob, "rules_activate", request_id="a-1", revision=1,
                                  expected_policy_revision=0)  # fmt: skip
        self.assertEqual(active["state"], "active")

        self.assertEqual((await self.ask(alice, "sync"))["policy_revision"], 1)
        view = await self.rules(alice, "rules_view")
        self.assertEqual(
            (view["policy_revision"], view["settings"], view["guidance"]),
            (1, {"starting_stamina_bonus": 1}, "Walkers are locals."),
        )
        self.assertEqual(view["requirements"][0]["id"], "character.name")
        mine = await self.rules(alice, "rules_view", character_id=MARA)
        self.assertEqual((mine["character_id"], mine["exception"]), (MARA, None))
        self.assertTrue(all(e["met"] is not False for e in mine["requirements"][:3]))
        host_view = await self.host_view(bob)
        self.assertEqual(
            host_view["policy"], {"revision": 1, "document": document(bonus=1)}
        )

    async def test_a_hosts_own_exception_needs_no_request(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        alice, _ = await self.enter("alice")
        created = await self.ask(bob, "character_create", request_id="c-1", name="Fen")
        fen = created["character"]["character_id"]
        self.assertEqual(created["character"]["draft_revision"], 1)
        content = document(exceptions=[exception(fen, bonus=2) | {
            "reason": "The host's own guide."}])  # fmt: skip
        await self.rules(bob, "rules_import", request_id="i-1", content=content)
        activated = await self.rules(bob, "rules_activate", request_id="a-1", revision=1,
                                     expected_policy_revision=0)  # fmt: skip
        self.assertEqual(activated["state"], "active")
        [listed] = (await self.ask(bob, "characters"))["characters"]
        self.assertEqual(listed["draft_revision"], 2)  # a shown approval is stale now
        own = await self.rules(bob, "rules_view", character_id=fen)
        self.assertEqual(own["exception"]["settings"], {"starting_stamina_bonus": 2})
        self.assertEqual(own["settings"], {"starting_stamina_bonus": 2})
        # A guest cannot look at another player's character, nor learn of it.
        refused = await self.rules(alice, "rules_view", character_id=fen)
        self.assertEqual(refused["code"], "character_unavailable")
        self.assertNotIn(
            "host's own guide", json.dumps(await self.rules(alice, "rules_view"))
        )
        # The host may read any character's rules.
        self.assertEqual(
            (await self.rules(bob, "rules_view", character_id=MARA))["exception"], None
        )

    async def test_a_transferred_host_can_no_longer_activate(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        alice, _ = await self.enter("alice")
        await self.rules(
            bob, "rules_import", request_id="i-1", content=document(bonus=1)
        )
        await self.transfer(bob, "alice")
        self.assertEqual(await self.role_notice(alice), "host")
        refused = await self.rules(bob, "rules_activate", request_id="a-1", revision=1,
                                   expected_policy_revision=0)  # fmt: skip
        self.assertEqual(refused["code"], "not_campaign_host")
        # Bob's import is still a candidate; only the new host can decide it.
        rejected = await self.rules(alice, "rules_reject", request_id="r-1", revision=1)
        self.assertEqual(rejected["state"], "rejected")
        with self.trail() as store:
            self.assertEqual(store.policy_revision(), 0)
            self.assertEqual(
                [
                    row[0]
                    for row in store.query(
                        "SELECT player_id FROM host_audit ORDER BY id"
                    )
                ],
                [self.players["bob"], self.players["alice"]],
            )

    async def test_malformed_rules_messages_are_refused_with_their_request_id(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        for kind, fields, request_id in (
            ("rules_activate", {"request_id": "a-1", "revision": 1}, "a-1"),
            ("rules_import", {"request_id": "i-1", "content": "{}"}, "i-1"),
            ("rules_import", {"content": document()}, None),
            ("rules_reject", {"request_id": "r-1", "revision": 0}, "r-1"),
            ("rules_view", {"character_id": 7}, None),
            ("rules_export", {"revision": "1"}, None),
        ):  # fmt: skip
            with self.subTest(kind=kind, fields=fields):
                refused = await self.rules(bob, kind, **fields)
                self.assertEqual(
                    (refused["code"], refused["request_id"]),
                    ("invalid_request", request_id),
                )
        with self.trail() as store:
            self.assertEqual(
                store.query("SELECT count(*) FROM campaign_documents"), [(0,)]
            )


class SingleGameTests(HostCase):
    async def test_a_single_game_host_reads_rules_but_has_no_campaign_host(self):
        await self.start_host()
        alice, snapshot = await self.joined("alice")
        self.assertNotIn("policy_revision", snapshot)  # its module has no settings
        await alice.send(json.dumps({"version": 1, "type": "rules_view"}))
        view = await self.reply_on(alice)
        self.assertEqual(
            (view["type"], view["settings"], view["requirements"]),
            ("rules_view", {}, None),
        )
        await alice.send(json.dumps({"version": 1, "type": "rules_import",
                                     "request_id": "i-1", "content": document()}))  # fmt: skip
        refused = await self.reply_on(alice)
        self.assertEqual(
            (refused["code"], refused["request_id"]), ("not_campaign_host", "i-1")
        )


if __name__ == "__main__":
    unittest.main()
