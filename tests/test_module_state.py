import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tesserae import store as store_module
from tesserae.builtin import DICE_PROVIDER, DICE_RUNTIME
from tesserae.credentials import new_token
from tesserae.modules import (
    ActionRejected,
    ArgumentError,
    Catalog,
    Command,
    Module,
    Outcome,
    Provider,
    ProviderChoice,
)
from tesserae.store import (
    SCHEMA_VERSION,
    GameStore,
    ModuleBinding,
    ParticipantContext,
    StoreError,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DICE_CHOICE = ProviderChoice("dice.roll.v1", "builtin.dice", "1")
LIMIT = 2


def validate_bump(args):
    if set(args) != {"note"} or not isinstance(args["note"], str):
        raise ArgumentError("Bump args need exactly a string 'note'.")


def bump(action):
    """Each participant may bump twice; a 6 finishes the shared tally."""
    state = action.state
    count = state["tallies"].get(action.participant_id, 0)
    if state["done"] or count >= LIMIT:
        raise ActionRejected("No bumps left.")
    roll = action.operations["dice.roll.v1"]("1d6")["total"]
    state["tallies"][action.participant_id] = count + 1
    state["done"] = roll == 6
    return Outcome(state, {"roll": roll, "mine": count + 1})


def tally(**overrides):
    fields = dict(
        id="tally", version="1", state_version=1,
        commands=(Command("tally.bump", validate_bump, bump),),
        initial_state=lambda: {"tallies": {}, "done": False, "secret": "referee only"},
        view=lambda state, pid: {"mine": state["tallies"].get(pid, 0), "done": state["done"]},
        requires=frozenset({"dice.roll.v1"}), providers=(DICE_CHOICE,),
    )  # fmt: skip
    return Module(**{**fields, **overrides})


def resolve(module=None, provider=DICE_PROVIDER, choice=DICE_CHOICE):
    module = module or tally()
    return Catalog([module], [provider]).resolve(module.id, providers=[choice])


TALLY = resolve()


def bump_request(request_id, expected_revision, note="go"):
    return json.dumps(
        {"version": 1, "type": "command", "request_id": request_id,
         "expected_revision": expected_revision, "command": "tally.bump",
         "args": {"note": note}}
    )  # fmt: skip


class StoreCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "game.sqlite3"

    def keep(self, store):
        self.addCleanup(store.close)
        return store

    def open(self, **options):
        return self.keep(GameStore(self.path, **options))

    def raw(self, query):
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute(query).fetchall()
        finally:
            connection.close()


class ModuleGameTests(StoreCase):
    def setUp(self):
        super().setUp()
        store = self.keep(GameStore.create(self.path, TALLY))
        self.contexts = {}
        for name in ("alice", "bob"):
            token = new_token()
            store.add_participant(name.title(), token)
            self.contexts[name] = store.authenticate(token)
        self.alice, self.bob = self.contexts["alice"], self.contexts["bob"]

    def test_create_binds_initial_state_and_reopens(self):
        store = self.open(runtime=TALLY)
        self.assertEqual(store.revision(), 0)
        self.assertEqual(
            store.binding(), ModuleBinding("tally", "1", 1, (DICE_CHOICE,))
        )
        self.assertEqual(store.inspect_state(), TALLY.initial_state())
        self.assertEqual(self.raw("PRAGMA user_version"), [(SCHEMA_VERSION,)])

        other_path = self.directory / "other.sqlite3"
        other = self.keep(
            GameStore.create(other_path, TALLY, randint=Mock(return_value=1))
        )
        other.execute(ParticipantContext("dev"), bump_request("r1", 0))
        self.assertNotEqual(other.game_id, store.game_id)
        self.assertEqual(store.inspect_state()["tallies"], {})  # games are independent

        for existing in (self.path, other_path):
            before = existing.read_bytes()
            with self.assertRaisesRegex(StoreError, "existing"):
                GameStore.create(existing, TALLY)
            self.assertEqual(existing.read_bytes(), before)

    def test_fresh_actions_commit_state_result_and_revision_together(self):
        store = self.open(runtime=TALLY, randint=Mock(return_value=3))
        reply = store.execute(self.alice, bump_request("r1", 0))
        self.assertEqual((reply["type"], reply["revision"]), ("result", 1))
        self.assertEqual(reply["result"], {"roll": 3, "mine": 1})

        reopened = self.open(runtime=TALLY)
        self.assertEqual(reopened.revision(), 1)
        self.assertEqual(
            reopened.inspect_state()["tallies"], {self.alice.participant_id: 1}
        )
        [record] = reopened.inspect_history()
        self.assertEqual(record.to_message(), reply)

    def test_guards_run_in_order_and_rejections_consume_nothing(self):
        store = self.open(runtime=TALLY, randint=Mock(return_value=2))
        first = store.execute(self.alice, bump_request("r1", 0))
        store.execute(self.alice, bump_request("r2", 1))

        draw = Mock()
        store = self.open(runtime=TALLY, randint=draw)
        state = store.inspect_state()
        rejected = store.execute(self.alice, bump_request("r3", 2))
        self.assertEqual(
            {k: rejected[k] for k in ("type", "code", "request_id", "revision")},
            {
                "type": "error",
                "code": "action_rejected",
                "request_id": "r3",
                "revision": 2,
            },
        )
        self.assertIsNone(store.lookup(self.alice, "r3"))  # the ID is still unused

        # A retry still replays after a new bump has become illegal.
        self.assertEqual(store.execute(self.alice, bump_request("r1", 0)), first)
        cases = [
            (self.alice, bump_request("r1", 2, note="other"), "request_conflict"),
            (self.alice, bump_request("r9", 0), "stale_revision"),
            (self.bob, bump_request("r1", 0), "stale_revision"),  # not Alice's r1
        ]
        for context, raw, code in cases:
            with self.subTest(code):
                self.assertEqual(store.execute(context, raw)["code"], code)

        store.revoke_credential(self.bob.participant_id)
        self.assertEqual(
            store.execute(self.bob, bump_request("r1", 2))["code"], "session_revoked"
        )
        draw.assert_not_called()
        self.assertEqual((store.revision(), store.inspect_state()), (2, state))
        self.assertEqual(len(store.inspect_history()), 2)

    def test_competing_fresh_changes_cannot_both_commit(self):
        first = self.open(runtime=TALLY, randint=Mock(return_value=1))
        second = self.open(runtime=TALLY, randint=Mock(return_value=1))
        self.assertEqual(
            first.execute(self.alice, bump_request("a1", 0))["type"], "result"
        )
        self.assertEqual(
            second.execute(self.bob, bump_request("b1", 0))["code"], "stale_revision"
        )
        self.assertEqual(
            second.inspect_state()["tallies"], {self.alice.participant_id: 1}
        )

    def test_failure_after_state_write_rolls_back_everything(self):
        store = self.open(runtime=TALLY, randint=Mock(return_value=1))
        writes = []
        original = GameStore._write_state
        with (
            patch.object(GameStore, "_write_state", lambda s, state: writes.append(state) or original(s, state)),
            patch.object(GameStore, "_insert_record", side_effect=RuntimeError("disk gone")),
        ):  # fmt: skip
            with self.assertRaises(RuntimeError):
                store.execute(self.alice, bump_request("r1", 0))
        self.assertEqual(len(writes), 1)  # the state was written, then undone
        fresh = self.open(runtime=TALLY)
        self.assertEqual(
            (fresh.revision(), fresh.inspect_history(), fresh.inspect_state()),
            (0, [], TALLY.initial_state()),
        )
        self.assertEqual(
            store.execute(self.alice, bump_request("r1", 0))["revision"], 1
        )

    def test_only_the_matching_runtime_runs_the_game(self):
        loaded = Provider(
            "fixture.loaded", "1", "dice.roll.v1", lambda randint, e: {"total": 6}
        )
        mismatches = [
            ("another module", DICE_RUNTIME),
            ("module version", resolve(tally(version="2"))),
            ("state version", resolve(tally(state_version=2))),
            ("provider", resolve(provider=loaded, choice=ProviderChoice("dice.roll.v1", "fixture.loaded", "1"))),
        ]  # fmt: skip
        for description, runtime in mismatches:
            with self.subTest(description):
                with self.assertRaisesRegex(
                    StoreError, "This game uses module tally 1"
                ):
                    GameStore(self.path, runtime=runtime)

        # Without a runtime, the bundled catalog has no `tally`: administration
        # and operator reads work, play and views do not, and nothing is drawn.
        draw = Mock()
        store = self.open(randint=draw)
        bo = store.add_participant("Bo", new_token())
        self.assertIn(bo, [record.participant_id for record in store.participants()])
        self.assertEqual(store.binding().module_id, "tally")
        for attempt in (
            lambda: store.execute(self.alice, bump_request("r1", 0)),
            lambda: store.participant_view(self.alice),
        ):
            with self.assertRaisesRegex(
                StoreError, "module 'tally', which is not bundled"
            ):
                attempt()
        draw.assert_not_called()
        self.assertEqual((store.revision(), store.inspect_history()), (0, []))

    def test_views_are_scoped_and_read_with_their_revision(self):
        store = self.open(runtime=TALLY, randint=Mock(return_value=6))
        store.execute(self.alice, bump_request("r1", 0))
        alice, bob = (
            store.participant_view(self.alice),
            store.participant_view(self.bob),
        )
        self.assertEqual((alice.revision, alice.view), (1, {"mine": 1, "done": True}))
        self.assertEqual((bob.revision, bob.view), (1, {"mine": 0, "done": True}))
        self.assertNotIn("referee", json.dumps(bob.view))

        store.revoke_credential(self.bob.participant_id)
        self.assertIsNone(store.participant_view(self.bob))
        self.assertEqual(store.inspect_state()["secret"], "referee only")  # unchanged


class LegacyGameTests(StoreCase):
    def make_version_2(self):
        """A schema-2 store, as the previous release left it: a dice game with
        a participant and some history, but no module table."""
        token = new_token()
        with GameStore(self.path, randint=Mock(return_value=4)) as store:
            participant = store.add_participant("Alice", token)
            context = store.authenticate(token)
            store.execute(context, dice_request("r1", 0))
            snapshot = (store.game_id, store.inspect_history())
        connection = sqlite3.connect(self.path)
        connection.executescript(
            "DROP TABLE seed; DROP TABLE module; PRAGMA user_version = 2;"
        )
        connection.close()
        return token, participant, snapshot

    def test_new_and_older_stores_are_dice_games(self):
        dice = ModuleBinding("dice", "1", 1, (DICE_CHOICE,))
        token, participant, (game_id, history) = self.make_version_2()
        draw = Mock()
        store = self.open(randint=draw)
        self.assertEqual(self.raw("PRAGMA user_version"), [(SCHEMA_VERSION,)])
        self.assertEqual((store.binding(), store.inspect_state()), (dice, {}))
        self.assertEqual(
            (store.game_id, store.revision(), store.inspect_history()),
            (game_id, 1, history),
        )
        context = store.authenticate(token)
        self.assertEqual(context.participant_id, participant)
        self.assertEqual(
            store.execute(context, dice_request("r1", 0)), history[0].to_message()
        )
        draw.assert_not_called()
        view = store.participant_view(context)
        self.assertEqual((view.revision, view.view), (1, None))  # metadata only

        version_1 = self.directory / "v1.sqlite3"
        shutil.copyfile(FIXTURES / "store_v1.sqlite3", version_1)
        with GameStore(version_1) as store:
            self.assertEqual((store.binding(), store.revision()), (dice, 2))
        with GameStore(self.directory / "new.sqlite3") as store:  # create-on-open
            self.assertEqual(store.binding(), dice)

    def test_failed_migration_leaves_version_2_untouched(self):
        self.make_version_2()
        broken = {3: [*store_module._MIGRATIONS[3], "INSERT INTO nowhere VALUES (1)"]}
        with patch.object(store_module, "_MIGRATIONS", broken):
            with self.assertRaises(sqlite3.Error):
                GameStore(self.path)
        self.assertEqual(self.raw("PRAGMA user_version"), [(2,)])
        tables = {name for (name,) in self.raw("SELECT name FROM sqlite_schema")}
        self.assertNotIn("module", tables)
        self.assertEqual(self.raw("SELECT revision FROM game"), [(1,)])


class GameCreateCommandTests(StoreCase):
    def tesserae(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "tesserae", *args],
            capture_output=True, text=True, timeout=30,
        )  # fmt: skip

    def test_create_then_provision(self):
        created = self.tesserae(
            "game", "create", "--database", str(self.path), "--module", "dice"
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        with GameStore(self.path) as store:
            self.assertIn(store.game_id, created.stdout)
            self.assertEqual((store.binding().module_id, store.revision()), ("dice", 0))
        added = self.tesserae(
            "participant", "add", "--database", str(self.path), "--name", "Alice",
            "--token-file", str(self.directory / "alice.token"),
        )  # fmt: skip
        self.assertEqual(added.returncode, 0, added.stderr)

        before = self.path.read_bytes()
        again = self.tesserae(
            "game", "create", "--database", str(self.path), "--module", "dice"
        )
        self.assertEqual(again.returncode, 1)
        self.assertIn("existing", again.stderr)
        self.assertEqual(self.path.read_bytes(), before)

        missing = self.directory / "chess.sqlite3"
        unknown = self.tesserae(
            "game", "create", "--database", str(missing), "--module", "chess"
        )
        self.assertEqual(unknown.returncode, 1)
        self.assertIn("No bundled module 'chess'", unknown.stderr)
        self.assertFalse(missing.exists())
        for process in (created, added, again, unknown):
            self.assertNotIn("Traceback", process.stderr)


def dice_request(request_id, expected_revision):
    return json.dumps(
        {"version": 1, "type": "command", "request_id": request_id,
         "expected_revision": expected_revision, "command": "roll",
         "args": {"expression": "1d6"}}
    )  # fmt: skip
