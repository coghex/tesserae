"""Seeded creation: a new game from an imported baseline (`game create --seed`).

Uses the original Beacon package, whose `validate_seed` checks colours and
that signal counts belong to seeded participants and add up.
"""

import copy
import dataclasses
import json
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tesserae.builtin import CATALOG
from tesserae.credentials import new_token
from tesserae.external import load_module_dir
from tesserae.modules import SeedError
from tesserae.seed import parse_seed
from tesserae.store import GameStore, StoreError, is_participant_id

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_client import Process
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_client import Process

BEACON = Path(__file__).resolve().parent / "fixtures" / "modules" / "beacon"
ALICE, BOB = "p_" + "a1" * 8, "p_" + "b2" * 8
SEED = {
    "seed_version": 1,
    "module": {
        "id": "beacon", "version": "1", "state_version": 1,
        "providers": [{"operation": "dice.roll.v1", "provider": "builtin.dice", "version": "1"}],
    },
    "participants": [{"participant_id": ALICE, "name": "Alice"},
                     {"participant_id": BOB, "name": "Bob"}],
    "state": {"shown": ["red", "white", "red"], "by": {ALICE: 2, BOB: 1}},
    "provenance": {"source": "an original test baseline", "inputs": {"a.json": "0" * 64}},
}  # fmt: skip


def seed_text(change=None) -> str:
    data = copy.deepcopy(SEED)
    if change:
        change(data)
    return json.dumps(data)


def files_named(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.glob(f"{path.name}*"))


class SeedCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "seeded.sqlite3"
        self.runtime = load_module_dir(BEACON)

    def assertRefused(self, seed_or_text, message, runtime=None):
        """Seeding fails with `message` and leaves no file at all."""
        with self.assertRaisesRegex(SeedError, message):
            seed = (
                parse_seed(seed_or_text)
                if isinstance(seed_or_text, str)
                else seed_or_text
            )
            GameStore.create(self.path, runtime or self.runtime, seed=seed).close()
        self.assertEqual(files_named(self.path), [])


class SeededStoreTests(SeedCase):
    def test_a_seed_is_a_new_baseline_without_credentials(self):
        with GameStore.create(
            self.path, self.runtime, seed=parse_seed(seed_text())
        ) as store:
            self.assertEqual(
                (store.revision(), store.inspect_history(), store.inspect_state()),
                (0, [], SEED["state"]),
            )
            self.assertEqual(store.seed_provenance(), SEED["provenance"])
            self.assertEqual(
                [(r.participant_id, r.name, r.active, r.ever_issued) for r in store.participants()],
                [(ALICE, "Alice", False, False), (BOB, "Bob", False, False)],
            )  # fmt: skip
            self.assertEqual(self.runtime.view(store.inspect_state(), BOB),
                             {"last": "red", "yours": 1})  # fmt: skip

            token = new_token()
            self.assertFalse(store.rotate_credential(ALICE, token))  # a first token
            self.assertEqual(store.authenticate(token).participant_id, ALICE)
            self.assertTrue(store.rotate_credential(ALICE, new_token()))
            self.assertIsNone(store.authenticate(token))
        with sqlite3.connect(self.path) as raw:
            self.assertEqual(
                raw.execute("SELECT count(*) FROM credentials").fetchone(), (2,)
            )

    def test_unseeded_games_are_unchanged(self):
        with GameStore.create(self.path, self.runtime) as store:
            self.assertEqual(
                (store.inspect_state(), store.participants(), store.seed_provenance()),
                ({"shown": [], "by": {}}, [], None),
            )
            created = store.add_participant("Cara", new_token())
        self.assertTrue(is_participant_id(created))  # the form seeds must use

    def test_the_envelope_is_checked_before_any_file_exists(self):
        def set_(*keys, value):
            def change(data):
                target = data
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value

            return change

        def drop(key):
            return lambda data: data.pop(key)

        cases = [
            ("not JSON", "{", "not valid JSON"),
            ("duplicate key", '{"seed_version": 1, "seed_version": 1}', "duplicate key"),
            ("NaN", seed_text().replace('"state": {', '"x": NaN, "state": {'), "non-finite"),
            ("version 2", seed_text(set_("seed_version", value=2)), "version 2"),
            ("version true", seed_text(set_("seed_version", value=True)), "version True"),
            ("missing", seed_text(drop("provenance")), "missing provenance"),
            ("extra", seed_text(set_("tokens", value=[])), "unexpected tokens"),
            ("module type", seed_text(set_("module", "version", value=1)), "must be strings"),
            ("state_version", seed_text(set_("module", "state_version", value=-1)), "non-negative"),
            ("providers", seed_text(set_("module", "providers", value={})), "must be a list"),
            ("choice", seed_text(set_("module", "providers", value=[{"operation": "dice.roll.v1"}])), "Provider choice 1 has missing"),
            ("uppercase ID", seed_text(set_("participants", 0, "participant_id", value=ALICE.upper())), "Participant 1's ID"),
            ("short ID", seed_text(set_("participants", 0, "participant_id", value="p_abc")), "Participant 1's ID"),
            ("ID with newline", seed_text(set_("participants", 0, "participant_id", value=ALICE + "\n")), "Participant 1's ID"),
            ("duplicate ID", seed_text(set_("participants", 1, "participant_id", value=ALICE)), "Participant 2 repeats"),
            ("bad name", seed_text(set_("participants", 0, "name", value=" ")), "Participant 1: A name"),
            ("credential", seed_text(set_("participants", 0, "token", value="x")), "Participant 1 has unexpected token"),
            ("state type", seed_text(set_("state", value=[])), "state must be a JSON object"),
            ("provenance type", seed_text(set_("provenance", value="copied")), "provenance must be a JSON object"),
        ]  # fmt: skip
        for name, text, message in cases:
            with self.subTest(name):
                self.assertRefused(text, message)

    def test_the_binding_and_module_state_are_checked(self):
        def module(**fields):
            return lambda data: data["module"].update(fields)

        steady = [
            {"operation": "dice.roll.v1", "provider": "beacon.steady", "version": "1"}
        ]
        cases = [
            ("module id", module(id="relay"), "for module relay 1"),
            ("module version", module(version="2"), "for module beacon 2"),
            ("state version", module(state_version=2), r"\(state 2\)"),
            ("providers", module(providers=steady), "provider choices differ"),
            ("dangling owner", lambda d: d["state"]["by"].update({"p_" + "c" * 16: 1}), "not a participant"),
            ("unknown colour", lambda d: d["state"]["shown"].append("blue"), "known colours"),
            ("counts", lambda d: d["state"]["by"].update({BOB: 2}), "do not add up"),
            ("extra state", lambda d: d["state"].update({"keeper": ALICE}), "exactly 'shown' and 'by'"),
        ]  # fmt: skip
        for name, change, message in cases:
            with self.subTest(name):
                self.assertRefused(seed_text(change), message)

    def test_modules_without_a_validator_refuse_seeds(self):
        dice = seed_text(lambda d: d.update(
            module={"id": "dice", "version": "1", "state_version": 1,
                    "providers": SEED["module"]["providers"]},
            state={}))  # fmt: skip
        self.assertRefused(
            dice, "'dice' does not accept seeds", CATALOG.resolve("dice")
        )

    def test_a_broken_validator_is_reported_not_trusted(self):
        def broken(state, participant_ids):
            return state["keeper"]  # a bug: raises KeyError, not SeedError

        module = dataclasses.replace(self.runtime.module, validate_seed=broken)
        runtime = dataclasses.replace(self.runtime, module=module)
        self.assertRefused(seed_text(), "could not check the seed: KeyError", runtime)

    def test_an_existing_destination_is_never_replaced(self):
        self.path.write_bytes(b"keep")
        with self.assertRaisesRegex(StoreError, "Refusing to replace"):
            GameStore.create(self.path, self.runtime, seed=parse_seed(seed_text()))
        self.assertEqual(self.path.read_bytes(), b"keep")

    def test_a_failure_while_writing_leaves_nothing(self):
        failing = mock.patch.object(
            GameStore, "_write_seed", side_effect=sqlite3.OperationalError("disk full")
        )
        with failing, self.assertRaises(sqlite3.OperationalError):
            GameStore.create(self.path, self.runtime, seed=parse_seed(seed_text()))
        self.assertEqual(files_named(self.path), [])


def tesserae(*args):
    return subprocess.run(
        [sys.executable, "-m", "tesserae", *map(str, args)],
        capture_output=True, text=True, timeout=30,
    )  # fmt: skip


class SeededCliTests(SeedCase):
    def test_create_issue_tokens_and_play_from_a_seed(self):
        seed = self.root / "seed.json"
        seed.write_text(seed_text())
        created = tesserae("game", "create", "--database", self.path, "--module", "beacon",
                           "--module-dir", BEACON, "--seed", seed)  # fmt: skip
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertIn(f"{ALICE}\tAlice", created.stdout)
        self.assertIn("participant rotate", created.stdout)
        listed = tesserae("participant", "list", "--database", self.path).stdout
        self.assertEqual(listed.count("\tno token\t"), 2)

        token_file = self.root / "bob.token"
        issued = tesserae("participant", "rotate", BOB, "--database", self.path,
                          "--token-file", token_file)  # fmt: skip
        self.assertEqual(issued.returncode, 0, issued.stderr)
        self.assertIn("had no active one", issued.stdout)

        host = Process(self, "serve", "--authenticated", "--port", "0", "--database",
                       str(self.path), "--module-dir", str(BEACON))  # fmt: skip
        uri = re.search(r"ws://127\.0\.0\.1:[0-9]+/", host.line()).group(0)
        bob = Process(self, "connect", uri, "--token-file", str(token_file))
        snapshot = bob.message()
        self.assertEqual(
            (snapshot["participant_id"], snapshot["revision"], snapshot["view"]),
            (BOB, 0, {"last": "red", "yours": 1}),
        )
        self.assertEqual(bob.finish(), 0)
        host.signal(signal.SIGINT)
        self.assertEqual(host.wait(), 0)
        self.assertNotIn(
            token_file.read_text().strip(), "".join(host.stdout) + host.stderr
        )

    def test_a_bad_seed_creates_nothing(self):
        seed = self.root / "seed.json"
        seed.write_text(seed_text(lambda d: d["state"]["by"].update({BOB: 5})))
        refused = tesserae("game", "create", "--database", self.path, "--module", "beacon",
                           "--module-dir", BEACON, "--seed", seed)  # fmt: skip
        self.assertEqual((refused.returncode, refused.stdout), (1, ""))
        self.assertIn("do not add up", refused.stderr)
        seed.write_bytes(b"\xff")
        refused = tesserae("game", "create", "--database", self.path, "--module", "beacon",
                           "--module-dir", BEACON, "--seed", seed)  # fmt: skip
        self.assertIn("not UTF-8", refused.stderr)
        self.assertEqual(files_named(self.path), [])


if __name__ == "__main__":
    unittest.main()
