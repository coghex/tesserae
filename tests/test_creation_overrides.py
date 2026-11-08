"""Module-described creation overrides and category policy (OVR-2, #174):
the optional capability's catalog checks, bounded discovery, pure and
deterministic previews whose digests follow their declared dependencies,
deterministic application with the host's own authorization, rules
document version 2, and the original Trailhead's two families (endurance
and supplies). The original Trailhead module, no network, no model.
docs/designs/creation_overrides_contract.md sections 3, 4 and 14."""

import json
import unittest
from dataclasses import replace

from tesserae import policy as policies
from tesserae.builtin import CATALOG, DICE_PROVIDER
from tesserae.modules import (
    ActionRejected,
    ArgumentError,
    Catalog,
    Category,
    IntentError,
    ModuleError,
    Outcome,
    UnsupportedOverride,
    preview_digest,
    result_bytes,
)
from tesserae.trailhead import OUTFITTER, TRAILHEAD_MODULE

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_campaign_rules import HOST, RulesCase, document
    from test_characters import CharacterCase
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_campaign_rules import HOST, RulesCase, document
    from tests.test_characters import CharacterCase

TRAILHEAD = CATALOG.resolve("trailhead")
ASTRAL = "\U0001f600"  # 12 result bytes once escaped


def change(quality_id, action_id, **parameters) -> dict:
    return {"quality_id": quality_id, "action_id": action_id, "parameters": parameters}


def intent(*changes) -> dict:
    return {"changes": list(changes)}


REPLACE_4 = intent(change("stamina_roll", "replace", value=4))
ROPE = intent(change("starting_gear", "grant", item="climbing rope"))


def direct(record_id="hr-000000000001") -> dict:
    """The host's authorization for a direct override."""
    return {"kind": "direct", "record_id": record_id,
            "host_player_id": HOST.player_id, "proposal_id": None,
            "offer_id": None, "requester_participant_id": None,
            "reason": "Agreed at the table."}  # fmt: skip


def accepted(participant_id, record_id="hr-000000000002") -> dict:
    """The host's authorization for an accepted offer."""
    return {"kind": "accepted_offer", "record_id": record_id,
            "host_player_id": HOST.player_id, "proposal_id": "hp-000000000001",
            "offer_id": "ho-000000000001",
            "requester_participant_id": participant_id,
            "reason": "A mountain guide by trade."}  # fmt: skip


def with_overrides(**changes):
    """Trailhead with its override capability's fields replaced."""
    overrides = replace(TRAILHEAD_MODULE.creation_overrides, **changes)
    return replace(TRAILHEAD_MODULE, creation_overrides=overrides)


def resolve(module):
    return Catalog([module], [DICE_PROVIDER]).resolve(module.id)


class OverrideCase(CharacterCase):
    """A store for drafts; overrides run on its state through the runtime
    (the store's own messages are OVR-3's)."""

    def state(self) -> dict:
        return self.store.inspect_state()

    def policy(self, character_id) -> dict:
        return self.store.effective_policy(character_id)

    def preview(self, character_id, wanted, state=None, runtime=TRAILHEAD):
        state = self.state() if state is None else state
        return runtime.override_preview(
            state, character_id, wanted, self.policy(character_id)
        )

    def apply(self, character_id, wanted, state=None, authorization=None):
        """Preview, then apply exactly that plan: the new state and receipt."""
        state = self.state() if state is None else state
        plan = self.preview(character_id, wanted, state)["plan"]
        outcome = TRAILHEAD.override_apply(
            state, character_id, plan, self.policy(character_id),
            authorization or direct(),
        )  # fmt: skip
        return outcome.state, outcome.result

    def step(self, state, character_id, name, args) -> dict:
        return TRAILHEAD.execute(
            state, self.alice.participant_id, name, args,
            randint=self.draws, character_id=character_id,
            policy=self.policy(character_id),
        ).state  # fmt: skip


class CatalogTests(unittest.TestCase):
    def test_the_capability_is_checked_before_anything_runs(self):
        endurance = Category("endurance", "Endurance", "")
        bad = {
            "no categories": ((), "1 to 16 override categories"),
            "seventeen": (
                tuple(Category(f"c{i}", "C", "") for i in range(17)),
                "1 to 16 override categories",
            ),
            "a path for an ID": (
                (Category("walkers/rolled", "Bad", ""),), "malformed override category",
            ),
            "an empty label": ((Category("x", "", ""),), "malformed override category"),
            "a long description": (
                (Category("x", "X", "d" * 501),), "malformed override category",
            ),
            "a duplicate": ((endurance, endurance), "Duplicate trailhead override"),
        }  # fmt: skip
        for name, (categories, message) in bad.items():
            with self.subTest(name), self.assertRaisesRegex(ModuleError, message):
                Catalog([with_overrides(categories=categories)], [DICE_PROVIDER])
        with self.assertRaisesRegex(ModuleError, "creation overrides version 2"):
            resolve(with_overrides(version=2))
        rules = replace(TRAILHEAD_MODULE.characters, version=1)
        with self.assertRaisesRegex(ModuleError, "need character rules version 2"):
            resolve(replace(with_overrides(), characters=rules))
        with self.assertRaisesRegex(ModuleError, "need character rules version 2"):
            resolve(replace(with_overrides(), characters=None))

    def test_modules_without_the_capability_are_unchanged(self):
        plain = resolve(replace(TRAILHEAD_MODULE, creation_overrides=None))
        self.assertEqual(plain.override_categories(), [])
        self.assertEqual(plain.override_allowances({"walkers": {}}, "c_0"), [])
        for call in (
            lambda: plain.override_discover({"walkers": {}}, "c_0", {}),
            lambda: plain.override_preview({"walkers": {}}, "c_0", REPLACE_4, {}),
            lambda: plain.override_apply(
                {"walkers": {}}, "c_0", REPLACE_4, {}, direct()
            ),
        ):
            with self.assertRaisesRegex(UnsupportedOverride, "no creation overrides"):
                call()
        for module_id in CATALOG.module_ids():  # only Trailhead adopts
            runtime = CATALOG.resolve(module_id)
            self.assertEqual(
                runtime.creation_overrides is not None, module_id == "trailhead"
            )


class DiscoveryTests(OverrideCase):
    def test_qualities_describe_their_actions_and_current_values(self):
        ash = self.draft()  # keen, rolled 4
        page = TRAILHEAD.override_discover(self.state(), ash, {})
        self.assertIsNone(page["next"])
        found = {item["quality_id"]: item for item in page["items"]}
        self.assertEqual(
            {q: (i["category_id"], i["current"]) for q, i in found.items()},
            {"stamina_roll": ("endurance", 4),
             "max_stamina_modifier": ("endurance", 0),
             "starting_gear": ("supplies", [])},
        )  # fmt: skip
        [replace_roll] = found["stamina_roll"]["actions"]
        self.assertEqual(
            replace_roll["parameters"],
            {
                "value": {
                    "type": "integer",
                    "minimum": 2,
                    "maximum": 4,
                    "required": True,
                }
            },
        )
        page = TRAILHEAD.override_discover(
            self.state(), ash, {"category_id": "supplies"}
        )
        self.assertEqual([i["quality_id"] for i in page["items"]], ["starting_gear"])
        self.assertEqual(
            [c["category_id"] for c in TRAILHEAD.override_categories()],
            ["endurance", "supplies"],
        )

    def test_options_page_to_the_end_and_filter(self):
        ash = self.draft()
        query = {"quality_id": "starting_gear", "parameter": "item"}
        seen, pages = [], 0
        while True:
            page = TRAILHEAD.override_discover(self.state(), ash, query)
            seen += [option["value"] for option in page["items"]]
            pages += 1
            if page["next"] is None:
                break
            query = {**query, "after": page["next"]}
        self.assertEqual((seen, pages), (list(OUTFITTER), 3))  # 5 a page
        page = TRAILHEAD.override_discover(
            self.state(), ash, {"quality_id": "starting_gear", "parameter": "item",
                                "match": "TRAIL"},
        )  # fmt: skip
        self.assertEqual(
            [o["value"] for o in page["items"]], ["trail map", "trail mix"]
        )

    def test_bad_queries_are_refused_and_never_read_as_paths(self):
        ash = self.draft()
        refused = {
            "an unknown field": ({"path": "walkers"}, ArgumentError),
            "a path for a quality": ({"quality_id": "walkers/rolled"}, ArgumentError),
            "a parameter alone": ({"parameter": "item"}, ArgumentError),
            "a forged cursor": (
                {"quality_id": "starting_gear", "parameter": "item", "after": "99"},
                ArgumentError,
            ),
            "an unknown category": ({"category_id": "magic"}, UnsupportedOverride),
            "an unknown quality": ({"quality_id": "level"}, UnsupportedOverride),
            "an unknown parameter": (
                {"quality_id": "stamina_roll", "parameter": "value"},
                UnsupportedOverride,
            ),
        }  # fmt: skip
        before = self.fingerprint()
        for name, (query, error) in refused.items():
            with self.subTest(name), self.assertRaises(error):
                TRAILHEAD.override_discover(self.state(), ash, query)
        self.assertEqual(self.fingerprint(), before)

    def test_a_page_over_its_bounds_is_a_module_failure_never_cut(self):
        ash = self.draft()
        option = {"value": "x", "label": "x"}
        too_many = {"items": [option] * 51, "next": None}
        too_big = {"items": [{"value": ASTRAL * 1400, "label": "x"}], "next": None}
        for page in (too_many, too_big):
            runtime = resolve(with_overrides(discover=lambda s, c, q, page=page: page))
            with self.assertRaisesRegex(TypeError, "at most 50 items|16384"):
                runtime.override_discover(
                    self.state(), ash, {"quality_id": "q", "parameter": "p"}
                )
        undeclared = {"items": [{"quality_id": "q", "category_id": "magic",
                                 "label": "Q", "description": "", "current": None,
                                 "actions": []}], "next": None}  # fmt: skip
        runtime = resolve(with_overrides(discover=lambda s, c, q: undeclared))
        with self.assertRaisesRegex(TypeError, "declared category"):
            runtime.override_discover(self.state(), ash, {})

    def test_parameter_descriptions_are_checked_by_their_type(self):
        ash = self.draft()

        def page(**described):
            action = {
                "action_id": "set",
                "label": "Set",
                "later_random": False,
                "parameters": {"value": {"required": True, **described}},
            }
            return {"items": [{"quality_id": "stamina_roll", "category_id": "endurance",
                               "label": "Q", "description": "", "current": None,
                               "actions": [action]}], "next": None}  # fmt: skip

        malformed = {
            "a word for a minimum": page(type="integer", minimum="not an integer",
                                         maximum=4),
            "no maximum": page(type="integer", minimum=2),
            "a true for a bound": page(type="integer", minimum=True, maximum=4),
            "an inverted range": page(type="integer", minimum=4, maximum=2),
            "a string with no length": page(type="string"),
            "a zero length": page(type="string", max_length=0),
            "an enum with no values": page(type="enum", values=[]),
            "an enum's repeated value": page(type="enum", values=["a", "a"]),
            "an enum of objects": page(type="enum", values=[{"a": 1}]),
            "an option with a range": page(type="option", minimum=1),
            "an unknown type": page(type="path"),
        }  # fmt: skip
        for name, bad in malformed.items():
            runtime = resolve(with_overrides(discover=lambda s, c, q, bad=bad: bad))
            with (
                self.subTest(name),
                self.assertRaisesRegex(TypeError, "exactly one of"),
            ):
                runtime.override_discover(self.state(), ash, {})
        for good in (
            page(type="integer", minimum=-1, maximum=2),
            page(type="string", max_length=60),
            page(type="enum", values=["steady", "quick", 3]),
            page(type="option"),
        ):
            runtime = resolve(with_overrides(discover=lambda s, c, q, good=good: good))
            self.assertEqual(runtime.override_discover(self.state(), ash, {}), good)


class PreviewTests(OverrideCase):
    def test_a_preview_is_pure_deterministic_and_reports_exact_effects(self):
        ash = self.draft()  # rolled 4 (the draws are 3s)
        before = self.fingerprint()
        draws = self.draws.call_count
        first = self.preview(ash, intent(change("stamina_roll", "replace", value=2)))
        second = self.preview(ash, intent(change("stamina_roll", "replace", value=2)))
        self.assertEqual(first, second)
        self.assertEqual(preview_digest(first), preview_digest(second))
        self.assertEqual((self.fingerprint(), self.draws.call_count), (before, draws))
        self.assertEqual(
            first["effects"],
            [{"quality_id": "stamina_roll", "before": 4, "after": 2,
              "summary": "Replaces the stamina roll of 4 with 2; the original roll "
              "of 4 is kept."}],
        )  # fmt: skip
        self.assertEqual(
            first["affected"],
            [{"quality_id": "stamina_roll", "category_id": "endurance"}],
        )
        self.assertEqual(first["dependencies"], {"rolled": 4, "stamina_roll": None})
        self.assertEqual(
            first["plan"], intent(change("stamina_roll", "replace", value=2))
        )

    def test_an_allowance_reports_its_later_step_and_expiry(self):
        ash = self.draft()
        preview = self.preview(ash, intent(change("starting_gear", "allow", count=2)))
        self.assertEqual(
            preview["allowances"],
            [{"allowance_id": "gear_picks", "quality_id": "starting_gear",
              "summary": "2 picks from the outfitter's list at no cost, until approval.",
              "creation_only": True}],
        )  # fmt: skip
        [later] = preview["later_steps"]
        self.assertEqual(later["command"], "trailhead.pick")
        self.assertIn("Unused picks expire when the walker is approved.",
                      preview["consequences"])  # fmt: skip
        self.assertEqual(preview["dependencies"], {"gear_picks": 0})

    def test_unrelated_steps_keep_the_digest_and_relevant_ones_change_it(self):
        ash = self.draft(trait="steady")
        roll = self.preview(ash, intent(change("stamina_roll", "replace", value=2)))
        rope = self.preview(ash, ROPE)
        state = self.state()
        for name, args in (
            ("trailhead.trait", {"trait": "quick"}),
            ("character.name", {"name": "Ash Vale"}),
            ("trailhead.fact", {"fact": "home", "text": "The valley",
                                "provenance": "chosen"}),
        ):  # fmt: skip
            state = self.step(state, ash, name, args)
        self.assertNotEqual(state, self.state())  # the draft did change
        for wanted, earlier in (
            (intent(change("stamina_roll", "replace", value=2)), roll),
            (ROPE, rope),
        ):
            self.assertEqual(
                preview_digest(self.preview(ash, wanted, state)),
                preview_digest(earlier),
            )
        # A relevant change: the gear the grant depends on.
        state["walkers"][ash]["gear"] = ["trail map"]
        moved = self.preview(ash, ROPE, state)
        self.assertNotEqual(preview_digest(moved), preview_digest(rope))
        self.assertEqual(
            moved["dependencies"], {"gear": ["trail map"], "gear_record": {}}
        )
        # And the roll replacement after another replacement applied.
        state, _ = self.apply(ash, intent(change("stamina_roll", "replace", value=3)),
                              state)  # fmt: skip
        again = self.preview(
            ash, intent(change("stamina_roll", "replace", value=2)), state
        )
        self.assertNotEqual(preview_digest(again), preview_digest(roll))

    def test_invalid_intents_are_refused_with_nothing_changed(self):
        ash = self.draft()
        refused = {
            "not an intent": ([], IntentError),
            "a privileged flag": (REPLACE_4 | {"privileged": True}, IntentError),
            "a change's extra field": (
                intent(change("stamina_roll", "replace", value=4) | {"as_host": True}),
                IntentError,
            ),
            "no changes": (intent(), IntentError),
            "seventeen changes": (
                intent(*[change("starting_gear", "allow", count=1)] * 17), IntentError,
            ),
            "a raw state path": (
                intent(change("walkers.c_1.rolled", "set", value=4)), IntentError,
            ),
            "too many bytes": (
                intent(change("starting_gear", "grant", item=ASTRAL * 400)), IntentError,
            ),
            "an unknown quality": (intent(change("level", "set", value=2)),
                                   UnsupportedOverride),
            "an unknown action": (intent(change("stamina_roll", "reroll")),
                                  UnsupportedOverride),
            "a missing parameter": (intent(change("stamina_roll", "replace")),
                                    IntentError),
            "a true for a number": (
                intent(change("stamina_roll", "replace", value=True)), IntentError,
            ),
            "out of range": (intent(change("stamina_roll", "replace", value=5)),
                             IntentError),
            "nothing would change": (
                intent(change("stamina_roll", "replace", value=4)), IntentError,
            ),
            "not the outfitter's": (
                intent(change("starting_gear", "grant", item="jetpack")), IntentError,
            ),
            "not carried": (
                intent(change("starting_gear", "remove", carried="compass")), IntentError,
            ),
            "too many picks": (
                intent(change("starting_gear", "allow", count=2),
                       change("starting_gear", "allow", count=2)), IntentError,
            ),
            "the roll twice": (
                intent(change("stamina_roll", "replace", value=2),
                       change("stamina_roll", "replace", value=3)), IntentError,
            ),
        }  # fmt: skip
        before = self.fingerprint()
        for name, (wanted, error) in refused.items():
            with self.subTest(name), self.assertRaises(error) as caught:
                self.preview(ash, wanted)
            if error is IntentError:
                self.assertTrue(caught.exception.problems)
        self.assertEqual(self.fingerprint(), before)
        unrolled = self.draft(name="Birch", roll=False)
        with self.assertRaisesRegex(IntentError, "Roll stamina first"):
            self.preview(unrolled, REPLACE_4)
        ready = self.ready(name="Cedar")
        with self.assertRaisesRegex(IntentError, "drafts only"):
            self.preview(ready, ROPE)

    def test_a_preview_over_its_bound_is_refused_never_cut(self):
        ash = self.draft()
        state = self.state()
        long_items = [f"{i}{ASTRAL * 59}" for i in range(8)]  # 8 items of 60
        state["walkers"][ash]["gear"] = long_items
        two = intent(
            *[change("starting_gear", "remove", carried=i) for i in long_items[:2]]
        )
        preview = self.preview(ash, two, state)
        self.assertLessEqual(result_bytes(preview), 12288)
        five = intent(
            *[change("starting_gear", "remove", carried=i) for i in long_items[:5]]
        )
        self.assertLessEqual(result_bytes(five), 4096)  # the intent itself fits
        with self.assertRaisesRegex(IntentError, "split the request"):
            self.preview(ash, five, state)

    def test_malformed_previews_are_module_failures(self):
        ash = self.draft()
        good = self.preview(ash, intent(change("stamina_roll", "replace", value=2)))
        broken = {
            "a missing field": {k: v for k, v in good.items() if k != "limitations"},
            "an undeclared category": good | {
                "affected": [{"quality_id": "stamina_roll", "category_id": "magic"}]},
            "the named quality unlisted": good | {"affected": []},
            "a long headline": good | {"headline": "x" * 201},
            "a two-line headline": good | {"headline": "one\ntwo"},
            "a plan as a path": good | {"plan": "walkers.c_1.rolled=2"},
            "a waiver without its flag": good | {"waivers": [
                {"quality_id": "stamina_roll", "summary": "No limit."}]},
            "an allowance outliving creation": good | {"allowances": [
                {"allowance_id": "a", "quality_id": "stamina_roll",
                 "summary": "Forever.", "creation_only": False}]},
            "33 consequences": good | {"consequences": ["c"] * 33},
        }  # fmt: skip
        for name, preview in broken.items():
            runtime = resolve(with_overrides(preview=lambda *a, p=preview: p))
            with self.subTest(name), self.assertRaises(TypeError):
                self.preview(ash, intent(change("stamina_roll", "replace", value=2)),
                             runtime=runtime)  # fmt: skip
        huge = good | {"dependencies": {"x": "a" * 13000}}
        runtime = resolve(with_overrides(preview=lambda *a: huge))
        with self.assertRaisesRegex(IntentError, "split the request"):
            self.preview(ash, intent(change("stamina_roll", "replace", value=2)),
                         runtime=runtime)  # fmt: skip


class ApplyTests(OverrideCase):
    def test_apply_performs_the_plan_without_dice_and_keeps_originals(self):
        ash = self.draft(trait="keen")  # rolled 4
        draws = self.draws.call_count
        state, receipt = self.apply(
            ash, intent(change("stamina_roll", "replace", value=2),
                        change("max_stamina_modifier", "set", amount=2)),
            authorization=accepted(self.alice.participant_id),
        )  # fmt: skip
        self.assertEqual(self.draws.call_count, draws)
        self.assertEqual(
            receipt, {"stamina_roll": {"original": 4, "value": 2},
                      "max_stamina_modifier": 2},
        )  # fmt: skip
        walker = state["walkers"][ash]
        self.assertEqual(walker["rolled"], 4)  # the original roll is kept
        sheet = TRAILHEAD.character_sheet(state, ash)
        self.assertEqual(sheet["stamina"], 2)  # the effective roll
        by = {"kind": "accepted_offer", "record_id": "hr-000000000002"}
        self.assertEqual(
            sheet["overrides"],
            {"stamina_roll": {"value": 2, "by": by, "original": 4},
             "max_stamina_modifier": {"amount": 2, "by": by}},
        )  # fmt: skip
        # One calculation: completion uses the replacement and the modifier.
        done = TRAILHEAD.complete_character(state, ash, self.policy(ash))
        self.assertEqual(done["walkers"][ash]["max_stamina"], 2 + 1 + 2)  # keen +1
        self.assertEqual(done["walkers"][ash]["overrides"], walker["overrides"])

    def test_a_still_valid_plan_keeps_intervening_unrelated_edits(self):
        ash = self.draft(trait="steady")
        plan = self.preview(ash, ROPE)["plan"]
        state = self.step(self.state(), ash, "trailhead.trait", {"trait": "quick"})
        state = TRAILHEAD.override_apply(
            state, ash, plan, self.policy(ash), direct()
        ).state
        walker = state["walkers"][ash]
        self.assertEqual(
            (walker["trait"], walker["gear"]), ("quick", ["climbing rope"])
        )

    def test_plans_it_would_not_make_and_bad_authorizations_change_nothing(self):
        ash = self.draft()
        state = self.state()
        plan = self.preview(ash, ROPE)["plan"]
        policy = self.policy(ash)
        for name, bad in {
            "not a plan": {"set": {"walkers": {}}},
            "a forged item": intent(change("starting_gear", "grant", item="jetpack")),
            "no longer valid": plan | {"changes": [
                change("starting_gear", "remove", carried="climbing rope")]},
        }.items():  # fmt: skip
            with self.subTest(name), self.assertRaises(IntentError):
                TRAILHEAD.override_apply(state, ash, bad, policy, direct())
        for name, authorization in {
            "a client's flag": {"privileged": True},
            "a guest's own kind": direct() | {"kind": "guest"},
            "a direct one naming a proposal": direct() | {"proposal_id": "hp-1"},
            "no reason": direct() | {"reason": ""},
        }.items():
            with self.subTest(name), self.assertRaises(ValueError):
                TRAILHEAD.override_apply(state, ash, plan, policy, authorization)
        self.assertEqual(state, self.state())

    def test_an_oversized_or_malformed_receipt_is_a_module_failure(self):
        ash = self.draft()
        plan = self.preview(ash, ROPE)["plan"]
        for receipt in ({"x": "a" * 8200}, ["not", "an", "object"]):
            runtime = resolve(with_overrides(
                apply=lambda action, r=receipt: Outcome(action.state, r)))  # fmt: skip
            with self.assertRaises(TypeError):
                runtime.override_apply(
                    self.state(), ash, plan, self.policy(ash), direct()
                )

    def test_reversals_at_the_record_limit_count_the_records_they_drop(self):
        ash = self.draft()
        state = self.state()
        originals = [f"old {i}" for i in range(8)]
        state["walkers"][ash]["gear"] = list(originals)
        state, _ = self.apply(
            ash,
            intent(*[change("starting_gear", "remove", carried=i) for i in originals]),
            state,
        )
        granted = list(OUTFITTER[:8])
        state, _ = self.apply(
            ash,
            intent(*[change("starting_gear", "grant", item=i) for i in granted]),
            state,
        )
        self.assertEqual(len(state["walkers"][ash]["overrides"]["gear"]), 16)  # full
        # Undoing a grant drops a record, so it is allowed at the limit.
        state, _ = self.apply(
            ash, intent(change("starting_gear", "remove", carried=granted[0])), state
        )
        self.assertEqual(len(state["walkers"][ash]["overrides"]["gear"]), 15)
        state, _ = self.apply(
            ash, intent(change("starting_gear", "grant", item=OUTFITTER[8])), state
        )
        # Full again: a reversal first makes room within one intent.
        state, _ = self.apply(
            ash,
            intent(
                change("starting_gear", "remove", carried=granted[1]),
                change("starting_gear", "grant", item=OUTFITTER[9]),
            ),
            state,
        )
        self.assertEqual(len(state["walkers"][ash]["overrides"]["gear"]), 16)
        # A change that adds a record at the limit is refused.
        full = self.state()
        full["walkers"][ash]["gear"] = ["old a"]
        full["walkers"][ash]["overrides"] = {"gear": {
            f"old r{i}": {"change": "removed",
                          "by": {"kind": "direct", "record_id": "hr-9"}}
            for i in range(16)}}  # fmt: skip
        with self.assertRaisesRegex(IntentError, "too many recorded changes"):
            self.preview(
                ash, intent(change("starting_gear", "remove", carried="old a")), full
            )
        # A pick that undoes a removal is allowed at the limit; a new one is not.
        by = {"kind": "direct", "record_id": "hr-9"}
        records = {f"old r{i}": {"change": "removed", "by": by} for i in range(15)}
        full["walkers"][ash]["gear"] = []
        full["walkers"][ash]["overrides"] = {
            "gear": {**records, "compass": {"change": "removed", "by": by}},
            "gear_picks": {"remaining": 2, "by": by},
        }
        with self.assertRaisesRegex(ActionRejected, "too many recorded changes"):
            self.step(full, ash, "trailhead.pick", {"item": "headlamp"})
        full = self.step(full, ash, "trailhead.pick", {"item": "compass"})
        walker = full["walkers"][ash]
        self.assertEqual(
            (walker["gear"], walker["overrides"]["gear"]), (["compass"], records)
        )

    def test_gear_keeps_what_creation_gave(self):
        ash = self.draft()
        state = self.state()
        state["walkers"][ash]["gear"] = ["trail map"]
        state, receipt = self.apply(ash, intent(
            change("starting_gear", "grant", item="climbing rope"),
            change("starting_gear", "remove", carried="trail map")), state)  # fmt: skip
        self.assertEqual(receipt, {"gear": {"added": ["climbing rope"],
                                            "removed": ["trail map"]}})  # fmt: skip
        walker = state["walkers"][ash]
        self.assertEqual(walker["gear"], ["climbing rope"])
        record = {
            item: entry["change"] for item, entry in walker["overrides"]["gear"].items()
        }
        self.assertEqual(record, {"climbing rope": "granted", "trail map": "removed"})
        # Undoing is a new intent; the original is then unrecorded again.
        state, _ = self.apply(ash, intent(
            change("starting_gear", "grant", item="trail map"),
            change("starting_gear", "remove", carried="climbing rope")), state)  # fmt: skip
        self.assertEqual(state["walkers"][ash]["gear"], ["trail map"])
        self.assertNotIn("overrides", state["walkers"][ash])


class AllowanceTests(OverrideCase):
    def test_picks_are_spent_by_an_ordinary_step_and_expire_at_completion(self):
        ash = self.draft()
        state, receipt = self.apply(
            ash, intent(change("starting_gear", "allow", count=2))
        )
        self.assertEqual(receipt, {"gear_picks": 2})
        self.assertEqual(
            TRAILHEAD.override_allowances(state, ash),
            [{"allowance_id": "gear_picks", "quality_id": "starting_gear",
              "summary": "2 picks from the outfitter's list.", "remaining": 2}],
        )  # fmt: skip
        listed = TRAILHEAD.creation_actions(state, ash, self.policy(ash))
        self.assertIn("trailhead.pick", [a["command"] for a in listed])
        draws = self.draws.call_count
        state = self.step(state, ash, "trailhead.pick", {"item": "headlamp"})
        self.assertEqual(self.draws.call_count, draws)
        with self.assertRaisesRegex(ActionRejected, "already carries"):
            self.step(state, ash, "trailhead.pick", {"item": "headlamp"})
        self.assertEqual(TRAILHEAD.override_allowances(state, ash)[0]["remaining"], 1)
        # A replacement step never resets the applied override.
        state, _ = self.apply(
            ash, intent(change("stamina_roll", "replace", value=2)), state
        )
        state = self.step(state, ash, "trailhead.trait", {"trait": "steady"})
        with self.assertRaisesRegex(ActionRejected, "already rolled"):
            self.step(state, ash, "trailhead.stamina", {})
        done = TRAILHEAD.complete_character(state, ash, self.policy(ash))
        walker = done["walkers"][ash]
        self.assertEqual(TRAILHEAD.override_allowances(done, ash), [])
        self.assertNotIn("gear_picks", walker["overrides"])
        self.assertEqual(walker["gear"], ["headlamp"])  # what was used stays
        self.assertEqual(walker["overrides"]["gear"]["headlamp"]["change"], "picked")
        self.assertEqual(walker["max_stamina"], 2)  # the replacement, steady
        with self.assertRaisesRegex(ActionRejected, "no gear picks"):
            self.step(done, ash, "trailhead.pick", {"item": "compass"})

    def test_no_pick_without_an_allowance_and_none_beyond_it(self):
        ash = self.draft()
        with self.assertRaisesRegex(ActionRejected, "no gear picks"):
            self.step(self.state(), ash, "trailhead.pick", {"item": "compass"})
        state, _ = self.apply(ash, intent(change("starting_gear", "allow", count=1)))
        state = self.step(state, ash, "trailhead.pick", {"item": "compass"})
        with self.assertRaisesRegex(ActionRejected, "no gear picks"):
            self.step(state, ash, "trailhead.pick", {"item": "headlamp"})
        self.assertNotIn(
            "trailhead.pick",
            [
                a["command"]
                for a in TRAILHEAD.creation_actions(state, ash, self.policy(ash))
            ],
        )

    def test_a_ready_walker_with_overrides_plays_and_is_corrected_by_one_rule(self):
        ash = self.draft(trait="quick")
        state, _ = self.apply(
            ash, intent(change("max_stamina_modifier", "set", amount=-1))
        )
        done = TRAILHEAD.complete_character(state, ash, self.policy(ash))
        self.assertEqual(done["walkers"][ash]["max_stamina"], 4 - 1)
        corrected = TRAILHEAD.for_host().execute(
            done, HOST.sender, "trailhead.set_trait",
            {"character_id": ash, "trait": "keen"}, policy=self.policy(ash),
        ).state  # fmt: skip
        self.assertEqual(corrected["walkers"][ash]["max_stamina"], 4 + 1 - 1)
        seeded = json.loads(json.dumps(corrected))
        TRAILHEAD.validate_character_seed(
            seeded, frozenset({self.alice.participant_id}),
            {ash: self.alice.participant_id},
        )  # fmt: skip
        seeded["walkers"][ash]["overrides"]["gear_picks"] = {
            "remaining": 1, "by": {"kind": "direct", "record_id": "hr-1"}}  # fmt: skip
        with self.assertRaisesRegex(Exception, "invalid overrides"):
            TRAILHEAD.validate_character_seed(
                seeded, frozenset({self.alice.participant_id}),
                {ash: self.alice.participant_id},
            )  # fmt: skip


class CategoryPolicyTests(RulesCase):
    def overrides_document(self, **categories) -> dict:
        return document(document_version=2, overrides={"categories": categories})

    def test_version_2_documents_set_category_policy(self):
        self.assertEqual(
            policies.category_policy(TRAILHEAD, None),
            {"endurance": "approval_required", "supplies": "approval_required"},
        )
        self.assertEqual(
            policies.category_policy(TRAILHEAD, document()),
            {"endurance": "approval_required", "supplies": "approval_required"},
        )
        content = self.overrides_document(supplies="blocked")
        result, _ = self.rules.import_document(content, HOST)
        self.rules.activate(result["revision"], None, HOST)
        _, active = self.store.active_document()
        self.assertEqual(active, content)
        self.assertEqual(
            policies.category_policy(TRAILHEAD, active),
            {"endurance": "approval_required", "supplies": "blocked"},
        )
        self.assertEqual(
            self.rules.projection()["override_categories"],
            [{"category_id": "endurance", "label": "Endurance",
              "policy": "approval_required"},
             {"category_id": "supplies", "label": "Supplies", "policy": "blocked"}],
        )  # fmt: skip

    def test_policy_blocks_a_guests_named_category_never_a_consequence(self):
        blocked = {"endurance": "approval_required", "supplies": "blocked"}
        rope = {
            "affected": [{"quality_id": "starting_gear", "category_id": "supplies"}]
        }
        self.assertEqual(
            policies.blocked_categories(TRAILHEAD, blocked, ROPE, rope),
            [{"category_id": "supplies", "label": "Supplies"}],
        )
        open_policy = {
            "endurance": "approval_required",
            "supplies": "approval_required",
        }
        self.assertEqual(
            policies.blocked_categories(TRAILHEAD, open_policy, ROPE, rope), []
        )
        consequential = {"affected": [
            {"quality_id": "stamina_roll", "category_id": "endurance"},
            {"quality_id": "starting_gear", "category_id": "supplies"}]}  # fmt: skip
        self.assertEqual(
            policies.blocked_categories(TRAILHEAD, blocked, REPLACE_4, consequential),
            [],
        )

    def test_policy_never_limits_what_the_host_applies(self):
        ash = self.walker_draft()
        state = self.store.inspect_state()
        policy = self.store.effective_policy(ash)
        plan = TRAILHEAD.override_preview(state, ash, ROPE, policy)["plan"]
        outcome = TRAILHEAD.override_apply(state, ash, plan, policy, direct())
        self.assertEqual(outcome.state["walkers"][ash]["gear"], ["climbing rope"])

    def test_documents_for_modules_without_the_capability_stay_version_1(self):
        plain = resolve(replace(TRAILHEAD_MODULE, creation_overrides=None))
        with self.assertRaises(policies.DocumentError) as caught:
            policies.check_shape(
                self.overrides_document(), dict(document()["module"]),
                frozenset(plain.settings_defaults()),
            )  # fmt: skip
        self.assertIn(
            "needs a module with creation overrides", caught.exception.problems[0]
        )
        self.assertEqual(policies.category_policy(plain, None), {})

    def walker_draft(self) -> str:
        reply, _ = self.store.create_character(self.alice, "w-1", "Ash")
        return reply["result"]["character"]["character_id"]


if __name__ == "__main__":
    unittest.main()
