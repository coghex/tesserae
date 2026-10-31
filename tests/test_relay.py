import json
import unittest

from tesserae.builtin import CATALOG, DICE_PROVIDER
from tesserae.modules import (
    ActionRejected,
    ArgumentError,
    Catalog,
    Provider,
    ProviderChoice,
)
from tesserae.protocol import ProtocolError, parse_request
from tesserae.relay import RELAY_MODULE

RELAY = CATALOG.resolve("relay")
NOTE = RELAY.initial_state()["referee"]["maintenance_note"]


def dice(*values):
    """A randint that returns `values` in order and records every draw."""
    draws, remaining = [], list(values)

    def randint(low, high):
        draws.append((low, high))
        return remaining.pop(0)

    return randint, draws


class RelayGame:
    """Module state threaded through successive actions, as a store would."""

    def __init__(self):
        self.state = RELAY.initial_state()

    def act(self, participant_id, command, args=None, *, rolls=()):
        randint, self.draws = dice(*rolls)
        outcome = RELAY.execute(
            self.state, participant_id, command, args or {}, randint=randint
        )
        self.state = outcome.state
        return outcome.result


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.game = RelayGame()

    def rejected(self, participant_id, command, args=None):
        """Assert the action is refused with no draw and no state change."""
        before = json.dumps(self.game.state)
        with self.assertRaises(ActionRejected) as caught:
            self.game.act(participant_id, command, args)
        self.assertEqual(self.game.draws, [])
        self.assertEqual(json.dumps(self.game.state), before)
        return caught.exception.message

    def test_initial_state_is_offline_empty_and_fresh_per_game(self):
        first, second = RELAY.initial_state(), RELAY.initial_state()
        self.assertEqual((first["relay"], first["workers"]), ({"restored": False}, {}))
        first["workers"]["p_a"] = {"name": "Ada"}
        first["relay"]["restored"] = True
        self.assertEqual(
            (second["relay"], second["workers"]), ({"restored": False}, {})
        )

    def test_enlist_creates_only_the_callers_worker_once(self):
        result = self.game.act("p_a", "relay.enlist", {"name": "Ada Lovelace"})
        self.assertEqual(self.game.draws, [])
        worker = {"name": "Ada Lovelace", "repair": 1, "effort": 3}
        self.assertEqual(result, {"worker": worker, "relay": {"restored": False}})
        self.assertEqual(self.game.state["workers"], {"p_a": worker})

        self.game.act("p_a", "relay.repair", rolls=[1])
        message = self.rejected("p_a", "relay.enlist", {"name": "Ada again"})
        self.assertIn("already enlisted", message)
        self.assertEqual(self.game.state["workers"]["p_a"]["effort"], 2)  # not reset

    def test_repair_success_and_failure_spend_effort(self):
        self.game.act("p_a", "relay.enlist", {"name": "Ada"})
        failure = self.game.act("p_a", "relay.repair", rolls=[3])
        self.assertEqual(self.game.draws, [(1, 6)])
        self.assertEqual(failure["check"]["roll"]["terms"][0]["rolls"], [3])
        self.assertEqual(
            {k: v for k, v in failure["check"].items() if k != "roll"},
            {"repair": 1, "total": 4, "target": 5, "success": False},
        )
        self.assertEqual(failure["effort"], {"before": 3, "after": 2})
        self.assertEqual(failure["relay"], {"restored": False})

        success = self.game.act("p_a", "relay.repair", rolls=[4])
        self.assertEqual(
            (success["check"]["total"], success["check"]["success"]), (5, True)
        )
        self.assertEqual(success["effort"], {"before": 2, "after": 1})
        self.assertEqual(success["relay"], {"restored": True})
        self.assertEqual(self.game.state["relay"], {"restored": True})
        for result in (failure, success):
            self.assertNotIn("workers", json.dumps(result))
            self.assertNotIn(NOTE, json.dumps(result))

    def test_ineligible_repairs_draw_nothing(self):
        self.assertIn("Enlist", self.rejected("p_a", "relay.repair"))

        self.game.act("p_a", "relay.enlist", {"name": "Ada"})
        for _ in range(3):
            self.game.act("p_a", "relay.repair", rolls=[1])
        self.assertIn("no effort", self.rejected("p_a", "relay.repair"))
        self.assertEqual(self.game.state["relay"], {"restored": False})  # no reset

        self.game.act("p_b", "relay.enlist", {"name": "Bo"})
        self.game.act("p_b", "relay.repair", rolls=[6])
        self.assertIn("already restored", self.rejected("p_b", "relay.repair"))
        self.assertEqual(self.game.state["workers"]["p_b"]["effort"], 2)

    def test_arguments_are_exact_and_never_client_chosen(self):
        cases = [
            ("relay.enlist", {}),
            ("relay.enlist", {"name": "Ada", "repair": 5}),
            ("relay.enlist", {"name": "Ada", "owner": "p_b"}),
            ("relay.enlist", {"name": True}),
            ("relay.enlist", {"name": ""}),
            ("relay.enlist", {"name": "   "}),
            ("relay.enlist", {"name": "x" * 41}),
            ("relay.enlist", {"name": "Ada\nBo"}),
            ("relay.repair", {"difficulty": 1}),
            ("relay.repair", {"expression": "1d20"}),
            ("relay.repair", {"success": True}),
        ]
        for name, args in cases:
            with self.subTest(command=name, args=args):
                with self.assertRaises(ArgumentError) as caught:
                    RELAY.command(name).validate(args)
                self.assertEqual(caught.exception.code, "invalid_request")
        RELAY.command("relay.enlist").validate({"name": "x" * 40})

        raw = json.dumps(
            {"version": 1, "type": "command", "request_id": "r1",
             "expected_revision": 0, "command": "roll", "args": {"expression": "1d20"}}
        )  # fmt: skip
        with self.assertRaises(ProtocolError) as caught:  # no raw roll action
            parse_request(raw, RELAY)
        self.assertEqual(caught.exception.code, "unsupported_command")

    def test_views_show_the_scene_and_only_the_callers_worker(self):
        self.assertEqual(RELAY.view(self.game.state, "p_a")["character"], None)
        self.game.act("p_a", "relay.enlist", {"name": "Ada"})
        self.game.act("p_b", "relay.enlist", {"name": "Bo"})
        self.game.act("p_b", "relay.repair", rolls=[2])

        views = {p: RELAY.view(self.game.state, p) for p in ("p_a", "p_b", "p_c")}
        self.assertEqual(
            views["p_a"]["character"], {"name": "Ada", "repair": 1, "effort": 3}
        )
        self.assertEqual(
            views["p_b"]["character"], {"name": "Bo", "repair": 1, "effort": 2}
        )
        self.assertEqual(
            [views[p]["commands"] for p in ("p_a", "p_b", "p_c")],
            [["relay.repair"], ["relay.repair"], ["relay.enlist"]],
        )
        for participant, other in [("p_a", "Bo"), ("p_b", "Ada"), ("p_c", "Ada")]:
            view = views[participant]
            self.assertEqual(set(view), {"scene", "rules", "character", "commands"})
            self.assertEqual(view["scene"]["restored"], False)
            text = json.dumps(view)
            for hidden in (f'"{other}"', NOTE, "p_a", "p_b", "workers", "referee"):
                self.assertNotIn(hidden, text)

    def test_repairs_use_the_selected_provider(self):
        loaded = Provider(
            "fixture.loaded", "1", "dice.roll.v1", lambda randint, e: {"total": 4}
        )
        catalog = Catalog([RELAY_MODULE], [DICE_PROVIDER, loaded])
        runtime = catalog.resolve(
            "relay", providers=[ProviderChoice("dice.roll.v1", "fixture.loaded", "1")]
        )
        state = runtime.execute(
            runtime.initial_state(), "p_a", "relay.enlist", {"name": "Ada"}
        ).state
        result = runtime.execute(state, "p_a", "relay.repair", {}).result
        self.assertEqual(
            (result["check"]["total"], result["relay"]), (5, {"restored": True})
        )
