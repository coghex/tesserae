import asyncio
import contextlib
import io
import json
import re
import signal
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from tesserae.builtin import CATALOG
from tesserae.credentials import new_token, write_token_file
from tesserae.server import GameHost, HostConfigError, run_foreground
from tesserae.store import GameStore, StoreError

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_client import Process
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_client import Process

RELAY = CATALOG.resolve("relay")
NOTE = RELAY.initial_state()["referee"]["maintenance_note"]
SYNC = json.dumps({"version": 1, "type": "sync"})
MODULE = {"id": "relay", "version": "1", "state_version": 1}


def command(request_id, expected_revision, name, args=None):
    return json.dumps(
        {"version": 1, "type": "command", "request_id": request_id,
         "expected_revision": expected_revision, "command": name,
         "args": args or {}}
    )  # fmt: skip


def enlist(request_id, expected_revision, name):
    return command(request_id, expected_revision, "relay.enlist", {"name": name})


def repair(request_id, expected_revision):
    return command(request_id, expected_revision, "relay.repair")


class Player:
    """One joined connection that keeps every message the host sent it."""

    def __init__(self, connection):
        self.connection = connection
        self.received = []

    async def recv(self) -> dict:
        message = json.loads(await asyncio.wait_for(self.connection.recv(), 5))
        self.received.append(message)
        return message

    async def reply(self) -> dict:
        """The next reply, skipping revision notifications."""
        while (message := await self.recv())["type"] == "revision":
            pass
        return message

    async def exchange(self, message) -> dict:
        await self.connection.send(message)
        return await self.reply()

    async def notified(self, revision):
        while True:
            message = await self.recv()
            self.assertNotification(message)
            if message["revision"] >= revision:
                return

    @staticmethod
    def assertNotification(message):
        assert message["type"] == "revision", message
        assert set(message) == {"version", "type", "game_id", "revision"}, message


def relay_game(directory) -> tuple[Path, str, dict, dict]:
    path = Path(directory) / "relay.sqlite3"
    tokens = {"alice": new_token(), "bob": new_token()}
    with GameStore.create(path, RELAY) as store:
        ids = {
            name: store.add_participant(name.title(), token)
            for name, token in tokens.items()
        }
        return path, store.game_id, tokens, ids


class RelayHostCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path, self.game_id, self.tokens, self.ids = relay_game(directory.name)

    async def start_host(self, randint=None):
        loop = asyncio.get_running_loop()
        stop, ready = loop.create_future(), loop.create_future()
        self.host = GameHost(self.path, authenticated=True, randint=randint)
        task = asyncio.create_task(self.host.run(0, stop, on_ready=ready.set_result))

        async def shutdown():
            if not stop.done():
                stop.set_result(None)
            await asyncio.wait_for(task, 10)

        self.addAsyncCleanup(shutdown)
        self.uri = await asyncio.wait_for(ready, 5)
        return shutdown

    async def joined(self, name) -> tuple[Player, dict]:
        connection = await connect(self.uri)
        self.addAsyncCleanup(connection.close)
        player = Player(connection)
        snapshot = await player.exchange(
            json.dumps({"version": 1, "type": "join", "token": self.tokens[name]})
        )
        self.assertEqual(snapshot["type"], "snapshot", snapshot)
        return player, snapshot

    def assertPrivate(self, player, *hidden):
        """Nothing `player` was ever sent contains another player's or the
        referee's data, or any token."""
        text = json.dumps(player.received)
        for value in (*hidden, NOTE, "workers", "referee", *self.tokens.values()):
            self.assertNotIn(value, text)


class RelayViewTests(RelayHostCase):
    async def test_players_see_the_scene_and_only_their_own_worker(self):
        await self.start_host(Mock(side_effect=[3, 4]))
        alice, snapshot = await self.joined("alice")
        self.assertEqual(
            {key: snapshot[key] for key in snapshot if key != "view"},
            {"version": 1, "type": "snapshot", "game_id": self.game_id,
             "participant_id": self.ids["alice"], "revision": 0,
         "participant_name": "Alice", "module": MODULE},
        )  # fmt: skip
        self.assertEqual(
            set(snapshot["view"]), {"scene", "rules", "character", "commands"}
        )
        self.assertEqual(
            (snapshot["view"]["character"], snapshot["view"]["commands"]),
            (None, ["relay.enlist"]),
        )
        bob, _ = await self.joined("bob")

        result = await alice.exchange(enlist("a1", 0, "Ada"))
        self.assertEqual(
            result["result"]["worker"], {"name": "Ada", "repair": 1, "effort": 3}
        )
        await bob.notified(1)  # metadata only; Bob syncs to see anything new
        await bob.exchange(enlist("b1", 1, "Bo"))
        await alice.notified(2)
        failure = await bob.exchange(repair("b2", 2))
        self.assertEqual(
            (failure["revision"], failure["result"]["check"]["total"]), (3, 4)
        )
        success = await bob.exchange(repair("b3", 3))
        self.assertEqual(success["result"]["relay"], {"restored": True})
        await alice.notified(4)

        mine = await alice.exchange(SYNC)
        self.assertEqual(mine["revision"], 4)
        self.assertEqual(
            mine["view"]["character"], {"name": "Ada", "repair": 1, "effort": 3}
        )
        self.assertEqual(
            (mine["view"]["scene"]["restored"], mine["view"]["commands"]), (True, [])
        )
        theirs = await bob.exchange(SYNC)
        self.assertEqual(theirs["view"]["character"]["effort"], 1)

        refused = await alice.exchange(repair("a2", 4))
        self.assertEqual(
            (refused["type"], refused["code"], refused["revision"]),
            ("error", "action_rejected", 4),
        )
        unknown = await alice.exchange(command("a3", 4, "roll", {"expression": "1d20"}))
        self.assertEqual(unknown["code"], "unsupported_command")
        self.assertEqual(await bob.exchange(repair("b3", 3)), success)  # replay

        self.assertPrivate(alice, '"Bo"', self.ids["bob"])
        self.assertPrivate(bob, '"Ada"', self.ids["alice"])
        with GameStore(self.path) as store:
            self.assertEqual(store.revision(), 4)

    async def test_views_keep_their_revision_when_commits_race_join_and_sync(self):
        await self.start_host(Mock(return_value=1))  # every repair fails
        alice, _ = await self.joined("alice")
        await alice.exchange(enlist("a1", 0, "Ada"))
        await alice.notified(1)
        loop = asyncio.get_running_loop()
        real = GameStore.participant_view
        racing = {"next": 2}

        def read_then_commit(store, context):
            current = real(store, context)  # the snapshot is read...
            if racing["next"]:
                # ...then a repair commits and is announced before it is sent.
                with GameStore(self.path, randint=Mock(return_value=1)) as other:
                    reply = other.execute(
                        other.authenticate(self.tokens["alice"]),
                        repair(f"race{racing['next']}", current.revision),
                    )
                racing["next"] -= 1
                loop.call_soon_threadsafe(self.host._publish, reply["revision"])
                time.sleep(0.3)  # time for the notification to try to go first
            return current

        def assertPaired(snapshot, revision):  # each repair spent one effort
            self.assertEqual(
                (snapshot["revision"], snapshot["view"]["character"]["effort"]),
                (revision, 4 - revision),
            )

        with patch.object(GameStore, "participant_view", read_then_commit):
            second, joined = await self.joined("alice")
            assertPaired(joined, 1)
            assertPaired(await second.exchange(SYNC), 2)
        await second.notified(3)  # the raced commit is announced afterwards
        assertPaired(await second.exchange(SYNC), 3)
        sent = [m["revision"] for m in second.received]
        self.assertEqual(sent, sorted(sent))  # never backwards

    async def test_revoked_and_failed_snapshots_send_no_view(self):
        await self.start_host()
        bob, _ = await self.joined("bob")
        await bob.exchange(enlist("b1", 0, "Bo"))

        failing = patch.object(
            GameStore, "participant_view", side_effect=OSError("disk")
        )
        with failing, self.assertLogs("tesserae.server", "ERROR"):
            self.assertEqual((await bob.exchange(SYNC))["code"], "internal_error")
            joining = await connect(self.uri)
            self.addAsyncCleanup(joining.close)
            await joining.send(
                json.dumps(
                    {"version": 1, "type": "join", "token": self.tokens["alice"]}
                )
            )
            refused = json.loads(await asyncio.wait_for(joining.recv(), 5))
            self.assertEqual(refused["code"], "internal_error")
            with self.assertRaises(ConnectionClosed):
                await asyncio.wait_for(joining.recv(), 5)
        self.assertEqual(
            [s.context.participant_id for s in self.host._sessions], [self.ids["bob"]]
        )

        with GameStore(self.path) as store:
            store.revoke_credential(self.ids["bob"])
        await bob.connection.send(SYNC)
        with self.assertRaises(ConnectionClosed):
            while True:
                await bob.recv()
        self.assertEqual(bob.received[-1]["code"], "session_revoked")
        self.assertEqual(bob.connection.close_code, 1008)
        self.assertNotIn("view", bob.received[-1])

    async def test_shutdown_is_finite_with_players_joined(self):
        shutdown = await self.start_host()
        players = [(await self.joined(name))[0] for name in ("alice", "bob")]
        await shutdown()
        for player in players:
            with self.assertRaises(ConnectionClosed):
                await player.recv()


class RelayStartupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path, *_ = relay_game(directory.name)

    def serve(self, *, authenticated=False):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = run_foreground(str(self.path), 0, authenticated=authenticated)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_relay_refuses_development_mode(self):
        with self.assertRaises(HostConfigError):
            GameHost(self.path)
        code, stdout, stderr = self.serve()
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("relay, requires --authenticated", stderr)

    def test_unknown_or_mismatched_modules_fail_before_listening(self):
        for column, value in [("module_version", "2"), ("module_id", "chess"),
                              ("providers", "[]")]:  # fmt: skip
            with self.subTest(column=column):
                original = self.binding(column)
                self.binding(column, value)
                with self.assertRaises(StoreError):
                    GameHost(self.path, authenticated=True)
                code, stdout, stderr = self.serve(authenticated=True)
                self.assertEqual((code, stdout), (1, ""))
                self.assertIn("cannot open database", stderr)
                self.assertEqual(self.binding(column), value)  # nothing was reset
                self.binding(column, original)
        GameHost(self.path, authenticated=True)

    def binding(self, column, value=None):
        """Read one module binding column, or overwrite it."""
        with contextlib.closing(sqlite3.connect(self.path)) as database:
            if value is None:
                return database.execute(f"SELECT {column} FROM module").fetchone()[0]
            with database:
                database.execute(f"UPDATE module SET {column} = ?", (value,))


class RelayProcessTests(unittest.TestCase):
    def test_connect_prints_views_and_results_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path, game_id, tokens, ids = relay_game(directory)
            token_file = Path(directory) / "alice.token"
            write_token_file(token_file, tokens["alice"])
            host = Process(
                self, "serve", "--authenticated", "--database", str(path), "--port", "0"
            )
            line = host.line()
            self.assertIn("module: relay 1", line)
            uri = re.search(r"ws://127\.0\.0\.1:[0-9]+/", line).group(0)
            client = Process(self, "connect", uri, "--token-file", str(token_file))
            joined = client.message()
            self.assertEqual(
                (joined["module"], joined["view"]["character"]), (MODULE, None)
            )
            client.send(enlist("a1", 0, "Ada"), SYNC)
            self.assertEqual(client.finish(), 0)
            host.signal(signal.SIGINT)
            self.assertEqual(host.wait(), 0)

            messages = [json.loads(line) for line in client.stdout[1:]]
            result, synced = [m for m in messages if m["type"] != "revision"]
            self.assertEqual(result["result"]["worker"]["name"], "Ada")
            self.assertEqual(
                (synced["revision"], synced["view"]["character"]["effort"]), (1, 3)
            )
            self.assertEqual(synced["participant_id"], ids["alice"])
            output = "".join(client.stdout) + client.stderr + host.stderr
            self.assertNotIn(tokens["alice"], output)
            self.assertNotIn(NOTE, output)
            self.assertNotIn("Traceback", output)


if __name__ == "__main__":
    unittest.main()
