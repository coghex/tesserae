"""The M1-4 demo end to end: real `game create`, `participant add`, `serve`
and two `connect` clients playing Relay with live dice. See docs/relay-demo.md.
"""

import json
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tesserae.relay import RELAY_MODULE
from tesserae.store import GameStore

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_client import Process
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_client import Process

NOTE = RELAY_MODULE.initial_state()["referee"]["maintenance_note"]
SYNC = json.dumps({"version": 1, "type": "sync"})


def command(request_id, expected_revision, name, args=None):
    return json.dumps(
        {"version": 1, "type": "command", "request_id": request_id,
         "expected_revision": expected_revision, "command": name,
         "args": args or {}}
    )  # fmt: skip


def tesserae(*args):
    return subprocess.run(
        [sys.executable, "-m", "tesserae", *args],
        capture_output=True, text=True, timeout=30,
    )  # fmt: skip


class RelayDemoTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.database = self.directory / "relay.sqlite3"
        self.processes = []
        self.clients = {"alice": [], "bob": []}  # every connection, by player

    def create_game(self, database):
        created = tesserae("game", "create", "--database", str(database),
                           "--module", "relay")  # fmt: skip
        self.assertEqual(created.returncode, 0, created.stderr)
        tokens = {}
        for name in ("alice", "bob"):
            tokens[name] = self.directory / f"{name}.token"
            added = tesserae("participant", "add", "--database", str(database),
                             "--name", name.title(), "--token-file", str(tokens[name]))  # fmt: skip
            self.assertEqual(added.returncode, 0, added.stderr)
        return tokens

    def start_host(self):
        self.host = Process(self, "serve", "--authenticated",
                            "--database", str(self.database), "--port", "0")  # fmt: skip
        self.processes.append(self.host)
        line = self.host.line()
        self.assertIn("module: relay 1", line)
        self.uri = re.search(r"ws://127\.0\.0\.1:[0-9]+/", line).group(0)

    def stop_host(self):
        self.host.signal(signal.SIGINT)
        self.assertEqual(self.host.wait(), 0)

    def joined(self, name):
        client = Process(self, "connect", self.uri,
                         "--token-file", str(self.tokens[name]))  # fmt: skip
        self.processes.append(client)
        self.clients[name].append(client)
        snapshot = client.message()
        self.assertEqual(snapshot["type"], "snapshot", snapshot)
        return client, snapshot

    @staticmethod
    def reply(client):
        """The client's next reply, skipping revision notifications."""
        while (message := client.message())["type"] == "revision":
            assert set(message) == {"version", "type", "game_id", "revision"}
        return message

    def test_two_players_repair_the_relay(self):
        self.tokens = self.create_game(self.database)
        self.start_host()
        alice, alice_view = self.joined("alice")
        bob, bob_view = self.joined("bob")
        self.assertNotEqual(alice_view["participant_id"], bob_view["participant_id"])
        for view in (alice_view, bob_view):
            self.assertEqual(
                view["module"], {"id": "relay", "version": "1", "state_version": 1}
            )
            self.assertEqual(view["revision"], 0)
            self.assertEqual(view["view"]["character"], None)
            self.assertEqual(view["view"]["scene"]["restored"], False)

        enlist = command("a1", 0, "relay.enlist", {"name": "Wren"})
        alice.send(enlist)
        enlisted = self.reply(alice)
        self.assertEqual(
            (enlisted["revision"], enlisted["result"]["worker"]),
            (1, {"name": "Wren", "repair": 1, "effort": 3}),
        )
        bob.send(SYNC)  # Bob learns of revision 1, but not Alice's worker
        self.assertEqual(
            (lambda s: (s["revision"], s["view"]["character"]))(self.reply(bob)),
            (1, None),
        )
        bob.send(command("b1", 1, "relay.enlist", {"name": "Odo"}))
        self.assertEqual(self.reply(bob)["revision"], 2)

        # Both repair at once from revision 2: only one can spend effort.
        lines = {"alice": command("a2", 2, "relay.repair"),
                 "bob": command("b2", 2, "relay.repair")}  # fmt: skip
        alice.send(lines["alice"])
        bob.send(lines["bob"])
        replies = {"alice": self.reply(alice), "bob": self.reply(bob)}
        self.assertEqual(
            sorted(r.get("code", r["type"]) for r in replies.values()),
            ["result", "stale_revision"],
        )
        winner = next(name for name, r in replies.items() if r["type"] == "result")
        loser = "bob" if winner == "alice" else "alice"
        repaired = replies[winner]
        check = repaired["result"]["check"]  # live dice: either outcome is fine
        [die] = check["roll"]["terms"][0]["rolls"]
        self.assertEqual(
            (check["roll"]["expression"], check["total"], check["target"]),
            ("1d6", die + 1, 5),
        )
        self.assertEqual(check["success"], check["total"] >= 5)
        self.assertEqual(repaired["result"]["effort"], {"before": 3, "after": 2})
        restored = check["success"]

        clients = {"alice": alice, "bob": bob}
        views = {}
        for name, client in clients.items():
            client.send(SYNC)
            views[name] = self.reply(client)
            self.assertEqual(views[name]["revision"], 3)
            self.assertEqual(views[name]["view"]["scene"]["restored"], restored)
        self.assertEqual(views[winner]["view"]["character"]["effort"], 2)
        self.assertEqual(views[loser]["view"]["character"]["effort"], 3)
        self.assertEqual((alice.finish(), bob.finish()), (0, 0))

        self.stop_host()
        self.start_host()  # restart; replay is exact and draws nothing
        clients = {name: self.joined(name)[0] for name in ("alice", "bob")}
        clients["alice"].send(enlist, command("a9", 3, "relay.enlist", {"name": "W2"}))
        self.assertEqual(self.reply(clients["alice"]), enlisted)
        refused = self.reply(clients["alice"])
        self.assertEqual((refused["code"], refused["revision"]), ("action_rejected", 3))
        clients[winner].send(lines[winner])
        self.assertEqual(self.reply(clients[winner]), repaired)
        clients[loser].send(lines[loser])  # it never committed; it stays refused
        self.assertEqual(self.reply(clients[loser])["code"], "stale_revision")
        for client in clients.values():
            self.assertEqual(client.finish(), 0)
        self.stop_host()

        with GameStore(self.database) as store:
            state = store.inspect_state()
            self.assertEqual((store.revision(), len(store.inspect_history())), (3, 3))
        efforts = sorted(worker["effort"] for worker in state["workers"].values())
        self.assertEqual((efforts, state["relay"]["restored"]), ([2, 3], restored))

        for process in self.processes:
            output = "".join(process.stdout) + process.stderr
            self.assertNotIn("Traceback", output)
            for token in self.tokens.values():
                self.assertNotIn(token.read_text().strip(), output)
            self.assertNotIn(NOTE, output)
        for name, other, other_id in [
            ("alice", '"Odo"', bob_view["participant_id"]),
            ("bob", '"Wren"', alice_view["participant_id"]),
        ]:
            text = "".join(line for c in self.clients[name] for line in c.stdout)
            for hidden in (other, other_id, "workers", "referee"):
                self.assertNotIn(hidden, text)

    def test_configuration_errors_stop_before_play(self):
        self.tokens = self.create_game(self.database)
        development = tesserae("serve", "--database", str(self.database), "--port", "0")
        self.assertEqual((development.returncode, development.stdout), (2, ""))
        self.assertIn("requires --authenticated", development.stderr)

        with closing(sqlite3.connect(self.database)) as database, database:
            database.execute("UPDATE module SET module_version = '9'")
        mismatched = tesserae("serve", "--authenticated",
                              "--database", str(self.database), "--port", "0")  # fmt: skip
        self.assertEqual((mismatched.returncode, mismatched.stdout), (1, ""))
        self.assertIn("is version 1, not 9", mismatched.stderr)

        unknown = self.directory / "unknown.sqlite3"
        refused = tesserae(
            "game", "create", "--database", str(unknown), "--module", "x"
        )
        self.assertEqual(refused.returncode, 1)
        self.assertFalse(unknown.exists())
        with GameStore(self.database) as store:
            self.assertEqual((store.revision(), store.inspect_history()), (0, []))


if __name__ == "__main__":
    unittest.main()
