"""Host administration through the lobby host (GOV-4, #135): a campaign host
with no character discovers and runs operations on a guest's character, the
NPCs and the scene; guests are refused; a stale editor and the busy rule
protect play going on meanwhile; the audit is readable; transfer and
revocation end the privilege. Over raw WebSocket messages, with Trail
(Trailhead) and a scripted executor; no model.
docs/designs/governance_contract.md sections 7, 8 and 13."""

import unittest

from tesserae.credentials import new_token

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_campaign_host import CampaignHost
    from test_lobby_characters import ALICE, MARA, answer, say
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_campaign_host import CampaignHost
    from tests.test_lobby_characters import ALICE, MARA, answer, say


class AdministrationTests(CampaignHost):
    async def command(self, connection, request_id, name, args, revision=None):
        if revision is None:
            revision = (await self.ask(connection, "sync"))["revision"]
        return await self.ask(
            connection, "host_command", request_id=request_id,
            expected_revision=revision, command=name, args=args,
        )  # fmt: skip

    async def test_a_host_with_no_character_administers_a_guests_walker(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        alice, _ = await self.enter("alice")
        await self.ask(alice, "character_select", character_id=MARA)
        offered = await self.ask(bob, "host_operations")
        self.assertIn(
            "trailhead.set_stamina", [o["command"] for o in offered["operations"]]
        )

        done = await self.command(bob, "h-1", "trailhead.set_stamina",
                                  {"character_id": MARA, "stamina": 1})  # fmt: skip
        self.assertEqual((done["type"], done["request_id"]), ("result", "h-1"), done)
        notice = await self.until(alice, "revision")
        self.assertEqual(notice["revision"], done["revision"])
        self.assertEqual((await self.ask(alice, "sync"))["sheet"]["stamina"], 1)
        # A lost reply retried is the same result; the owner never changes.
        again = await self.command(bob, "h-1", "trailhead.set_stamina",
                                   {"character_id": MARA, "stamina": 1}, done["revision"] - 1)  # fmt: skip
        self.assertEqual(again, done)
        self.assertEqual(self.trail().character(MARA).participant_id, ALICE)

        await self.command(bob, "h-2", "trailhead.npc_set", {
            "npc_id": "warden", "name": "The warden", "note": "Keeps the hut.",
            "visible": False})  # fmt: skip
        await self.command(bob, "h-3", "trailhead.scene_set", {"description": "Snow."})
        snapshot = await self.ask(alice, "sync")
        self.assertEqual(snapshot["view"]["scene"]["description"], "Snow.")
        self.assertNotIn("npcs", snapshot["view"])  # hidden from players
        inspected = await self.host_view(bob)
        self.assertIn("warden", inspected["inspect"]["npcs"])

        audit = await self.ask(bob, "host_audit", after=0)
        self.assertEqual(
            [(r["kind"], r["player_id"], r["target"]) for r in audit["records"]],
            [("operation", self.players["bob"], MARA), ("operation", self.players["bob"], "warden"),
             ("operation", self.players["bob"], None)],
        )  # fmt: skip
        self.assertIsNone(audit["next_after"])
        self.assertEqual(
            (await self.ask(bob, "host_audit", after=audit["records"][0]["id"]))[
                "records"
            ],
            audit["records"][1:],
        )

    async def test_guests_are_refused_and_change_nothing(self):
        await self.start_host()
        alice, snapshot = await self.enter("alice")
        refused = await self.command(alice, "g-1", "trailhead.set_stamina",
                                     {"character_id": MARA, "stamina": 0})  # fmt: skip
        self.assertEqual(
            (refused["code"], refused["request_id"]), ("not_campaign_host", "g-1")
        )
        for kind, fields in (("host_operations", {}), ("host_audit", {"after": 0})):
            with self.subTest(kind=kind):
                refused = await self.ask(alice, kind, **fields)
                self.assertEqual(
                    (refused["code"], refused["request_id"]),
                    ("not_campaign_host", None),
                )
        # Nor through the ordinary command path, as a forged gameplay command.
        await self.ask(alice, "character_select", character_id=MARA)
        forged = await self.ask(alice, "command", request_id="f-1",
                                expected_revision=snapshot["revision"],
                                command="trailhead.set_stamina",
                                args={"character_id": MARA, "stamina": 0})  # fmt: skip
        self.assertIn(forged["code"], ("referee_mode", "unsupported_command"))
        store = self.trail()
        self.assertEqual(store.inspect_state()["walkers"][MARA]["stamina"], 3)
        self.assertEqual(store.host_audit(0)[0], [])

    async def test_an_operation_waits_while_the_referee_answers_a_guest(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        alice, _ = await self.enter("alice")
        await self.ask(alice, "character_select", character_id=MARA)
        seen = (await self.ask(bob, "sync"))["revision"]
        await alice.send(say("a-1", "Push on", MARA, "play"))
        self.assertEqual((await self.reply(alice))["type"], "accepted")
        await self.job("trail")
        busy = await self.command(bob, "h-1", "trailhead.set_stamina",
                                  {"character_id": MARA, "stamina": 0}, seen)  # fmt: skip
        self.assertEqual((busy["code"], busy["request_id"]), ("referee_busy", "h-1"))
        self.finish("trail", answer(("table", "Mara pushes on.")))
        await self.until(alice, "referee", state="idle")
        # Nothing was stored: the same request runs once the job has ended.
        done = await self.command(bob, "h-1", "trailhead.set_stamina",
                                  {"character_id": MARA, "stamina": 0}, seen)  # fmt: skip
        self.assertEqual(done["type"], "result", done)

    async def test_a_stale_editor_cannot_overwrite_intervening_work(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        seen = (await self.ask(bob, "sync"))["revision"]
        first = await self.command(bob, "h-1", "trailhead.scene_set",
                                   {"description": "Fog."}, seen)  # fmt: skip
        self.assertEqual(first["type"], "result")
        stale = await self.command(bob, "h-2", "trailhead.scene_set",
                                   {"description": "Clear."}, seen)  # fmt: skip
        self.assertEqual(
            (stale["code"], stale["revision"]), ("stale_revision", seen + 1)
        )
        self.assertEqual(self.trail().inspect_state()["scene"]["description"], "Fog.")

    async def test_a_host_whose_link_ended_learns_nothing_from_a_bad_command(self):
        await self.start_host()
        for name, args in (("trailhead.set_stamina", {"character_id": MARA}),
                           ("trailhead.rewrite", {})):  # fmt: skip
            with self.subTest(name=name):
                bob, _ = await self.enter("bob")  # admitted (again)
                with self.trail() as store:  # then Bob's participant link ends
                    store.unlink_host_player(self.host_id, self.players["bob"])
                refused = await self.command(bob, "h-1", name, args, 0)
                # Back in the lobby, with no validation detail or revision.
                self.assertEqual(
                    (refused["code"], refused["request_id"], refused["revision"]),
                    ("campaign_unavailable", None, None),
                )

    async def test_transfer_and_revocation_end_the_privilege(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        alice, _ = await self.enter("alice")
        await self.transfer(bob, "alice")
        self.assertEqual(await self.role_notice(alice), "host")
        refused = await self.command(
            bob, "h-1", "trailhead.scene_set", {"description": "Mine."}
        )
        self.assertEqual(refused["code"], "not_campaign_host")
        done = await self.command(
            alice, "h-1", "trailhead.scene_set", {"description": "Hers."}
        )
        self.assertEqual(done["type"], "result")
        token = new_token()
        with self.registry() as registry:
            registry.rotate_credential(self.players["alice"], token)
        revoked = await self.command(alice, "h-2", "trailhead.scene_set",
                                     {"description": "Again."}, done["revision"])  # fmt: skip
        self.assertEqual(revoked["code"], "session_revoked")
        self.assertEqual(self.trail().inspect_state()["scene"]["description"], "Hers.")


if __name__ == "__main__":
    unittest.main()
