"""Host-player links in a campaign's own store (HOST-2, #97): the schema 6
upgrade, admission, operator linking, and link-based authority everywhere a
credential is checked. docs/designs/lobby_contract.md section 5."""

import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from tesserae.credentials import new_token
from tesserae.referee.store import RefereeError, RefereeStore
from tesserae.store import GameStore, ParticipantContext, StoreError

HOST = str(uuid.uuid4())


def roll(request_id, revision):
    return json.dumps({"version": 1, "type": "command", "request_id": request_id,
                       "expected_revision": revision, "command": "roll",
                       "args": {"expression": "1d6"}})  # fmt: skip


class HostLinkTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "g.sqlite3"
        self.store = self.enterContext(GameStore(self.path))

    def test_admission_creates_one_participant_without_a_credential(self):
        first = self.store.admit_host_player(HOST, "u_a", "Alice")
        again = self.store.admit_host_player(HOST, "u_a", "Alice (renamed)")
        self.assertEqual(again, first)
        self.assertIsNotNone(first.link_id)
        self.assertIsNone(first.credential_id)
        [record] = self.store.participants()
        self.assertEqual(
            (record.participant_id, record.name), (first.participant_id, "Alice")
        )
        self.assertFalse(record.ever_issued)  # no game token exists for it
        other_host = self.store.admit_host_player(str(uuid.uuid4()), "u_a", "Alice")
        self.assertNotEqual(other_host.participant_id, first.participant_id)
        self.assertEqual(len(self.store.participants()), 2)

    def test_concurrent_admissions_of_one_player_find_one_participant(self):
        results, errors = [], []
        barrier = threading.Barrier(6)

        def admit():
            try:
                with GameStore(self.path) as store:
                    barrier.wait()
                    results.append(store.admit_host_player(HOST, "u_a", "Alice"))
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=admit) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(self.store.participants()), 1)

    def test_link_contexts_are_checked_wherever_credentials_are(self):
        RefereeStore(self.store).enable()
        context = self.store.admit_host_player(HOST, "u_a", "Alice")
        self.assertEqual(self.store.session_revision(context), 0)
        self.assertEqual(self.store.participant_view(context).name, "Alice")
        self.assertTrue(self.store.credential_active(context))
        reply, committed = self.store.execute_command(context, roll("r1", 0))
        self.assertTrue(committed, reply)
        submission = RefereeStore(self.store).submit(context, "s1", "Hello", False)
        self.assertEqual(submission.outcome, "accepted")

        self.assertTrue(self.store.unlink_host_player(HOST, "u_a"))
        self.assertFalse(self.store.unlink_host_player(HOST, "u_a"))
        self.assertIsNone(self.store.session_revision(context))
        self.assertIsNone(self.store.participant_view(context))
        self.assertFalse(self.store.credential_active(context))
        self.assertEqual(
            self.store.execute_command(context, roll("r1", 0))[0]["code"],
            "session_revoked",  # not even a replay once the link has ended
        )
        with self.assertRaises(RefereeError) as refused:
            RefereeStore(self.store).submit(context, "s1", "Hello", False)
        self.assertEqual(refused.exception.code, "session_revoked")
        self.assertEqual(self.store.revision(), 1)  # nothing undone

    def test_links_and_credentials_never_stand_in_for_each_other(self):
        alice = self.store.add_participant("Alice", new_token())
        bob = self.store.admit_host_player(HOST, "u_b", "Bob")
        with self.assertRaises(ValueError):
            ParticipantContext(alice, credential_id=1, link_id=bob.link_id)
        for forged in (
            ParticipantContext(alice, link_id=bob.link_id),  # another's link
            ParticipantContext(
                bob.participant_id, credential_id=1
            ),  # Alice's credential
            ParticipantContext(bob.participant_id, link_id=-1),
        ):
            with self.subTest(context=forged):
                self.assertIsNone(self.store.session_revision(forged))
                self.assertIsNone(self.store.participant_view(forged))

    def test_operator_links_an_existing_participant_reversibly(self):
        token = new_token()
        existing = self.store.add_participant("Wren", token)
        link_id = self.store.link_host_player(HOST, "u_a", existing)
        context = self.store.admit_host_player(HOST, "u_a", "Alice")
        self.assertEqual((context.participant_id, context.link_id), (existing, link_id))
        self.assertIsNotNone(self.store.authenticate(token))  # its game token stays
        for player, participant in (("u_a", existing), ("u_b", existing)):
            with self.subTest(player=player), self.assertRaises(StoreError):
                self.store.link_host_player(HOST, player, participant)
        with self.assertRaises(StoreError):
            self.store.link_host_player(HOST, "u_b", "p_0000000000000000")
        self.assertTrue(self.store.unlink_host_player(HOST, "u_a"))
        self.store.link_host_player(HOST, "u_b", existing)  # free again
        links = self.store.host_links()
        self.assertEqual(
            [
                (link.player_id, link.participant_id, link.unlinked_at is None)
                for link in links
            ],
            [("u_a", existing, False), ("u_b", existing, True)],
        )
        self.assertEqual(len(self.store.participants()), 1)
        self.assertEqual(self.store.revision(), 0)

    def test_version_5_games_are_upgraded_with_everything_kept(self):
        token = new_token()
        alice = self.store.add_participant("Alice", token)
        self.store.execute_command(self.store.authenticate(token), roll("r1", 0))
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.executescript(
                "DROP INDEX one_link_per_player; DROP INDEX one_link_per_participant;"
                "DROP TABLE host_links; PRAGMA user_version = 5;"
            )
        with GameStore.open_existing(self.path) as store:
            self.assertEqual(store.query("PRAGMA user_version"), [(6,)])
            self.assertEqual(store.host_links(), [])
            self.assertEqual(store.revision(), 1)
            self.assertEqual(store.participants()[0].participant_id, alice)
            self.assertIsNotNone(store.authenticate(token))
        self.store = self.enterContext(GameStore(self.path))

    def test_host_identities_are_opaque_strings(self):
        for host_id, player_id in (
            (HOST, ""),
            ("", "u_a"),
            (HOST, "u a"),
            (HOST, None),
        ):
            with self.subTest(player_id=player_id), self.assertRaises(ValueError):
                self.store.admit_host_player(host_id, player_id, "Alice")
        with self.assertRaises(ValueError):
            self.store.admit_host_player(HOST, "u_a", " ")
        self.assertEqual(self.store.participants(), [])


if __name__ == "__main__":
    unittest.main()
