import json
import re
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from websockets.exceptions import InvalidHandshake
from websockets.sync.client import connect

from tesserae.credentials import new_token
from tesserae.store import GameStore
from tesserae.transport import client_context

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_transport import make_certificate, openssl_tools
except ImportError:  # running as `python -m unittest tests.test_tls_host`
    from tests.test_transport import make_certificate, openssl_tools


def roll(request_id, expected_revision):
    return json.dumps(
        {
            "version": 1,
            "type": "command",
            "request_id": request_id,
            "expected_revision": expected_revision,
            "command": "roll",
            "args": {"expression": "1d20"},
        }
    )


class TlsHostCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.database = self.directory / "game.sqlite3"
        openssl = openssl_tools()[0]
        self.cert, self.key = make_certificate(
            openssl, self.directory, "IP:127.0.0.1,DNS:localhost"
        )
        _, self.other_key = make_certificate(
            openssl, self.directory, "IP:127.0.0.1", "other"
        )
        self.token = new_token()
        with GameStore(self.database) as store:
            store.add_participant("Alice", self.token)

    def serve(self, *args):
        return subprocess.Popen(
            [sys.executable, "-m", "tesserae", "serve",
             "--database", str(self.database), "--port", "0", *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )  # fmt: skip


class TlsListenerTests(TlsHostCase):
    def start(self):
        process = self.serve(
            "--authenticated", "--tls-cert", str(self.cert), "--tls-key", str(self.key)
        )
        watchdog = threading.Timer(30, process.kill)
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        line = process.stdout.readline()
        match = re.search(r"(\w+)://127\.0\.0\.1:([0-9]+)/", line)
        self.assertIsNotNone(match, line)
        self.assertEqual(match.group(1), "wss")
        self.assertNotEqual(match.group(2), "0")
        self.assertIn("TLS", line)
        return process, match.group(0)

    def test_tls_join_roll_shutdown_and_restart(self):
        replies = []
        for _ in range(2):  # the second run is a restart
            process, uri = self.start()
            with connect(uri, ssl=client_context(self.cert), open_timeout=10) as ws:
                ws.send(json.dumps({"version": 1, "type": "join", "token": self.token}))
                self.assertEqual(json.loads(ws.recv(timeout=10))["type"], "snapshot")
                ws.send(roll("r1", 0))
                replies.append(json.loads(ws.recv(timeout=10)))
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertNotIn("Traceback", stderr)
            for secret in (self.token, "PRIVATE KEY"):
                self.assertNotIn(secret, stdout + stderr)

        self.assertEqual(replies[0]["type"], "result")
        self.assertEqual(replies[1], replies[0])  # replayed, not rolled again
        with GameStore(self.database) as store:
            self.assertEqual((store.revision(), len(store.inspect_history())), (1, 1))

    def test_plaintext_and_untrusted_clients_cannot_reach_the_game(self):
        process, uri = self.start()
        with self.assertRaises(InvalidHandshake):  # plaintext gets no WebSocket
            with connect(uri.replace("wss://", "ws://"), open_timeout=5):
                pass
        with self.assertRaises(ssl.SSLCertVerificationError):
            with connect(uri, ssl=client_context(), open_timeout=5):  # system trust
                pass
        with connect(uri, ssl=client_context(self.cert), open_timeout=10) as ws:
            ws.send(json.dumps({"version": 1, "type": "join", "token": self.token}))
            self.assertEqual(json.loads(ws.recv(timeout=10))["type"], "snapshot")
        process.send_signal(signal.SIGINT)
        _, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertNotIn("Traceback", stderr)  # failed handshakes are not crashes


class RefusedConfigurationTests(TlsHostCase):
    def test_unsafe_configurations_never_listen(self):
        tls = ["--tls-cert", str(self.cert), "--tls-key", str(self.key)]
        lan = ["--host", "192.168.1.50"]
        cases = [
            ("plaintext LAN", [*lan, "--authenticated"], 2, "requires --authenticated"),
            ("unauthenticated LAN", [*lan, *tls], 2, "TLS requires --authenticated"),
            ("development LAN", lan, 2, "not loopback"),
            ("TLS without authentication", tls, 2, "TLS requires --authenticated"),
            ("certificate alone", ["--authenticated", tls[0], tls[1]], 2, "together"),
            ("every interface", ["--host", "0.0.0.0", "--authenticated", *tls], 2, "every interface"),
            ("hostname", ["--host", "gamehost.local", "--authenticated", *tls], 2, "IPv4"),
            ("missing key", ["--authenticated", tls[0], tls[1], "--tls-key",
                             str(self.directory / "missing.key")], 1, "Cannot read the private key"),
            ("mismatched key", ["--authenticated", tls[0], tls[1], "--tls-key",
                                str(self.other_key)], 1, "does not belong"),
            ("address not on this computer", ["--host", "192.0.2.1", "--authenticated", *tls],
             1, "cannot listen on 192.0.2.1"),
        ]  # fmt: skip
        for description, args, code, diagnostic in cases:
            with self.subTest(description):
                process = self.serve(*args)
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, code, stderr)
                self.assertIn(diagnostic, stderr)
                self.assertNotIn("listening", stdout)
                self.assertNotIn("Traceback", stderr)
                self.assertNotIn("PRIVATE KEY", stderr)
