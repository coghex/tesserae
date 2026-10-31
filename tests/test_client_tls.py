import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from tesserae.credentials import new_token, write_token_file
from tesserae.transport import server_context

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_transport import make_certificate, openssl_tools
except ImportError:  # running as `python -m unittest tests.test_client_tls`
    from tests.test_transport import make_certificate, openssl_tools

ROLL = json.dumps(
    {"version": 1, "type": "command", "request_id": "r1", "expected_revision": 0,
     "command": "roll", "args": {"expression": "1d20"}}
)  # fmt: skip


class RecordingHost:
    """A library WebSocket server that records every message it receives and
    answers like a Tesserae host: a snapshot for the join, a result per roll."""

    def __init__(self, test, ssl_context=None):
        self.received = []

        def handler(connection):
            try:
                for raw in connection:
                    message = json.loads(raw)
                    self.received.append(message)
                    if message.get("type") == "join":
                        reply = {"version": 1, "type": "snapshot", "game_id": "g",
                                 "participant_id": "p_test", "revision": 0}  # fmt: skip
                    else:
                        reply = {"version": 1, "type": "result", "revision": 1,
                                 "request_id": message.get("request_id"), "result": {}}  # fmt: skip
                    connection.send(json.dumps(reply))
            except ConnectionClosed:
                pass

        self.server = serve(handler, "127.0.0.1", 0, ssl=ssl_context)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        test.addCleanup(self.server.shutdown)
        self.port = self.server.socket.getsockname()[1]


class ClientTrustTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        openssl = openssl_tools()[0]
        self.cert, self.key = make_certificate(
            openssl, self.directory, "IP:127.0.0.1,DNS:localhost"
        )
        self.unrelated, _ = make_certificate(
            openssl, self.directory, "IP:127.0.0.1", "unrelated"
        )
        self.named, self.named_key = make_certificate(
            openssl, self.directory, "DNS:gamehost.example", "named"
        )
        self.token = new_token()
        self.token_file = self.directory / "alice.token"
        write_token_file(self.token_file, self.token)

    def connect(self, uri, *extra, stdin=ROLL + "\n"):
        process = subprocess.run(
            [sys.executable, "-m", "tesserae", "connect", uri,
             "--token-file", str(self.token_file), *extra],
            input=stdin, capture_output=True, text=True, timeout=60,
        )  # fmt: skip
        self.assertNotIn(self.token, process.stdout + process.stderr)
        self.assertNotIn("Traceback", process.stderr)
        return process

    def test_trusted_certificate_connects_by_address_and_name(self):
        host = RecordingHost(self, server_context(self.cert, self.key))
        for name in ("127.0.0.1", "localhost"):
            with self.subTest(name):
                process = self.connect(
                    f"wss://{name}:{host.port}/", "--ca-file", str(self.cert)
                )
                self.assertEqual(process.returncode, 0, process.stderr)
                replies = [json.loads(line) for line in process.stdout.splitlines()]
                self.assertEqual([r["type"] for r in replies], ["snapshot", "result"])
        self.assertEqual(
            [m["type"] for m in host.received], ["join", "command", "join", "command"]
        )

    def test_unverified_hosts_receive_nothing(self):
        trusted_host = RecordingHost(self, server_context(self.cert, self.key))
        named_host = RecordingHost(self, server_context(self.named, self.named_key))
        plaintext_host = RecordingHost(self)
        cases = [
            ("unrelated certificate", trusted_host,
             f"wss://127.0.0.1:{trusted_host.port}/", ["--ca-file", str(self.unrelated)],
             "cannot trust"),
            ("system trust only", trusted_host,
             f"wss://127.0.0.1:{trusted_host.port}/", [], "cannot trust"),
            ("address not in the certificate", named_host,
             f"wss://127.0.0.1:{named_host.port}/", ["--ca-file", str(self.named)],
             "cannot trust"),
            ("host without TLS", plaintext_host,
             f"wss://127.0.0.1:{plaintext_host.port}/", ["--ca-file", str(self.cert)],
             "securely"),
        ]  # fmt: skip
        for description, host, uri, extra, diagnostic in cases:
            with self.subTest(description):
                process = self.connect(uri, *extra)
                self.assertEqual(process.returncode, 1)
                self.assertIn(diagnostic, process.stderr)
                self.assertEqual(process.stdout, "")
                self.assertEqual(host.received, [])  # no join, no action line

    def test_plaintext_to_another_computer_is_refused_without_contact(self):
        cases = [
            ("LAN address", "ws://192.0.2.1:8790/", [], "only for this computer"),
            ("hostname", "ws://gamehost.example:8790/", [], "only for this computer"),
            ("trust file with ws", "ws://127.0.0.1:8790/",
             ["--ca-file", str(self.cert)], "needs a wss"),
            ("missing trust file", "wss://127.0.0.1:8790/",
             ["--ca-file", str(self.directory / "missing.crt")], "Cannot read the trust file"),
            ("trust file is a key", "wss://127.0.0.1:8790/",
             ["--ca-file", str(self.key)], "does not contain a PEM certificate"),
        ]  # fmt: skip
        for description, uri, extra, diagnostic in cases:
            with self.subTest(description):
                started = time.monotonic()
                process = self.connect(uri, *extra)
                # 192.0.2.1 is unroutable: contacting it would wait for the
                # 10-second connect timeout. Refusing first is immediate.
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(process.returncode, 1)
                self.assertIn(diagnostic, process.stderr)
                self.assertEqual(process.stdout, "")
