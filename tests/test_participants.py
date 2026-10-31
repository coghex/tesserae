import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tesserae import store as store_module
from tesserae.credentials import new_token, read_token_file, write_token_file
from tesserae.server import DEVELOPMENT_PARTICIPANT
from tesserae.store import SCHEMA_VERSION, GameStore, StoreError

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# Created by the schema-version-1 GameStore (master at f894d84): game
# 64101485-8c87-4363-b796-a9ff4f8d5e6d at revision 2, with local-development's
# r1 (2d6+3 rolled [4, 3]) and r2 (1d20 rolled [17]).
V1_FIXTURE = FIXTURES / "store_v1.sqlite3"
V1_GAME_ID = "64101485-8c87-4363-b796-a9ff4f8d5e6d"


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


class StoreCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "game.sqlite3"

    def open(self, **options):
        store = GameStore(self.path, **options)
        self.addCleanup(store.close)
        return store

    def state(self):
        with GameStore(self.path) as store:
            return store.revision(), store.inspect_history()


class ParticipantTests(StoreCase):
    def test_participants_authenticate_independently_after_reopen(self):
        alice_token, bob_token = new_token(), new_token()
        store = self.open()
        alice = store.add_participant("Alice", alice_token)
        bob = store.add_participant("Alice", bob_token)  # names are only labels
        store.close()

        store = self.open()
        self.assertNotEqual(alice, bob)
        for participant_id in (alice, bob):
            self.assertTrue(participant_id.startswith("p_"))
            self.assertNotEqual(participant_id, DEVELOPMENT_PARTICIPANT.participant_id)
        alice_context = store.authenticate(alice_token)
        self.assertEqual(alice_context.participant_id, alice)
        self.assertEqual(store.authenticate(bob_token).participant_id, bob)
        self.assertTrue(store.credential_active(alice_context))
        for wrong in (new_token(), "", None, 12345, alice_token + "x", "tess_\ud800"):
            self.assertIsNone(store.authenticate(wrong))
        self.assertFalse(store.credential_active(DEVELOPMENT_PARTICIPANT))

    def test_rotation_and_revocation_keep_identity_and_history(self):
        old, new = new_token(), new_token()
        store = self.open(randint=Mock(return_value=1))
        alice = store.add_participant("Alice", old)
        before = store.authenticate(old)
        result = store.execute(before, request("r1", 0, "1d6"))
        store.rotate_credential(alice, new)
        store.close()

        store = self.open()
        self.assertIsNone(store.authenticate(old))
        self.assertFalse(store.credential_active(before))
        after = store.authenticate(new)
        self.assertEqual(after.participant_id, alice)
        self.assertEqual(store.lookup(after, "r1").to_message(), result)

        self.assertTrue(store.revoke_credential(alice))
        self.assertFalse(store.revoke_credential(alice))
        store.close()
        store = self.open()
        self.assertIsNone(store.authenticate(new))
        self.assertFalse(store.credential_active(after))
        [record] = store.participants()
        self.assertEqual((record.participant_id, record.active), (alice, False))
        self.assertEqual(len(store.history(after)), 1)

    def test_administration_changes_no_mechanical_state(self):
        store = self.open(randint=Mock(return_value=2))
        store.execute(DEVELOPMENT_PARTICIPANT, request("r1", 0, "1d6"))
        before = self.state()

        alice = store.add_participant("Alice", new_token())
        store.rotate_credential(alice, new_token())
        store.revoke_credential(alice)
        store.participants()
        self.assertEqual(self.state(), before)
        with self.assertRaises(StoreError):
            store.rotate_credential("p_nobody", new_token())
        with self.assertRaises(ValueError):
            store.add_participant("bad\nname", new_token())

    def test_failed_change_rolls_back_whole(self):
        store = self.open()
        alice_token = new_token()
        alice = store.add_participant("Alice", alice_token)
        failing = patch.object(
            GameStore, "_issue_credential", side_effect=RuntimeError("disk gone")
        )
        with failing, self.assertRaises(RuntimeError):
            store.add_participant("Bob", new_token())
        with failing, self.assertRaises(RuntimeError):
            store.rotate_credential(alice, new_token())

        store = self.open()
        self.assertEqual([r.participant_id for r in store.participants()], [alice])
        self.assertEqual(store.authenticate(alice_token).participant_id, alice)


class MigrationTests(StoreCase):
    def setUp(self):
        super().setUp()
        shutil.copyfile(V1_FIXTURE, self.path)

    def raw(self, query):
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute(query).fetchall()
        finally:
            connection.close()

    def test_version_1_store_upgrades_with_history_intact(self):
        draw = Mock()
        store = self.open(randint=draw)
        self.assertEqual(store.game_id, V1_GAME_ID)
        self.assertEqual(store.revision(), 2)
        self.assertEqual(self.raw("PRAGMA user_version"), [(SCHEMA_VERSION,)])

        replay = store.execute(DEVELOPMENT_PARTICIPANT, request("r1", 0))
        self.assertEqual(replay["revision"], 1)
        self.assertEqual(replay["result"]["terms"][0]["rolls"], [4, 3])
        self.assertEqual(replay["result"]["total"], 10)
        draw.assert_not_called()
        self.assertEqual(
            [(r.participant_id, r.request_id) for r in store.inspect_history()],
            [("local-development", "r1"), ("local-development", "r2")],
        )

        token = new_token()
        alice = store.add_participant("Alice", token)
        context = store.authenticate(token)
        self.assertEqual(context.participant_id, alice)
        self.assertIsNone(store.lookup(context, "r1"))
        self.assertEqual(store.history(context), [])

    def test_failed_migration_leaves_version_1_untouched(self):
        broken = {2: [*store_module._MIGRATIONS[2], "INSERT INTO nowhere VALUES (1)"]}
        with patch.object(store_module, "_MIGRATIONS", broken):
            with self.assertRaises(sqlite3.Error):
                GameStore(self.path)
        self.assertEqual(self.raw("PRAGMA user_version"), [(1,)])
        tables = {name for (name,) in self.raw("SELECT name FROM sqlite_schema")}
        self.assertNotIn("participants", tables)
        self.assertEqual(self.raw("SELECT revision FROM game"), [(2,)])

    def test_newer_schema_is_refused_unchanged(self):
        connection = sqlite3.connect(self.path)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(StoreError, "newer"):
            GameStore(self.path)
        self.assertEqual(self.path.read_bytes(), before)


class OperatorCommandTests(StoreCase):
    def admin(self, *args):
        process = subprocess.run(
            [sys.executable, "-m", "tesserae", "participant", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.output += process.stdout + process.stderr
        return process

    def test_provision_list_rotate_revoke(self):
        self.output = ""
        db = ["--database", str(self.path)]
        alice_file = self.directory / "alice.token"

        added = self.admin(
            "add", *db, "--name", "Alice", "--token-file", str(alice_file)
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(stat.S_IMODE(alice_file.stat().st_mode), 0o600)
        first_token = read_token_file(alice_file)
        tokens = [first_token]

        refused = self.admin(
            "add", *db, "--name", "Bob", "--token-file", str(alice_file)
        )
        self.assertEqual(refused.returncode, 1)
        self.assertIn("overwrite", refused.stderr)
        self.assertEqual(read_token_file(alice_file), first_token)

        listed = self.admin("list", *db)
        [header, row] = listed.stdout.splitlines()
        alice, status, _, name = row.split("\t")
        self.assertEqual((status, name), ("active", "Alice"))

        rotated_file = self.directory / "alice-2.token"
        rotate = self.admin("rotate", *db, alice, "--token-file", str(rotated_file))
        self.assertEqual(rotate.returncode, 0, rotate.stderr)
        tokens.append(read_token_file(rotated_file))
        self.assertEqual(self.admin("revoke", *db, alice).returncode, 0)
        self.assertIn("\trevoked\t", self.admin("list", *db).stdout)

        orphan = self.directory / "orphan.token"
        failed = self.admin("rotate", *db, "p_nobody", "--token-file", str(orphan))
        self.assertEqual(failed.returncode, 1)
        self.assertFalse(orphan.exists())
        self.assertEqual(self.admin("list", "--database", "missing.db").returncode, 1)

        with GameStore(self.path) as store:
            self.assertEqual(store.revision(), 0)
            self.assertIsNone(store.authenticate(tokens[-1]))
        stored = b"".join(p.read_bytes() for p in self.directory.glob("game.sqlite3*"))
        for token in tokens:
            self.assertNotIn(token, self.output)
            self.assertNotIn(token.encode(), stored)
            self.assertNotIn(token[len("tess_") :].encode(), stored)


class TokenFileTests(unittest.TestCase):
    def test_token_files_are_private_and_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            token = new_token()
            self.assertGreaterEqual(len(token), len("tess_") + 43)
            write_token_file(path, token)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(read_token_file(path), token)
            with self.assertRaises(FileExistsError):
                write_token_file(path, new_token())
            self.assertEqual(read_token_file(path), token)

    def test_short_writes_complete_and_failures_leave_no_file(self):
        real_write = os.write

        def trickle(descriptor, data):  # the OS may write fewer bytes than asked
            return real_write(descriptor, bytes(data[:3]))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            token = new_token()
            with patch("tesserae.credentials.os.write", side_effect=trickle):
                write_token_file(path, token)
            self.assertEqual(read_token_file(path), token)

            retry = Path(directory) / "retry"
            failure = OSError("fsync failed")
            with patch("tesserae.credentials.os.fsync", side_effect=failure):
                with self.assertRaises(OSError):
                    write_token_file(retry, new_token())
            self.assertFalse(retry.exists())
            write_token_file(retry, token)
            self.assertEqual(read_token_file(retry), token)
