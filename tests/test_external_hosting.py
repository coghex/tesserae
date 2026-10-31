"""`game create` and `serve` with an external module package (`--module-dir`).

Real processes, the original Beacon package from tests/fixtures/modules, and
copies of it changed one way at a time.
"""

import hashlib
import json
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tesserae.store import GameStore

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_client import Process
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_client import Process

BEACON = Path(__file__).resolve().parent / "fixtures" / "modules" / "beacon"
SYNC = json.dumps({"version": 1, "type": "sync"})


def signal_command(request_id, expected_revision):
    return json.dumps(
        {"version": 1, "type": "command", "request_id": request_id,
         "expected_revision": expected_revision, "command": "beacon.signal", "args": {}}
    )  # fmt: skip


def tesserae(*args):
    return subprocess.run(
        [sys.executable, "-m", "tesserae", *map(str, args)],
        capture_output=True, text=True, timeout=30,
    )  # fmt: skip


def fingerprint(path: Path):
    """The file's bytes, and which SQLite sidecar files exist beside it."""
    sidecars = [s for s in ("-wal", "-shm", "-journal") if Path(f"{path}{s}").exists()]
    return hashlib.sha256(path.read_bytes()).hexdigest(), sidecars


class ExternalHostingCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / "beacon.sqlite3"
        self.package = self.variant("beacon")
        self.processes = []

    def variant(self, name, rules=(), manifest=()):
        """A copy of Beacon with (old, new) replacements in rules.py/module.toml."""
        target = self.root / name
        shutil.copytree(BEACON, target, ignore=shutil.ignore_patterns("__pycache__"))
        for file, changes in (("rules.py", rules), ("module.toml", manifest)):
            text = (target / file).read_text()
            for old, new in changes:
                self.assertIn(old, text)
                text = text.replace(old, new)
            (target / file).write_text(text)
        return target

    def create(self, database=None, module="beacon", package=None):
        return tesserae(
            "game", "create", "--database", database or self.database,
            "--module", module, "--module-dir", package or self.package,
        )  # fmt: skip

    def start_host(self, *extra):
        host = Process(self, "serve", "--authenticated", "--port", "0",
                       "--database", str(self.database), *map(str, extra))  # fmt: skip
        self.processes.append(host)
        line = host.line()
        self.assertIn("module: beacon 1", line)
        self.uri = re.search(r"ws://127\.0\.0\.1:[0-9]+/", line).group(0)
        return host

    def stop(self, host):
        host.signal(signal.SIGINT)
        self.assertEqual(host.wait(), 0)

    def joined(self, name):
        client = Process(self, "connect", self.uri, "--token-file",
                         str(self.root / f"{name}.token"))  # fmt: skip
        self.processes.append(client)
        return client, client.message()

    @staticmethod
    def reply(client):
        while (message := client.message())["type"] == "revision":
            pass
        return message


class HostingTests(ExternalHostingCase):
    def test_create_serve_connect_replay_and_reopen(self):
        created = self.create()
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertIn("(beacon 1)", created.stdout)
        for name in ("alice", "bob"):  # participant commands never load the package
            added = tesserae("participant", "add", "--database", self.database,
                             "--name", name.title(), "--token-file", self.root / f"{name}.token")  # fmt: skip
            self.assertEqual(added.returncode, 0, added.stderr)
        self.assertIn(
            "Bob", tesserae("participant", "list", "--database", self.database).stdout
        )
        with GameStore(self.database) as store:  # raw inspection, no package loaded
            self.assertEqual(
                (
                    store.binding().module_id,
                    store.inspect_state(),
                    store.inspect_history(),
                ),
                ("beacon", {"shown": [], "by": {}}, []),
            )

        host = self.start_host("--module-dir", self.package)
        alice, alice_view = self.joined("alice")
        bob, bob_view = self.joined("bob")
        for snapshot in (alice_view, bob_view):
            self.assertEqual(
                snapshot["module"], {"id": "beacon", "version": "1", "state_version": 1}
            )
            self.assertEqual(snapshot["view"], {"last": None, "yours": 0})

        alice.send(signal_command("a1", 0))
        signalled = self.reply(alice)
        colour = signalled["result"]["colour"]
        self.assertEqual(signalled["revision"], 1)
        self.assertEqual(bob.message(), {"version": 1, "type": "revision",
                                         "game_id": bob_view["game_id"], "revision": 1})  # fmt: skip
        views = {}
        for name, client in (("alice", alice), ("bob", bob)):
            client.send(SYNC)
            views[name] = self.reply(client)["view"]
        self.assertEqual(views, {"alice": {"last": colour, "yours": 1},
                                 "bob": {"last": colour, "yours": 0}})  # fmt: skip
        self.assertEqual((alice.finish(), bob.finish()), (0, 0))
        self.stop(host)

        host = self.start_host("--module-dir", self.package)  # reopen
        alice, _ = self.joined("alice")
        alice.send(signal_command("a1", 0), signal_command("a2", 1))
        self.assertEqual(self.reply(alice), signalled)  # exact replay, nothing drawn
        self.assertEqual(self.reply(alice)["revision"], 2)
        self.assertEqual(alice.finish(), 0)
        self.stop(host)

        with GameStore(self.database) as store:
            self.assertEqual((store.revision(), len(store.inspect_history())), (2, 2))
        bob_text = "".join(bob.stdout)
        self.assertNotIn(
            alice_view["participant_id"], bob_text
        )  # the "by" map stays private
        for process in self.processes:
            self.assertNotIn("Traceback", process.stderr)


class RefusalTests(ExternalHostingCase):
    def assertRefused(self, result, message):
        self.assertEqual((result.returncode, result.stdout), (1, ""), result.stderr)
        self.assertIn(message, result.stderr)

    def serve(self, database, package=None):
        args = ["serve", "--authenticated", "--port", "0", "--database", database]
        return tesserae(*args, *(["--module-dir", package] if package else []))

    def test_serve_refuses_before_listening_and_changes_nothing(self):
        self.assertEqual(self.create().returncode, 0)
        dice = self.root / "dice.sqlite3"
        self.assertEqual(
            tesserae(
                "game", "create", "--database", dice, "--module", "dice"
            ).returncode,
            0,
        )
        variants = {
            "other module": self.variant("other", rules=[('id="beacon"', 'id="lantern"')]),
            "other version": self.variant("v2", rules=[('version="1"', 'version="2"')]),
            "other host API": self.variant("api", rules=[("    providers=(", "    api_version=2,\n    providers=(")]),
            "other provider": self.variant("steady", manifest=[('provider = "builtin.dice"', 'provider = "beacon.steady"')]),
            "bad manifest": self.variant("schema", manifest=[("schema_version = 1", "schema_version = 2")]),
        }  # fmt: skip
        cases = [
            ("no --module-dir", self.database, None, "'beacon', which is not bundled"),
            ("missing code", self.database, self.root / "absent", "cannot load the module package"),
            ("other module", self.database, variants["other module"], "not lantern 1"),
            ("other version", self.database, variants["other version"], "not beacon 2"),
            ("other host API", self.database, variants["other host API"], "needs host API 2"),
            ("other provider", self.database, variants["other provider"], "beacon.steady"),
            ("bad manifest", self.database, variants["bad manifest"], "manifest schema 2"),
            ("a dice game", dice, self.package, "uses module dice 1"),
        ]  # fmt: skip
        for name, database, package, message in cases:
            with self.subTest(name):
                before = fingerprint(database)
                self.assertRefused(self.serve(database, package), message)
                self.assertEqual(fingerprint(database), before)

    def test_serve_never_creates_a_game_for_a_package(self):
        missing = self.root / "typo.sqlite3"
        self.assertRefused(self.serve(missing, self.package), "No game store")
        self.assertEqual(sorted(p.name for p in self.root.glob("typo*")), [])
        empty = self.root / "empty.sqlite3"
        empty.touch()
        self.assertRefused(self.serve(empty, self.package), "empty, not a game store")
        self.assertEqual(fingerprint(empty), (hashlib.sha256(b"").hexdigest(), []))

    def test_game_create_checks_the_package_first(self):
        cases = [
            ("module mismatch", "relay", self.package, "is module 'beacon', not 'relay'"),
            ("not bundled", "beacon", None, "No bundled module 'beacon'"),
            ("broken package", "beacon", self.variant("broken", manifest=[("rules:bundle", "rules:absent")]), "is not a function"),
        ]  # fmt: skip
        for name, module, package, message in cases:
            with self.subTest(name):
                database = self.root / f"{name}.sqlite3"
                args = ["game", "create", "--database", database, "--module", module]
                result = tesserae(
                    *args, *(["--module-dir", package] if package else [])
                )
                self.assertRefused(result, message)
                self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
