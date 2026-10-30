import json
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve as sync_serve

from tesserae.credentials import new_token, write_token_file
from tesserae.store import GameStore

TIMEOUT = 15


def roll(request_id, expected_revision, expression="1d20"):
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


class Process:
    """A subprocess whose stdout lines can be read with a timeout."""

    def __init__(self, test, *args):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "tesserae", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.lines, self.stdout = queue.Queue(), []
        threading.Thread(target=self._read, daemon=True).start()
        test.addCleanup(self.kill)

    def _read(self):
        for line in self.process.stdout:
            self.stdout.append(line)
            self.lines.put(line)

    def line(self) -> str:
        return self.lines.get(timeout=TIMEOUT)

    def message(self) -> dict:
        return json.loads(self.line())

    def send(self, *lines):
        self.process.stdin.write("".join(line + "\n" for line in lines))
        self.process.stdin.flush()

    def finish(self) -> int:
        """Close stdin and wait for exit."""
        self.process.stdin.close()
        return self.wait()

    def wait(self) -> int:
        code = self.process.wait(timeout=TIMEOUT)
        self.stderr = self.process.stderr.read()
        return code

    def signal(self, signum):
        self.process.send_signal(signum)

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if not stream.closed:
                stream.close()


class ClientCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.database = self.directory / "game.sqlite3"
        self.tokens, self.token_files = {}, {}
        with GameStore(self.database) as store:
            self.game_id = store.game_id
            for name in ("alice", "bob"):
                self.tokens[name] = new_token()
                store.add_participant(name.title(), self.tokens[name])
                self.token_files[name] = self.directory / f"{name}.token"
                write_token_file(self.token_files[name], self.tokens[name])
        self.processes = []

    def start_host(self):
        host = Process(
            self,
            "serve",
            "--authenticated",
            "--database",
            str(self.database),
            "--port",
            "0",
        )
        self.uri = re.search(r"ws://127\.0\.0\.1:[0-9]+/", host.line()).group(0)
        self.host = host
        return host

    def stop_host(self):
        self.host.signal(signal.SIGINT)
        self.assertEqual(self.host.wait(), 0)

    def connect(self, name, *extra, uri=None):
        client = Process(
            self,
            "connect",
            uri or self.uri,
            "--token-file",
            str(self.token_files[name]),
            *extra,
        )
        self.processes.append(client)
        return client

    def joined(self, name):
        client = self.connect(name)
        snapshot = client.message()
        self.assertEqual(snapshot["type"], "snapshot", snapshot)
        return client, snapshot

    def assertCleanOutput(self, client):
        for line in client.stdout:
            json.loads(line)  # one JSON object per line, nothing else
        for token in self.tokens.values():
            self.assertNotIn(token, "".join(client.stdout) + client.stderr)
        self.assertNotIn("Traceback", client.stderr)

    def history(self):
        with GameStore(self.database) as store:
            return store.revision(), store.inspect_history()


class StreamingTests(ClientCase):
    def test_two_identities_stream_without_lockstep(self):
        self.start_host()
        alice, alice_view = self.joined("alice")
        bob, bob_view = self.joined("bob")
        self.assertNotEqual(alice_view["participant_id"], bob_view["participant_id"])
        self.assertEqual(alice_view["game_id"], self.game_id)

        alice.send(roll("r1", 0), "", roll("r2", 1))  # both before any reply
        alice_results = []
        while len(alice_results) < 2:
            message = alice.message()
            if message["type"] == "result":
                alice_results.append(message)
        self.assertEqual([m["request_id"] for m in alice_results], ["r1", "r2"])

        seen = 0
        while seen < 2:  # Bob is idle, but hears about both commits
            update = bob.message()
            self.assertEqual(set(update), {"version", "type", "game_id", "revision"})
            seen = update["revision"]
        bob.send(roll("r1", 2))  # the same ID as Alice's, in his own namespace
        while (mine := bob.message())["type"] != "result":
            pass
        self.assertEqual(mine["revision"], 3)

        self.assertEqual(alice.finish(), 0)
        self.assertEqual(bob.finish(), 0)
        for client in (alice, bob):
            self.assertCleanOutput(client)
            self.assertEqual(client.stderr, "")
        bob_results = [json.loads(line) for line in bob.stdout if '"result"' in line]
        self.assertEqual(bob_results, [mine])
        self.assertEqual(self.history()[0], 3)

    def test_concurrent_fresh_requests_cannot_both_roll(self):
        self.start_host()
        alice, _ = self.joined("alice")
        bob, _ = self.joined("bob")
        alice.send(roll("a1", 0))
        bob.send(roll("b1", 0))
        self.assertEqual((alice.finish(), bob.finish()), (0, 0))
        messages = [json.loads(line) for c in (alice, bob) for line in c.stdout]
        replies = [m for m in messages if m["type"] not in ("snapshot", "revision")]
        outcomes = sorted(r.get("code", r["type"]) for r in replies)
        self.assertEqual(outcomes, ["result", "stale_revision"])
        self.assertEqual(len(self.history()[1]), 1)

    def test_reconnect_and_restart_retry_returns_the_original(self):
        self.start_host()
        alice, _ = self.joined("alice")
        alice.send(roll("r1", 0))
        original = alice.message()
        self.assertEqual(alice.finish(), 0)

        alice, _ = self.joined("alice")  # reconnect
        alice.send(roll("r1", 0))
        self.assertEqual(alice.message(), original)
        self.assertEqual(alice.finish(), 0)

        self.stop_host()
        self.start_host()  # restart
        alice, snapshot = self.joined("alice")
        self.assertEqual(snapshot["revision"], 1)
        alice.send(roll("r1", 0))
        self.assertEqual(alice.message(), original)
        self.assertEqual(alice.finish(), 0)
        self.assertEqual(len(self.history()[1]), 1)


class ReplyTests(ClientCase):
    def test_syncs_and_rejected_lines_get_their_replies(self):
        self.start_host()
        alice, snapshot = self.joined("alice")
        alice.send(
            '{"version": 1, "type": "sync"}',
            "not json",
            json.dumps({"version": 1, "type": "join", "token": "x"}),
            roll("r1", 5),
        )
        replies = [alice.message() for _ in range(4)]
        self.assertEqual(replies[0], snapshot)
        self.assertEqual(
            [r.get("code") for r in replies[1:]],
            ["invalid_request", "already_joined", "stale_revision"],
        )
        self.assertEqual(alice.finish(), 0)
        self.assertCleanOutput(alice)


class FailureTests(ClientCase):
    def test_failed_starts_exit_nonzero_and_send_nothing(self):
        self.start_host()
        self.token_files["mallory"] = self.directory / "mallory.token"
        write_token_file(self.token_files["mallory"], new_token())
        self.token_files["missing"] = self.directory / "missing.token"
        self.tokens["mallory"] = self.token_files["mallory"].read_text().strip()

        cases = [
            ("wrong token", "mallory", (), None, "refused to join: authentication_failed"),
            ("game mismatch", "alice", ("--game-id", "another-game"), None, "nothing was sent"),
            ("missing token file", "missing", (), None, "cannot read token file"),
            ("connection refused", "alice", (), "ws://127.0.0.1:1/", "cannot connect"),
            ("secret in URI", "alice", (), self.uri + "?token=hunter2", "must not carry"),
            ("bad IPv6 URI", "alice", (), "ws://[::1/", "must look like"),
            ("port out of range", "alice", (), "ws://127.0.0.1:99999/", "must look like"),
            ("port not a number", "alice", (), "ws://127.0.0.1:abc/", "must look like"),
        ]  # fmt: skip
        for description, name, extra, uri, diagnostic in cases:
            with self.subTest(description):
                client = self.connect(name, *extra, uri=uri)
                client.send(roll("r1", 0))  # must never be sent
                self.assertEqual(client.finish(), 1)
                self.assertIn(diagnostic, client.stderr)
                self.assertNotIn("hunter2", client.stderr)
                self.assertCleanOutput(client)
        self.assertEqual(self.history(), (0, []))

    def test_ctrl_c_and_host_disconnect_end_promptly(self):
        self.start_host()
        idle, _ = self.joined("bob")
        idle.signal(signal.SIGINT)  # stdin is open and idle
        self.assertEqual(idle.wait(), 130)
        self.assertCleanOutput(idle)

        waiting, _ = self.joined("alice")  # the host still serves others
        self.stop_host()
        self.assertEqual(waiting.wait(), 1)  # stdin is still open
        self.assertIn("host closed the connection", waiting.stderr)
        self.assertCleanOutput(waiting)


class UnansweredRequestTests(ClientCase):
    """A host that never answers is not something the real host does; these
    check that the client's bounded drain reports it rather than hanging."""

    def fake_host(self, behavior, greet=True):
        def handler(connection):
            if not greet:
                return behavior(connection)
            json.loads(connection.recv())  # the join
            connection.send(
                json.dumps(
                    {"version": 1, "type": "snapshot", "game_id": "g",
                     "participant_id": "p_fake", "revision": 0}
                )
            )  # fmt: skip
            behavior(connection)

        server = sync_serve(handler, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f"ws://127.0.0.1:{server.socket.getsockname()[1]}/"

    def test_drain_timeout_names_the_uncertain_requests(self):
        def ignore_everything(connection):
            try:
                for _ in connection:
                    pass
            except ConnectionClosed:
                pass

        uri = self.fake_host(ignore_everything)
        client = self.connect("alice", "--drain-timeout", "0.5", uri=uri)
        self.assertEqual(client.message()["type"], "snapshot")
        client.send(roll("r1", 0))
        self.assertEqual(client.finish(), 3)
        self.assertIn("'r1'", client.stderr)
        self.assertIn("resend those lines unchanged", client.stderr)
        self.assertCleanOutput(client)

    def test_host_closing_at_once_is_a_clean_failure(self):
        uri = self.fake_host(lambda connection: connection.close(), greet=False)
        client = self.connect("alice", uri=uri)
        self.assertEqual(client.finish(), 1)
        self.assertIn("host closed the connection", client.stderr)
        self.assertCleanOutput(client)

    def test_invalid_replies_leave_the_request_uncertain(self):
        # Each is answered to a roll; none of them is a valid answer to it.
        bogus = {
            "unknown type": json.dumps({"version": 1, "type": "ack"}),
            "not JSON": "ok!",
            "another request's result": json.dumps(
                {"version": 1, "type": "result", "request_id": "r9", "revision": 1,
                 "result": {}}
            ),
            "snapshot instead of a result": json.dumps(
                {"version": 1, "type": "snapshot", "game_id": "g",
                 "participant_id": "p_fake", "revision": 1}
            ),
            "result without a payload": json.dumps(
                {"version": 1, "type": "result", "request_id": "r1", "revision": 1}
            ),
            "error without a message": json.dumps(
                {"version": 1, "type": "error", "request_id": "r1", "revision": 0,
                 "code": "stale_revision"}
            ),
            "unsupported version": json.dumps(
                {"version": 2, "type": "result", "request_id": "r1", "revision": 1,
                 "result": {}}
            ),
        }  # fmt: skip
        for description, reply in bogus.items():
            with self.subTest(description):

                def answer_wrongly(connection, reply=reply):
                    connection.recv()
                    connection.send(reply)
                    try:
                        for _ in connection:
                            pass
                    except ConnectionClosed:
                        pass

                client = self.connect("alice", uri=self.fake_host(answer_wrongly))
                self.assertEqual(client.message()["type"], "snapshot")
                client.send(roll("r1", 0))
                self.assertEqual(client.wait(), 3)
                self.assertIn("'r1'", client.stderr)
                self.assertIn("resend those lines unchanged", client.stderr)
                self.assertEqual(
                    len(client.stdout), 1
                )  # the bogus reply is not printed
                self.assertCleanOutput(client)

    def test_disconnect_with_a_pending_request_is_uncertain(self):
        def close_on_first_command(connection):
            connection.recv()
            connection.close()

        uri = self.fake_host(close_on_first_command)
        client = self.connect("alice", uri=uri)
        self.assertEqual(client.message()["type"], "snapshot")
        client.send(roll("r1", 0))
        self.assertEqual(client.wait(), 3)  # stdin is still open
        self.assertIn("'r1'", client.stderr)
        self.assertCleanOutput(client)
