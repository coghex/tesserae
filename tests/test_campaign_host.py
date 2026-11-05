"""The campaign host through the lobby host (GOV-2, #133): the role in the
snapshot, host inspection with no character, guest denial, transfer and its
replay, role notices, operator recovery and rotation, legacy and single-game
refusals, over raw WebSocket messages. Trail is an original Trailhead
campaign; a scripted executor stands in for Codex.
docs/designs/governance_contract.md sections 3, 4, 7 and 13."""

import json
import unittest
from pathlib import Path

from tesserae.builtin import DICE_RUNTIME
from tesserae.credentials import new_token
from tesserae.referee.store import RefereeStore
from tesserae.store import GameStore

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_lobby_characters import ALICE, MARA, Trail, answer, say
    from test_lobby_host import message
    from test_referee_host import HostCase
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_lobby_characters import ALICE, MARA, Trail, answer, say
    from tests.test_lobby_host import message
    from tests.test_referee_host import HostCase

UNSOLICITED = ("message", "referee", "revision", "actor")


class CampaignHost(Trail):
    """Trail with Bob, who has no character, as its campaign host; Alice
    (Mara's player) is a guest. Dora is registered but has no grant."""

    def setUp(self):
        super().setUp()
        self.tokens["dora"] = new_token()
        with self.registry() as registry:
            self.players["dora"] = registry.add_player("Dora", self.tokens["dora"])
            registry.set_host(self.campaigns["trail"], self.players["bob"])

    async def host_view(self, connection) -> dict:
        return await self.ask(connection, "host_view")

    async def transfer(self, connection, to, request_id="t-1") -> dict:
        return await self.ask(
            connection,
            "campaign_host_transfer",
            request_id=request_id,
            player_id=self.players.get(to, to),
        )

    async def role_notice(self, connection) -> dict:
        while (received := await self.next(connection))["type"] in UNSOLICITED:
            pass
        self.assertEqual(
            (received["type"], "request_id" in received), ("role", False), received
        )
        return received["role"]


class InspectionTests(CampaignHost):
    async def test_a_host_with_no_character_inspects_everything(self):
        await self.start_host()
        # The host is offline: Alice plays as before.
        alice, snapshot = await self.enter("alice")
        self.assertEqual(snapshot["role"], "guest")
        await self.ask(alice, "character_select", character_id=MARA)
        await alice.send(say("a-1", "A secret plan", MARA, "play", private=True))
        self.assertEqual((await self.reply(alice))["type"], "accepted")
        await self.job("trail")
        self.finish("trail", answer(("sender", "Only you hear this.")))
        await self.until(alice, "referee", state="idle")

        bob, snapshot = await self.enter("bob")
        self.assertEqual(
            (snapshot["role"], snapshot["actor"]), ("host", {"mode": "setup", "character": None})
        )  # fmt: skip
        view = await self.host_view(bob)
        self.assertEqual(view["type"], "host_view", view)
        [mara] = view["characters"]
        self.assertEqual(
            (mara["character_id"], mara["participant_id"], mara["phase"], mara["sheet"]["trait"]),
            (MARA, ALICE, "ready", "quick"),
        )  # fmt: skip
        self.assertEqual(
            sorted(p["name"] for p in view["participants"]), ["Alice", "Bob"]
        )
        store = self.trail()
        context = store.runtime().referee_context(store.inspect_state())
        self.assertEqual(view["revision"], store.revision())
        self.assertEqual(view["inspect"], context)
        self.assertEqual(
            (view["pending_choices"], view["rulings"], view["policy"]),
            ([], [], {"revision": 0, "document": None}),  # Trailhead has settings
        )
        # Every message, private ones included, and to whom they went.
        history = await self.ask(bob, "host_messages", after_seq=0)
        self.assertEqual(
            [(m["text"], m["audience"]) for m in history["messages"]],
            [("A secret plan", "private"), ("Only you hear this.", "private")],
        )
        self.assertEqual(
            history["messages"][0]["to"], [{"participant_id": ALICE, "name": "Alice"}]
        )
        self.assertIsNone(history["next_after_seq"])
        later = await self.ask(bob, "host_messages", after_seq=1)
        self.assertEqual(len(later["messages"]), 1)
        # Never pushed: Bob's own conversation view is unchanged.
        self.assertEqual((await self.ask(bob, "messages", after_seq=0))["messages"], [])
        bad = await self.ask(bob, "host_messages")
        self.assertEqual(bad["code"], "invalid_request")

    async def test_guests_and_campaigns_without_a_host_are_refused(self):
        await self.start_host()
        alice, _ = await self.enter("alice")
        for kind, fields in (("host_view", {}), ("host_messages", {"after_seq": 0})):
            with self.subTest(kind=kind):
                refused = await self.ask(alice, kind, **fields)
                self.assertEqual(
                    (refused["code"], refused["request_id"]),
                    ("not_campaign_host", None),
                )
        refused = await self.transfer(alice, "alice")
        self.assertEqual(
            (refused["code"], refused["request_id"]), ("not_campaign_host", "t-1")
        )
        # Harbor was never adopted: no role, no host controls, as before.
        await alice.send(message("leave"))
        await self.until(alice, "lobby")
        await alice.send(message("select", campaign_id=self.campaigns["harbor"]))
        snapshot = await self.until(alice, "snapshot")
        self.assertNotIn("role", snapshot)
        self.assertEqual((await self.host_view(alice))["code"], "not_campaign_host")


class TransferTests(CampaignHost):
    async def test_transfer_replays_and_tells_every_connection_its_role(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        alice, _ = await self.enter("alice")
        for to, why in (("dora", "no grant"), ("bob", "already"), ("u_0", "unknown")):
            with self.subTest(why=why):
                refused = await self.transfer(bob, to)
                self.assertEqual(
                    (refused["code"], refused["request_id"]), ("invalid_request", "t-1")
                )
        moved = await self.transfer(bob, "alice")
        self.assertEqual(
            moved, {"version": 1, "type": "role", "role": "guest", "request_id": "t-1"}
        )
        self.assertEqual(await self.role_notice(alice), "host")
        self.assertEqual((await self.host_view(alice))["type"], "host_view")
        self.assertEqual((await self.host_view(bob))["code"], "not_campaign_host")
        self.assertEqual((await self.ask(bob, "sync"))["role"], "guest")
        # The former host's exact retry is its recorded reply; nothing moves.
        self.assertEqual(await self.transfer(bob, "alice"), moved)
        conflict = await self.transfer(bob, "bob")
        self.assertEqual(conflict["code"], "request_conflict")
        with self.registry() as registry:
            self.assertEqual(
                registry.campaign(self.campaigns["trail"])["host_player_id"],
                self.players["alice"],
            )
            self.assertEqual(len(registry.host_changes(self.campaigns["trail"])), 2)
        # Actor ownership and history are untouched.
        self.assertEqual(self.trail().character(MARA).participant_id, ALICE)

    async def test_a_transfer_needs_the_senders_own_campaign_session(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        alice, _ = await self.enter("alice")
        with self.trail() as store:  # Bob's participant link ends
            store.unlink_host_player(self.host_id, self.players["bob"])
        refused = await self.transfer(bob, "alice")
        self.assertEqual(
            (refused["code"], refused["request_id"]), ("campaign_unavailable", None)
        )  # back in the lobby, as for any other request
        bob, snapshot = await self.enter("bob")  # a new participant, still host
        self.assertEqual(snapshot["role"], "host")
        for suffix in ("", "-wal", "-shm"):  # the file now holds another game
            Path(f"{self.trail_path}{suffix}").unlink(missing_ok=True)
        with GameStore.create(self.trail_path, DICE_RUNTIME):
            pass
        refused = await self.transfer(bob, "alice")
        self.assertEqual(
            (refused["code"], refused["request_id"]), ("campaign_unavailable", None)
        )
        with self.registry() as registry:
            self.assertEqual(
                registry.campaign(self.campaigns["trail"])["host_player_id"],
                self.players["bob"],
            )
            self.assertEqual(len(registry.host_changes(self.campaigns["trail"])), 1)
        await alice.close()

    async def test_a_guests_requests_racing_a_transfer_are_each_answered_once(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        alice, _ = await self.enter("alice")
        count = 6
        for _ in range(count):
            await alice.send(message("host_view"))
        await bob.send(
            message("campaign_host_transfer", request_id="t-1", player_id=self.players["alice"])
        )  # fmt: skip
        for _ in range(count):
            await alice.send(message("host_view"))
        replies, told = [], None
        while len(replies) < 2 * count or told is None:
            received = await self.next(alice)
            if received["type"] == "role":
                self.assertNotIn("request_id", received)
                self.assertIsNone(told, "told twice")
                told = len(replies)
            elif received["type"] not in UNSOLICITED:
                replies.append(received["type"])
        # Never in place of a reply, and once told, never refused as a guest.
        self.assertTrue(all(t in ("error", "host_view") for t in replies))
        self.assertNotIn("error", replies[told:])
        self.assertEqual(replies[-count:], ["host_view"] * count)
        self.assertEqual((await self.reply(bob))["role"], "guest")


class OperatorTests(CampaignHost):
    async def test_set_host_is_noticed_and_rotation_keeps_the_designation(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        alice, _ = await self.enter("alice")
        with self.registry() as registry:
            registry.set_host(self.campaigns["trail"], self.players["alice"])
        # Learned at the connection's next guarded check: the reply first.
        status = await self.ask(alice, "referee_status")
        self.assertEqual(status["type"], "referee_status")
        self.assertEqual(await self.role_notice(alice), "host")
        self.assertEqual((await self.ask(bob, "sync"))["role"], "guest")

        # Rotation keeps the designation; a revoked host is powerless, and
        # nobody is promoted in its place.
        token = new_token()
        with self.registry() as registry:
            registry.rotate_credential(self.players["alice"], token)
        self.assertEqual(
            (await self.ask(alice, "host_view"))["code"], "session_revoked"
        )
        self.tokens["alice"] = token
        alice, snapshot = await self.enter("alice")
        self.assertEqual(snapshot["role"], "host")
        with self.registry() as registry:
            registry.revoke_credential(self.players["alice"])
        self.assertEqual(
            (await self.ask(alice, "host_view"))["code"], "session_revoked"
        )
        self.assertEqual((await self.host_view(bob))["code"], "not_campaign_host")
        with self.registry() as registry:
            self.assertEqual(
                registry.campaign(self.campaigns["trail"])["host_player_id"],
                self.players["alice"],
            )


class SingleGameTests(HostCase):
    async def test_a_single_game_host_has_no_campaign_host(self):
        await self.start_host()
        alice, snapshot = await self.joined("alice")
        self.assertNotIn("role", snapshot)
        await alice.send(json.dumps({"version": 1, "type": "host_view"}))
        self.assertEqual((await self.reply_on(alice))["code"], "not_campaign_host")
        await alice.send(
            json.dumps({"version": 1, "type": "campaign_host_transfer",
                        "request_id": "t-1", "player_id": self.ids["bob"]})
        )  # fmt: skip
        refused = await self.reply_on(alice)
        self.assertEqual(
            (refused["code"], refused["request_id"]), ("not_campaign_host", "t-1")
        )
        with GameStore(self.path) as store:
            self.assertFalse(RefereeStore(store).running_job())


if __name__ == "__main__":
    unittest.main()
