"""The combined encrypted flow: real `tesserae serve` over TLS and real
`tesserae connect --ca-file` clients. Mechanics, replay rules, and failure
handling are covered by the store, host, and client tests; this checks that
they hold end to end through TLS."""

import json
import re
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tesserae.credentials import new_token, write_token_file
from tesserae.store import GameStore

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_client import Process
    from test_transport import make_certificate, openssl_tools
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_client import Process
    from tests.test_transport import make_certificate, openssl_tools


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


class EncryptedMultiplayerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.database = self.directory / "game.sqlite3"
        openssl = openssl_tools()[0]
        self.cert, self.key = make_certificate(openssl, self.directory, "IP:127.0.0.1")
        self.unrelated, _ = make_certificate(
            openssl, self.directory, "IP:127.0.0.1", "unrelated"
        )
        self.tokens, self.token_files = {}, {}
        # Bob's valid ID deliberately contains Alice's dice expression. Privacy
        # assertions must inspect messages, not search opaque identity strings.
        with (
            GameStore(self.database) as store,
            patch(
                "tesserae.store.secrets.token_hex",
                side_effect=["a" * 16, "3d6" + "b" * 13],
            ),
        ):
            for name in ("alice", "bob"):
                self.tokens[name] = new_token()
                store.add_participant(name.title(), self.tokens[name])
                self.token_files[name] = self.directory / f"{name}.token"
                write_token_file(self.token_files[name], self.tokens[name])
        self.processes = []

    def start_host(self):
        host = Process(
            self, "serve", "--authenticated", "--database", str(self.database),
            "--port", "0", "--tls-cert", str(self.cert), "--tls-key", str(self.key),
        )  # fmt: skip
        self.uri = re.search(r"wss://127\.0\.0\.1:[0-9]+/", host.line()).group(0)
        self.processes.append(host)
        self.host = host

    def stop_host(self):
        self.host.signal(signal.SIGINT)
        self.assertEqual(self.host.wait(), 0)

    def connect(self, name, ca_file=None):
        client = Process(
            self, "connect", self.uri, "--token-file", str(self.token_files[name]),
            "--ca-file", str(ca_file or self.cert),
        )  # fmt: skip
        self.processes.append(client)
        return client

    def joined(self, name):
        client = self.connect(name)
        snapshot = client.message()
        self.assertEqual(snapshot["type"], "snapshot", snapshot)
        return client, snapshot

    def until(self, client, kind):
        while (message := client.message())["type"] != kind:
            pass
        return message

    def test_two_players_over_tls(self):
        self.start_host()
        alice, alice_view = self.joined("alice")
        bob, bob_view = self.joined("bob")
        self.assertNotEqual(alice_view["participant_id"], bob_view["participant_id"])

        alice.send(roll("r1", 0, "3d6"))
        result = self.until(alice, "result")
        update = self.until(bob, "revision")  # Bob is idle and only told the revision
        self.assertEqual(set(update), {"version", "type", "game_id", "revision"})
        self.assertEqual(update["revision"], 1)

        alice.send(roll("a2", 1))  # both fresh at revision 1, at the same time
        bob.send(roll("b2", 1))
        replies = []
        for client in (alice, bob):
            while (message := client.message())["type"] == "revision":
                pass
            replies.append(message)
        outcomes = sorted(r.get("code", r["type"]) for r in replies)
        self.assertEqual(outcomes, ["result", "stale_revision"])
        self.assertEqual((alice.finish(), bob.finish()), (0, 0))

        self.stop_host()
        self.start_host()  # restart, then replay Alice's first roll unchanged
        alice, snapshot = self.joined("alice")
        self.assertEqual(snapshot["revision"], 2)
        alice.send(roll("r1", 0, "3d6"))
        self.assertEqual(self.until(alice, "result"), result)
        self.assertEqual(alice.finish(), 0)
        self.stop_host()

        # Bob receives his snapshot, revision notices and only his own reply.
        # Inspect structured messages: opaque IDs may legitimately contain 3d6.
        bob_messages = [json.loads(line) for line in bob.stdout]
        self.assertEqual(
            [message for message in bob_messages if message["type"] != "revision"],
            [bob_view, replies[1]],
        )
        self.assertEqual(replies[1]["request_id"], "b2")
        if replies[1]["type"] == "result":
            self.assertEqual(replies[1]["result"]["expression"], "1d20")
        for message in bob_messages:
            if message["type"] == "revision":
                self.assertEqual(
                    set(message), {"version", "type", "game_id", "revision"}
                )
                self.assertEqual(message["game_id"], bob_view["game_id"])
        for process in self.processes:
            output = "".join(process.stdout) + process.stderr
            for secret in (*self.tokens.values(), "PRIVATE KEY"):
                self.assertNotIn(secret, output)
            self.assertNotIn("Traceback", process.stderr)
        with GameStore(self.database) as store:
            self.assertEqual((store.revision(), len(store.inspect_history())), (2, 2))

    def test_unverified_or_plaintext_clients_change_nothing(self):
        self.start_host()
        untrusted = self.connect("alice", ca_file=self.unrelated)
        untrusted.send(roll("r1", 0))
        self.assertEqual(untrusted.finish(), 1)
        self.assertIn("cannot trust", untrusted.stderr)

        plaintext = Process(
            self, "connect", self.uri.replace("wss://", "ws://"),
            "--token-file", str(self.token_files["bob"]),
        )  # fmt: skip
        plaintext.send(roll("r1", 0))
        self.assertEqual(plaintext.finish(), 1)
        self.stop_host()

        for client in (untrusted, plaintext):
            self.assertEqual(client.stdout, [])
            self.assertNotIn("Traceback", client.stderr)
        with GameStore(self.database) as store:
            self.assertEqual((store.revision(), store.inspect_history()), (0, []))
