import json
import unittest

from tesserae.builtin import CATALOG, DICE_PROVIDER, DICE_RUNTIME
from tesserae.dice import roll_expression
from tesserae.modules import (
    ActionRejected,
    ArgumentError,
    Catalog,
    Command,
    Module,
    ModuleError,
    Outcome,
    Provider,
    ProviderChoice,
    exact_fields,
)
from tesserae.protocol import (
    ProtocolError,
    error_message,
    parse_client_message,
    parse_request,
)

SHARED_START = {"total": 0, "log": []}  # returned as-is: the runtime must copy it


def command(name, args, request_id="r1"):
    return json.dumps(
        {"version": 1, "type": "command", "request_id": request_id,
         "expected_revision": 0, "command": name, "args": args}
    )  # fmt: skip


def validate_add(args):
    exact_fields(args, {"amount"}, "Add args")
    amount = args["amount"]
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 1:
        raise ArgumentError("'amount' must be a positive integer.")


def execute_add(action):
    state = action.state
    state["log"].append(action.participant_id)  # mutates only its private copy
    if state["total"] + action.args["amount"] > 10:
        raise ActionRejected("The counter cannot go past 10.")
    state["total"] += action.args["amount"]
    return Outcome(
        state, {"total": state["total"], "operations": len(action.operations)}
    )


# A module with no dice: tagged "dice", which must not give it a roll command
# or a dice provider.
COUNTER = Module(
    id="counter", version="1", state_version=1,
    commands=(Command("counter.add", validate_add, execute_add),),
    initial_state=lambda: SHARED_START,
    view=lambda state, participant_id: {"total": state["total"]},
    tags=frozenset({"dice"}),
)  # fmt: skip


def execute_draw(action):
    return Outcome(action.state, action.operations["dice.roll.v1"]("2d6"))


def needs_dice(**overrides):
    fields = dict(
        id="draws", version="1", state_version=1,
        commands=(Command("draws.go", lambda args: exact_fields(args, (), "Args"), execute_draw),),
        initial_state=dict, view=lambda state, participant_id: None,
        requires=frozenset({"dice.roll.v1"}),
        providers=(ProviderChoice("dice.roll.v1", "builtin.dice", "1"),),
    )  # fmt: skip
    return Module(**{**fields, **overrides})


LOADED = Provider(
    "fixture.loaded", "1", "dice.roll.v1", lambda randint, e: {"loaded": e}
)
OTHER_OPERATION = Provider("fixture.cards", "1", "cards.draw.v1", lambda randint: {})


def recording_randint(draws):
    def randint(low, high):
        draws.append((low, high))
        return high

    return randint


class CommandRoutingTests(unittest.TestCase):
    def setUp(self):
        self.counter = Catalog([COUNTER], []).resolve("counter")

    def assertRejected(self, raw, runtime, code):
        with self.assertRaises(ProtocolError) as caught:
            parse_request(raw, runtime)
        self.assertEqual(
            (caught.exception.code, caught.exception.request_id), (code, "r1")
        )
        return caught.exception

    def test_only_the_selected_modules_commands_are_accepted(self):
        request = parse_request(command("counter.add", {"amount": 2}), self.counter)
        self.assertEqual(
            (request.command, request.args), ("counter.add", {"amount": 2})
        )
        session = parse_client_message(
            command("counter.add", {"amount": 2}), self.counter
        )
        self.assertEqual(session, request)

        self.assertRejected(
            command("roll", {"expression": "1d20"}), self.counter, "unsupported_command"
        )
        with self.assertRaises(ProtocolError) as caught:  # the default is dice
            parse_request(command("counter.add", {"amount": 2}))
        self.assertEqual(caught.exception.code, "unsupported_command")

    def test_module_validators_reject_arguments_before_anything_runs(self):
        cases = [
            ("missing field", {}),
            ("extra field", {"amount": 1, "tess_secret": 1}),
            ("boolean amount", {"amount": True}),
            ("zero amount", {"amount": 0}),
        ]
        for description, args in cases:
            with self.subTest(description):
                error = self.assertRejected(
                    command("counter.add", args), self.counter, "invalid_request"
                )
                self.assertNotIn("tess_secret", error.message)

    def test_structural_argument_errors_precede_the_version_check(self):
        dice_error = json.loads(command("roll", {"expression": "2d6*3"}))
        counter_error = json.loads(command("counter.add", {"amount": 0}))
        for data, runtime, code in [
            ({**dice_error, "version": 2}, DICE_RUNTIME, "unsupported_version"),
            ({**counter_error, "version": 2}, self.counter, "invalid_request"),
        ]:
            with self.subTest(code):
                self.assertRejected(json.dumps(data), runtime, code)

    def test_tags_activate_nothing(self):
        self.assertEqual(
            (self.counter.providers, self.counter.command_names), ({}, ("counter.add",))
        )
        outcome = self.counter.execute(
            {"total": 0, "log": []}, "p_a", "counter.add", {"amount": 1}
        )
        self.assertEqual(outcome.result["operations"], 0)


class ProviderSelectionTests(unittest.TestCase):
    def test_bad_catalogs_and_selections_fail_clearly(self):
        dice = ProviderChoice("dice.roll.v1", "builtin.dice", "1")
        loaded = ProviderChoice("dice.roll.v1", "fixture.loaded", "1")
        providers = [DICE_PROVIDER, LOADED, OTHER_OPERATION]
        cases = [
            ("unknown module", lambda: CATALOG.resolve("chess"), "No bundled module"),
            ("module version", lambda: CATALOG.resolve("dice", version="2"), "version 1, not 2"),
            ("host API", lambda: Catalog([needs_dice(api_version=2)], providers).resolve("draws"),
             "host API 2"),
            ("no choice, one candidate", lambda: Catalog(
                [needs_dice(providers=())], [DICE_PROVIDER]).resolve("draws"), "no provider selected"),
            ("unknown provider", lambda: Catalog([needs_dice()], [LOADED]).resolve("draws"),
             "No provider 'builtin.dice'"),
            ("provider version", lambda: Catalog([needs_dice()], providers).resolve(
                "draws", providers=[ProviderChoice("dice.roll.v1", "builtin.dice", "2")]),
             "version 1, not 2"),
            ("wrong operation", lambda: Catalog([needs_dice()], providers).resolve(
                "draws", providers=[ProviderChoice("dice.roll.v1", "fixture.cards", "1")]),
             "supplies 'cards.draw.v1'"),
            ("competing choices", lambda: Catalog([needs_dice()], providers).resolve(
                "draws", providers=[dice, loaded]), "More than one provider"),
            ("unused operation", lambda: Catalog([COUNTER], providers).resolve(
                "counter", providers=[dice]), "does not use"),
            ("duplicate module", lambda: Catalog([COUNTER, COUNTER], []), "Duplicate module"),
            ("duplicate provider", lambda: Catalog([], [LOADED, LOADED]), "Duplicate provider"),
            ("duplicate command", lambda: Catalog(
                [needs_dice(commands=COUNTER.commands * 2)], providers), "Duplicate draws command"),
        ]  # fmt: skip
        for description, attempt, diagnostic in cases:
            with self.subTest(description):
                with self.assertRaises(ModuleError) as caught:
                    attempt()
                self.assertIn(diagnostic, str(caught.exception))

    def test_the_selection_not_registration_order_decides(self):
        for first, second in [(DICE_PROVIDER, LOADED), (LOADED, DICE_PROVIDER)]:
            catalog = Catalog([needs_dice()], [first, second])
            for choice, loaded in [
                (ProviderChoice("dice.roll.v1", "builtin.dice", "1"), False),
                (ProviderChoice("dice.roll.v1", "fixture.loaded", "1"), True),
            ]:
                runtime = catalog.resolve("draws", providers=[choice])
                self.assertEqual(runtime.selection, (choice,))
                result = runtime.execute(
                    {}, "p_a", "draws.go", {}, randint=lambda a, b: b
                ).result
                self.assertEqual("loaded" in result, loaded)


class ExecutionTests(unittest.TestCase):
    def test_initial_and_execution_state_are_never_shared(self):
        runtime = Catalog([COUNTER], []).resolve("counter")
        first, second = runtime.initial_state(), runtime.initial_state()
        first["log"].append("changed")
        self.assertEqual((second, SHARED_START), ({"total": 0, "log": []},) * 2)

        state = {"total": 9, "log": []}
        with self.assertRaises(ActionRejected) as caught:
            runtime.execute(state, "p_a", "counter.add", {"amount": 2})
        self.assertEqual(caught.exception.code, "action_rejected")
        self.assertEqual(state, {"total": 9, "log": []})  # the rejected copy is gone
        message = error_message(
            "action_rejected", caught.exception.message, request_id="r1", revision=4
        )
        self.assertEqual(message["code"], "action_rejected")

        outcome = runtime.execute(state, "p_a", "counter.add", {"amount": 1})
        self.assertEqual(outcome.state, {"total": 10, "log": ["p_a"]})
        outcome.state["log"].append("later")
        self.assertEqual(runtime.view(outcome.state, "p_b"), {"total": 10})
        self.assertEqual(state, {"total": 9, "log": []})

    def test_operations_draw_from_the_executions_random_source(self):
        runtime = Catalog([needs_dice()], [DICE_PROVIDER]).resolve("draws")
        draws = []
        result = runtime.execute(
            {}, "p_a", "draws.go", {}, randint=recording_randint(draws)
        ).result
        self.assertEqual((draws, result["total"]), ([(1, 6), (1, 6)], 12))

    def test_executors_must_return_json_outcomes(self):
        for returned in [{"total": 1}, Outcome({}, {"bad": {1, 2}}), Outcome([], {})]:
            module = needs_dice(
                commands=(Command("draws.go", lambda a: None, lambda a, r=returned: r),)
            )
            runtime = Catalog([module], [DICE_PROVIDER]).resolve("draws")
            with self.subTest(returned=returned), self.assertRaises(TypeError):
                runtime.execute({}, "p_a", "draws.go", {})


class DiceCompatibilityTests(unittest.TestCase):
    def test_roll_is_the_original_command(self):
        self.assertEqual(DICE_RUNTIME.command_names, ("roll",))
        self.assertEqual(
            DICE_RUNTIME.selection,
            (ProviderChoice("dice.roll.v1", "builtin.dice", "1"),),
        )
        outcome = DICE_RUNTIME.execute(
            {}, "p_a", "roll", {"expression": " 3d6+2 "}, randint=lambda a, b: b
        )
        expected = roll_expression(" 3d6+2 ", randint=lambda a, b: b).as_dict()
        self.assertEqual((outcome.state, outcome.result), ({}, expected))
        self.assertIsNone(DICE_RUNTIME.view({}, "p_a"))  # snapshots stay metadata-only
