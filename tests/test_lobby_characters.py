"""Characters through the lobby host (CHR-4, #114): entering a character
campaign, listing, selecting, creating, completing and archiving characters
over raw WebSocket messages, per-connection actors, notices to other
connections, and the single-game refusal, under
docs/designs/character_contract.md sections 9 to 11. The campaigns are
original Trailhead games; a scripted executor stands in for Codex and, where
a job acts, drives the real bridge tools."""

import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from websockets.exceptions import ConnectionClosed

from tesserae import server
from tesserae.builtin import CATALOG, DICE_RUNTIME
from tesserae.credentials import new_token
from tesserae.referee.store import RefereeStore
from tesserae.referee.tools import BridgeTools
from tesserae.seed import parse_seed
from tesserae.server import GameHost, HostConfigError
from tesserae.store import Actor, GameStore, ParticipantContext

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_lobby_host import LOBBY, SYNC, TIMEOUT, Lobby, message
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_lobby_host import LOBBY, SYNC, TIMEOUT, Lobby, message

TRAILHEAD = CATALOG.resolve("trailhead")
ALICE = "p_a1a1a1a1a1a1a1a1"
MARA = "c_0123456789abcdef"
UNSOLICITED = ("message", "referee", "revision", "actor")


def say(request_id, text, character_id, mode, private=False):
    return message("say", request_id=request_id, text=text, private=private,
                   actor={"character_id": character_id, "mode": mode})  # fmt: skip


def answer(*messages, ask=(), resolve=()):
    return {
        "messages": [{"audience": a, "text": t} for a, t in messages],
        "choices": {"ask": [{"actor": a, "question": q} for a, q in ask],
                    "resolve": list(resolve)},
        "narrated": [],
    }  # fmt: skip


def seed() -> str:
    """A Trailhead game where Alice's participant already has Mara, ready."""
    walker = {"trait": "quick", "rolled": 3, "stamina": 3, "max_stamina": 3,
              "ready": True, "in_danger": False}  # fmt: skip
    return json.dumps({
        "seed_version": 2,
        "module": {"id": "trailhead", "version": "1", "state_version": 1,
                   "providers": [{"operation": "dice.roll.v1", "provider": "builtin.dice",
                                  "version": "1"}]},
        "participants": [{"participant_id": ALICE, "name": "Alice"}],
        "characters": [{"character_id": MARA, "participant_id": ALICE, "name": "Mara"}],
        "state": {"walkers": {MARA: walker}},
        "provenance": {"source": "test"},
    })  # fmt: skip


class Trail(Lobby):
    """The lobby fixture plus Trail, a Trailhead campaign in referee mode:
    Alice returns to her seeded participant (linked) with Mara; Bob is new."""

    def setUp(self):
        super().setUp()
        self.trail_path = self.root / "trail.sqlite3"
        with GameStore.create(
            self.trail_path, TRAILHEAD, seed=parse_seed(seed())
        ) as store:
            RefereeStore(store).enable()
        with self.registry() as registry:
            trail = registry.register_campaign(self.trail_path, "Trail")
            registry.set_campaign_active(trail, True)
            for name in ("alice", "bob"):
                registry.grant(trail, self.players[name])
        self.campaigns["trail"] = trail
        with GameStore.open_existing(self.trail_path) as store:
            store.link_host_player(self.host_id, self.players["alice"], ALICE)

    def trail(self) -> GameStore:
        store = GameStore.open_existing(self.trail_path, randint=lambda low, high: 2)
        self.addCleanup(store.close)
        return store

    async def enter(self, name, campaign="trail"):
        connection, _ = await self.login(name)
        await connection.send(
            message("select", campaign_id=self.campaigns[campaign], characters=1)
        )
        snapshot = await self.next(connection)
        self.assertEqual(snapshot["type"], "snapshot", snapshot)
        return connection, snapshot

    async def ask(self, connection, kind, **fields) -> dict:
        await connection.send(message(kind, **fields))
        return await self.reply(connection)

    async def reply(self, connection) -> dict:
        while (received := await self.next(connection))["type"] in UNSOLICITED or (
            received["type"] == "proposal" and "request_id" not in received
        ):  # a proposal notice (governance contract section 9)
            pass
        return received

    async def create(self, connection, request_id="c-1", name=None) -> dict:
        created = await self.ask(
            connection, "character_create", request_id=request_id, name=name
        )
        self.assertEqual(created["type"], "character", created)
        return created["character"]

    def acting(self, *calls, then):
        """A scripted outcome that runs bridge calls in the job, then replies."""

        def outcome(job):
            tools = BridgeTools(
                lambda: GameStore(self.trail_path, randint=lambda low, high: 2)
            )
            token = tools.grant(job.job_id)
            self.results = [
                tools.handle({"token": token, "tool": tool, "args": args})
                for tool, args in calls
            ]
            tools.revoke()
            return then

        return outcome


class EnteringTests(Trail):
    async def test_a_client_must_declare_character_support(self):
        await self.start_host()
        connection, _ = await self.login("bob")
        await connection.send(message("select", campaign_id=self.campaigns["trail"]))
        refused = await self.next(connection)
        self.assertEqual(
            (refused["code"], refused["message"]),
            (
                "characters_unsupported",
                "This campaign uses characters; update your client.",
            ),
        )
        self.assertEqual(
            (await self.ask(connection, "lobby"))["type"], "lobby"
        )  # still there
        self.assertEqual(len(self.trail().participants()), 1)  # no participant was made
        await connection.send(
            message("select", campaign_id=self.campaigns["trail"], characters=2)
        )
        self.assertEqual((await self.next(connection))["code"], "invalid_request")

    async def test_entering_starts_in_setup_with_an_ordinary_empty_list(self):
        await self.start_host()
        bob, snapshot = await self.enter("bob")
        self.assertEqual(
            (snapshot["actor"], snapshot["sheet"]),
            ({"mode": "setup", "character": None}, None),
        )
        listed = await self.ask(bob, "characters")
        self.assertEqual(listed, {"version": 1, "type": "characters",
                                  "actor": {"mode": "setup", "character": None},
                                  "characters": []})  # fmt: skip

    async def test_a_campaign_without_characters_is_unchanged(self):
        await self.start_host()
        alice, snapshot = await self.enter(
            "alice", "harbor"
        )  # characters: 1 is ignored
        self.assertNotIn("actor", snapshot)
        refused = await self.ask(alice, "characters")
        self.assertEqual(refused["code"], "characters_unsupported")
        await alice.send(say("a-1", "Hi", None, "setup"))
        self.assertEqual((await self.reply(alice))["code"], "characters_unsupported")
        legacy, _ = await self.login("bob")
        await legacy.send(message("select", campaign_id=self.campaigns["harbor"]))
        self.assertEqual((await self.next(legacy))["type"], "snapshot")


class SelectionTests(Trail):
    async def test_a_returning_player_plays_their_seeded_character(self):
        await self.start_host()
        alice, _ = await self.enter("alice")
        listed = await self.ask(alice, "characters")
        self.assertEqual(listed["characters"], [
            {"character_id": MARA, "name": "Mara", "phase": "ready", "draft_revision": None}])  # fmt: skip
        snapshot = await self.ask(alice, "character_select", character_id=MARA)
        self.assertEqual(snapshot["actor"]["mode"], "play")
        self.assertEqual(snapshot["sheet"], {"trait": "quick", "stamina": 3, "max_stamina": 3,
                                             "in_danger": False})  # fmt: skip
        await alice.send(say("a-1", "Mara pushes on", MARA, "play"))
        accepted = await self.reply(alice)
        self.assertEqual(accepted["type"], "accepted", accepted)
        job = await self.job("trail")
        self.assertEqual((job.character_id, job.mode), (MARA, "play"))
        said = await self.until(alice, "message")
        self.assertEqual(
            said["from"], {"kind": "player", "name": "Alice", "character": "Mara"}
        )
        self.finish("trail", self.acting(
            ("act", {"step_id": "p1", "command": "trailhead.push", "args": {}}),
            then=answer(("table", "Mara slips."))))  # fmt: skip
        told = await self.until(alice, "message")
        self.assertEqual(
            (told["from"], told["text"]), ({"kind": "referee"}, "Mara slips.")
        )
        await self.until(alice, "referee", state="idle")
        self.assertTrue(self.results[0]["ok"], self.results)
        self.assertEqual(self.trail().inspect_state()["walkers"][MARA]["stamina"], 2)
        sheet = (await self.ask(alice, "sync"))["sheet"]
        self.assertEqual(
            (sheet["stamina"], sheet["in_danger"]), (2, True)
        )  # 2 against 4

    async def test_selection_belongs_to_each_connection(self):
        await self.start_host()
        first, _ = await self.enter("alice")
        second, _ = await self.enter("alice")
        await self.ask(first, "character_select", character_id=MARA)
        self.assertEqual((await self.ask(second, "sync"))["actor"]["mode"], "setup")
        await second.send(say("a-1", "What is the trail like?", None, "setup"))
        self.assertEqual((await self.reply(second))["type"], "accepted")
        job = await self.job("trail")
        self.assertEqual((job.character_id, job.mode), (None, "setup"))
        self.finish("trail", answer(("table", "Steep.")))
        await self.until(second, "referee", state="idle")
        self.assertEqual((await self.ask(first, "sync"))["actor"]["mode"], "play")

    async def test_status_shows_the_connections_own_characters_question(self):
        await self.start_host()
        alice, _ = await self.enter("alice")
        await self.ask(alice, "character_select", character_id=MARA)
        await alice.send(say("a-1", "Can Mara cross?", MARA, "play"))
        self.assertEqual((await self.reply(alice))["type"], "accepted")
        await self.job("trail")
        self.finish(
            "trail", answer(("table", "The bridge sways."), ask=[(MARA, "Cross now?")])
        )
        await self.until(alice, "referee", state="idle")
        status = await self.ask(alice, "referee_status")
        self.assertEqual(status["choices"], [{"question": "Cross now?"}])
        self.assertEqual(status["your_last_input"]["text"], "Can Mara cross?")
        setup, _ = await self.enter("alice")  # another connection, in setup
        status = await self.ask(setup, "referee_status")
        self.assertEqual((status["choices"], status["your_last_input"]), ([], None))

    async def test_nobody_else_can_select_a_character_and_every_refusal_looks_alike(
        self,
    ):
        await self.start_host()
        bob, _ = await self.enter("bob")
        draft = (await self.create(bob))["character_id"]
        alice, _ = await self.enter("alice")
        refusals = [
            await self.ask(alice, "character_select", character_id=draft),  # Bob's
            await self.ask(
                alice, "character_select", character_id="c_ffffffffffffffff"
            ),
            await self.ask(alice, "character_select", character_id="nonsense"),
        ]
        self.assertEqual(
            {(r["type"], r["code"], r["message"]) for r in refusals},
            {
                (
                    "error",
                    "character_unavailable",
                    "That character is not available to you.",
                )
            },
        )
        self.assertEqual((await self.ask(alice, "sync"))["actor"]["mode"], "setup")
        self.assertNotIn(draft, json.dumps(await self.ask(alice, "characters")))

    async def test_a_say_must_name_the_connections_actor(self):
        await self.start_host()
        alice, _ = await self.enter("alice")
        before = self.trail().query("SELECT count(*) FROM referee_inputs")
        await alice.send(say("a-1", "Push", MARA, "play"))  # Mara is not selected yet
        self.assertEqual((await self.reply(alice))["code"], "character_changed")
        await alice.send(message("say", request_id="a-2", text="Hi", private=False))
        self.assertEqual(
            (await self.reply(alice))["code"], "invalid_request"
        )  # no actor
        self.assertEqual(
            self.trail().query("SELECT count(*) FROM referee_inputs"), before
        )


class CreationTests(Trail):
    async def test_a_newcomer_creates_privately_and_completes_their_character(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        alice, _ = await self.enter("alice")
        draft = await self.create(bob, "c-1")
        again = await self.create(bob, "c-1")  # an exact retry: the same draft
        self.assertEqual(again, draft)
        self.assertEqual(len((await self.ask(bob, "characters"))["characters"]), 1)
        snapshot = await self.ask(
            bob, "character_select", character_id=draft["character_id"]
        )
        self.assertEqual(snapshot["actor"]["mode"], "creation")
        self.assertEqual(snapshot["sheet"]["trait"], None)

        cid = draft["character_id"]
        await bob.send(say("b-1", "Someone keen", cid, "creation", private=False))
        self.assertEqual((await self.reply(bob))["type"], "accepted")
        said = await self.until(bob, "message")
        self.assertEqual(
            (said["audience"], said["creation"]),
            ("private", {"character_id": cid, "name": None}),
        )
        await self.job("trail")
        self.finish("trail", self.acting(
            ("act", {"step_id": "s1", "command": "character.name", "args": {"name": "Ash"}}),
            ("act", {"step_id": "s2", "command": "trailhead.trait", "args": {"trait": "keen"}}),
            ("act", {"step_id": "s3", "command": "trailhead.stamina", "args": {}}),
            then=answer(("sender", "Ash is keen, with 3 stamina. Approve?")),
        ))  # fmt: skip
        told = await self.until(bob, "message")
        self.assertEqual(
            (told["audience"], told["creation"]["name"]), ("private", "Ash")
        )
        await self.until(bob, "referee", state="idle")
        # Alice heard the referee get busy and idle, and nothing of Bob's creation.
        await alice.send(LOBBY)
        while (received := await self.next(alice))["type"] != "lobby":
            self.assertNotEqual(received["type"], "message", received)

        sheet = (await self.ask(bob, "sync"))["sheet"]
        self.assertEqual(
            sheet,
            {"trait": "keen", "stamina": 3, "max_stamina": None, "in_danger": False},
        )
        stale = await self.ask(
            bob,
            "character_complete",
            request_id="k-1",
            character_id=cid,
            draft_revision=1,
        )
        self.assertEqual((stale["code"], stale["draft_revision"]), ("stale_draft", 4))
        done = await self.ask(
            bob,
            "character_complete",
            request_id="k-2",
            character_id=cid,
            draft_revision=4,
        )
        self.assertEqual(done["character"]["phase"], "ready")
        self.assertEqual(
            done["actor"], {"mode": "play", "character": done["character"]}
        )
        self.assertEqual(
            (await self.ask(bob, "sync"))["sheet"]["stamina"], 4
        )  # keen: 3 + 1

    async def test_an_incomplete_draft_cannot_be_completed(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        cid = (await self.create(bob))["character_id"]
        refused = await self.ask(
            bob,
            "character_complete",
            request_id="k-1",
            character_id=cid,
            draft_revision=1,
        )
        self.assertEqual(refused["code"], "character_incomplete")
        self.assertEqual(
            refused["problems"],
            ["Give the character a name.", "Choose a trait.", "Roll stamina."],
        )


class OtherConnectionTests(Trail):
    async def test_completion_and_archiving_elsewhere_reach_this_connection(self):
        await self.start_host()
        first, _ = await self.enter("bob")
        second, _ = await self.enter("bob")
        cid = (await self.create(first, name="Ash"))["character_id"]
        with self.trail() as store:  # finish the draft with raw creation steps
            bob = store.admit_host_player(self.host_id, self.players["bob"], "Bob")
            for step, (command, args) in enumerate((("trailhead.trait", {"trait": "steady"}),
                                                    ("trailhead.stamina", {}))):  # fmt: skip
                raw = message("command", request_id=f"s-{step}", expected_revision=store.revision(),
                              command=command, args=args)  # fmt: skip
                self.assertTrue(
                    store.execute_command(bob, raw, actor=Actor(cid, "creation"))[1]
                )
        for connection in (first, second):
            await self.ask(connection, "character_select", character_id=cid)
        done = await self.ask(
            first,
            "character_complete",
            request_id="k-1",
            character_id=cid,
            draft_revision=3,
        )
        self.assertEqual(done["type"], "character")
        notice = await self.until(second, "actor")
        self.assertEqual(
            (notice["reason"], notice["actor"]["mode"]), ("completed", "play")
        )
        await second.send(
            say("b-1", "Hello", cid, "creation")
        )  # it thought it was creating
        self.assertEqual((await self.reply(second))["code"], "character_changed")

        archived = await self.ask(
            first, "character_archive", request_id="a-1", character_id=cid
        )
        self.assertEqual(
            (archived["character"]["phase"], archived["actor"]["mode"]),
            ("archived", "setup"),
        )
        notice = await self.until(second, "actor")
        self.assertEqual(notice, {"version": 1, "type": "actor", "reason": "archived",
                                  "actor": {"mode": "setup", "character": None}})  # fmt: skip
        self.assertEqual((await self.ask(second, "characters"))["characters"], [])

    async def test_archiving_waits_for_the_characters_job(self):
        await self.start_host()
        alice, _ = await self.enter("alice")
        await self.ask(alice, "character_select", character_id=MARA)
        await alice.send(say("a-1", "Rest", MARA, "play"))
        self.assertEqual((await self.reply(alice))["type"], "accepted")
        await self.job("trail")
        other, _ = await self.enter("alice")
        busy = await self.ask(
            other, "character_archive", request_id="a-1", character_id=MARA
        )
        self.assertEqual(busy["code"], "character_busy")
        self.finish("trail", answer(("table", "Mara rests.")))
        await self.until(alice, "referee", state="idle")
        archived = await self.ask(
            other, "character_archive", request_id="a-2", character_id=MARA
        )
        self.assertEqual(archived["character"]["phase"], "archived")
        notice = await self.until(alice, "actor")
        self.assertEqual(notice["reason"], "archived")

    async def test_a_late_reply_keeps_its_actor_and_a_retry_cannot_change_it(self):
        await self.start_host()
        alice, _ = await self.enter("alice")
        await self.ask(alice, "character_select", character_id=MARA)
        await alice.send(say("a-1", "Mara looks up", MARA, "play"))
        self.assertEqual((await self.reply(alice))["type"], "accepted")
        await self.job("trail")
        back = await self.ask(alice, "character_select", character_id=None)
        self.assertEqual(back["actor"]["mode"], "setup")
        self.finish("trail", answer(("table", "Mara sees the clouds.")))
        await self.until(alice, "referee", state="idle")
        [stored] = self.trail().query("SELECT character_id, mode FROM referee_inputs")
        self.assertEqual(stored, (MARA, "play"))
        await alice.send(
            say("a-1", "Mara looks up", None, "setup")
        )  # same ID, another actor
        self.assertEqual((await self.reply(alice))["code"], "request_conflict")
        await self.ask(alice, "character_select", character_id=MARA)
        await alice.send(say("a-1", "Mara looks up", MARA, "play"))
        self.assertEqual(
            (await self.reply(alice))["type"], "accepted"
        )  # its recorded outcome
        self.assertEqual(
            self.trail().query("SELECT count(*) FROM referee_jobs"), [(1,)]
        )


class ReviewRegressionTests(Trail):
    """Notices and snapshots under races (review of 9947973)."""

    def ready_for_alice(self, name: str) -> str:
        """Another ready character for Alice's seeded participant."""
        alice = ParticipantContext(ALICE)
        with self.trail() as store:
            made, _ = store.create_character(alice, f"make-{name}", name)
            cid = made["result"]["character"]["character_id"]
            for step, (command, args) in enumerate((("trailhead.trait", {"trait": "steady"}),
                                                    ("trailhead.stamina", {}))):  # fmt: skip
                raw = message("command", request_id=f"{name}-{step}",
                              expected_revision=store.revision(), command=command, args=args)  # fmt: skip
                self.assertTrue(
                    store.execute_command(alice, raw, actor=Actor(cid, "creation"))[1]
                )
            self.assertTrue(store.complete_character(alice, f"done-{name}", cid, 3)[1])
        return cid

    async def test_a_rotated_out_connection_gets_no_notice(self):
        await self.start_host()
        old, _ = await self.enter("bob")
        cid = (await self.create(old, name="Ash"))["character_id"]
        with self.trail() as store:
            bob = store.admit_host_player(self.host_id, self.players["bob"], "Bob")
            for step, (command, args) in enumerate((("trailhead.trait", {"trait": "keen"}),
                                                    ("trailhead.stamina", {}))):  # fmt: skip
                raw = message("command", request_id=f"s-{step}", expected_revision=store.revision(),
                              command=command, args=args)  # fmt: skip
                self.assertTrue(
                    store.execute_command(bob, raw, actor=Actor(cid, "creation"))[1]
                )
        await self.ask(old, "character_select", character_id=cid)
        self.tokens["bob"] = new_token()
        with self.registry() as registry:
            registry.rotate_credential(self.players["bob"], self.tokens["bob"])
        new, _ = await self.enter("bob")
        done = await self.ask(
            new,
            "character_complete",
            request_id="k-1",
            character_id=cid,
            draft_revision=3,
        )
        self.assertEqual(done["character"]["phase"], "ready")
        received = []
        with self.assertRaises(ConnectionClosed) as ended:
            while True:
                received.append(await self.next(old))
        self.assertEqual(
            (ended.exception.rcvd.code, ended.exception.rcvd.reason),
            (1008, "session_revoked"),
        )
        self.assertNotIn("actor", [m["type"] for m in received])
        self.assertNotIn("Ash", json.dumps(received))

    async def test_a_notice_never_contradicts_a_newer_selection(self):
        wren = self.ready_for_alice("Wren")
        await self.start_host()
        first, _ = await self.enter("alice")
        second, _ = await self.enter("alice")
        for connection in (first, second):
            await self.ask(connection, "character_select", character_id=MARA)
        # Hold the second connection's selection of Wren mid-request, with its
        # send lock held and Mara still selected, while the first archives Mara.
        reached, release = threading.Event(), threading.Event()
        original = server._actor_view

        def held(store, context, character_id):
            if character_id == wren and not release.is_set():
                reached.set()
                release.wait(TIMEOUT)
            return original(store, context, character_id)

        with mock.patch.object(server, "_actor_view", held):
            await second.send(message("character_select", character_id=wren))
            await asyncio.to_thread(reached.wait, TIMEOUT)
            archived = await self.ask(
                first, "character_archive", request_id="a-1", character_id=MARA
            )
            self.assertEqual(archived["character"]["phase"], "archived")
            await asyncio.sleep(0.1)  # the notice task is waiting for the lock
            release.set()
            selected = await self.next(second)
        self.assertEqual((selected["type"], selected["actor"]["character"]["character_id"]),
                         ("snapshot", wren))  # fmt: skip
        await second.send(SYNC)  # nothing obsolete comes before its reply
        between = []
        while (received := await self.next(second))["type"] != "snapshot":
            between.append(received)
        self.assertNotIn("actor", [m["type"] for m in between], between)
        status = received
        self.assertEqual((status["type"], status["actor"]["character"]["character_id"]),
                         ("snapshot", wren))  # fmt: skip

    async def test_a_snapshot_is_one_committed_state(self):
        await self.start_host()
        alice, _ = await self.enter("alice")
        before = await self.ask(alice, "character_select", character_id=MARA)
        original = server._actor_view
        pushed = []

        def commit_first(store, context, character_id):
            if not pushed:  # another writer commits between the snapshot's reads
                with GameStore.open_existing(
                    self.trail_path, randint=lambda low, high: 2
                ) as other:
                    raw = message("command", request_id="push-1", expected_revision=other.revision(),
                                  command="trailhead.push", args={})  # fmt: skip
                    pushed.append(other.execute_command(
                        ParticipantContext(ALICE), raw, actor=Actor(MARA, "play"))[1])  # fmt: skip
            return original(store, context, character_id)

        with mock.patch.object(server, "_actor_view", commit_first):
            snapshot = await self.ask(alice, "sync")
        self.assertEqual(pushed, [True])
        self.assertEqual(
            (snapshot["revision"], snapshot["sheet"]["stamina"]),
            (before["revision"], 3),
        )  # the state it read, not the newer one
        after = await self.ask(alice, "sync")
        self.assertEqual(
            (after["revision"], after["sheet"]["stamina"]), (before["revision"] + 1, 2)
        )


class AccessTests(Trail):
    async def test_a_relinked_player_sees_another_participants_characters(self):
        await self.start_host()
        alice, _ = await self.enter("alice")
        await self.ask(alice, "character_select", character_id=MARA)
        with self.trail() as store:
            store.unlink_host_player(self.host_id, self.players["alice"])
        refused = await self.ask(alice, "characters")
        self.assertEqual(refused["code"], "campaign_unavailable")  # back in the lobby
        alice, _ = await self.enter("alice")  # admitted as a new participant
        self.assertEqual((await self.ask(alice, "characters"))["characters"], [])
        refused = await self.ask(alice, "character_select", character_id=MARA)
        self.assertEqual(refused["code"], "character_unavailable")
        self.assertEqual(
            self.trail().character(MARA).participant_id, ALICE
        )  # never moved

    async def test_a_revoked_host_token_ends_the_session(self):
        await self.start_host()
        bob, _ = await self.enter("bob")
        with self.registry() as registry:
            registry.revoke_credential(self.players["bob"])
        refused = await self.ask(bob, "character_create", request_id="c-1", name=None)
        self.assertEqual(refused["code"], "session_revoked")
        self.assertEqual(await self.closed(bob), (1008, "session_revoked"))
        self.assertEqual(self.trail().inspect_characters()[1:], [])  # only Mara

    async def test_selection_does_not_survive_a_host_restart(self):
        await self.start_host()
        alice, _ = await self.enter("alice")
        await self.ask(alice, "character_select", character_id=MARA)
        self.stop.set_result(None)
        await self.closed(alice)
        await self.start_host()
        alice, snapshot = await self.enter("alice")
        self.assertEqual(snapshot["actor"]["mode"], "setup")
        self.assertEqual(
            (await self.ask(alice, "character_select", character_id=MARA))["actor"][
                "mode"
            ],
            "play",
        )


class SingleGameTests(unittest.TestCase):
    def test_a_single_game_host_refuses_a_character_game(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = directory / "trail.sqlite3"
        with GameStore.create(path, TRAILHEAD):
            pass
        with self.assertRaisesRegex(
            HostConfigError, "uses characters; serve it from a lobby"
        ):
            GameHost(path, authenticated=True)
        done = subprocess.run(
            [sys.executable, "-m", "tesserae", "serve", "--database", str(path),
             "--authenticated", "--port", "0"],
            capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn(
            "this game uses characters; serve it from a lobby host", done.stderr
        )
        self.assertIn("nothing was started", done.stderr)
        relay = directory / "dice.sqlite3"
        with GameStore.create(relay, DICE_RUNTIME):
            pass
        GameHost(relay, authenticated=True)  # games without characters still serve


if __name__ == "__main__":
    unittest.main()
