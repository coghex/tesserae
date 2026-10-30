import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tesserae.store import (
    APPLICATION_ID,
    GameStore,
    ParticipantContext,
    StoreBusy,
    StoreError,
)

ALICE = ParticipantContext("alice")
BOB = ParticipantContext("bob")


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


class GameStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "game.sqlite3"

    def open(self, **options):
        store = GameStore(self.path, **options)
        self.addCleanup(store.close)
        return store

    def test_fresh_command_commits_and_survives_reopen(self):
        store = self.open(randint=Mock(side_effect=[4, 3]))
        game_id = store.game_id
        message = store.execute(ALICE, request("r1", 0))
        self.assertEqual(message["type"], "result")
        self.assertEqual(message["revision"], 1)
        self.assertEqual(message["result"]["terms"][0]["rolls"], [4, 3])
        self.assertEqual(message["result"]["total"], 10)
        store.close()

        reopened = self.open()
        self.assertEqual(reopened.game_id, game_id)
        self.assertEqual(reopened.revision(), 1)
        [record] = reopened.inspect_history()
        self.assertEqual((record.participant_id, record.request_id), ("alice", "r1"))
        self.assertEqual(record.to_message(), message)
        with GameStore(":memory:") as other:
            self.assertNotEqual(other.game_id, game_id)

    def test_retries_replay_without_executing_again(self):
        store = self.open(randint=Mock(side_effect=[4, 3, 1, 1]))
        original = store.execute(ALICE, request("r1", 0))
        store.execute(BOB, request("b1", 1))
        self.assertEqual(store.execute(ALICE, request("r1", 0)), original)
        self.assertEqual(store.execute(ALICE, request("r1", 2)), original)
        reordered = json.dumps(dict(reversed(json.loads(request("r1", 9)).items())))
        self.assertEqual(store.execute(ALICE, reordered), original)
        store.close()

        draw = Mock()
        reopened = self.open(randint=draw)
        self.assertEqual(reopened.execute(ALICE, request("r1", 5)), original)
        draw.assert_not_called()
        self.assertEqual(reopened.revision(), 2)
        self.assertEqual(len(reopened.inspect_history()), 2)

    def test_participants_have_separate_request_namespaces(self):
        store = self.open(randint=Mock(side_effect=[1, 1, 6, 6]))
        alice = store.execute(ALICE, request("r1", 0))
        self.assertIsNone(store.lookup(BOB, "r1"))
        self.assertEqual(store.history(BOB), [])

        bob = store.execute(BOB, request("r1", 1))
        self.assertEqual(bob["revision"], 2)
        self.assertEqual(bob["result"]["total"], 15)
        self.assertEqual(alice["result"]["total"], 5)
        self.assertEqual(store.lookup(ALICE, "r1").to_message(), alice)
        self.assertEqual(store.lookup(BOB, "r1").to_message(), bob)
        self.assertEqual([r.revision for r in store.history(ALICE)], [1])
        self.assertEqual([r.participant_id for r in store.inspect_history()],
                         ["alice", "bob"])  # fmt: skip
        store.close()

        reconnected = self.open()
        self.assertEqual(
            reconnected.execute(ParticipantContext("bob"), request("r1", 0)), bob
        )

    def test_rejections_and_reads_change_nothing(self):
        store = self.open(randint=Mock(side_effect=[2, 2]))
        store.execute(ALICE, request("r1", 0))
        before = store.inspect_history()
        draw = Mock()
        store = self.open(randint=draw)

        cases = [
            ("{", "invalid_request", None),
            (request("r\ud800", 1), "invalid_request", None),
            (request("r2", 1, "2d6*3"), "invalid_expression", "r2"),
            (request("r1", 1, "1d20"), "request_conflict", "r1"),
            (request("r2", 0), "stale_revision", "r2"),
        ]
        for raw, code, request_id in cases:
            with self.subTest(code):
                message = store.execute(ALICE, raw)
                self.assertEqual(message["code"], code)
                self.assertEqual(message["request_id"], request_id)
                self.assertEqual(message["revision"], 1)

        store.revision()
        store.lookup(ALICE, "r1")
        store.lookup(BOB, "r1")
        store.history(ALICE)
        draw.assert_not_called()
        self.assertEqual(store.revision(), 1)
        self.assertEqual(store.inspect_history(), before)

    def test_competing_callers_cannot_both_execute_fresh(self):
        bob_draw = Mock()
        bob_store = self.open(randint=bob_draw)
        holding, release, alice = self.start_slow_alice()
        self.assertTrue(holding.wait(5))
        threading.Timer(0.2, release.set).start()

        message = bob_store.execute(BOB, request("b1", 0))
        alice.join(5)
        self.assertEqual(message["code"], "stale_revision")
        self.assertEqual(message["revision"], 1)
        bob_draw.assert_not_called()
        self.assertEqual(
            [r.participant_id for r in bob_store.inspect_history()], ["alice"]
        )

    def test_busy_store_fails_within_bounded_wait_without_drawing(self):
        bob_draw = Mock()
        bob_store = self.open(randint=bob_draw, busy_timeout=0.05)
        holding, release, alice = self.start_slow_alice()
        self.assertTrue(holding.wait(5))
        try:
            with self.assertRaises(StoreBusy):
                bob_store.execute(BOB, request("b1", 0))
        finally:
            release.set()
            alice.join(5)
        bob_draw.assert_not_called()
        self.assertEqual(bob_store.revision(), 1)
        self.assertEqual(len(bob_store.inspect_history()), 1)

    def start_slow_alice(self):
        """Alice holds the write guard mid-roll until released."""
        self.open().close()  # create the schema first
        holding, release = threading.Event(), threading.Event()

        def slow_draw(low, high):
            holding.set()
            release.wait(5)
            return 1

        def run():
            with GameStore(self.path, randint=slow_draw) as store:
                store.execute(ALICE, request("r1", 0, "1d6"))

        thread = threading.Thread(target=run)
        thread.start()
        return holding, release, thread

    def test_failure_before_commit_leaves_no_partial_command(self):
        store = self.open(randint=Mock(return_value=1))
        with patch.object(
            GameStore, "_advance_revision", side_effect=RuntimeError("disk gone")
        ):
            with self.assertRaises(RuntimeError):
                store.execute(ALICE, request("r1", 0))
        self.assertEqual(self.open().inspect_history(), [])
        self.assertEqual(self.open().revision(), 0)

        message = store.execute(ALICE, request("r1", 0))
        self.assertEqual(message["revision"], 1)
        self.assertEqual(len(self.open().inspect_history()), 1)

    def test_other_databases_are_refused_unchanged(self):
        impostors = {
            "unrelated tables": "CREATE TABLE notes (text TEXT);",
            "matching user_version": "PRAGMA user_version = 1;"
            "CREATE TABLE game (singleton, game_id, revision);",
            "missing commands table": f"PRAGMA application_id = {APPLICATION_ID};"
            "PRAGMA user_version = 1;"
            "CREATE TABLE game (singleton, game_id, revision);"
            "INSERT INTO game VALUES (1, 'g', 0);",
            "same schema without application_id": "PRAGMA user_version = 1;"
            "CREATE TABLE game (singleton, game_id, revision);"
            "CREATE TABLE commands (id, revision, participant_id, request_id,"
            " content, result);"
            "INSERT INTO game VALUES (1, 'g', 0);",
            "not SQLite": None,
        }
        for description, script in impostors.items():
            with self.subTest(description):
                self.path.unlink(missing_ok=True)
                if script is None:
                    self.path.write_bytes(b"not a database" * 100)
                else:
                    connection = sqlite3.connect(self.path)
                    connection.executescript(script)
                    connection.close()
                before = self.path.read_bytes()
                with self.assertRaises(StoreError):
                    GameStore(self.path)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse(self.path.with_name("game.sqlite3-wal").exists())
