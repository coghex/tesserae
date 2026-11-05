"""Host administration in a game's own store (GOV-4, #135): the campaign
host's operations on another player's character, NPCs and the scene, with
replay, stale revisions, the busy rule, audit atomicity and history kept;
discovery and target digests; and no player, referee or guest job ever
running one. The original Trailhead module, no network, no model.
docs/designs/governance_contract.md sections 6 and 8."""

import json
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch

from tesserae.builtin import CATALOG, DICE_PROVIDER
from tesserae.modules import ArgumentError, Catalog, Command, ModuleError, Outcome
from tesserae.referee.store import RefereeStore
from tesserae.referee.tools import BridgeTools
from tesserae.store import (
    OPERATOR_DECIDER,
    REFEREE_PARTICIPANT,
    Actor,
    Decider,
    GameStore,
    ParticipantContext,
)
from tesserae.trailhead import TRAILHEAD_MODULE

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_characters import CharacterCase, command, new_request
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_characters import CharacterCase, command, new_request

TRAILHEAD = CATALOG.resolve("trailhead")
HOST = Decider("u_0123456789abcdef", str(uuid.uuid4()))


def host_command(request_id, name, args, revision) -> str:
    """A host_command as the lobby hands it to the store: in command form."""
    return json.dumps({"version": 1, "type": "command", "request_id": request_id,
                       "expected_revision": revision, "command": name, "args": args})  # fmt: skip


class AdminCase(CharacterCase):
    def run_host(self, name, args, request_id=None, revision=None, by=HOST):
        revision = self.store.revision() if revision is None else revision
        return self.store.execute_host_command(
            by, host_command(request_id or new_request(), name, args, revision)
        )

    def audit(self) -> list:
        return self.store.host_audit(0)[0]

    def walker(self, character_id) -> dict:
        return self.store.inspect_state()["walkers"][character_id]


class OperationTests(AdminCase):
    def test_the_host_corrects_another_players_walker_without_rolling(self):
        wren = self.ready(name="Wren")  # Alice's: keen, rolled 4, so 5 stamina
        draws = self.draws.call_count
        reply, committed = self.run_host(
            "trailhead.set_stamina", {"character_id": wren, "stamina": 2}, "h-1"
        )
        self.assertTrue(committed, reply)
        self.assertEqual(reply["result"]["stamina"], {"before": 5, "after": 2})
        reply, _ = self.run_host(
            "trailhead.set_trait", {"character_id": wren, "trait": "steady"}
        )
        # Recomputed by the module's rule (keen's +1 gone), stamina kept.
        self.assertEqual(
            (reply["result"]["max_stamina"], reply["result"]["stamina"]), (4, 2)
        )
        self.assertEqual(self.draws.call_count, draws)  # nothing rolled
        self.assertEqual(self.record(wren).participant_id, self.alice.participant_id)
        last = self.store.inspect_history()[-1]
        self.assertEqual(last.participant_id, HOST.sender)  # administrative, not play
        [first, second] = self.audit()
        self.assertEqual(
            (first["kind"], first["player_id"], first["via"], first["target"]),
            ("operation", HOST.player_id, "client", wren),
        )
        self.assertEqual(
            first["detail"],
            {"command": "trailhead.set_stamina", "args": {"character_id": wren, "stamina": 2},
             "request_id": "h-1"},
        )  # fmt: skip
        self.assertEqual(second["revision"], self.store.revision())

    def test_unsupported_edits_are_refused_with_nothing_changed(self):
        wren, ash = self.ready(name="Wren"), self.draft()
        before = (self.fingerprint(), self.audit())
        for name, args, code in (
            ("trailhead.set_stamina", {"character_id": wren, "stamina": 9}, "action_rejected"),
            ("trailhead.set_stamina", {"character_id": ash, "stamina": 1}, "action_rejected"),
            ("trailhead.set_stamina", {"character_id": "c_ffffffffffffffff", "stamina": 1},
             "character_unavailable"),
            ("trailhead.set_stamina", {"character_id": wren}, "invalid_request"),
            ("trailhead.set_trait", {"character_id": wren, "trait": "lucky"}, "invalid_request"),
            ("trailhead.npc_set", {"npc_id": "Bad Id", "name": "X", "note": "", "visible": True},
             "invalid_request"),
            ("trailhead.push", {}, "unsupported_command"),  # gameplay is not administration
            ("trailhead.rewrite", {}, "unsupported_command"),
        ):  # fmt: skip
            with self.subTest(name=name, args=args):
                reply, committed = self.run_host(name, args)
                self.assertEqual((reply["code"], committed), (code, False))
        self.assertEqual((self.fingerprint(), self.audit()), before)

    def test_npcs_scene_and_preparation_reach_only_who_should_see_them(self):
        for name, args in (
            ("trailhead.npc_set", {"npc_id": "warden", "name": "The warden",
                                   "note": "Keeps the hut.", "visible": True}),
            ("trailhead.npc_set", {"npc_id": "thief", "name": "A thief",
                                   "note": "Follows the walkers.", "visible": False}),
            ("trailhead.scene_set", {"description": "Snow on the pass."}),
            ("trailhead.prep_set", {"note": "The thief strikes at the hut."}),
        ):  # fmt: skip
            self.assertTrue(self.run_host(name, args)[1])
        state = self.store.inspect_state()
        view = TRAILHEAD.view(state, self.alice.participant_id)
        self.assertEqual(view["scene"]["description"], "Snow on the pass.")
        self.assertEqual(list(view["npcs"]), ["warden"])
        self.assertNotIn("thief", json.dumps(view))
        context = TRAILHEAD.referee_context(state)
        self.assertEqual(
            (sorted(context["npcs"]), context["prep"]),
            (["thief", "warden"], "The thief strikes at the hut."),
        )
        self.assertEqual(TRAILHEAD.host_inspect(state)["prep"], context["prep"])
        # Updating one NPC changes no other, nor any walker.
        reply, _ = self.run_host("trailhead.npc_set", {"npc_id": "thief", "name": "A thief",
                                                       "note": "Gone.", "visible": True})  # fmt: skip
        self.assertEqual(reply["result"], {"npc_id": "thief", "added": False})
        self.assertEqual(
            self.store.inspect_state()["npcs"]["warden"]["note"], "Keeps the hut."
        )

    def test_a_drafts_correction_makes_a_shown_approval_stale(self):
        ash = self.draft()
        self.assertEqual(self.record(ash).draft_revision, 4)
        self.run_host(
            "trailhead.set_gear", {"character_id": ash, "gear": ["rope", "map"]}
        )
        self.assertEqual(self.record(ash).draft_revision, 5)
        stale, _ = self.store.complete_character(self.alice, new_request(), ash, 4)
        self.assertEqual(stale["code"], "stale_draft")
        done, committed = self.store.complete_character(
            self.alice, new_request(), ash, 5
        )
        self.assertTrue(committed, done)
        sheet = TRAILHEAD.character_sheet(self.store.inspect_state(), ash)
        self.assertEqual(sheet["gear"], ["rope", "map"])

    def test_history_and_other_entities_are_kept(self):
        wren, kit = self.ready(name="Wren"), self.ready(name="Kit")
        playing = Actor(wren, "play")
        pushed, _ = self.run_command(self.alice, "p-1", "trailhead.push", {}, playing)
        history = self.store.inspect_history()
        kit_before = self.walker(kit)
        self.run_host(
            "trailhead.describe", {"character_id": wren, "description": "Tall."}
        )
        self.assertEqual(self.store.inspect_history()[: len(history)], history)
        self.assertEqual(self.store.lookup(self.alice, "p-1").result, pushed["result"])
        self.assertEqual(self.walker(kit), kit_before)
        self.assertEqual(
            self.walker(wren)["stamina"], pushed["result"]["stamina"]["after"]
        )


class ReplayTests(AdminCase):
    def test_exact_replay_conflict_and_stale_revision(self):
        wren = self.ready(name="Wren")
        args = {"character_id": wren, "stamina": 3}
        revision = self.store.revision()
        first, committed = self.run_host("trailhead.set_stamina", args, "h-1", revision)
        self.assertTrue(committed)
        again, committed = self.run_host("trailhead.set_stamina", args, "h-1", revision)
        self.assertEqual((again, committed), (first, False))
        conflict, _ = self.run_host(
            "trailhead.set_stamina", {**args, "stamina": 1}, "h-1", revision
        )
        self.assertEqual(conflict["code"], "request_conflict")
        stale, _ = self.run_host("trailhead.set_stamina", args, "h-2", revision)
        self.assertEqual(
            (stale["code"], stale["revision"]), ("stale_revision", revision + 1)
        )
        # The host's namespace is its own: Alice may use the same request ID.
        playing = Actor(wren, "play")
        reply, committed = self.run_command(
            self.alice, "h-1", "trailhead.push", {}, playing
        )
        self.assertTrue(committed, reply)
        self.assertEqual(len(self.audit()), 1)

    def test_the_busy_rule_refuses_new_operations_but_not_replays(self):
        wren = self.ready(name="Wren")
        args = {"character_id": wren, "stamina": 3}
        revision = self.store.revision()
        done, _ = self.run_host("trailhead.set_stamina", args, "h-1", revision)
        referee = RefereeStore(self.store)
        referee.enable()
        referee.submit(self.alice, "s-1", "Hello", False, actor=Actor(wren, "play"))
        before = (self.fingerprint(), self.audit())
        busy, _ = self.run_host("trailhead.set_stamina", {**args, "stamina": 1})
        self.assertEqual((busy["code"], busy["job"]["for"]), ("referee_busy", "Alice"))
        self.assertEqual(
            self.run_host("trailhead.set_stamina", args, "h-1", revision)[0], done
        )
        self.assertEqual((self.fingerprint(), self.audit()), before)

    def test_an_operation_and_its_audit_commit_together(self):
        wren = self.ready(name="Wren")
        before = (self.fingerprint(), self.audit())
        with patch.object(GameStore, "audit", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                self.run_host(
                    "trailhead.set_stamina", {"character_id": wren, "stamina": 1}
                )
        self.assertEqual((self.fingerprint(), self.audit()), before)


class AuthorityTests(AdminCase):
    def test_no_player_referee_or_operator_context_runs_an_operation(self):
        wren = self.ready(name="Wren")
        args = {"character_id": wren, "stamina": 0}
        reply, _ = self.run_command(
            self.alice,
            new_request(),
            "trailhead.set_stamina",
            args,
            Actor(wren, "play"),
        )
        self.assertEqual(reply["code"], "unsupported_command")
        raw = command(
            new_request(), "trailhead.set_stamina", args, self.store.revision()
        )
        reply, _ = self.store.execute_command(
            ParticipantContext(REFEREE_PARTICIPANT), raw, referee=True
        )
        self.assertEqual(reply["code"], "unsupported_command")
        with self.assertRaises(ValueError):
            self.run_host("trailhead.set_stamina", args, by=OPERATOR_DECIDER)
        self.assertEqual(self.walker(wren)["stamina"], 5)

    def test_a_guest_job_cannot_borrow_host_operations(self):
        wren = self.ready(name="Wren")
        referee = RefereeStore(self.store)
        referee.enable()
        job = referee.submit(
            self.alice, "s-1", "Fix me", False, actor=Actor(wren, "play")
        ).job
        tools = BridgeTools(lambda: GameStore(self.path, runtime=TRAILHEAD))
        token = tools.grant(job.job_id)
        for tool in ("act", "referee_act"):
            with self.subTest(tool=tool):
                found = tools.handle({"token": token, "tool": tool, "args": {
                    "step_id": f"{tool}-1", "command": "trailhead.set_stamina",
                    "args": {"character_id": wren, "stamina": 0}}})  # fmt: skip
                self.assertFalse(found["ok"], found)
        self.assertEqual(self.walker(wren)["stamina"], 5)
        self.assertEqual(self.audit(), [])


class DiscoveryTests(AdminCase):
    def test_operations_follow_the_state_and_targets_have_digests(self):
        names = [
            o["command"] for o in TRAILHEAD.host_operations(self.store.inspect_state())
        ]
        self.assertEqual(
            names, ["trailhead.npc_set", "trailhead.scene_set", "trailhead.prep_set"]
        )
        wren = self.ready(name="Wren")
        described = TRAILHEAD.host_operations(self.store.inspect_state())
        stamina = next(o for o in described if o["command"] == "trailhead.set_stamina")
        self.assertEqual(stamina["target_kinds"], ["character"])
        self.assertEqual(
            [a["name"] for a in stamina["args"]], ["character_id", "stamina"]
        )

        target = {"kind": "character", "id": wren}
        digest = self.store.target_digest(target)
        self.assertEqual(self.store.target_digest(target), digest)
        self.run_host("trailhead.set_stamina", {"character_id": wren, "stamina": 1})
        self.assertNotEqual(self.store.target_digest(target), digest)
        scene = {"kind": "scene", "id": "trailhead"}
        digest = self.store.target_digest(scene)
        self.run_host("trailhead.prep_set", {"note": "Fog."})
        self.assertNotEqual(self.store.target_digest(scene), digest)
        for bad in ({"kind": "character", "id": "c_ffffffffffffffff"},
                    {"kind": "dragon", "id": "x"}, {"kind": "npc"}):  # fmt: skip
            with self.subTest(target=bad), self.assertRaises(ArgumentError):
                self.store.target_digest(bad)

    def test_modules_without_administration_have_no_operations(self):
        module = replace(TRAILHEAD_MODULE, administration=None)
        runtime = Catalog([module], [DICE_PROVIDER]).resolve("trailhead")
        with GameStore.create(self.directory / "plain.sqlite3", runtime) as plain:
            self.assertEqual(runtime.host_operations(plain.inspect_state()), [])
            self.assertIsNone(runtime.host_inspect(plain.inspect_state()))
            reply, committed = plain.execute_host_command(
                HOST, host_command("h-1", "trailhead.set_stamina", {}, 0)
            )
            self.assertEqual((reply["code"], committed), ("unsupported_command", False))
            # A character's digest is its sheet's, the only target there is.
            reply, _ = plain.create_character(self.store_context(plain), "c-1", "Ash")
            ash = reply["result"]["character"]["character_id"]
            sheet = runtime.character_sheet(plain.inspect_state(), ash)
            self.assertEqual(runtime.target_state(plain.inspect_state(),
                                                  {"kind": "character", "id": ash}), sheet)  # fmt: skip
            with self.assertRaises(ArgumentError):
                runtime.target_state(plain.inspect_state(), {"kind": "npc", "id": "x"})

    def store_context(self, store):
        from tesserae.credentials import new_token

        token = new_token()
        store.add_participant("Ann", token)
        return store.authenticate(token)

    def test_an_operation_gets_the_hosts_sender_a_policy_and_no_dice(self):
        seen = []

        def peek(action):
            seen.append((action.participant_id, dict(action.operations), action.policy))
            return Outcome(action.state, {})

        administration = replace(TRAILHEAD_MODULE.administration, operations=(
            Command("trailhead.peek", lambda args: None, peek),))  # fmt: skip
        module = replace(TRAILHEAD_MODULE, administration=administration)
        runtime = Catalog([module], [DICE_PROVIDER]).resolve("trailhead")
        with GameStore.create(self.directory / "peek.sqlite3", runtime) as store:
            reply, committed = store.execute_host_command(
                HOST, host_command("h-1", "trailhead.peek", {}, 0)
            )
        self.assertTrue(committed, reply)
        self.assertEqual(
            seen, [(HOST.sender, {}, {"revision": 0, "settings": {"starting_stamina_bonus": 0}})]
        )  # fmt: skip

    def test_the_catalog_checks_operation_names_and_versions(self):
        administration = TRAILHEAD_MODULE.administration
        clash = replace(administration, operations=(
            *administration.operations,
            Command("trailhead.push", lambda args: None, lambda a: Outcome(a.state, {})),
        ))  # fmt: skip
        with self.assertRaises(ModuleError):
            Catalog([replace(TRAILHEAD_MODULE, administration=clash)], [DICE_PROVIDER])
        later = replace(
            TRAILHEAD_MODULE, administration=replace(administration, version=2)
        )
        with self.assertRaisesRegex(ModuleError, "administration version 2"):
            Catalog([later], [DICE_PROVIDER]).resolve("trailhead")

    def test_seeds_may_carry_npcs_a_scene_and_preparation(self):
        walker = {"trait": "quick", "rolled": 3, "stamina": 3, "max_stamina": 3,
                  "ready": True, "in_danger": False, "gear": ["rope"]}  # fmt: skip
        owners = {"c_0123456789abcdef": "p_a1a1a1a1a1a1a1a1"}
        state = {"walkers": {"c_0123456789abcdef": walker},
                 "npcs": {"warden": {"name": "The warden", "note": "", "visible": True}},
                 "scene": {"description": "Fog."}, "prep": "A storm comes."}  # fmt: skip
        TRAILHEAD.validate_character_seed(state, frozenset(owners.values()), owners)
        for bad in ({"npcs": {"Bad": {"name": "x", "note": "", "visible": True}}},
                    {"scene": {"title": "x"}}, {"prep": 7}, {"world": {}}):  # fmt: skip
            with self.subTest(bad=bad), self.assertRaises(Exception):
                TRAILHEAD.validate_character_seed(
                    {**state, **bad}, frozenset(owners.values()), owners
                )


if __name__ == "__main__":
    unittest.main()
