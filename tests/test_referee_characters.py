"""The referee in a character game (CHR-3, #113): frozen actors, the mode
table for the bridge, actor-scoped choices and recovery, host-enforced private
creation, creation results that stay private through recovery, completion by
approval and introduction into play, under docs/designs/character_contract.md
sections 5 to 8. Uses the bundled original `trailhead` module, the real store
and bridge tools, and, for one job, the scripted referee through the real
runner and bridge process. No network host (CHR-4) and no model."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tesserae.builtin import CATALOG
from tesserae.credentials import new_token
from tesserae.referee import runner
from tesserae.referee.orchestrator import CodexReferee, job_prompt
from tesserae.referee.runner import REPLY_SCHEMA, _matches, reply_schema
from tesserae.referee.store import RefereeError, RefereeStore
from tesserae.referee.tools import BridgeTools
from tesserae.store import Actor, GameStore, ParticipantContext

TRAILHEAD = CATALOG.resolve("trailhead")
SCRIPTED = Path(__file__).resolve().parent / "fixtures" / "scripted_referee.py"


def reply(*messages, ask=(), resolve=(), narrated=()):
    """A character game's reply: asks and resolves name actors."""
    return {
        "messages": [{"audience": a, "text": t} for a, t in messages],
        "choices": {"ask": [{"actor": a, "question": q} for a, q in ask],
                    "resolve": list(resolve)},
        "narrated": [{"job_id": j, "step_id": s} for j, s in narrated],
    }  # fmt: skip


class Table(unittest.TestCase):
    """A Trailhead game in referee mode: Alice and Bob, the referee store,
    and bridge tools for each job."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "trail.sqlite3"
        self.roll = 3
        self.store = GameStore.create(self.path, TRAILHEAD, randint=self.randint)
        self.addCleanup(self.store.close)
        self.tokens = {"alice": new_token(), "bob": new_token()}
        for name, token in self.tokens.items():
            self.store.add_participant(name.title(), token)
        self.alice = self.store.authenticate(self.tokens["alice"])
        self.bob = self.store.authenticate(self.tokens["bob"])
        self.referee = RefereeStore(self.store)
        self.referee.enable()
        self.tools = BridgeTools(self.open)
        self.requests = iter(range(10**6))

    def randint(self, low, high):
        return self.roll

    def open(self):
        return GameStore(self.path, randint=self.randint, runtime=TRAILHEAD)

    def rid(self) -> str:
        return f"r-{next(self.requests)}"

    # --- characters, made through the store's own operations -----------------

    def draft(self, who=None, name=None) -> str:
        reply_, committed = self.store.create_character(
            who or self.alice, self.rid(), name
        )
        self.assertTrue(committed, reply_)
        return reply_["result"]["character"]["character_id"]

    def ready(self, who=None, name="Ash", trait="quick") -> str:
        who = who or self.alice
        character_id = self.draft(who, name)
        creating = Actor(character_id, "creation")
        for command, args in (("trailhead.trait", {"trait": trait}),
                              ("trailhead.stamina", {})):  # fmt: skip
            raw = json.dumps({"version": 1, "type": "command", "request_id": self.rid(),
                              "expected_revision": self.store.revision(),
                              "command": command, "args": args})  # fmt: skip
            self.assertTrue(self.store.execute_command(who, raw, actor=creating)[1])
        revision = self.store.character(character_id).draft_revision
        done, committed = self.store.complete_character(
            who, self.rid(), character_id, revision
        )
        self.assertTrue(committed, done)
        return character_id

    # --- jobs and tools ------------------------------------------------------

    def start(self, who, actor, text="Hello", private=False, request_id=None):
        submission = self.referee.submit(
            who, request_id or self.rid(), text, private, actor=actor
        )
        self.assertEqual(submission.outcome, "accepted")
        return submission.job, self.tools.grant(submission.job.job_id)

    def call(self, token, tool, **args):
        return self.tools.handle({"token": token, "tool": tool, "args": args})

    def ok(self, token, tool, **args) -> dict:
        response = self.call(token, tool, **args)
        self.assertTrue(response["ok"], response)
        return response["result"]

    def refused(self, code, token, tool, **args):
        response = self.call(token, tool, **args)
        self.assertEqual(
            (response["ok"], response["error"]["code"]), (False, code), response
        )

    def act(self, token, step_id, command, **args):
        return self.call(token, "act", step_id=step_id, command=command, args=args)

    def answer(self, job, *messages, **choices):
        """Publish a job's reply, ending it."""
        self.tools.revoke()
        return self.referee.publish_reply(job.job_id, reply(*messages, **choices))

    def fail(self, job):
        self.tools.revoke()
        self.referee.finish(job.job_id, "failed", "malformed_reply")

    def walker(self, character_id) -> dict:
        return self.store.inspect_state()["walkers"][character_id]

    def steps(self) -> list:
        return self.store.query(
            "SELECT job_id, step_id, status, narrated FROM referee_steps"
        )


class AcceptanceTests(Table):
    def test_an_input_keeps_the_actor_it_was_accepted_for(self):
        ash = self.ready()
        job, _ = self.start(self.alice, Actor(ash, "play"))
        self.assertEqual(
            (job.character_id, job.mode, job.actor), (ash, "play", Actor(ash, "play"))
        )
        self.answer(job, ("table", "The trail climbs."))
        setup = self.referee.submit(self.bob, "b-1", "Who is here?", False).job
        self.assertEqual(
            (setup.character_id, setup.mode), (None, "setup")
        )  # the default

    def test_creation_input_is_private_whatever_its_flag(self):
        draft = self.draft(self.bob)
        job, _ = self.start(
            self.bob, Actor(draft, "creation"), "Someone who reads weather"
        )
        self.assertTrue(job.private)
        said = self.referee.input_message(job.job_id)
        self.assertEqual(
            (said["audience"], said["creation"], said["character_id"]),
            ([self.bob.participant_id], True, draft),
        )
        self.assertEqual(self.referee.messages_for(self.alice.participant_id), [])
        # An exact retry compares the effective privacy: the same input.
        again = self.referee.submit(
            self.bob, job.request_id, job.text, False, actor=Actor(draft, "creation")
        )
        self.assertEqual((again.outcome, again.job.job_id), ("replayed", job.job_id))

    def test_a_retry_under_another_character_or_mode_is_a_conflict(self):
        ash, wren = self.ready(), self.ready(name="Wren")
        job, _ = self.start(self.alice, Actor(ash, "play"), "Push on", request_id="a-1")
        self.answer(job, ("table", "Onward."))
        for actor in (Actor(wren, "play"), Actor(None, "setup")):
            with self.subTest(actor=actor), self.assertRaises(RefereeError) as refused:
                self.referee.submit(self.alice, "a-1", "Push on", False, actor=actor)
            self.assertEqual(refused.exception.code, "request_conflict")
        replay = self.referee.submit(
            self.alice, "a-1", "Push on", False, actor=Actor(ash, "play")
        )
        self.assertEqual(replay.outcome, "replayed")
        self.assertEqual(len(self.store.query("SELECT 1 FROM referee_jobs")), 1)

    def test_an_actor_that_no_longer_fits_is_refused_with_nothing_stored(self):
        ash = self.ready()
        draft = self.draft()
        done = self.ready(name="Wren")
        self.store.archive_character(self.alice, self.rid(), done)
        cases = [
            (self.bob, Actor(ash, "play"), "character_unavailable"),  # not Bob's
            (self.alice, Actor(done, "play"), "character_unavailable"),  # archived
            (self.alice, Actor(ash, "creation"), "character_changed"),  # ready now
            (self.alice, Actor(draft, "play"), "character_changed"),  # still a draft
        ]
        for who, actor, code in cases:
            with self.subTest(actor=actor), self.assertRaises(RefereeError) as refused:
                self.referee.submit(who, self.rid(), "Hi", False, actor=actor)
            self.assertEqual(refused.exception.code, code)
        self.assertEqual(
            self.store.query("SELECT count(*) FROM referee_inputs"), [(0,)]
        )

    def test_a_game_without_characters_takes_no_actor(self):
        relay = GameStore.create(
            self.path.with_name("relay.sqlite3"), CATALOG.resolve("relay")
        )
        self.addCleanup(relay.close)
        token = new_token()
        relay.add_participant("Alice", token)
        referee = RefereeStore(relay)
        referee.enable()
        with self.assertRaises(ValueError):
            referee.submit(
                relay.authenticate(token),
                "r-1",
                "Hi",
                False,
                actor=Actor(None, "setup"),
            )
        job = referee.submit(relay.authenticate(token), "r-1", "Hi", False).job
        self.assertEqual((job.character_id, job.mode), (None, None))
        self.assertNotIn("creation", referee.input_message(job.job_id))


class ModeTableTests(Table):
    def test_setup_jobs_change_nothing_and_journal_nothing(self):
        self.ready()
        job, token = self.start(self.bob, Actor(None, "setup"), "Can I play a dog?")
        before = self.store.inspect_state()
        self.refused(
            "mode_forbidden",
            token,
            "act",
            step_id="s1",
            command="trailhead.push",
            args={},
        )
        self.refused("mode_forbidden", token, "act", step_id="s2", command="trailhead.trait",
                     args={"trait": "keen"})  # fmt: skip
        self.refused(
            "mode_forbidden", token, "referee_act", step_id="s3", command="x", args={}
        )
        self.assertEqual((self.steps(), self.store.inspect_state()), ([], before))
        self.assertEqual(self.ok(token, "list_actions")["actions"], [])
        self.ok(token, "record_ruling", text="Dogs may walk the trail.", scope="setup")

    def test_a_creation_job_takes_only_its_own_drafts_steps(self):
        ash = self.ready()
        draft, other = self.draft(), self.draft(name="Fen")
        job, token = self.start(self.alice, Actor(draft, "creation"), "Make me keen")
        result = self.ok(token, "act", step_id="s1", command="trailhead.trait",
                         args={"trait": "keen"})  # fmt: skip
        self.assertEqual(result["status"], "committed")
        self.ok(
            token, "act", step_id="s2", command="character.name", args={"name": "Kit"}
        )
        self.assertEqual(
            (self.walker(draft)["trait"], self.walker(other)["trait"]), ("keen", None)
        )
        record = self.store.character(draft)
        self.assertEqual((record.name, record.draft_revision), ("Kit", 3))
        stamina = self.walker(ash)["stamina"]
        self.refused(
            "mode_forbidden",
            token,
            "act",
            step_id="s3",
            command="trailhead.push",
            args={},
        )
        self.refused(
            "mode_forbidden", token, "referee_act", step_id="s4", command="x", args={}
        )
        self.assertEqual(
            self.walker(ash)["stamina"], stamina
        )  # never a ready one's resources
        actions = [a["command"] for a in self.ok(token, "list_actions")["actions"]]
        self.assertEqual(
            actions, ["character.name", "trailhead.trait", "trailhead.stamina"]
        )

    def test_a_play_job_acts_as_its_character_and_never_edits_a_draft(self):
        ash, wren = self.ready(), self.ready(name="Wren")
        self.draft()
        job, token = self.start(self.alice, Actor(ash, "play"), "Push on")
        self.roll = 5
        result = self.ok(token, "act", step_id="p1", command="trailhead.push", args={})
        self.assertEqual(
            result["result"]["result"]["stamina"], {"before": 4, "after": 3}
        )
        self.assertEqual(
            (self.walker(ash)["stamina"], self.walker(wren)["stamina"]), (3, 4)
        )
        self.refused("mode_forbidden", token, "act", step_id="p2", command="trailhead.trait",
                     args={"trait": "keen"})  # fmt: skip
        self.assertEqual(
            [a["command"] for a in self.ok(token, "list_actions")["actions"]],
            ["trailhead.push"],
        )

    def test_switching_characters_never_resets_what_they_spent(self):
        ash, wren = self.ready(), self.ready(name="Wren")
        self.roll = 5
        for character in (ash, wren, ash):
            job, token = self.start(self.alice, Actor(character, "play"), "Push on")
            self.ok(token, "act", step_id="p1", command="trailhead.push", args={})
            self.answer(job, ("table", "Onward."))
        self.assertEqual(
            (self.walker(ash)["stamina"], self.walker(wren)["stamina"]), (2, 3)
        )

    def test_a_retried_step_runs_once(self):
        draft = self.draft()
        job, token = self.start(self.alice, Actor(draft, "creation"), "Roll it")
        first = self.ok(
            token, "act", step_id="s1", command="trailhead.stamina", args={}
        )
        self.roll = 1
        again = self.ok(
            token, "act", step_id="s1", command="trailhead.stamina", args={}
        )
        self.assertEqual(again, first)
        self.assertEqual(
            (self.walker(draft)["rolled"], self.store.character(draft).draft_revision),
            (4, 2),
        )

    def test_the_actor_holds_though_its_player_loses_access(self):
        token_file = new_token()
        cara_id = self.store.add_participant("Cara", token_file)
        cara = self.store.authenticate(token_file)
        draft = self.draft(cara)
        job, token = self.start(cara, Actor(draft, "creation"), "Steady, please")
        self.store.revoke_credential(cara_id)  # accepted first: the job finishes
        self.ok(
            token,
            "act",
            step_id="s1",
            command="trailhead.trait",
            args={"trait": "steady"},
        )
        self.assertEqual(self.walker(draft)["trait"], "steady")
        self.answer(job, ("sender", "Steady it is."))
        with self.assertRaises(RefereeError) as refused:
            self.referee.submit(
                cara, self.rid(), "More", False, actor=Actor(draft, "creation")
            )
        self.assertEqual(refused.exception.code, "session_revoked")


class ChoiceTests(Table):
    def test_each_actor_sees_and_resolves_only_its_own_question(self):
        ash, wren = self.ready(), self.ready(name="Wren")
        bob = self.ready(self.bob, name="Odo")
        job, _ = self.start(self.alice, Actor(ash, "play"), "Can Ash cross?")
        self.answer(
            job, ("table", "The bridge sways."),
            ask=[(ash, "Cross now?"), (bob, "Will Odo hold the rope?")],
        )  # fmt: skip
        alice = self.alice.participant_id

        def question(participant, actor):
            choices = self.referee.projection(participant, actor)["choices"]
            return choices[0]["question"] if choices else None

        self.assertEqual(question(alice, Actor(ash, "play")), "Cross now?")
        self.assertIsNone(question(alice, Actor(wren, "play")))
        self.assertIsNone(question(alice, None))  # setup
        self.assertEqual(
            question(self.bob.participant_id, Actor(bob, "play")),
            "Will Odo hold the rope?",
        )
        # Resolving Wren or Alice's setup question leaves Ash's and Odo's alone.
        job, _ = self.start(self.alice, Actor(wren, "play"), "Wren waits")
        self.answer(job, ("table", "Wren waits."), resolve=[wren, alice])
        self.assertEqual(question(alice, Actor(ash, "play")), "Cross now?")
        job, _ = self.start(self.alice, Actor(ash, "play"), "Yes")
        self.answer(job, ("table", "Ash crosses."), resolve=[ash])
        self.assertIsNone(question(alice, Actor(ash, "play")))
        self.assertEqual(
            question(self.bob.participant_id, Actor(bob, "play")),
            "Will Odo hold the rope?",
        )

    def test_who_a_reply_may_address_depends_on_its_mode(self):
        ash = self.ready()
        draft = self.draft(self.bob)
        alice, bob = self.alice.participant_id, self.bob.participant_id
        cases = [  # (sender, actor, choices) the reply may not use
            (self.bob, Actor(None, "setup"), {"ask": [(alice, "Ready?")]}),
            (self.bob, Actor(None, "setup"), {"ask": [(ash, "Ready?")]}),
            (self.bob, Actor(draft, "creation"), {"ask": [(ash, "Ready?")]}),
            (self.bob, Actor(draft, "creation"), {"resolve": [bob]}),
            (self.alice, Actor(ash, "play"), {"ask": [(draft, "Ready?")]}),  # a draft
            (self.alice, Actor(ash, "play"), {"resolve": [draft]}),
            (self.alice, Actor(ash, "play"), {"ask": [("c_ffffffffffffffff", "?")]}),
        ]
        for who, actor, choices in cases:
            with self.subTest(actor=actor, choices=choices):
                audience = "sender" if actor.mode == "creation" else "table"
                job, _ = self.start(who, actor, "Hm")
                with self.assertRaises(RefereeError) as refused:
                    self.tools.revoke()
                    self.referee.publish_reply(
                        job.job_id, reply((audience, "Hm."), **choices)
                    )
                self.assertEqual(refused.exception.code, "malformed_reply")
                self.referee.finish(job.job_id, "failed", "malformed_reply")
        self.assertEqual(
            self.store.query("SELECT count(*) FROM referee_choices"), [(0,)]
        )

    def test_the_last_input_shown_is_the_actors_own(self):
        ash, wren = self.ready(), self.ready(name="Wren")
        for actor, text in (
            (Actor(ash, "play"), "Ash speaks"),
            (Actor(wren, "play"), "Wren speaks"),
        ):
            job, _ = self.start(self.alice, actor, text)
            self.answer(job, ("table", "Noted."))
        last = {
            actor: self.referee.projection(self.alice.participant_id, actor)[
                "your_last_input"
            ]
            for actor in (Actor(ash, "play"), Actor(wren, "play"), None)
        }
        self.assertEqual(last[Actor(ash, "play")]["text"], "Ash speaks")
        self.assertEqual(last[Actor(wren, "play")]["text"], "Wren speaks")
        self.assertIsNone(last[None])


class CreationPrivacyTests(Table):
    def test_a_creation_reply_cannot_reach_the_table(self):
        draft = self.draft(self.bob)
        job, _ = self.start(self.bob, Actor(draft, "creation"), "Pick for me")
        with self.assertRaises(RefereeError) as refused:
            self.answer(
                job, ("sender", "Keen, then."), ("table", "Bob's walker is keen!")
            )
        self.assertEqual(refused.exception.code, "malformed_reply")
        self.assertEqual(
            self.store.query(
                "SELECT count(*) FROM referee_messages WHERE sender = 'referee'"
            ),
            [(0,)],
        )
        self.referee.finish(job.job_id, "failed", "malformed_reply")
        job, _ = self.start(self.bob, Actor(draft, "creation"), "Pick for me again")
        [message] = self.answer(
            job, ("sender", "Keen, then."), ask=[(draft, "Roll stamina?")]
        )
        self.assertEqual(
            (message["audience"], message["creation"]),
            ([self.bob.participant_id], True),
        )
        self.assertNotIn(
            message["seq"],
            [m["seq"] for m in self.referee.messages_for(self.alice.participant_id)],
        )
        self.assertEqual(
            self.referee.projection(self.bob.participant_id, Actor(draft, "creation"))[
                "choices"
            ],
            [{"question": "Roll stamina?"}],
        )

    def test_a_failed_creation_jobs_results_stay_with_its_draft(self):
        draft = self.draft(self.bob)
        job, token = self.start(self.bob, Actor(draft, "creation"), "Keen")
        self.ok(
            token,
            "act",
            step_id="s1",
            command="trailhead.trait",
            args={"trait": "keen"},
        )
        self.fail(job)  # its result was never told to Bob

        # Alice's next job neither sees it nor may narrate it.
        ash = self.ready()
        alice_job, token = self.start(self.alice, Actor(ash, "play"), "Look around")
        context = self.ok(token, "get_context")
        self.assertEqual(context["earlier_results"], [])
        with self.assertRaises(RefereeError) as refused:
            self.answer(
                alice_job, ("table", "Bob chose keen."), narrated=[(job.job_id, "s1")]
            )
        self.assertEqual(refused.exception.code, "malformed_reply")
        self.referee.finish(alice_job.job_id, "failed", "malformed_reply")

        # Bob's own next creation job sees it and tells him privately.
        again, token = self.start(self.bob, Actor(draft, "creation"), "Where was I?")
        context = self.ok(token, "get_context")
        self.assertEqual([(s["job_id"], s["step_id"]) for s in context["earlier_results"]],
                         [(job.job_id, "s1")])  # fmt: skip
        self.answer(again, ("sender", "You chose keen."), narrated=[(job.job_id, "s1")])
        self.assertIn((job.job_id, "s1", "committed", 1), self.steps())

    def test_get_context_shows_the_job_its_own_draft_and_no_one_elses(self):
        ash = self.ready()
        mine = self.draft(self.bob, name="Fen")
        theirs = self.draft(name="Kit")
        job, token = self.start(self.bob, Actor(mine, "creation"), "Hi")
        context = self.ok(token, "get_context")
        self.assertEqual([c["character_id"] for c in context["characters"]], [ash])
        self.assertEqual(context["input"]["actor"]["character"]["character_id"], mine)
        self.assertEqual(context["sheet"]["trait"], None)
        self.assertNotIn(theirs, json.dumps(context))
        self.assertNotIn(
            mine, json.dumps(context["context"])
        )  # the module leaves drafts out


class CompletionTests(Table):
    def test_completion_waits_for_approval_at_the_revision_shown(self):
        draft = self.draft(name="Fen")
        job, token = self.start(
            self.alice, Actor(draft, "creation"), "Steady, and roll"
        )
        self.ok(
            token,
            "act",
            step_id="s1",
            command="trailhead.trait",
            args={"trait": "steady"},
        )
        busy, _ = self.store.complete_character(self.alice, self.rid(), draft, 2)
        self.assertEqual(busy["code"], "character_busy")  # not while its job runs
        self.ok(token, "act", step_id="s2", command="trailhead.stamina", args={})
        self.answer(job, ("sender", "Fen is ready for you to approve."))
        stale, _ = self.store.complete_character(self.alice, self.rid(), draft, 2)
        self.assertEqual((stale["code"], stale["draft_revision"]), ("stale_draft", 3))
        self.assertTrue(
            self.store.complete_character(self.alice, self.rid(), draft, 3)[1]
        )

    def test_a_new_character_is_introduced_on_its_first_play_input(self):
        fen = self.ready(name="Fen")
        playing = Actor(fen, "play")
        job, _ = self.start(self.alice, playing, "Fen steps out")
        prompt = job_prompt(job, "Alice", 0, new_session=False, previous=None,
                            character=self.store.character(fen))  # fmt: skip
        self.assertIn(
            '"Fen" has just been completed and is not yet in the scene', prompt
        )
        self.assertIn(f'as "Fen" ({fen}), to the table', prompt)
        self.answer(job, ("table", "Fen joins the walkers."))
        self.assertIsNotNone(self.store.character(fen).introduced_at)
        job, _ = self.start(self.alice, playing, "Onward")
        prompt = job_prompt(job, "Alice", 0, new_session=False, previous=None,
                            character=self.store.character(fen))  # fmt: skip
        self.assertNotIn("not yet in the scene", prompt)

    def test_archiving_waits_for_the_characters_job(self):
        ash = self.ready()
        job, _ = self.start(self.alice, Actor(ash, "play"), "Rest")
        busy, _ = self.store.archive_character(self.alice, self.rid(), ash)
        self.assertEqual(busy["code"], "character_busy")
        self.answer(job, ("table", "Ash rests."))
        self.assertTrue(self.store.archive_character(self.alice, self.rid(), ash)[1])


class RecoveryTests(Table):
    def test_an_interrupted_creation_job_resumes_from_what_it_committed(self):
        draft = self.draft(self.bob)
        job, token = self.start(self.bob, Actor(draft, "creation"), "Keen, and roll")
        self.ok(
            token,
            "act",
            step_id="s1",
            command="trailhead.trait",
            args={"trait": "keen"},
        )
        # The host died after journaling s2 but before running it.
        self.referee.intend_step(job.job_id, "s2", "act", self.bob.participant_id,
                                 "trailhead.stamina", {})  # fmt: skip
        self.tools.revoke()
        self.store.close()
        self.store = GameStore(self.path, randint=self.randint, runtime=TRAILHEAD)
        self.referee = RefereeStore(self.store)
        self.referee.reconcile_steps()
        self.referee.finish(job.job_id, "interrupted", "host_stopped")
        self.assertEqual(
            sorted(self.store.query("SELECT step_id, status FROM referee_steps")),
            [("s1", "committed"), ("s2", "not_committed")],
        )
        again, token = self.start(self.bob, Actor(draft, "creation"), "Go on")
        steps = self.ok(token, "get_context")["earlier_results"]
        self.assertEqual([s["step_id"] for s in steps], ["s1"])
        self.assertIsNone(self.walker(draft)["rolled"])  # never rolled twice
        self.ok(token, "act", step_id="s1", command="trailhead.stamina", args={})
        self.assertEqual(self.walker(draft)["rolled"], 4)

    def test_a_relinked_player_cannot_act_as_their_old_participants_characters(self):
        ash = self.ready()
        alice = self.alice.participant_id
        self.store.link_host_player("host", "u_alice", alice)
        linked = self.store.admit_host_player("host", "u_alice", "Alice")
        job, token = self.start(linked, Actor(ash, "play"), "Push")
        self.store.unlink_host_player("host", "u_alice")  # mid-job: it still finishes
        self.roll = 5
        self.ok(token, "act", step_id="p1", command="trailhead.push", args={})
        self.answer(job, ("table", "Onward."))
        moved = self.store.admit_host_player(
            "host", "u_alice", "Alice"
        )  # a new participant
        self.assertNotEqual(moved.participant_id, alice)
        with self.assertRaises(RefereeError) as refused:
            self.referee.submit(
                moved, self.rid(), "Push", False, actor=Actor(ash, "play")
            )
        self.assertEqual(refused.exception.code, "character_unavailable")
        self.assertEqual(self.store.characters(moved), [])


class SchemaAndPromptTests(Table):
    def test_creation_replies_are_shaped_for_the_sender_only(self):
        creation = reply_schema(characters=True, creation=True)
        play = reply_schema(characters=True)
        self.assertIs(reply_schema(), REPLY_SCHEMA)
        private = reply(("sender", "Hi."), ask=[("c_0123456789abcdef", "Roll?")])
        table = reply(("table", "Hi."))
        self.assertTrue(_matches(private, creation))
        self.assertFalse(_matches(table, creation))
        self.assertTrue(_matches(table, play))
        two = reply(
            ("sender", "Hi."),
            ask=[("c_0123456789abcdef", "A?"), ("c_0123456789abcdef", "B?")],
        )
        self.assertFalse(_matches(two, creation))
        legacy = {"messages": [{"audience": "table", "text": "Hi."}],
                  "choices": {"ask": [{"participant_id": "p_x", "question": "Q?"}], "resolve": []},
                  "narrated": []}  # fmt: skip
        self.assertFalse(_matches(legacy, play))
        self.assertTrue(_matches(legacy, REPLY_SCHEMA))

    def test_each_mode_tells_the_referee_its_rules(self):
        draft = self.draft(self.bob)
        setup, _ = self.start(self.bob, Actor(None, "setup"), "Rules?")
        self.answer(setup, ("table", "Ask away."))
        creating, _ = self.start(self.bob, Actor(draft, "creation"), "Hi")
        prompts = {
            job.mode: job_prompt(job, "Bob", 0, new_session=False, previous=None,
                                 character=None if job.character_id is None
                                 else self.store.character(job.character_id))
            for job in (setup, creating)
        }  # fmt: skip
        self.assertIn("who has no character selected, to the table", prompts["setup"])
        self.assertIn("nothing in the game may change", prompts["setup"])
        self.assertIn(
            f'creating draft {draft} "Unnamed" (revision 1), privately to you',
            prompts["creation"],
        )
        self.assertIn("Reply only to the sender", prompts["creation"])


class ScriptedCreationTests(unittest.IsolatedAsyncioTestCase):
    """One creation job through the real runner, bridge process and per-job
    schema, with the scripted stand-in for Codex."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(
            dir="/tmp", prefix="tc"
        )  # short socket paths
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.folder = runner.setup(root / "r")
        self.fake = root / "f"
        self.fake.mkdir()
        patch = mock.patch.dict(os.environ, {"FAKE_REFEREE_DIR": str(self.fake)})
        patch.start()
        self.addCleanup(patch.stop)
        self.path = root / "trail.sqlite3"
        token = new_token()
        with GameStore.create(self.path, TRAILHEAD) as store:
            self.game_id = store.game_id
            store.add_participant("Bob", token)
            RefereeStore(store).enable()
            self.bob = store.authenticate(token)
            created, _ = store.create_character(self.bob, "c-1", None)
            self.draft = created["result"]["character"]["character_id"]

    def open(self):
        return GameStore(self.path, randint=lambda low, high: 2, runtime=TRAILHEAD)

    async def test_a_creation_job_runs_privately_with_its_own_schema(self):
        script = {
            "calls": [
                {
                    "tool": "act",
                    "args": {
                        "step_id": "s1",
                        "command": "trailhead.stamina",
                        "args": {},
                    },
                }
            ],
            "reply": reply(
                ("sender", "You rolled {results}."), ask=[(self.draft, "Which trait?")]
            ),
        }
        (self.fake / "script.json").write_text(json.dumps([script]))
        referee = CodexReferee(self.folder, self.game_id, codex=(sys.executable, str(SCRIPTED)),
                               timeout=30, grace=1, lock_retry=0.1)  # fmt: skip
        referee.open("run-1", self.open, None)
        self.addCleanup(referee.close)
        with self.open() as store:
            job = RefereeStore(store).submit(
                ParticipantContext(self.bob.participant_id, credential_id=self.bob.credential_id),
                "b-1", "Roll my stamina", False, "run-1", actor=Actor(self.draft, "creation"),
            ).job  # fmt: skip
        answer = await asyncio.wait_for(referee(job), 30)
        with self.open() as store:
            [message] = RefereeStore(store).publish_reply(job.job_id, answer)
            self.assertEqual(
                store.inspect_state()["walkers"][self.draft]["rolled"], 3
            )  # 2 + 1
            self.assertEqual(store.character(self.draft).draft_revision, 2)
        self.assertEqual(
            (message["audience"], message["creation"]),
            ([self.bob.participant_id], True),
        )
        [run] = [
            json.loads(line)
            for line in (self.fake / "log.jsonl").read_text().splitlines()
        ]
        schema_path = Path(run["argv"][run["argv"].index("--output-schema") + 1])
        self.assertEqual(schema_path.name, f"{job.job_id}.schema.json")
        self.assertEqual(
            json.loads(schema_path.read_text()),
            reply_schema(characters=True, creation=True),
        )
        self.assertIn("Mode: creation.", run["prompt"])


if __name__ == "__main__":
    unittest.main()
