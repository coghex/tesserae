import asyncio
import json
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as sync_connect

from tesserae.credentials import new_token, read_token_file
from tesserae.protocol import session_error
from tesserae.server import GameHost
from tesserae.store import GameStore


def request(request_id, expected_revision, expression="2d6+3"):
    return json.dumps(
        {
            "version": 1,
            "type": "command",
            "request_id": request_id,
            "expected_revision": expected_revision,
            "command": "roll",
            "args": {"expression": expression},
        }
    )


def join_message(token):
    return json.dumps({"version": 1, "type": "join", "token": token})


SYNC = json.dumps({"version": 1, "type": "sync"})


async def exchange(connection, message):
    await connection.send(message)
    return json.loads(await asyncio.wait_for(connection.recv(), 5))


async def drain(connection):
    """Every message until the host closes, and the close code."""
    messages = []
    try:
        while True:
            messages.append(json.loads(await asyncio.wait_for(connection.recv(), 5)))
    except ConnectionClosed:
        return messages, connection.close_code


class AuthenticatedHostCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "game.sqlite3"
        self.tokens = {"alice": new_token(), "bob": new_token()}
        with GameStore(self.path) as store:
            self.game_id = store.game_id
            self.ids = {
                name: store.add_participant(name.title(), token)
                for name, token in self.tokens.items()
            }

    async def start_host(self, randint=None, join_timeout=5):
        loop = asyncio.get_running_loop()
        stop, ready = loop.create_future(), loop.create_future()
        host = GameHost(
            self.path, authenticated=True, randint=randint, join_timeout=join_timeout
        )
        task = asyncio.create_task(host.run(0, stop, on_ready=ready.set_result))

        async def shutdown():
            if not stop.done():
                stop.set_result(None)
            await asyncio.wait_for(task, 10)

        self.addAsyncCleanup(shutdown)
        self.uri = await asyncio.wait_for(ready, 5)
        return shutdown

    async def joined(self, name):
        connection = await connect(self.uri)
        self.addAsyncCleanup(connection.close)
        snapshot = await exchange(connection, join_message(self.tokens[name]))
        self.assertEqual(snapshot["type"], "snapshot", snapshot)
        return connection, snapshot

    def store(self):
        store = GameStore(self.path)
        self.addCleanup(store.close)
        return store

    def state(self):
        with GameStore(self.path) as store:
            return store.revision(), store.inspect_history()


class JoiningTests(AuthenticatedHostCase):
    async def test_joined_connection_acts_as_its_participant(self):
        await self.start_host(Mock(side_effect=[4, 3]))
        alice, snapshot = await self.joined("alice")
        self.assertEqual(
            snapshot,
            {
                "version": 1,
                "type": "snapshot",
                "game_id": self.game_id,
                "participant_id": self.ids["alice"],
                "revision": 0,
            },
        )
        result = await exchange(alice, request("r1", 0))
        self.assertEqual((result["type"], result["revision"]), ("result", 1))
        self.assertEqual((await exchange(alice, SYNC))["revision"], 1)

        again = await exchange(alice, join_message(self.tokens["bob"]))
        self.assertEqual(again, session_error("already_joined"))
        self.assertEqual(
            (await exchange(alice, SYNC))["participant_id"], self.ids["alice"]
        )
        [record] = self.state()[1]
        self.assertEqual(record.participant_id, self.ids["alice"])

    async def test_failed_joins_disclose_nothing_and_close(self):
        draw = Mock()
        await self.start_host(draw, join_timeout=0.3)
        bob, _ = await self.joined("bob")
        self.store().revoke_credential(self.ids["alice"])
        refused = session_error("authentication_failed")

        cases = [
            ("unknown token", join_message(new_token()), refused),
            ("revoked token", join_message(self.tokens["alice"]), refused),
            ("malformed token", join_message("has space"), refused),
            ("command first", request("r1", 0), session_error("join_required")),
            ("sync first", SYNC, session_error("join_required")),
            ("claimed identity", '{"version": 1, "type": "join", "participant_id": "x"}', None),
            ("malformed JSON", '{"version": 1, "type": "join"', None),
            ("binary frame", b"\x00join", None),
            ("silence", None, session_error("join_timeout")),
        ]  # fmt: skip
        for description, first, expected in cases:
            with self.subTest(description):
                connection = await connect(self.uri)
                if first is not None:
                    await connection.send(first)
                [reply], code = await drain(connection)
                self.assertEqual(code, 1008)
                self.assertEqual(
                    reply["code"], expected["code"] if expected else "invalid_request"
                )
                if expected:
                    self.assertEqual(reply, expected)
                self.assertIsNone(reply["revision"])
                self.assertNotIn(self.game_id, json.dumps(reply))
                self.assertNotIn(self.tokens["alice"], json.dumps(reply))

        draw.assert_not_called()
        self.assertEqual(self.state(), (0, []))
        self.assertEqual((await exchange(bob, SYNC))["type"], "snapshot")


class NamespaceTests(AuthenticatedHostCase):
    async def test_same_request_id_is_separate_per_participant_across_restart(self):
        draw = Mock(side_effect=[1, 1, 6, 6])
        shutdown = await self.start_host(draw)
        alice, _ = await self.joined("alice")
        bob, _ = await self.joined("bob")
        alice_r1 = await exchange(alice, request("r1", 0))
        bob_r1 = await exchange(bob, request("r1", 1))
        self.assertEqual((alice_r1["revision"], bob_r1["revision"]), (1, 2))
        self.assertNotEqual(alice_r1["result"], bob_r1["result"])
        await alice.close()

        alice, _ = await self.joined("alice")  # reconnect
        self.assertEqual(await exchange(alice, request("r1", 0)), alice_r1)
        await shutdown()

        await self.start_host(draw)  # restart
        bob, _ = await self.joined("bob")
        alice, _ = await self.joined("alice")
        self.assertEqual(await exchange(bob, request("r1", 1)), bob_r1)
        self.assertEqual(await exchange(alice, request("r1", 0)), alice_r1)
        self.assertEqual(draw.call_count, 4)
        self.assertEqual(self.state()[0], 2)


class RevocationTests(AuthenticatedHostCase):
    async def test_revoked_or_rotated_sessions_lose_protected_work(self):
        draw = Mock(return_value=3)
        await self.start_host(draw)
        alice, _ = await self.joined("alice")
        bob, _ = await self.joined("bob")
        await exchange(alice, request("r1", 0, "1d6"))
        before = self.state()

        self.store().revoke_credential(self.ids["alice"])
        await alice.send(request("r1", 0, "1d6"))  # even a replay is refused
        [reply], code = await drain(alice)
        self.assertEqual((reply, code), (session_error("session_revoked"), 1008))

        self.store().rotate_credential(self.ids["bob"], new_token())
        await bob.send(SYNC)
        [reply], code = await drain(bob)
        self.assertEqual((reply, code), (session_error("session_revoked"), 1008))

        new_bob = new_token()
        self.store().rotate_credential(self.ids["bob"], new_bob)
        self.tokens["bob"] = new_bob
        bob, snapshot = await self.joined("bob")
        self.assertEqual(snapshot["participant_id"], self.ids["bob"])
        self.store().revoke_credential(self.ids["bob"])
        reply = await exchange(bob, "{not json")  # an error would show the revision
        self.assertEqual(reply, session_error("session_revoked"))

        self.assertEqual(draw.call_count, 1)
        self.assertEqual(self.state(), before)


class AuthorizationOrderingTests(unittest.TestCase):
    def test_revocation_committed_while_waiting_for_the_guard_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.sqlite3"
            token = new_token()
            with GameStore(path) as store:
                participant = store.add_participant("Alice", token)
                context = store.authenticate(token)

            holder = sqlite3.connect(path, isolation_level=None)
            holder.execute("BEGIN IMMEDIATE")  # an operator holds the write guard
            draw, outcome = Mock(return_value=1), {}

            def act():
                with GameStore(path, randint=draw) as store:
                    outcome["reply"] = store.execute(context, request("r1", 0))

            actor = threading.Thread(target=act)
            actor.start()
            time.sleep(0.3)  # the action has been checked, if it checks too early
            holder.execute(
                "UPDATE credentials SET revoked_at = 'now' WHERE participant_id = ?",
                (participant,),
            )
            holder.execute("COMMIT")
            holder.close()
            actor.join(10)

            self.assertEqual(outcome["reply"], session_error("session_revoked"))
            draw.assert_not_called()
            with GameStore(path) as store:
                self.assertEqual((store.revision(), store.inspect_history()), (0, []))
                # Invalid input would otherwise reply with the current revision.
                self.assertEqual(
                    store.execute(context, "{"), session_error("session_revoked")
                )


class AuthenticatedProcessTests(unittest.TestCase):
    def test_serve_authenticated_with_operator_provisioning(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "game.sqlite3"
            token_file = Path(directory) / "alice.token"
            subprocess.run(
                [sys.executable, "-m", "tesserae", "participant", "add",
                 "--database", str(database), "--name", "Alice",
                 "--token-file", str(token_file)],
                check=True, capture_output=True, timeout=30,
            )  # fmt: skip
            token = read_token_file(token_file)
            outputs, replies = [], []
            for _ in range(2):  # the second run is a restart
                process = subprocess.Popen(
                    [sys.executable, "-m", "tesserae", "serve", "--authenticated",
                     "--database", str(database), "--port", "0"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )  # fmt: skip
                watchdog = threading.Timer(30, process.kill)
                watchdog.start()
                try:
                    line = process.stdout.readline()
                    self.assertIn("authenticated", line)
                    uri = re.search(r"ws://127\.0\.0\.1:[0-9]+/", line).group(0)
                    with sync_connect(uri, open_timeout=5) as connection:
                        connection.send(join_message(token))
                        snapshot = json.loads(connection.recv(timeout=5))
                        connection.send(request("r1", 0))
                        replies.append(json.loads(connection.recv(timeout=5)))
                    process.send_signal(signal.SIGINT)
                    stdout, stderr = process.communicate(timeout=15)
                finally:
                    watchdog.cancel()
                    if process.poll() is None:
                        process.kill()
                self.assertEqual(process.returncode, 0, stderr)
                self.assertNotIn("Traceback", stderr)
                outputs.append(line + stdout + stderr)

            self.assertEqual(replies[0], replies[1])
            self.assertEqual(snapshot["revision"], 1)
            for output in outputs:
                self.assertNotIn(token, output)
            with GameStore(database) as store:
                [record] = store.inspect_history()
                self.assertEqual(record.participant_id, snapshot["participant_id"])
