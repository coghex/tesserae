"""Characters in a game's own store (CHR-2, #112): the schema 7 records, the
module capability, the lifecycle operations and seed version 2, under
docs/designs/character_contract.md. Uses the bundled original `trailhead`
module; no network and no referee."""

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from tesserae import store as store_module
from tesserae.builtin import CATALOG, DICE_PROVIDER
from tesserae.credentials import new_token
from tesserae.modules import (
    ActionRejected,
    Catalog,
    Command,
    ModuleError,
    Outcome,
    SeedError,
)
from tesserae.seed import parse_seed
from tesserae.store import SCHEMA_VERSION, Actor, GameStore
from tesserae.trailhead import TRAILHEAD_MODULE

TRAILHEAD = CATALOG.resolve("trailhead")
ALICE_ID = "p_a1a1a1a1a1a1a1a1"
BOB_ID = "p_b2b2b2b2b2b2b2b2"
MARA = "c_0123456789abcdef"


def command(request_id: str, name: str, args: dict, revision: int) -> str:
    return json.dumps({
        "version": 1, "type": "command", "request_id": request_id,
        "expected_revision": revision, "command": name, "args": args,
    })  # fmt: skip


def as_schema_6(path):
    """Turn a current store back into the schema 6 one the previous release
    left: no characters table, no actor columns, participant-keyed choices."""
    choices = next(
        statement
        for statement in store_module._MIGRATIONS[5]
        if "CREATE TABLE referee_choices" in statement
    )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            "DROP TABLE characters;"
            "ALTER TABLE referee_inputs DROP COLUMN character_id;"
            "ALTER TABLE referee_inputs DROP COLUMN mode;"
            "ALTER TABLE referee_steps DROP COLUMN character_id;"
            "ALTER TABLE referee_messages DROP COLUMN character_id;"
            "ALTER TABLE referee_messages DROP COLUMN creation;"
            "DROP TABLE referee_choices;"
            f"{choices};"
            "PRAGMA user_version = 6;"
        )


class CharacterCase(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.directory / "trail.sqlite3"
        self.draws = Mock(return_value=3)
        self.store = self.enterContext(
            GameStore.create(self.path, TRAILHEAD, randint=self.draws)
        )
        self.alice = self.player("Alice")
        self.bob = self.player("Bob")

    def player(self, name: str):
        token = new_token()
        self.store.add_participant(name, token)
        return self.store.authenticate(token)

    def run_command(self, context, request_id, name, args, actor, store=None):
        store = store or self.store
        return store.execute_command(
            context, command(request_id, name, args, store.revision()), actor=actor
        )

    def draft(self, context=None, name="Ash", trait="keen", roll=True) -> str:
        """A draft taken as far as asked, by its owner's raw creation steps."""
        context = context or self.alice
        reply, _ = self.store.create_character(context, new_request(), None)
        character_id = reply["result"]["character"]["character_id"]
        creating = Actor(character_id, "creation")
        steps = []
        if name:
            steps.append(("character.name", {"name": name}))
        if trait:
            steps.append(("trailhead.trait", {"trait": trait}))
        if roll:
            steps.append(("trailhead.stamina", {}))
        for step, args in steps:
            reply, committed = self.run_command(
                context, new_request(), step, args, creating
            )
            self.assertTrue(committed, reply)
        return character_id

    def ready(self, context=None, **draft) -> str:
        context = context or self.alice
        character_id = self.draft(context, **draft)
        revision = self.record(character_id).draft_revision
        reply, committed = self.store.complete_character(
            context, new_request(), character_id, revision
        )
        self.assertTrue(committed, reply)
        return character_id

    def record(self, character_id):
        return {c.character_id: c for c in self.store.inspect_characters()}[
            character_id
        ]

    def fingerprint(self):
        """Everything a refused operation must leave alone."""
        return (
            self.store.revision(),
            self.store.inspect_state(),
            self.store.inspect_characters(),
            self.store.inspect_history(),
        )


_requests = iter(range(10**9))


def new_request() -> str:
    return f"r-{next(_requests)}"


class LifecycleTests(CharacterCase):
    def test_a_draft_is_created_built_completed_and_archived(self):
        reply, committed = self.store.create_character(self.alice, "c-1", None)
        self.assertTrue(committed)
        character = reply["result"]["character"]
        ash = character["character_id"]
        self.assertTrue(store_module.is_character_id(ash))
        self.assertEqual(
            character, {"character_id": ash, "name": None, "phase": "draft", "draft_revision": 1}
        )  # fmt: skip
        self.assertEqual(self.store.inspect_state()["walkers"][ash]["ready"], False)
        self.assertEqual(self.store.revision(), 1)

        creating = Actor(ash, "creation")
        self.run_command(self.alice, "s-1", "character.name", {"name": "Ash"}, creating)
        self.run_command(
            self.alice, "s-2", "trailhead.trait", {"trait": "keen"}, creating
        )
        reply, _ = self.run_command(
            self.alice, "s-3", "trailhead.stamina", {}, creating
        )
        self.assertEqual(reply["result"]["stamina"], 4)  # 3 + 1, recorded with its roll
        self.assertEqual(reply["result"]["roll"]["terms"][0]["rolls"], [3])
        self.assertEqual(
            (self.record(ash).name, self.record(ash).draft_revision), ("Ash", 4)
        )

        self.draws.reset_mock()
        reply, committed = self.store.complete_character(self.alice, "k-1", ash, 4)
        self.assertTrue(committed)
        self.draws.assert_not_called()  # completion never draws
        self.assertEqual(
            reply["result"]["character"],
            {"character_id": ash, "name": "Ash", "phase": "ready", "draft_revision": None},
        )  # fmt: skip
        walker = self.store.inspect_state()["walkers"][ash]
        self.assertEqual((walker["stamina"], walker["max_stamina"]), (5, 5))  # keen +1
        self.assertIsNotNone(self.record(ash).completed_at)

        reply, committed = self.store.archive_character(self.alice, "a-1", ash)
        self.assertTrue(committed)
        self.assertEqual(reply["result"]["character"]["phase"], "archived")
        self.assertEqual(self.store.characters(self.alice), [])
        self.assertIn(ash, self.store.inspect_state()["walkers"])  # data stays
        self.assertEqual(len(self.store.inspect_history()), 6)  # every step recorded

    def test_lifecycle_operations_replay_exactly(self):
        first, _ = self.store.create_character(self.alice, "c-1", "Ash")
        again, committed = self.store.create_character(self.alice, "c-1", "Ash")
        self.assertEqual((again, committed), (first, False))
        self.assertEqual(len(self.store.inspect_characters()), 1)
        other, _ = self.store.create_character(self.alice, "c-1", "Wren")
        self.assertEqual(other["code"], "request_conflict")

        ash = first["result"]["character"]["character_id"]
        creating = Actor(ash, "creation")
        self.run_command(
            self.alice, "s-1", "trailhead.trait", {"trait": "quick"}, creating
        )
        self.run_command(self.alice, "s-2", "trailhead.stamina", {}, creating)
        done, _ = self.store.complete_character(self.alice, "k-1", ash, 3)
        before = self.fingerprint()
        self.assertEqual(
            self.store.complete_character(self.alice, "k-1", ash, 3), (done, False)
        )
        self.assertEqual(self.fingerprint(), before)
        archived, _ = self.store.archive_character(self.alice, "a-1", ash)
        self.assertEqual(
            self.store.archive_character(self.alice, "a-1", ash), (archived, False)
        )

    def test_refused_completions_change_nothing(self):
        unnamed = self.draft(name=None)
        before = self.fingerprint()
        reply, _ = self.store.complete_character(self.alice, "k-1", unnamed, 3)
        self.assertEqual(
            (reply["code"], reply["problems"]),
            ("character_incomplete", ["Give the character a name."]),
        )
        self.assertEqual(self.fingerprint(), before)
        bare = self.draft(trait=None, roll=False)
        before = self.fingerprint()
        reply, _ = self.store.complete_character(self.alice, "k-2", bare, 2)
        self.assertEqual(
            reply["problems"], ["Choose a trait.", "Roll stamina."]
        )  # the module's own list
        self.assertEqual(self.fingerprint(), before)

        ash = self.draft()
        before = self.fingerprint()
        reply, _ = self.store.complete_character(self.alice, "k-3", ash, 3)
        self.assertEqual((reply["code"], reply["draft_revision"]), ("stale_draft", 4))
        reply, _ = self.store.complete_character(self.bob, "k-4", ash, 4)
        self.assertEqual(reply["code"], "character_unavailable")  # not Bob's
        self.assertEqual(self.fingerprint(), before)

        self.store.complete_character(self.alice, "k-5", ash, 4)
        reply, _ = self.store.complete_character(self.alice, "k-6", ash, 4)
        self.assertEqual(
            (reply["code"], reply["draft_revision"]), ("stale_draft", None)
        )

    def test_unknown_foreign_and_archived_characters_look_the_same(self):
        ash = self.ready()
        gone = self.ready(name="Wren")
        self.store.archive_character(self.alice, new_request(), gone)
        before = self.fingerprint()
        replies = [
            self.store.archive_character(self.bob, "a-1", ash)[0],
            self.store.archive_character(self.alice, "a-2", gone)[0],
            self.store.archive_character(self.alice, "a-3", "c_ffffffffffffffff")[0],
            self.store.complete_character(self.alice, "k-1", "nonsense", 1)[0],
        ]
        self.assertEqual(
            {(r["code"], r["message"]) for r in replies},
            {("character_unavailable", "That character is not available to you.")},
        )
        self.assertEqual(self.fingerprint(), before)
        self.assertIsNone(self.store.character_sheet(self.bob, ash))
        self.assertIsNone(self.store.character_sheet(self.alice, gone))
        self.assertEqual([c.name for c in self.store.characters(self.bob)], [])

    def test_the_module_can_veto_archiving(self):
        ash = self.ready()
        self.draws.return_value = 1  # 1d6 = 1 against 4: in danger
        self.run_command(self.alice, "p-1", "trailhead.push", {}, Actor(ash, "play"))
        before = self.fingerprint()
        reply, _ = self.store.archive_character(self.alice, "a-1", ash)
        self.assertEqual(
            (reply["code"], reply["message"]),
            ("action_rejected", "Not while this walker is in danger; recover first."),
        )
        self.assertEqual(self.fingerprint(), before)

    def test_a_running_job_for_the_character_blocks_completion_and_archiving(self):
        ash = self.draft()
        other = self.draft(name="Wren")
        with self.store.transaction() as db:
            input_id = db.execute(
                "INSERT INTO referee_inputs (participant_id, request_id, text, private, "
                "received_at, character_id, mode) VALUES (?, 'say-1', 'Hi', 1, 'now', ?, "
                "'creation')",
                (self.alice.participant_id, ash),
            ).lastrowid
            db.execute(
                "INSERT INTO referee_jobs (job_id, input_id, state, revision_at_start, "
                "started_at) VALUES ('j-1', ?, 'running', 0, 'now')",
                (input_id,),
            )
        before = self.fingerprint()
        busy = [
            self.store.complete_character(self.alice, "k-1", ash, 4)[0],
            self.store.archive_character(self.alice, "a-1", ash)[0],
        ]
        self.assertEqual({r["code"] for r in busy}, {"character_busy"})
        self.assertEqual(self.fingerprint(), before)
        self.assertTrue(self.store.complete_character(self.alice, "k-2", other, 4)[1])

    def test_completion_ends_the_drafts_question_and_creation_results(self):
        ash, wren = self.draft(), self.draft(name="Wren")
        alice = self.alice.participant_id
        with self.store.transaction() as db:
            db.executemany(
                "INSERT INTO referee_choices (participant_id, character_id, question, "
                "asked_in_job, asked_at) VALUES (?, ?, 'Roll now?', 'j-1', 'now')",
                [(alice, ash), (alice, wren), (alice, None)],
            )
            # A failed creation job's committed, unnarrated steps, one per draft.
            input_id = db.execute(
                "INSERT INTO referee_inputs (participant_id, request_id, text, private, "
                "received_at, character_id, mode) VALUES (?, 'say-1', 'Hi', 1, 'now', ?, "
                "'creation')",
                (alice, ash),
            ).lastrowid
            db.execute(
                "INSERT INTO referee_jobs (job_id, input_id, state, revision_at_start, "
                "started_at) VALUES ('j-1', ?, 'failed', 0, 'now')",
                (input_id,),
            )
            db.executemany(
                "INSERT INTO referee_steps (job_id, step_id, kind, context, command, "
                "args, request_id, status, character_id) VALUES ('j-1', ?, 'act', ?, "
                "'trailhead.trait', '{}', ?, 'committed', ?)",
                [
                    ("s-1", alice, "ref:j-1:s-1", ash),
                    ("s-2", alice, "ref:j-1:s-2", wren),
                ],
            )
        self.store.complete_character(self.alice, "k-1", ash, 4)
        self.assertEqual(
            set(self.store.query("SELECT character_id FROM referee_choices")),
            {(None,), (wren,)},
        )
        self.assertEqual(
            set(self.store.query("SELECT step_id, narrated FROM referee_steps")),
            {("s-1", 1), ("s-2", 0)},
        )

    def test_archiving_keeps_a_failed_play_jobs_results_recoverable(self):
        ash, wren = self.ready(), self.draft(name="Wren")
        alice = self.alice.participant_id
        with self.store.transaction() as db:
            for job, character, mode in (
                ("j-1", ash, "play"),
                ("j-2", wren, "creation"),
            ):
                input_id = db.execute(
                    "INSERT INTO referee_inputs (participant_id, request_id, text, "
                    "private, received_at, character_id, mode) VALUES (?, ?, 'Go', 0, "
                    "'now', ?, ?)",
                    (alice, f"say-{job}", character, mode),
                ).lastrowid
                db.execute(
                    "INSERT INTO referee_jobs (job_id, input_id, state, "
                    "revision_at_start, started_at) VALUES (?, ?, 'failed', 0, 'now')",
                    (job, input_id),
                )
                db.execute(
                    "INSERT INTO referee_steps (job_id, step_id, kind, context, command, "
                    "args, request_id, status, character_id) VALUES (?, 's-1', 'act', ?, "
                    "'x', '{}', ?, 'committed', ?)",
                    (job, alice, f"ref:{job}:s-1", character),
                )
        self.store.archive_character(self.alice, "a-1", ash)
        self.store.archive_character(self.alice, "a-2", wren)
        self.assertEqual(
            set(self.store.query("SELECT job_id, narrated FROM referee_steps")),
            {("j-1", 0), ("j-2", 1)},  # play stays listed; creation is retired
        )

    def test_a_failure_inside_an_operation_rolls_everything_back(self):
        def broken_complete(state, character_id):
            raise RuntimeError("module bug")

        rules = replace(TRAILHEAD_MODULE.characters, complete=broken_complete)
        module = replace(TRAILHEAD_MODULE, characters=rules)
        runtime = Catalog([module], [DICE_PROVIDER]).resolve("trailhead")
        ash = self.draft()
        self.store.close()
        self.store = self.enterContext(GameStore(self.path, runtime=runtime))
        before = self.fingerprint()
        with self.assertRaises(RuntimeError):
            self.store.complete_character(self.alice, "k-1", ash, 4)
        self.assertEqual(self.fingerprint(), before)

    def test_concurrent_completion_and_archiving_commit_once(self):
        ash = self.draft()
        barrier = threading.Barrier(2)
        results = [None, None]

        def complete():
            store = GameStore(self.path)
            with store:
                barrier.wait()
                results[0] = store.complete_character(self.alice, "k-1", ash, 4)

        def archive():
            store = GameStore(self.path)
            with store:
                barrier.wait()
                results[1] = store.archive_character(self.alice, "a-1", ash)

        threads = [threading.Thread(target=complete), threading.Thread(target=archive)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # Serialized either way: completing first leaves a ready character to
        # archive; archiving first leaves nothing to complete.
        completed, archived = results
        self.assertTrue(archived[1], archived[0])
        self.assertTrue(completed[1] or completed[0]["code"] == "character_unavailable")
        self.assertEqual(self.record(ash).phase, "archived")

        # Two completions of one draft: exactly one wins.
        wren = self.draft(name="Wren")
        winners = []

        def approve(request_id):
            with GameStore(self.path) as store:
                barrier.wait()
                winners.append(
                    store.complete_character(self.alice, request_id, wren, 4)
                )

        threads = [threading.Thread(target=approve, args=(r,)) for r in ("x", "y")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(committed for _, committed in winners), [False, True])
        loser = next(reply for reply, committed in winners if not committed)
        self.assertEqual(loser["code"], "stale_draft")

    def test_a_revoked_participant_can_do_nothing(self):
        token = new_token()
        cara_id = self.store.add_participant("Cara", token)
        cara = self.store.authenticate(token)
        ash = self.draft(cara)
        self.store.revoke_credential(cara_id)
        before = self.fingerprint()
        for reply, _ in (
            self.store.create_character(cara, "c-1", None),
            self.store.complete_character(cara, "k-1", ash, 4),
            self.store.archive_character(cara, "a-1", ash),
        ):
            self.assertEqual(reply["code"], "session_revoked")
        self.assertIsNone(self.store.characters(cara))
        self.assertEqual(self.fingerprint(), before)

    def test_invalid_input_is_refused_before_anything_runs(self):
        before = self.fingerprint()
        for reply, _ in (
            self.store.create_character(self.alice, "has space", None),
            self.store.create_character(self.alice, "c-1", " "),
            self.store.complete_character(self.alice, "k-1", 7, 1),
            self.store.complete_character(self.alice, "k-1", MARA, 0),
            self.store.archive_character(self.alice, "a-1", None),
        ):
            self.assertEqual(reply["code"], "invalid_request")
        self.assertEqual(self.fingerprint(), before)


class ActorTests(CharacterCase):
    def test_each_mode_runs_only_its_own_commands(self):
        ash = self.draft()
        wren = self.ready(name="Wren")
        before = self.fingerprint()
        cases = [
            (Actor(None, "setup"), "trailhead.push", {}, "mode_forbidden"),
            (Actor(None, "setup"), "trailhead.trait", {"trait": "quick"}, "mode_forbidden"),
            (Actor(ash, "creation"), "trailhead.push", {}, "mode_forbidden"),
            (Actor(wren, "play"), "trailhead.trait", {"trait": "quick"}, "mode_forbidden"),
            (Actor(wren, "play"), "character.name", {"name": "X"}, "mode_forbidden"),
            (Actor(wren, "creation"), "trailhead.trait", {"trait": "quick"}, "character_changed"),
            (Actor(ash, "play"), "trailhead.push", {}, "character_changed"),
        ]  # fmt: skip
        for actor, name, args, code in cases:
            with self.subTest(actor=actor, command=name):
                reply, committed = self.run_command(
                    self.alice, new_request(), name, args, actor
                )
                self.assertEqual((reply["code"], committed), (code, False))
        reply, _ = self.store.execute_command(
            self.alice, command("r-x", "trailhead.push", {}, self.store.revision())
        )
        self.assertEqual(reply["code"], "mode_forbidden")  # no actor is setup
        self.assertEqual(self.fingerprint(), before)

    def test_nobody_acts_as_another_players_character(self):
        ash = self.draft()
        wren = self.ready(name="Wren")
        before = self.fingerprint()
        for actor, name, args in (
            (Actor(ash, "creation"), "trailhead.trait", {"trait": "quick"}),
            (Actor(wren, "play"), "trailhead.push", {}),
        ):
            reply, _ = self.run_command(self.bob, new_request(), name, args, actor)
            self.assertEqual(reply["code"], "character_unavailable")
        self.assertEqual(self.fingerprint(), before)

    def test_play_acts_as_the_character_and_replays_only_for_it(self):
        ash = self.ready()
        wren = self.ready(name="Wren", trait="quick")
        reply, _ = self.run_command(
            self.alice, "p-1", "trailhead.push", {}, Actor(ash, "play")
        )
        walkers = self.store.inspect_state()["walkers"]
        self.assertEqual((walkers[ash]["stamina"], walkers[wren]["stamina"]), (4, 4))
        self.assertEqual(self.store.inspect_history()[-1].content["character_id"], ash)
        before = self.fingerprint()
        again, committed = self.run_command(
            self.alice, "p-1", "trailhead.push", {}, Actor(ash, "play")
        )
        self.assertEqual((again, committed), (reply, False))
        conflict, _ = self.run_command(
            self.alice, "p-1", "trailhead.push", {}, Actor(wren, "play")
        )
        self.assertEqual(conflict["code"], "request_conflict")  # never into Wren
        self.assertEqual(self.fingerprint(), before)

    def test_creation_steps_touch_only_their_own_draft(self):
        ash, wren = self.draft(trait=None), self.draft(name="Wren", trait=None)
        self.run_command(
            self.alice, new_request(), "trailhead.trait", {"trait": "steady"},
            Actor(ash, "creation"),
        )  # fmt: skip
        walkers = self.store.inspect_state()["walkers"]
        self.assertEqual(
            (walkers[ash]["trait"], walkers[wren]["trait"]), ("steady", None)
        )
        self.assertEqual(
            (self.record(ash).draft_revision, self.record(wren).draft_revision), (4, 3)
        )
        reply, _ = self.run_command(
            self.alice, new_request(), "trailhead.stamina", {}, Actor(ash, "creation")
        )
        self.assertEqual(reply["code"], "action_rejected")  # rolled once already
        self.assertEqual(self.record(ash).draft_revision, 4)  # a refusal is no step

    def test_games_without_characters_are_unchanged(self):
        with GameStore.create(
            self.directory / "relay.sqlite3", CATALOG.resolve("relay")
        ) as relay:
            token = new_token()
            relay.add_participant("Alice", token)
            alice = relay.authenticate(token)
            reply, _ = relay.create_character(alice, "c-1", None)
            self.assertEqual(reply["code"], "characters_unsupported")
            with self.assertRaises(ValueError):
                relay.execute_command(
                    alice, command("e-1", "relay.enlist", {"name": "Wren"}, 0),
                    actor=Actor(None, "setup"),
                )  # fmt: skip
            reply, committed = relay.execute_command(
                alice, command("e-1", "relay.enlist", {"name": "Wren"}, 0)
            )
            self.assertTrue(committed)
            self.assertNotIn("character_id", relay.inspect_history()[0].content)
            self.assertEqual(relay.inspect_characters(), [])


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.directory / "relay.sqlite3"

    def test_version_6_games_are_upgraded_with_everything_kept(self):
        token = new_token()
        with GameStore.create(self.path, CATALOG.resolve("relay")) as store:
            alice = store.add_participant("Alice", token)
            store.execute_command(
                store.authenticate(token),
                command("e-1", "relay.enlist", {"name": "Wren"}, 0),
            )
            state, history = store.inspect_state(), store.inspect_history()
        as_schema_6(self.path)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "INSERT INTO referee_choices (participant_id, question, asked_in_job, "
                "asked_at) VALUES (?, 'Try now?', 'j-1', 'now')",
                (alice,),
            )
        with GameStore.open_existing(self.path) as store:
            self.assertEqual(store.query("PRAGMA user_version"), [(SCHEMA_VERSION,)])
            self.assertEqual(
                (store.inspect_state(), store.inspect_history()), (state, history)
            )
            self.assertEqual(store.inspect_characters(), [])
            self.assertEqual(
                store.query("SELECT participant_id, character_id, question FROM referee_choices"),
                [(alice, None, "Try now?")],  # now the participant's setup scope
            )  # fmt: skip
            self.assertIsNotNone(store.authenticate(token))
            with self.assertRaises(sqlite3.IntegrityError):
                with store.transaction() as db:  # still one choice per scope
                    db.execute(
                        "INSERT INTO referee_choices (participant_id, question, "
                        "asked_in_job, asked_at) VALUES (?, 'Again?', 'j-2', 'now')",
                        (alice,),
                    )

    def test_a_failed_upgrade_leaves_version_6_untouched(self):
        with GameStore.create(self.path, CATALOG.resolve("relay")):
            pass
        as_schema_6(self.path)
        broken = {
            **store_module._MIGRATIONS,
            7: [*store_module._MIGRATIONS[7], "INSERT INTO nowhere VALUES (1)"],
        }
        with patch.object(store_module, "_MIGRATIONS", broken):
            with self.assertRaises(sqlite3.Error):
                GameStore.open_existing(self.path)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone(), (6,))
            tables = {
                name for (name,) in connection.execute("SELECT name FROM sqlite_schema")
            }
        self.assertNotIn("characters", tables)


class CatalogTests(unittest.TestCase):
    def catalog(self, **changes):
        rules = changes.pop("rules", {})
        module = replace(
            TRAILHEAD_MODULE,
            characters=replace(TRAILHEAD_MODULE.characters, **rules),
            **changes,
        )
        return Catalog([module], [DICE_PROVIDER])

    def test_the_catalog_refuses_ambiguous_character_modules(self):
        def noop(action):
            return Outcome(action.state, {})

        push = Command("trailhead.push", lambda args: None, noop)
        reserved = Command("character.rename", lambda args: None, noop)
        cases = {
            "a step named like a command": {"rules": {"steps": (push,)}},
            "a reserved name": {"commands": (reserved,)},
            "two seed validators": {"validate_seed": lambda state, ids: None},
        }
        for name, changes in cases.items():
            with self.subTest(name), self.assertRaises(ModuleError):
                self.catalog(**changes)

    def test_an_unknown_rules_version_is_refused_when_resolved(self):
        catalog = self.catalog(rules={"version": 2})
        with self.assertRaisesRegex(ModuleError, "character rules version 2"):
            catalog.resolve("trailhead")


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def seed(self, **changes) -> dict:
        walker = {"trait": "steady", "rolled": 3, "stamina": 3, "max_stamina": 3,
                  "ready": True, "in_danger": False}  # fmt: skip
        data = {
            "seed_version": 2,
            "module": {"id": "trailhead", "version": "1", "state_version": 1,
                       "providers": [{"operation": "dice.roll.v1",
                                      "provider": "builtin.dice", "version": "1"}]},
            "participants": [{"participant_id": ALICE_ID, "name": "Alice"},
                             {"participant_id": BOB_ID, "name": "Bob"}],
            "characters": [{"character_id": MARA, "participant_id": ALICE_ID, "name": "Mara"}],
            "state": {"walkers": {MARA: walker}},
            "provenance": {"source": "test"},
        }  # fmt: skip
        data.update(changes)
        return data

    def create(self, data: dict, module="trailhead"):
        path = self.directory / f"{len(list(self.directory.iterdir()))}.sqlite3"
        seed = parse_seed(json.dumps(data))
        return GameStore.create(path, CATALOG.resolve(module), seed=seed), path

    def test_a_version_2_seed_declares_ready_characters_and_their_owners(self):
        store, _ = self.create(self.seed())
        with store:
            [mara] = store.inspect_characters()
            self.assertEqual(
                (mara.character_id, mara.participant_id, mara.name, mara.phase),
                (MARA, ALICE_ID, "Mara", "ready"),
            )
            self.assertIsNotNone(mara.introduced_at)  # already in the scene
            self.assertEqual(store.query("SELECT seed_version FROM seed"), [(2,)])
            self.assertEqual(store.revision(), 0)
            token = new_token()
            store.rotate_credential(ALICE_ID, token)
            alice = store.authenticate(token)
            reply, committed = store.execute_command(
                alice,
                command("p-1", "trailhead.push", {}, 0),
                actor=Actor(MARA, "play"),
            )
            self.assertTrue(committed, reply)

    def test_a_keen_walker_is_seeded_with_its_bonus(self):
        walker = {"trait": "keen", "rolled": 3, "stamina": 2, "max_stamina": 4,
                  "ready": True, "in_danger": False}  # fmt: skip
        store, _ = self.create(self.seed(state={"walkers": {MARA: walker}}))
        store.close()

    def test_a_version_1_seed_for_a_character_module_has_no_characters(self):
        data = self.seed(seed_version=1, state={"walkers": {}})
        del data["characters"]
        store, _ = self.create(data)
        with store:
            self.assertEqual(store.inspect_characters(), [])

    def test_bad_character_declarations_are_refused_before_any_file_exists(self):
        walker = self.seed()["state"]["walkers"][MARA]
        other = "c_fedcba9876543210"
        cases = {
            "repeated ID": (self.seed(characters=[
                {"character_id": MARA, "participant_id": ALICE_ID, "name": "Mara"},
                {"character_id": MARA, "participant_id": BOB_ID, "name": "Mara"}]),
                "repeats an earlier ID"),
            "orphaned owner": (self.seed(characters=[
                {"character_id": MARA, "participant_id": "p_cccccccccccccccc", "name": "Mara"}]),
                "owner is not a seeded participant"),
            "bad ID": (self.seed(characters=[
                {"character_id": "c_XYZ", "participant_id": ALICE_ID, "name": "Mara"}]),
                "not c_ followed"),
            "a draft": (self.seed(characters=[
                {"character_id": MARA, "participant_id": ALICE_ID, "name": "Mara", "phase": "draft"}]),
                "unexpected phase"),
            "undeclared walker": (self.seed(state={"walkers": {MARA: walker, other: walker}}),
                                  "exactly its declared characters"),
            "missing walker": (self.seed(state={"walkers": {}}), "exactly its declared characters"),
            "owner of the wrong type": (self.seed(characters=[
                {"character_id": MARA, "participant_id": [], "name": "Mara"}]),
                "owner is not a seeded participant"),
            "impossible stamina": (self.seed(state={"walkers": {MARA: {
                **walker, "stamina": 999, "max_stamina": 999}}}), "not a valid ready walker"),
            "a keen bonus without keen": (self.seed(state={"walkers": {MARA: {
                **walker, "stamina": 4, "max_stamina": 4}}}), "not a valid ready walker"),
            "missing characters": (
                {k: v for k, v in self.seed().items() if k != "characters"}, "missing characters"),
        }  # fmt: skip
        for name, (data, message) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(SeedError, message):
                self.create(data)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_a_module_without_characters_refuses_version_2(self):
        data = self.seed(
            module={"id": "relay", "version": "1", "state_version": 1,
                    "providers": [{"operation": "dice.roll.v1",
                                   "provider": "builtin.dice", "version": "1"}]},
            characters=[],
        )  # fmt: skip
        with self.assertRaisesRegex(SeedError, "has no characters"):
            self.create(data, module="relay")


class TrailheadTests(CharacterCase):
    def test_the_referee_context_and_views_leave_drafts_out(self):
        ash = self.ready()
        draft = self.draft(name="Wren")
        context = TRAILHEAD.referee_context(self.store.inspect_state())
        self.assertEqual(set(context["walkers"]), {ash})
        self.assertNotIn(draft, json.dumps(self.store.participant_view(self.bob).view))
        record, sheet = self.store.character_sheet(self.alice, draft)
        self.assertEqual(
            (record.phase, sheet["stamina"], sheet["trait"]), ("draft", 4, "keen")
        )

    def test_push_and_recover_follow_the_rules(self):
        ash = self.ready(trait="steady")
        playing = Actor(ash, "play")
        self.draws.return_value = 2  # 2 + 0 against 4: in danger
        reply, _ = self.run_command(self.alice, "p-1", "trailhead.push", {}, playing)
        self.assertEqual((reply["result"]["in_danger"], reply["result"]["stamina"]),
                         (True, {"before": 4, "after": 3}))  # fmt: skip
        reply, _ = self.run_command(self.alice, "p-2", "trailhead.push", {}, playing)
        self.assertEqual(reply["code"], "action_rejected")
        reply, _ = self.run_command(self.alice, "v-1", "trailhead.recover", {}, playing)
        self.assertEqual(reply["result"]["check"]["total"], 3)  # 2 + 1 steady
        self.assertFalse(reply["result"]["in_danger"])

    def test_described_actions_follow_the_walker(self):
        ash = self.draft(trait=None, roll=False)
        steps = [
            a["command"]
            for a in TRAILHEAD.creation_actions(self.store.inspect_state(), ash)
        ]
        self.assertEqual(
            steps, ["character.name", "trailhead.trait", "trailhead.stamina"]
        )
        wren = self.ready(name="Wren")
        actions = TRAILHEAD.character_actions(self.store.inspect_state(), wren)
        self.assertEqual([a["command"] for a in actions], ["trailhead.push"])

    def test_a_raising_start_creates_nothing(self):
        def refuse(action):
            raise ActionRejected("The trail is closed.")

        module = replace(
            TRAILHEAD_MODULE,
            characters=replace(TRAILHEAD_MODULE.characters, start=refuse),
        )
        runtime = Catalog([module], [DICE_PROVIDER]).resolve("trailhead")
        self.store.close()
        self.store = self.enterContext(GameStore(self.path, runtime=runtime))
        before = self.fingerprint()
        reply, _ = self.store.create_character(self.alice, "c-1", None)
        self.assertEqual(
            (reply["code"], reply["message"]),
            ("action_rejected", "The trail is closed."),
        )
        self.assertEqual(self.fingerprint(), before)


if __name__ == "__main__":
    unittest.main()
