"""The referee store (#65, REF-2): input, the busy gate, retries, choices,
audiences, the step journal and sessions. docs/designs/referee_contract.md."""

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from tesserae.credentials import new_token
from tesserae.referee.store import REFEREE, RefereeError, RefereeStore
from tesserae.store import SCHEMA_VERSION, GameStore, ParticipantContext

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def reply(*messages, ask=(), resolve=(), narrated=()):
    return {
        "narrated": [{"job_id": j, "step_id": s} for j, s in narrated],
        "messages": [{"audience": a, "text": t} for a, t in messages],
        "choices": {
            "ask": [{"participant_id": p, "question": q} for p, q in ask],
            "resolve": list(resolve),
        },
    }


def roll(request_id, revision):
    return json.dumps({"version": 1, "type": "command", "request_id": request_id,
                       "expected_revision": revision, "command": "roll",
                       "args": {"expression": "1d6"}})  # fmt: skip


class RefereeCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "game.sqlite3"
        self.store = GameStore(self.path, randint=lambda low, high: 4)
        self.addCleanup(self.store.close)
        self.alice = ParticipantContext(
            self.store.add_participant("Alice", new_token())
        )
        self.bob = ParticipantContext(self.store.add_participant("Bob", new_token()))
        self.referee = RefereeStore(self.store)
        self.referee.enable()

    def ask(self, context, request_id, text="Hello", private=False):
        return self.referee.submit(context, request_id, text, private)

    def refused(self, code, call, *args):
        with self.assertRaises(RefereeError) as caught:
            call(*args)
        self.assertEqual(caught.exception.code, code)


class SchemaTests(RefereeCase):
    def test_new_and_upgraded_stores_have_the_referee_tables(self):
        self.assertEqual(self.store.query("PRAGMA user_version"), [(SCHEMA_VERSION,)])
        older = Path(self.path.parent) / "v1.sqlite3"
        older.write_bytes((FIXTURES / "store_v1.sqlite3").read_bytes())
        with GameStore(older) as upgraded:
            tables = {
                row[0]
                for row in upgraded.query(
                    "SELECT name FROM sqlite_schema WHERE type = 'table'"
                )
            }
            self.assertLessEqual(
                {"referee", "referee_jobs", "referee_steps", "referee_messages"}, tables
            )
            self.assertEqual(
                (upgraded.revision(), len(upgraded.inspect_history())), (2, 2)
            )
            self.assertFalse(RefereeStore(upgraded).enabled())

    def test_one_running_job_is_a_database_rule(self):
        self.ask(self.alice, "a-1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.query(
                "INSERT INTO referee_jobs (job_id, input_id, state, revision_at_start, started_at) "
                "SELECT 'j-x', id, 'running', 0, 'now' FROM referee_inputs"
            )


class SubmitTests(RefereeCase):
    def test_accepting_stores_the_input_its_message_and_a_running_job(self):
        submitted = self.ask(self.alice, "a-1", "What is this place?")
        self.assertEqual(
            (submitted.outcome, submitted.job.state), ("accepted", "running")
        )
        self.assertEqual(submitted.job.revision_at_start, 0)
        self.assertEqual(
            self.store.revision(), 0
        )  # conversation never moves the revision
        [message] = self.referee.messages_for(self.bob.participant_id)
        self.assertEqual((message["sender"], message["audience"], message["text"]),
                         (self.alice.participant_id, "table", "What is this place?"))  # fmt: skip

    def test_busy_input_is_refused_and_not_stored(self):
        self.ask(self.alice, "a-1")
        self.refused("referee_busy", self.ask, self.bob, "b-1", "Me too")
        self.assertEqual(
            self.store.query("SELECT count(*) FROM referee_inputs"), [(1,)]
        )
        self.assertEqual(len(self.referee.messages_for(self.bob.participant_id)), 1)

    def test_an_accepted_input_replays_even_while_busy(self):
        first = self.ask(self.alice, "a-1", "Hi")
        again = self.ask(self.alice, "a-1", "Hi")
        self.assertEqual(
            (again.outcome, again.job.job_id, again.messages),
            ("replayed", first.job.job_id, ()),
        )
        self.refused("request_conflict", self.ask, self.alice, "a-1", "Something else")
        self.refused("request_conflict", self.ask, self.alice, "a-1", "Hi", True)

    def test_a_replay_after_the_reply_returns_the_reply(self):
        job = self.ask(self.alice, "a-1", "Hi").job
        self.referee.publish_reply(job.job_id, reply(("table", "Welcome.")))
        again = self.ask(self.alice, "a-1", "Hi")
        self.assertEqual(
            (again.job.state, [m["text"] for m in again.messages]),
            ("completed", ["Welcome."]),
        )

    def test_a_busy_refused_input_sent_later_is_fresh(self):
        job = self.ask(self.alice, "a-1").job
        self.refused("referee_busy", self.ask, self.bob, "b-1", "Me too")
        self.referee.publish_reply(job.job_id, reply(("table", "Done.")))
        later = self.ask(self.bob, "b-1", "Me too")
        self.assertEqual((later.outcome, later.job.state), ("accepted", "running"))

    def test_simultaneous_submissions_start_one_job(self):
        barrier, outcomes = threading.Barrier(2), []

        def submit(context, request_id):
            # sqlite3 objects belong to the thread that made them: open here.
            with GameStore(self.path) as mine:
                barrier.wait()
                try:
                    outcomes.append(
                        RefereeStore(mine)
                        .submit(context, request_id, "Go", False)
                        .outcome
                    )
                except RefereeError as error:
                    outcomes.append(error.code)

        threads = [threading.Thread(target=submit, args=(c, r))
                   for c, r in ((self.alice, "a-1"), (self.bob, "b-1"))]  # fmt: skip
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["accepted", "referee_busy"])

    def test_input_checks(self):
        for args in (
            ("bad id", "Hi"),
            ("a-1", ""),
            ("a-1", "   "),
            ("a-1", "x" * 2001),
            ("a-1", "bell\x07"),
        ):
            with self.subTest(args=args):
                self.refused("invalid_request", self.ask, self.alice, *args)
        with self.assertRaises(TypeError):
            self.ask(ParticipantContext(REFEREE), "r-1")

    def test_referee_mode_must_be_on(self):
        other = GameStore(Path(self.path.parent) / "plain.sqlite3")
        self.addCleanup(other.close)
        self.refused(
            "referee_off", RefereeStore(other).submit, self.alice, "a-1", "Hi", False
        )


class ReplyTests(RefereeCase):
    def test_private_input_gets_a_private_reply_only_its_sender_sees(self):
        job = self.ask(
            self.bob, "b-1", "What is Alice's worker like?", private=True
        ).job
        self.referee.publish_reply(
            job.job_id, reply(("sender", "Only what you can see: a toolbag."))
        )
        self.assertEqual(
            [m["text"] for m in self.referee.messages_for(self.alice.participant_id)],
            [],
        )
        self.assertEqual(len(self.referee.messages_for(self.bob.participant_id)), 2)
        self.assertEqual(
            len(self.referee.transcript()[0]), 2
        )  # the referee sees everything

    def test_a_table_input_cannot_get_a_private_reply(self):
        job = self.ask(self.alice, "a-1").job
        self.refused(
            "malformed_reply",
            self.referee.publish_reply,
            job.job_id,
            reply(("sender", "Psst.")),
        )
        self.assertEqual(
            self.referee.job(job.job_id).state, "running"
        )  # nothing changed

    def test_malformed_replies_change_nothing(self):
        job = self.ask(self.alice, "a-1").job
        for bad in ({"messages": []}, reply(), reply(("everyone", "Hi")), reply(("table", "")),
                    reply(("table", "Hi"), ask=[("p_" + "0" * 16, "Who?")]),
                    {**reply(("table", "Hi")), "extra": 1}):  # fmt: skip
            with self.subTest(bad=bad):
                self.refused(
                    "malformed_reply", self.referee.publish_reply, job.job_id, bad
                )
        self.assertEqual(
            self.store.query("SELECT count(*) FROM referee_messages"), [(1,)]
        )

    def test_pending_choices_survive_other_conversation(self):
        a, b = self.alice.participant_id, self.bob.participant_id
        job = self.ask(self.alice, "a-1", "Can Wren fix the relay?").job
        self.referee.publish_reply(
            job.job_id, reply(("table", "Try now?"), ask=[(a, "Try the repair now?")])
        )
        job = self.ask(self.bob, "b-1", "What's the weather like?").job
        self.referee.publish_reply(job.job_id, reply(("table", "Stormy.")))
        self.assertEqual(
            self.referee.projection(a)["choices"], [{"question": "Try the repair now?"}]
        )
        job = self.ask(self.alice, "a-2", "What happens if it fails?").job
        self.referee.publish_reply(job.job_id, reply(("table", "You lose the effort.")))
        self.assertEqual(len(self.referee.all_choices()), 1)
        job = self.ask(self.alice, "a-3", "Yes, go").job
        self.referee.publish_reply(
            job.job_id, reply(("table", "Wren tries."), resolve=[a])
        )
        self.assertEqual(self.referee.all_choices(), [])
        self.assertEqual(
            self.referee.projection(b)["choices"], []
        )  # Bob was never asked

    def test_status_never_shows_another_players_choice(self):
        job = self.ask(self.alice, "a-1", "Secret plan?", private=True).job
        self.referee.publish_reply(
            job.job_id,
            reply(
                ("sender", "Maybe."),
                ask=[(self.alice.participant_id, "Use the hidden route?")],
            ),
        )
        bob = self.referee.projection(self.bob.participant_id)
        self.assertEqual(
            (bob["choices"], bob["your_last_input"], bob["last_seq"]), ([], None, 0)
        )
        alice = self.referee.projection(self.alice.participant_id)
        self.assertEqual(alice["choices"], [{"question": "Use the hidden route?"}])
        self.assertEqual((alice["your_last_input"]["request_id"], alice["your_last_input"]["job_state"]),
                         ("a-1", "completed"))  # fmt: skip
        self.assertNotIn("hidden route", json.dumps(bob))


class StepTests(RefereeCase):
    def setUp(self):
        super().setUp()
        self.job = self.ask(self.alice, "a-1", "Roll for me").job

    def test_a_step_is_journaled_before_it_runs_and_recorded_after(self):
        step = self.referee.intend_step(self.job.job_id, "s1", "act", self.alice.participant_id, "roll",
                                        {"expression": "1d6"})  # fmt: skip
        self.assertEqual(
            (step.status, step.request_id), ("intended", f"ref:{self.job.job_id}:s1")
        )
        result = self.store.execute(self.alice, roll(step.request_id, 0))
        self.referee.record_step(self.job.job_id, "s1", "committed", result)
        again = self.referee.intend_step(self.job.job_id, "s1", "act", self.alice.participant_id, "roll",
                                         {"expression": "1d6"})  # fmt: skip
        self.assertEqual((again.status, again.result), ("committed", result))

    def test_a_step_id_conflicts_across_both_kinds(self):
        self.referee.intend_step(
            self.job.job_id,
            "s1",
            "act",
            self.alice.participant_id,
            "roll",
            {"expression": "1d6"},
        )
        for kind, context, command, args in (
            ("referee_act", REFEREE, "npc.act", {}),
            ("act", self.alice.participant_id, "roll", {"expression": "2d6"}),
        ):
            with self.subTest(kind=kind):
                self.refused("request_conflict", self.referee.intend_step, self.job.job_id, "s1", kind, context,
                             command, args)  # fmt: skip

    def test_a_crash_after_commit_is_found_and_one_before_is_not(self):
        committed = self.referee.intend_step(self.job.job_id, "s1", "act", self.alice.participant_id, "roll",
                                             {"expression": "1d6"})  # fmt: skip
        result = self.store.execute(
            self.alice, roll(committed.request_id, 0)
        )  # then the host "dies"
        self.referee.intend_step(self.job.job_id, "s2", "act", self.alice.participant_id, "roll",
                                 {"expression": "1d6"})  # fmt: skip
        self.referee.finish(self.job.job_id, "interrupted")
        settled = {step.step_id: step for step in self.referee.reconcile_steps()}
        self.assertEqual(
            (settled["s1"].status, settled["s1"].result), ("committed", result)
        )
        self.assertEqual(settled["s2"].status, "not_committed")
        self.assertEqual([s.step_id for s in self.referee.unnarrated_steps()], ["s1"])
        # A later table reply that lists it narrates it; the interrupted job stays interrupted.
        job = self.ask(self.bob, "b-1", "What happened?").job
        self.referee.publish_reply(
            job.job_id,
            reply(("table", "Alice rolled a 4."), narrated=[(self.job.job_id, "s1")]),
        )
        self.assertEqual((self.referee.unnarrated_steps(), self.referee.job(self.job.job_id).state),
                         ([], "interrupted"))  # fmt: skip

    def test_an_interrupted_input_replays_its_state_and_never_reruns(self):
        self.referee.finish(self.job.job_id, "interrupted")
        again = self.ask(self.alice, "a-1", "Roll for me")
        self.assertEqual((again.outcome, again.job.state), ("replayed", "interrupted"))
        self.assertIsNone(self.referee.running_job())

    def test_steps_and_rulings_need_a_running_job(self):
        self.referee.publish_reply(self.job.job_id, reply(("table", "Done.")))
        self.refused("job_not_running", self.referee.intend_step, self.job.job_id, "s9", "act",
                     self.alice.participant_id, "roll", {})  # fmt: skip
        self.refused(
            "job_not_running",
            self.referee.record_ruling,
            self.job.job_id,
            "Dark is -3",
            "this scene",
        )


class SessionTests(RefereeCase):
    def test_sessions_and_rebuild_keep_history(self):
        self.referee.set_session("thread-1")
        self.referee.set_session("thread-1")  # recording it again is harmless
        self.refused("session_conflict", self.referee.set_session, "thread-2")
        self.referee.mark_needs_rebuild()
        self.refused("referee_needs_rebuild", self.ask, self.alice, "a-1")
        self.referee.rebuild()
        self.assertEqual(self.referee.session(), (None, "ok"))
        self.referee.set_session("thread-2")
        self.assertEqual(
            [(s[0], s[3]) for s in self.referee.sessions()],
            [("thread-1", "rebuild"), ("thread-2", None)],
        )
        self.assertEqual(self.store.revision(), 0)

    def test_rebuild_waits_for_the_running_job(self):
        self.ask(self.alice, "a-1")
        self.refused("referee_busy", self.referee.rebuild)

    def test_transcript_is_complete_in_pages(self):
        for number in range(205):
            job = self.ask(
                self.alice if number % 2 else self.bob, f"r-{number}", f"Line {number}"
            ).job
            self.referee.finish(job.job_id, "failed", "test")
        first, after = self.referee.transcript()
        second, end = self.referee.transcript(after)
        self.assertEqual((len(first), len(second), end), (200, 5, None))
        self.assertEqual(second[-1]["text"], "Line 204")


class CommandLineTests(unittest.TestCase):
    def test_referee_enable(self):
        import subprocess
        import sys

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database, missing = (
            Path(directory.name) / "g.sqlite3",
            Path(directory.name) / "none.sqlite3",
        )
        GameStore(database).close()
        run = lambda path: subprocess.run(  # noqa: E731
            [sys.executable, "-m", "tesserae", "referee", "enable", "--database", str(path)],
            capture_output=True, text=True,
        )  # fmt: skip
        self.assertEqual(run(database).returncode, 0)
        with GameStore(database) as store:
            self.assertTrue(RefereeStore(store).enabled())
        self.assertEqual((run(missing).returncode, missing.exists()), (1, False))


class ReviewRegressionTests(RefereeCase):
    """The review of 6789920 (R75-1 to R75-4)."""

    def test_a_revoked_credential_can_neither_submit_nor_replay(self):  # R75-1
        token = new_token()
        carol_id = self.store.add_participant("Carol", token)
        carol = self.store.authenticate(token)
        job = self.ask(carol, "c-1", "Secret?", private=True).job
        self.referee.publish_reply(job.job_id, reply(("sender", "Only for you.")))
        self.store.revoke_credential(carol_id)
        before = self.store.query("SELECT count(*) FROM referee_inputs")
        for request_id in (
            "c-1",
            "c-2",
        ):  # a replay of the private reply, and new input
            with self.subTest(request_id):
                self.refused(
                    "session_revoked", self.ask, carol, request_id, "Secret?", True
                )
        self.assertEqual(
            self.store.query("SELECT count(*) FROM referee_inputs"), before
        )
        self.assertIsNone(self.referee.running_job())

    def test_an_unrelated_reply_does_not_acknowledge_someone_elses_result(
        self,
    ):  # R75-2
        job = self.ask(self.alice, "a-1", "Roll for me").job
        step = self.referee.intend_step(
            job.job_id,
            "s1",
            "act",
            self.alice.participant_id,
            "roll",
            {"expression": "1d6"},
        )
        self.store.execute(self.alice, roll(step.request_id, 0))
        self.referee.reconcile_steps()
        self.referee.finish(job.job_id, "interrupted")
        bob_job = self.ask(self.bob, "b-1", "Weather?", private=True).job
        # Bob's private reply cannot carry Alice's result, so it may not claim it.
        self.refused("malformed_reply", self.referee.publish_reply, bob_job.job_id,
                     reply(("sender", "Stormy."), narrated=[(job.job_id, "s1")]))  # fmt: skip
        self.referee.publish_reply(bob_job.job_id, reply(("sender", "Stormy.")))
        self.assertEqual([s.step_id for s in self.referee.unnarrated_steps()], ["s1"])
        # A reply the table sees, which lists it, acknowledges it.
        next_job = self.ask(self.bob, "b-2", "What happened to Alice?").job
        self.referee.publish_reply(
            next_job.job_id,
            reply(("table", "Alice rolled a 4."), narrated=[(job.job_id, "s1")]),
        )
        self.assertEqual(self.referee.unnarrated_steps(), [])

    def test_a_reply_may_list_its_own_committed_steps(self):
        job = self.ask(self.alice, "a-1", "Roll for me").job
        step = self.referee.intend_step(job.job_id, "s1", "act", self.alice.participant_id, "roll",
                                        {"expression": "1d6"})  # fmt: skip
        own = reply(("table", "Rolled a 4."), narrated=[(job.job_id, "s1")])
        # Not committed yet: claiming it is refused.
        self.refused("malformed_reply", self.referee.publish_reply, job.job_id, own)
        self.store.execute(self.alice, roll(step.request_id, 0))
        self.referee.record_step(job.job_id, "s1", "committed", {"type": "result"})
        self.referee.publish_reply(job.job_id, own)
        self.assertEqual(self.referee.job(job.job_id).state, "completed")

    def test_a_reply_cannot_acknowledge_a_step_that_is_not_waiting(self):  # R75-2
        job = self.ask(self.alice, "a-1").job
        self.refused("malformed_reply", self.referee.publish_reply, job.job_id,
                     reply(("table", "Hi"), narrated=[("j-none", "s1")]))  # fmt: skip

    def test_step_ids_are_valid_request_id_parts(self):  # R75-3
        job = self.ask(self.alice, "a-1").job
        for step_id in ("first roll", "tab\tstep", "", "x" * 65):
            with self.subTest(step_id=step_id):
                self.refused("invalid_request", self.referee.intend_step, job.job_id, step_id, "act",
                             self.alice.participant_id, "roll", {"expression": "1d6"})  # fmt: skip
        step = self.referee.intend_step(job.job_id, "x" * 64, "act", self.alice.participant_id, "roll",
                                        {"expression": "1d6"})  # fmt: skip
        self.assertEqual(
            self.store.execute(self.alice, roll(step.request_id, 0))["type"], "result"
        )

    def test_the_projection_reads_one_committed_state(self):  # R75-4
        a = self.alice.participant_id
        first = self.ask(self.alice, "a-1", "Can I go?").job
        self.referee.publish_reply(
            first.job_id, reply(("table", "Proceed?"), ask=[(a, "Proceed?")])
        )
        pending = self.ask(self.alice, "a-2", "Yes").job
        other = GameStore(self.path)
        self.addCleanup(other.close)
        original = RefereeStore.messages_for

        def publish_midway(store, participant_id, after_seq=0):
            # Another connection commits between the projection's reads.
            RefereeStore(other).publish_reply(
                pending.job_id, reply(("table", "Done."), resolve=[a])
            )
            RefereeStore.messages_for = original
            return original(store, participant_id, after_seq)

        RefereeStore.messages_for = publish_midway
        self.addCleanup(setattr, RefereeStore, "messages_for", original)
        seen = self.referee.projection(a)
        # Everything from before the other commit, nothing mixed:
        self.assertEqual(
            (
                seen["state"],
                seen["choices"],
                seen["your_last_input"]["job_state"],
                seen["last_seq"],
            ),
            ("busy", [{"question": "Proceed?"}], "running", 3),
        )
        after = self.referee.projection(a)
        self.assertEqual((after["state"], after["choices"], after["your_last_input"]["job_state"], after["last_seq"]),
                         ("idle", [], "completed", 4))  # fmt: skip


if __name__ == "__main__":
    unittest.main()
