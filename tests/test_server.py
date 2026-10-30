import asyncio
import json
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from websockets.asyncio.client import connect
from websockets.sync.client import connect as sync_connect

from tesserae.server import DEVELOPMENT_PARTICIPANT, DevelopmentHost
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


async def exchange(connection, message):
    await connection.send(message)
    return json.loads(await connection.recv())


async def welcomed(uri):
    connection = await connect(uri)
    welcome = json.loads(await asyncio.wait_for(connection.recv(), 5))
    return connection, welcome


class TemporaryStore:
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "game.sqlite3"

    def history(self):
        with GameStore(self.path) as store:
            return store.revision(), store.inspect_history()


class DevelopmentHostTests(TemporaryStore, unittest.IsolatedAsyncioTestCase):
    async def start_host(self, randint=None):
        loop = asyncio.get_running_loop()
        self.stop, ready = loop.create_future(), loop.create_future()
        host = DevelopmentHost(self.path, randint=randint)
        self.host_task = asyncio.create_task(
            host.run(0, self.stop, on_ready=ready.set_result)
        )
        self.addAsyncCleanup(self.stop_host)
        return await asyncio.wait_for(ready, 5)

    async def stop_host(self):
        if not self.stop.done():
            self.stop.set_result(None)
        await asyncio.wait_for(self.host_task, 10)

    async def test_welcome_roll_and_replay_after_reconnect(self):
        draw = Mock(side_effect=[4, 3])
        uri = await self.start_host(draw)
        self.assertRegex(uri, r"^ws://127\.0\.0\.1:[1-9][0-9]*/$")

        first, welcome = await welcomed(uri)
        self.assertEqual(welcome, {"version": 1, "type": "welcome", "revision": 0})
        result = await exchange(first, request("r1", 0))
        self.assertEqual((result["type"], result["revision"]), ("result", 1))
        self.assertEqual(result["result"]["terms"][0]["rolls"], [4, 3])
        await first.close()

        second, welcome = await welcomed(uri)
        self.assertEqual(welcome["revision"], 1)
        self.assertEqual(await exchange(second, request("r1", 0)), result)
        await second.close()

        self.assertEqual(draw.call_count, 2)
        revision, records = self.history()
        self.assertEqual(revision, 1)
        self.assertEqual(
            [r.participant_id for r in records],
            [DEVELOPMENT_PARTICIPANT.participant_id],
        )

    async def test_bad_input_and_disconnects_do_not_affect_other_connections(self):
        uri = await self.start_host(Mock(return_value=1))
        noisy, _ = await welcomed(uri)
        steady, _ = await welcomed(uri)

        for bad in ("not json", b"\x00binary", '{"type": "chat", "text": "hi"}'):
            with self.subTest(bad):
                reply = await exchange(noisy, bad)
                self.assertEqual(
                    (reply["type"], reply["code"]), ("error", "invalid_request")
                )
        noisy.transport.abort()

        reply = await exchange(steady, request("r1", 0, "1d1"))
        self.assertEqual(reply["result"]["total"], 1)
        await steady.close()

    async def test_concurrent_requests_execute_at_most_once(self):
        draw = Mock(return_value=1)
        uri = await self.start_host(draw)
        a, _ = await welcomed(uri)
        b, _ = await welcomed(uri)

        replies = await asyncio.gather(
            exchange(a, request("a1", 0, "1d6")), exchange(b, request("b1", 0, "1d6"))
        )
        self.assertEqual(
            sorted(r.get("code", "result") for r in replies),
            ["result", "stale_revision"],
        )

        retries = await asyncio.gather(
            exchange(a, request("r2", 1, "1d6")), exchange(b, request("r2", 1, "1d6"))
        )
        self.assertEqual(retries[0], retries[1])
        self.assertEqual(retries[0]["revision"], 2)
        self.assertEqual(draw.call_count, 2)
        self.assertEqual(len(self.history()[1]), 2)
        await a.close()
        await b.close()

    async def test_slow_store_work_does_not_stall_other_connections(self):
        holding, release = threading.Event(), threading.Event()

        def slow_draw(low, high):
            holding.set()
            release.wait(10)
            return 1

        uri = await self.start_host(slow_draw)
        slow, _ = await welcomed(uri)
        await slow.send(request("r1", 0, "1d6"))
        self.assertTrue(await asyncio.to_thread(holding.wait, 5))

        # The roll holds the write lock; another connection is still served.
        other, welcome = await asyncio.wait_for(welcomed(uri), 2)
        self.assertEqual(welcome["revision"], 0)
        reply = await asyncio.wait_for(exchange(other, "{"), 2)
        self.assertEqual(reply["code"], "invalid_request")

        # Stopping mid-roll waits for the in-flight transaction to finish whole.
        self.stop.set_result(None)
        threading.Timer(0.3, release.set).start()
        await asyncio.wait_for(self.host_task, 10)
        revision, records = self.history()
        self.assertEqual((revision, len(records)), (1, 1))

    async def test_unexpected_store_failure_reports_internal_error(self):
        uri = await self.start_host(Mock(return_value=1))
        connection, _ = await welcomed(uri)
        with (
            patch.object(
                GameStore, "execute", side_effect=RuntimeError("disk on fire")
            ),
            self.assertLogs("tesserae.server", "ERROR") as logs,
        ):
            reply = await exchange(connection, request("r1", 0))
        self.assertEqual(reply["code"], "internal_error")
        self.assertEqual(reply["request_id"], "r1")
        self.assertNotIn("disk on fire", reply["message"])
        self.assertIn("disk on fire", "\n".join(logs.output))
        self.assertEqual(self.history(), (0, []))

        reply = await exchange(connection, request("r1", 0))
        self.assertEqual(reply["type"], "result")
        await connection.close()


class ServeProcessTests(TemporaryStore, unittest.TestCase):
    def serve(self, *args):
        return subprocess.Popen(
            [sys.executable, "-m", "tesserae", "serve", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def start(self):
        process = self.serve("--database", str(self.path), "--port", "0")
        watchdog = threading.Timer(30, process.kill)
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        line = process.stdout.readline()
        match = re.search(r"ws://127\.0\.0\.1:([0-9]+)/", line)
        self.assertIsNotNone(match, line)
        self.assertNotEqual(match.group(1), "0")
        self.assertIn(str(self.path.resolve()), line)
        return process, match.group(0)

    def interrupt(self, process):
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("stopped", stdout)
        self.assertNotIn("Traceback", stderr)

    def roll(self, uri, request_id, expected_revision):
        with sync_connect(uri, open_timeout=5) as connection:
            welcome = json.loads(connection.recv(timeout=5))
            connection.send(request(request_id, expected_revision))
            return welcome, json.loads(connection.recv(timeout=5))

    def test_sigint_stops_cleanly_and_restart_replays(self):
        process, uri = self.start()
        welcome, result = self.roll(uri, "r1", 0)
        self.assertEqual((welcome["revision"], result["revision"]), (0, 1))
        self.interrupt(process)

        process, uri = self.start()
        welcome, replay = self.roll(uri, "r1", 0)
        self.assertEqual(welcome["revision"], 1)
        self.assertEqual(replay, result)
        self.interrupt(process)

        revision, records = self.history()
        self.assertEqual((revision, len(records)), (1, 1))

    def test_startup_failures_exit_nonzero_with_diagnostics(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        busy_port = str(listener.getsockname()[1])
        foreign = self.path.with_name("foreign.sqlite3")
        foreign.write_bytes(b"not a database" * 100)
        missing = self.path.with_name("missing") / "game.sqlite3"

        cases = [
            (["--database", str(self.path), "--port", "65536"], 2, "65535"),
            (["--database", str(self.path), "--port", "http"], 2, "65535"),
            (["--port", "0"], 2, "--database"),
            (["--database", str(self.path), "--port", busy_port], 1, "cannot listen"),
            (["--database", str(missing), "--port", "0"], 1, "cannot open database"),
            (["--database", str(foreign), "--port", "0"], 1, "cannot open database"),
        ]
        for args, code, diagnostic in cases:
            with self.subTest(args=args):
                process = self.serve(*args)
                _, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, code, stderr)
                self.assertIn(diagnostic, stderr)
                self.assertNotIn("Traceback", stderr)
        self.assertEqual(foreign.read_bytes(), b"not a database" * 100)
        self.assertFalse(missing.parent.exists())
