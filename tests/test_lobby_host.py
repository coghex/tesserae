"""The registry-backed lobby host (HOST-2, #97): login, catalog, selection,
per-campaign isolation, admission and revocation ordering. A scripted
executor per campaign stands in for Codex. docs/designs/lobby_contract.md."""

import asyncio
import json
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from tesserae import server
from tesserae.builtin import DICE_RUNTIME
from tesserae.credentials import new_token
from tesserae.lobby import LobbyHost
from tesserae.referee.store import RefereeStore
from tesserae.registry import HostRegistry
from tesserae.store import GameStore

TIMEOUT = 5


def message(kind, **fields):
    return json.dumps({"version": 1, "type": kind, **fields})


def say(request_id, text, private=False):
    return message("say", request_id=request_id, text=text, private=private)


def reply(*texts):
    return {
        "messages": [{"audience": "table", "text": t} for t in texts],
        "choices": {"ask": [], "resolve": []},
        "narrated": [],
    }


SYNC = message("sync")
LOBBY = message("lobby")
LEAVE = message("leave")
UNSOLICITED = ("message", "referee", "revision")


class Lobby(unittest.IsolatedAsyncioTestCase):
    """A registry with two referee campaigns, Harbor and Bridge. Alice may
    enter both; Bob only Harbor."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry_path = self.root / "host.sqlite3"
        self.tokens = {"alice": new_token(), "bob": new_token()}
        self.campaigns = {}
        with HostRegistry.create(self.registry_path) as registry:
            self.host_id = registry.host_id
            self.players = {
                name: registry.add_player(name.title(), token)
                for name, token in self.tokens.items()
            }
            for name in ("harbor", "bridge"):
                path = self.root / f"{name}.sqlite3"
                with GameStore.create(path, DICE_RUNTIME) as store:
                    RefereeStore(store).enable()
                campaign = registry.register_campaign(path, name.title())
                registry.set_campaign_active(campaign, True)
                self.campaigns[name] = campaign
            registry.grant(self.campaigns["harbor"], self.players["alice"])
            registry.grant(self.campaigns["bridge"], self.players["alice"])
            registry.grant(self.campaigns["harbor"], self.players["bob"])
        self.scripts, self.started = {}, {}

    def registry(self) -> HostRegistry:
        return HostRegistry(self.registry_path)

    def executor_for(self, campaign_id):
        script, started = asyncio.Queue(), asyncio.Queue()
        self.scripts[campaign_id], self.started[campaign_id] = script, started

        async def executor(job):
            started.put_nowait(job)
            outcome = await script.get()
            return outcome(job) if callable(outcome) else outcome

        return executor

    async def start_host(self, **options):
        loop = asyncio.get_running_loop()
        self.stop, ready = loop.create_future(), loop.create_future()
        self.host = LobbyHost(
            self.registry_path, referee_executor_for=self.executor_for, **options
        )
        task = asyncio.create_task(
            self.host.run(0, self.stop, on_ready=ready.set_result)
        )

        async def shutdown():
            if not self.stop.done():
                self.stop.set_result(None)
            await asyncio.wait_for(task, 10)

        self.addAsyncCleanup(shutdown)
        self.uri = await asyncio.wait_for(ready, TIMEOUT)

    async def open(self):
        connection = await connect(self.uri)
        self.addAsyncCleanup(connection.close)
        return connection

    async def login(self, name):
        connection = await self.open()
        await connection.send(message("login", token=self.tokens[name]))
        lobby = await self.next(connection)
        self.assertEqual(lobby["type"], "lobby", lobby)
        return connection, lobby

    async def entered(self, name, campaign):
        connection, _ = await self.login(name)
        snapshot = await self.select(connection, campaign)
        self.assertEqual(snapshot["type"], "snapshot", snapshot)
        return connection, snapshot

    async def select(self, connection, campaign):
        await connection.send(message("select", campaign_id=self.campaigns[campaign]))
        return await self.next(connection)

    async def next(self, connection) -> dict:
        return json.loads(await asyncio.wait_for(connection.recv(), TIMEOUT))

    async def reply_on(self, connection) -> dict:
        while (received := await self.next(connection))["type"] in UNSOLICITED:
            pass
        return received

    async def until(self, connection, kind, **fields) -> dict:
        while True:
            received = await self.next(connection)
            if received["type"] == kind and all(
                received.get(k) == v for k, v in fields.items()
            ):
                return received

    async def closed(self, connection) -> tuple:
        with self.assertRaises(ConnectionClosed) as ended:
            while True:
                await asyncio.wait_for(connection.recv(), TIMEOUT)
        return ended.exception.rcvd.code, ended.exception.rcvd.reason

    async def job(self, campaign):
        return await asyncio.wait_for(
            self.started[self.campaigns[campaign]].get(), TIMEOUT
        )

    def finish(self, campaign, outcome):
        self.scripts[self.campaigns[campaign]].put_nowait(outcome)

    def jobs(self, campaign) -> list:
        with GameStore(self.root / f"{campaign}.sqlite3") as store:
            return store.query("SELECT state FROM referee_jobs ORDER BY started_at")


class LobbyMessageTests(Lobby):
    async def test_login_catalog_select_leave_and_return(self):
        await self.start_host()
        alice, lobby = await self.login("alice")
        harbor, bridge = self.campaigns["harbor"], self.campaigns["bridge"]
        self.assertEqual(
            lobby,
            {"version": 1, "type": "lobby", "player_id": self.players["alice"],
             "player_name": "Alice",
             "campaigns": [{"campaign_id": bridge, "name": "Bridge"},
                           {"campaign_id": harbor, "name": "Harbor"}],
             "selected": None},
        )  # fmt: skip
        snapshot = await self.select(alice, "harbor")
        self.assertEqual(snapshot["game_id"], harbor)
        self.assertEqual(
            snapshot["campaign"], {"campaign_id": harbor, "name": "Harbor"}
        )
        self.assertEqual(snapshot["participant_name"], "Alice")
        self.assertEqual(snapshot["referee"]["state"], "idle")
        await alice.send(LOBBY)
        self.assertEqual((await self.next(alice))["selected"], snapshot["campaign"])
        await alice.send(LEAVE)
        self.assertIsNone((await self.next(alice))["selected"])
        again = await self.select(alice, "harbor")
        self.assertEqual(again["participant_id"], snapshot["participant_id"])
        with GameStore(self.root / "harbor.sqlite3") as store:
            [participant] = store.participants()
            self.assertEqual(participant.participant_id, snapshot["participant_id"])
            [link] = store.host_links()
            self.assertEqual(
                (link.host_id, link.player_id), (self.host_id, self.players["alice"])
            )
            self.assertEqual(store.revision(), 0)  # choosing a campaign plays nothing

        bob, lobby = await self.login("bob")
        self.assertEqual(
            lobby["campaigns"], [{"campaign_id": harbor, "name": "Harbor"}]
        )

    async def test_refusals_before_and_after_login(self):
        await self.start_host(join_timeout=0.3)
        cases = [
            (SYNC, "login_required"),
            (message("join", token=self.tokens["alice"]), "login_required"),
            (message("login", token=new_token()), "authentication_failed"),
            (message("login", token="has space"), "authentication_failed"),
            (message("login", token=self.tokens["alice"], extra=1), "invalid_request"),
            ("not json", "invalid_request"),
        ]
        for first, code in cases:
            with self.subTest(first=first[:30]):
                connection = await self.open()
                await connection.send(first)
                received = await self.next(connection)
                self.assertEqual(received["code"], code, received)
                self.assertNotIn(self.tokens["alice"], json.dumps(received))
                self.assertEqual(await self.closed(connection), (1008, code))
        silent = await self.open()
        self.assertEqual((await self.next(silent))["code"], "join_timeout")

        alice, _ = await self.login("alice")
        steps = [
            (message("login", token=self.tokens["alice"]), "already_logged_in", None),
            (message("join", token=self.tokens["alice"]), "already_logged_in", None),
            (say("s1", "Hello"), "no_campaign", "s1"),
            (SYNC, "no_campaign", None),
            (LEAVE, "no_campaign", None),
            (message("select", campaign_id="has space"), "invalid_request", None),
        ]
        for sent, code, request_id in steps:
            with self.subTest(sent=sent[:40]):
                await alice.send(sent)
                received = await self.next(alice)
                self.assertEqual(
                    (received["code"], received["request_id"]), (code, request_id)
                )
        await alice.send(b"binary")
        self.assertEqual((await self.next(alice))["code"], "invalid_request")
        self.assertEqual((await self.select(alice, "harbor"))["type"], "snapshot")
        await alice.send(message("select", campaign_id=self.campaigns["bridge"]))
        self.assertEqual((await self.next(alice))["code"], "campaign_selected")
        await alice.send(message("join", token=self.tokens["alice"]))
        self.assertEqual((await self.next(alice))["code"], "already_logged_in")
        await alice.send(SYNC)  # still in Harbor
        self.assertEqual((await self.next(alice))["game_id"], self.campaigns["harbor"])

    async def test_malformed_types_are_invalid_requests_in_and_out_of_campaigns(self):
        await self.start_host()
        alice, _ = await self.login("alice")
        bad = [message(kind) for kind in ([], {}, 1, None, True)] + ['{"version": 1}']
        for selected in (False, True):
            if selected:
                self.assertEqual(
                    (await self.select(alice, "harbor"))["type"], "snapshot"
                )
            for sent in bad:
                with self.subTest(selected=selected, sent=sent):
                    await alice.send(sent)
                    self.assertEqual(
                        (await self.next(alice))["code"], "invalid_request"
                    )
            await alice.send(LOBBY)  # still open and answering
            self.assertEqual((await self.next(alice))["type"], "lobby")

    async def test_inaccessible_campaigns_are_indistinguishable(self):
        with self.registry() as registry:
            registry.set_campaign_active(self.campaigns["bridge"], False)
            path = self.root / "secret.sqlite3"
            with GameStore.create(path, DICE_RUNTIME):
                pass
            secret = registry.register_campaign(path, "Secret")
            registry.set_campaign_active(secret, True)
        await self.start_host()
        bob, lobby = await self.login("bob")
        self.assertEqual(len(lobby["campaigns"]), 1)
        alice, lobby = await self.login("alice")
        self.assertEqual(len(lobby["campaigns"]), 1)  # Bridge is inactive
        refusals = []
        for connection, campaign_id in (
            (bob, self.campaigns["bridge"]),  # not granted, and inactive
            (bob, secret),  # active, not granted
            (alice, self.campaigns["bridge"]),  # granted, inactive
            (alice, "00000000-0000-0000-0000-000000000000"),  # unknown
        ):
            await connection.send(message("select", campaign_id=campaign_id))
            refusals.append(await self.next(connection))
        self.assertEqual(len({json.dumps(r) for r in refusals}), 1, refusals)
        self.assertEqual(refusals[0]["code"], "campaign_unavailable")
        with GameStore(path) as store:
            self.assertEqual(store.participants(), [])  # never admitted

    async def test_every_handshake_with_an_origin_is_refused(self):
        await self.start_host()
        with self.assertRaises(Exception) as refused:
            await connect(self.uri, origin="http://127.0.0.1:1")
        self.assertIn("403", str(refused.exception))


class CampaignIsolationTests(Lobby):
    async def test_campaigns_are_busy_and_converse_independently(self):
        await self.start_host()
        alice, _ = await self.entered("alice", "bridge")
        bob, _ = await self.entered("bob", "harbor")
        await alice.send(say("a1", "Across the bridge."))
        self.assertEqual((await self.reply_on(alice))["type"], "accepted")
        await self.job("bridge")  # Bridge is busy now
        await bob.send(say("b1", "At the harbor."))
        self.assertEqual((await self.reply_on(bob))["type"], "accepted")
        await self.job("harbor")
        self.finish("harbor", reply("Gulls circle."))
        self.assertEqual(
            (await self.until(bob, "message", **{"from": {"kind": "referee"}}))["text"],
            "Gulls circle.",
        )
        self.finish("bridge", reply("The planks creak."))
        heard = await self.until(alice, "message", **{"from": {"kind": "referee"}})
        self.assertEqual(heard["text"], "The planks creak.")
        await self.until(alice, "referee", state="idle")
        await bob.send(message("messages", after_seq=0))
        texts = [m["text"] for m in (await self.reply_on(bob))["messages"]]
        self.assertEqual(texts, ["At the harbor.", "Gulls circle."])
        self.assertEqual(
            (self.jobs("harbor"), self.jobs("bridge")), ([("completed",)],) * 2
        )

    async def test_one_player_in_two_campaigns_over_two_connections(self):
        await self.start_host()
        first, in_harbor = await self.entered("alice", "harbor")
        second, in_bridge = await self.entered("alice", "bridge")
        self.assertNotEqual(in_harbor["participant_id"], in_bridge["participant_id"])
        await first.send(say("same-id", "Harbor words."))
        self.assertEqual((await self.reply_on(first))["type"], "accepted")
        await self.job("harbor")
        await second.send(say("same-id", "Bridge words."))  # another namespace
        self.assertEqual((await self.reply_on(second))["type"], "accepted")
        await self.job("bridge")
        self.finish("harbor", reply("Harbor reply."))
        self.finish("bridge", reply("Bridge reply."))
        for connection, text in ((first, "Harbor reply."), (second, "Bridge reply.")):
            heard = await self.until(
                connection, "message", **{"from": {"kind": "referee"}}
            )
            self.assertEqual(heard["text"], text)
            await self.until(connection, "referee", state="idle")
            await connection.send(message("messages", after_seq=0))
            texts = {m["text"] for m in (await self.reply_on(connection))["messages"]}
            self.assertEqual(len(texts & {"Harbor reply.", "Bridge reply."}), 1)

    async def test_nothing_from_a_campaign_after_leaving_it(self):
        await self.start_host()
        alice, _ = await self.entered("alice", "harbor")
        bob, _ = await self.entered("bob", "harbor")
        await alice.send(say("a1", "I set sail.", private=True))
        self.assertEqual((await self.reply_on(alice))["type"], "accepted")
        await self.job("harbor")
        await alice.send(LEAVE)
        self.assertEqual((await self.reply_on(alice))["type"], "lobby")
        self.assertEqual((await self.select(alice, "bridge"))["type"], "snapshot")
        self.finish("harbor", reply("The tide turns."))
        await self.until(bob, "referee", state="idle")
        await alice.send(SYNC)  # the next message after the reply is this sync's
        received = await self.next(alice)
        self.assertEqual(
            (received["type"], received["game_id"]),
            ("snapshot", self.campaigns["bridge"]),
        )
        await alice.send(LEAVE)
        await self.next(alice)
        await self.select(alice, "harbor")
        await alice.send(message("messages", after_seq=0))  # it is in the history
        texts = [m["text"] for m in (await self.reply_on(alice))["messages"]]
        self.assertEqual(texts, ["I set sail.", "The tide turns."])

    async def test_an_exact_retry_replays_in_its_own_campaign(self):
        await self.start_host()
        alice, _ = await self.entered("alice", "harbor")
        await alice.send(say("a1", "Hello harbor."))
        accepted = await self.reply_on(alice)
        await self.job("harbor")
        self.finish("harbor", reply("Welcome."))
        await self.until(alice, "referee", state="idle")
        await alice.send(LEAVE)
        await self.reply_on(alice)
        await self.select(alice, "harbor")
        await alice.send(say("a1", "Hello harbor."))
        self.assertEqual(await self.reply_on(alice), accepted)
        self.assertEqual(
            (await self.next(alice))["text"], "Welcome."
        )  # its reply again
        self.assertEqual(self.jobs("harbor"), [("completed",)])

    async def test_concurrent_selections_admit_one_participant(self):
        await self.start_host()
        connections = [(await self.login("alice"))[0] for _ in range(4)]
        await asyncio.gather(*(self.select(c, "harbor") for c in connections))
        with GameStore(self.root / "harbor.sqlite3") as store:
            self.assertEqual(len(store.participants()), 1)
            self.assertEqual(len(store.host_links()), 1)


class AccessTests(Lobby):
    async def test_lost_campaign_access_returns_to_the_lobby(self):
        await self.start_host()
        harbor = self.campaigns["harbor"]
        changes = {
            "grant": lambda r: r.revoke_access(harbor, self.players["alice"]),
            "campaign": lambda r: r.set_campaign_active(harbor, False),
        }
        for kind, change in changes.items():
            with self.subTest(kind=kind):
                alice, _ = await self.entered("alice", "harbor")
                with self.registry() as registry:
                    change(registry)
                await alice.send(say("x", "Still here?"))
                refused = await self.next(alice)  # like session_revoked: no request ID
                self.assertEqual(
                    (refused["code"], refused["request_id"]),
                    ("campaign_unavailable", None),
                )
                await alice.send(SYNC)  # back in the lobby, still logged in
                self.assertEqual((await self.next(alice))["code"], "no_campaign")
                await alice.send(
                    message("select", campaign_id=self.campaigns["bridge"])
                )
                self.assertEqual((await self.next(alice))["type"], "snapshot")
                with self.registry() as registry:
                    registry.grant(harbor, self.players["alice"])
                    registry.set_campaign_active(harbor, True)
        self.assertEqual(self.jobs("harbor"), [])

    async def test_an_ended_link_returns_to_the_lobby(self):
        await self.start_host()
        alice, _ = await self.entered("alice", "harbor")
        with GameStore(self.root / "harbor.sqlite3") as store:
            store.unlink_host_player(self.host_id, self.players["alice"])
        await alice.send(SYNC)
        self.assertEqual((await self.next(alice))["code"], "campaign_unavailable")
        snapshot = await self.select(alice, "harbor")  # a new participant now
        with GameStore(self.root / "harbor.sqlite3") as store:
            self.assertEqual(len(store.participants()), 2)
            self.assertEqual(
                store.host_links()[-1].participant_id, snapshot["participant_id"]
            )

    async def test_a_revoked_host_token_closes_the_connection(self):
        await self.start_host()
        lobby_only, _ = await self.login("alice")
        playing, _ = await self.entered("alice", "harbor")
        with self.registry() as registry:
            registry.revoke_credential(self.players["alice"])
        for connection, sent in ((lobby_only, LOBBY), (playing, SYNC)):
            await connection.send(sent)
            self.assertEqual((await self.next(connection))["code"], "session_revoked")
            self.assertEqual(await self.closed(connection), (1008, "session_revoked"))
        stale = await self.open()
        await stale.send(message("login", token=self.tokens["alice"]))
        self.assertEqual((await self.next(stale))["code"], "authentication_failed")

    async def test_every_request_notices_a_revoked_host_token(self):
        command = json.dumps({"version": 1, "type": "command", "request_id": "c1",
                              "expected_revision": 0, "command": "roll",
                              "args": {"expression": "1d6"}})  # fmt: skip
        in_lobby = [
            SYNC,
            say("s1", "Hello"),
            LEAVE,
            b"binary",
            message("login", token=new_token()),
            message("join", token=new_token()),
            message("select", campaign_id="has space"),
            message([]),
        ]
        in_campaign = [
            b"binary",
            message("select", campaign_id=self.campaigns["bridge"]),
            message("login", token=new_token()),
            command,  # referee_mode, if the token were still valid
        ]
        cases = [(False, m) for m in in_lobby] + [(True, m) for m in in_campaign]
        await self.start_host()
        for selected, sent in cases:
            with self.subTest(selected=selected, sent=sent[:40]):
                self.tokens["alice"] = new_token()
                with self.registry() as registry:
                    registry.rotate_credential(
                        self.players["alice"], self.tokens["alice"]
                    )
                if selected:
                    connection, _ = await self.entered("alice", "harbor")
                else:
                    connection, _ = await self.login("alice")
                with self.registry() as registry:
                    registry.revoke_credential(self.players["alice"])
                await connection.send(sent)
                self.assertEqual(
                    (await self.next(connection))["code"], "session_revoked"
                )
                self.assertEqual(
                    await self.closed(connection), (1008, "session_revoked")
                )

    async def test_store_free_campaign_replies_notice_lost_access(self):
        await self.start_host()
        alice, _ = await self.entered("alice", "harbor")
        with self.registry() as registry:
            registry.revoke_access(self.campaigns["harbor"], self.players["alice"])
        await alice.send(b"binary")
        self.assertEqual((await self.next(alice))["code"], "campaign_unavailable")
        await alice.send(SYNC)
        self.assertEqual((await self.next(alice))["code"], "no_campaign")

    async def test_a_delivery_notices_lost_access_and_sends_left(self):
        await self.start_host()
        alice, _ = await self.entered("alice", "harbor")
        bob, _ = await self.entered("bob", "harbor")
        with self.registry() as registry:
            registry.revoke_access(self.campaigns["harbor"], self.players["alice"])
        await bob.send(say("b1", "Anyone there?"))  # delivered to the table
        self.assertEqual((await self.reply_on(bob))["type"], "accepted")
        left = await self.next(alice)
        self.assertEqual(
            left,
            {"version": 1, "type": "left", "campaign_id": self.campaigns["harbor"],
             "reason": "campaign_unavailable"},
        )  # fmt: skip
        await self.job("harbor")
        self.finish("harbor", reply("Silence."))
        await self.until(bob, "referee", state="idle")
        await alice.send(LOBBY)  # nothing from Harbor came in between
        self.assertEqual((await self.next(alice))["type"], "lobby")

    async def test_revocation_cannot_commit_during_a_guarded_acceptance(self):
        await self.start_host()
        alice, _ = await self.entered("alice", "harbor")
        inside, release = threading.Event(), threading.Event()
        submit = server._submit

        def held_submit(*args):
            inside.set()
            self.assertTrue(release.wait(TIMEOUT))
            return submit(*args)

        revoked = threading.Event()

        def revoke():
            with self.registry() as registry:
                registry.revoke_credential(self.players["alice"])
            revoked.set()

        with mock.patch.object(server, "_submit", held_submit):
            await alice.send(say("a1", "Quickly!"))
            await asyncio.to_thread(inside.wait, TIMEOUT)
            revoker = threading.Thread(target=revoke)
            revoker.start()
            self.assertFalse(await asyncio.to_thread(revoked.wait, 0.3))
            release.set()
            self.assertEqual((await self.reply_on(alice))["type"], "accepted")
            await asyncio.to_thread(revoker.join, TIMEOUT)
        self.assertTrue(revoked.is_set())  # after the acceptance, not before
        await self.job("harbor")
        self.finish("harbor", reply("Done."))
        await alice.send(SYNC)
        self.assertEqual((await self.reply_on(alice))["code"], "session_revoked")
        self.assertEqual(await self.closed(alice), (1008, "session_revoked"))

    async def test_revocation_while_busy_lets_the_accepted_job_finish(self):
        await self.start_host()
        alice, _ = await self.entered("alice", "harbor")
        bob, _ = await self.entered("bob", "harbor")
        await alice.send(say("a1", "I pull the lever."))
        self.assertEqual((await self.reply_on(alice))["type"], "accepted")
        await self.job("harbor")
        with self.registry() as registry:
            registry.revoke_credential(self.players["alice"])
        self.finish("harbor", reply("The gate opens."))
        heard = await self.until(bob, "message", **{"from": {"kind": "referee"}})
        self.assertEqual(heard["text"], "The gate opens.")
        self.assertEqual(self.jobs("harbor"), [("completed",)])
        code, reason = await self.closed(alice)  # and never got the reply
        self.assertEqual((code, reason), (1008, "session_revoked"))
        await bob.send(say("b2", "Next."))
        self.assertEqual((await self.reply_on(bob))["type"], "accepted")


class RuntimeTests(Lobby):
    async def test_campaigns_the_host_cannot_open_are_unavailable(self):
        harbor = self.root / "harbor.sqlite3"
        moved = self.root / "moved.sqlite3"
        shutil.move(harbor, moved)
        bridge = self.root / "bridge.sqlite3"
        bridge.unlink()
        with GameStore.create(bridge, DICE_RUNTIME):  # another game at its path
            pass
        await self.start_host()
        alice, _ = await self.login("alice")
        for campaign in ("harbor", "bridge"):
            refused = await self.select(alice, campaign)
            self.assertEqual(refused["code"], "campaign_unavailable")
            self.assertIn("cannot be opened right now", refused["message"])
        self.assertFalse(harbor.exists())  # never created, even to refuse it
        shutil.move(moved, harbor)  # back in place: the next select works
        self.assertEqual((await self.select(alice, "harbor"))["type"], "snapshot")

    async def test_a_file_removed_while_serving_is_unavailable(self):
        await self.start_host()
        alice, _ = await self.entered("alice", "harbor")
        harbor = self.root / "harbor.sqlite3"
        for suffix in ("", "-wal", "-shm"):
            Path(f"{harbor}{suffix}").unlink(missing_ok=True)
        await alice.send(SYNC)
        self.assertEqual((await self.next(alice))["code"], "campaign_unavailable")
        self.assertFalse(harbor.exists())

    async def test_an_unknown_module_is_unavailable_without_changing_the_game(self):
        path = self.root / "harbor.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE module SET module_id = 'not-installed'")
        before = path.read_bytes()
        await self.start_host()
        alice, _ = await self.login("alice")
        self.assertEqual(
            (await self.select(alice, "harbor"))["code"], "campaign_unavailable"
        )
        self.assertEqual(path.read_bytes(), before)


class NoRefereeFolderTests(Lobby):
    async def test_a_referee_campaign_without_a_referee_folder_is_unavailable(self):
        self.executor_for = None  # the real CodexReferee, in an empty folder
        await self.start_host(referee_dir=self.root / "no-referee")
        alice, _ = await self.login("alice")
        refused = await self.select(alice, "harbor")
        self.assertEqual(refused["code"], "campaign_unavailable")
        self.assertFalse((self.root / "no-referee").exists())


class ServeCommandTests(unittest.TestCase):
    def tesserae(self, *args, **options):
        return subprocess.run(
            [sys.executable, "-m", "tesserae", *args],
            text=True, capture_output=True, timeout=20, **options,
        )  # fmt: skip

    def test_refusals_start_nothing(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        registry = root / "host.sqlite3"
        HostRegistry.create(registry).close()
        base = ["serve", "--registry", str(registry), "--port", "0"]
        for extra in (
            ["--database", str(root / "g.sqlite3")],
            ["--authenticated"],
            ["--browser"],
            ["--tls-cert", "c.pem", "--tls-key", "k.pem"],
            ["--host", "192.0.2.1"],
        ):
            with self.subTest(extra=extra):
                result = self.tesserae(*base, *extra)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("nothing was started", result.stderr)
        self.assertFalse((root / "g.sqlite3").exists())
        missing = self.tesserae(
            "serve", "--registry", str(root / "none"), "--port", "0"
        )
        self.assertEqual(missing.returncode, 1, missing.stderr)
        self.assertFalse((root / "none").exists())
        neither = self.tesserae("serve", "--port", "0")
        self.assertEqual(neither.returncode, 2)

    def test_a_real_lobby_host_starts_admits_and_stops(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        registry_path = root / "host.sqlite3"
        token = new_token()
        with HostRegistry.create(registry_path) as registry:
            player = registry.add_player("Alice", token)
            game = root / "g.sqlite3"
            with GameStore.create(game, DICE_RUNTIME):
                pass
            campaign = registry.register_campaign(game, "Harbor")
            registry.set_campaign_active(campaign, True)
            registry.grant(campaign, player)
        host = subprocess.Popen(
            [sys.executable, "-m", "tesserae", "serve", "--registry", str(registry_path),
             "--port", "0", "--referee-dir", str(root / "referee")],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )  # fmt: skip
        self.addCleanup(lambda: host.poll() is None and host.kill())
        line = host.stdout.readline()
        self.assertIn("Tesserae lobby host listening on ws://127.0.0.1:", line)
        self.assertIn(str(registry_path.resolve()), line)
        uri = line.split()[5]

        async def play():
            async with connect(uri) as connection:
                await connection.send(message("login", token=token))
                lobby = json.loads(await connection.recv())
                await connection.send(message("select", campaign_id=campaign))
                snapshot = json.loads(await connection.recv())
                await connection.send(
                    json.dumps({"version": 1, "type": "command", "request_id": "r1",
                                "expected_revision": 0, "command": "roll",
                                "args": {"expression": "1d6"}})
                )  # fmt: skip
                while (result := json.loads(await connection.recv()))[
                    "type"
                ] == "revision":
                    pass
                return lobby, snapshot, result

        lobby, snapshot, result = asyncio.run(play())
        self.assertEqual([c["name"] for c in lobby["campaigns"]], ["Harbor"])
        self.assertEqual(snapshot["campaign"]["campaign_id"], campaign)
        self.assertEqual((result["type"], result["revision"]), ("result", 1))
        host.send_signal(signal.SIGINT)
        stdout, stderr = host.communicate(timeout=20)
        self.assertEqual(host.returncode, 0, stderr)
        self.assertIn("Tesserae lobby host stopped.", stdout)
        self.assertNotIn(token, stdout + stderr)
        self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()
