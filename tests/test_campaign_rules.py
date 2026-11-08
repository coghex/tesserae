"""Campaign settings documents and module requirements (GOV-3, #134): import,
export, list and activation with exact replay; unsupported, stale and busy
activation changing nothing; the effective policy each character's rules
see; activation making a shown approval stale without touching completed
characters; compatibility with modules without the capability; the referee
context; and the operator's `tesserae game rules`. The original Trailhead
module, no network, no model. docs/designs/governance_contract.md sections 5,
6 and 10."""

import io
import json
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from tesserae import policy
from tesserae.builtin import CATALOG, DICE_PROVIDER
from tesserae.campaign_rules import OPERATOR_DECIDER, CampaignRules, Decider
from tesserae.cli import main
from tesserae.credentials import new_token
from tesserae.modules import (
    CampaignSettings,
    Catalog,
    Command,
    ModuleError,
    Outcome,
    SeedError,
)
from tesserae.referee.store import RefereeStore
from tesserae.referee.tools import BridgeTools
from tesserae.store import SETUP, Actor, GameStore, Refusal
from tesserae.trailhead import TRAILHEAD_MODULE

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_characters import CharacterCase, new_request
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_characters import CharacterCase, new_request

TRAILHEAD = CATALOG.resolve("trailhead")
BINDING = {"id": "trailhead", "version": "1", "state_version": 1}
HOST = Decider("u_0123456789abcdef", str(uuid.uuid4()))


def document(bonus=None, exceptions=(), guidance="Walkers are locals.", **changes):
    settings = {} if bonus is None else {"starting_stamina_bonus": bonus}
    return {"document_version": 1, "module": dict(BINDING), "settings": settings,
            "exceptions": list(exceptions), "guidance": guidance, **changes}  # fmt: skip


def exception(character_id, bonus=1, exception_id="x-1"):
    return {"exception_id": exception_id, "character_id": character_id,
            "settings": {"starting_stamina_bonus": bonus},
            "reason": "Agreed: a mountain guide by trade."}  # fmt: skip


class RulesCase(CharacterCase):
    def setUp(self):
        super().setUp()
        self.rules = CampaignRules(self.store)

    def imported(self, content, by=HOST, **options) -> int:
        result, applied = self.rules.import_document(content, by, **options)
        self.assertEqual(result["state"], "candidate")
        return result["revision"]

    def refusal(self, call, *args, **options) -> Refusal:
        with self.assertRaises(Refusal) as refused:
            call(*args, **options)
        return refused.exception

    def audit(self) -> list:
        return self.store.query(
            "SELECT kind, player_id, via, detail FROM host_audit ORDER BY id"
        )

    def walker(self, character_id) -> dict:
        return self.store.inspect_state()["walkers"][character_id]


class DocumentTests(RulesCase):
    def test_an_import_is_a_candidate_until_activated_and_exports_round_trip(self):
        wren = self.ready(name="Wren")  # keen, 1d3+1 rolled 4: 5 stamina
        first = document(bonus=1)
        self.assertEqual(self.imported(first), 1)
        self.assertEqual(self.store.policy_revision(), 0)
        self.assertEqual(
            self.store.effective_policy(), {"revision": 0, "settings": {"starting_stamina_bonus": 0}}
        )  # fmt: skip
        [listed] = self.rules.revisions()
        self.assertEqual(
            (listed["revision"], listed["state"], listed["origin"], listed["sha256"]),
            (1, "candidate", "import", policy.digest(policy.canonical(first))),
        )
        self.assertEqual(self.rules.document(1)["content"], first)
        self.assertEqual(
            self.refusal(self.rules.document).code, "invalid_request"
        )  # nothing active to export yet

        result, applied = self.rules.activate(1, 0, HOST)
        self.assertEqual((result, applied), ({"revision": 1, "state": "active"}, True))
        self.assertEqual(
            self.store.effective_policy()["settings"]["starting_stamina_bonus"], 1
        )
        # A completed walker is never reallocated or refreshed.
        self.assertEqual(
            (self.walker(wren)["max_stamina"], "bonus" in self.walker(wren)), (5, False)
        )

        second = document(bonus=2, guidance=None)
        self.rules.activate(self.imported(second), 1, HOST)
        self.assertEqual(
            [(r["revision"], r["state"], r["activated_by"]) for r in self.rules.revisions()],
            [(1, "superseded", HOST.player_id), (2, "active", HOST.player_id)],
        )  # fmt: skip
        self.assertEqual(self.rules.document()["content"], second)
        self.assertEqual(
            [(kind, player, via) for kind, player, via, _ in self.audit()],
            [("import", HOST.player_id, "client"), ("activation", HOST.player_id, "client")] * 2,
        )  # fmt: skip

    def test_exact_replays_conflicts_and_rejection(self):
        self.assertEqual(self.imported(document(bonus=1), request_id="i-1"), 1)
        result, applied = self.rules.import_document(
            document(bonus=1), HOST, request_id="i-1"
        )
        self.assertEqual((result["revision"], applied), (1, False))  # one candidate
        self.assertEqual(len(self.rules.revisions()), 1)
        conflict = self.refusal(
            self.rules.import_document, document(bonus=2), HOST, request_id="i-1"
        )
        self.assertEqual(conflict.code, "request_conflict")
        other = Decider("u_fedcba9876543210", HOST.host_id)  # its own namespace
        self.assertEqual(self.imported(document(bonus=2), other, request_id="i-1"), 2)

        self.rules.activate(1, 0, HOST, request_id="a-1")
        self.rules.activate(2, 1, other, request_id="a-1")
        # A retried activation that succeeded returns that success, not stale.
        self.assertEqual(
            self.rules.activate(1, 0, HOST, request_id="a-1"),
            ({"revision": 1, "state": "active"}, False),
        )
        self.assertEqual(self.store.policy_revision(), 2)

        three = self.imported(document(bonus=0))
        self.assertEqual(
            self.rules.reject(three, HOST, request_id="r-1"),
            ({"revision": 3, "state": "rejected"}, True),
        )
        for call, args in ((self.rules.activate, (three, 2, HOST)),
                           (self.rules.reject, (three, HOST)),
                           (self.rules.activate, (1, 2, HOST)),  # superseded
                           (self.rules.activate, (9, 2, HOST))):  # fmt: skip
            with self.subTest(args=args):
                self.assertEqual(self.refusal(call, *args).code, "invalid_request")
        self.assertEqual(self.store.policy_revision(), 2)

    def test_stale_and_busy_activation_change_nothing(self):
        ash = self.draft()
        revision = self.imported(document(bonus=1))
        before = (self.fingerprint(), self.rules.revisions(), self.audit())
        stale = self.refusal(self.rules.activate, revision, 3, HOST)
        self.assertEqual(
            (stale.code, stale.extra), ("stale_policy", {"policy_revision": 0})
        )
        referee = RefereeStore(self.store)
        referee.enable()
        referee.submit(self.alice, "s-1", "Hello", False, actor=Actor(ash, "creation"))
        busy = self.refusal(self.rules.activate, revision, 0, HOST)
        self.assertEqual(
            (busy.code, busy.extra["job"]["for"]), ("referee_busy", "Alice")
        )
        self.assertEqual(
            (self.fingerprint(), self.rules.revisions(), self.audit()), before
        )
        self.assertEqual(self.record(ash).draft_revision, 4)
        self.imported(document(bonus=2))  # imports go on while the referee answers

    def test_unsupported_documents_are_refused_with_nothing_stored(self):
        ash = self.draft()
        token = new_token()
        cases = {
            "unknown setting": (document(bonus=1) | {"settings": {"bonus": 1}}, "'bonus'"),
            "a token as a key": (document() | {"settings": {token: 1}}, "(not shown)"),
            "another module": (document(module={**BINDING, "id": "relay"}), "this game uses trailhead"),
            "another version": (document(module={**BINDING, "version": "2"}), "'2'"),
            "a true state version": (
                document(module={**BINDING, "state_version": True}), "this game uses",
            ),
            "too big a bonus": (document(bonus=5), "0 to 2"),
            "a true bonus": (document(bonus=True), "0 to 2"),
            "no such character": (document(exceptions=[exception("c_ffffffffffffffff")]), "no such character"),
            "two for one character": (
                document(exceptions=[exception(ash), exception(ash, exception_id="x-2")]),
                "already has an exception",
            ),
            "an empty exception": (
                document(exceptions=[exception(ash) | {"settings": {}}]), "at least one",
            ),
            "an extra field": (document(script="rm"), "1 unexpected field"),
            "a later document version": (document(document_version=3), "must be 1 or 2"),
            "version 2 without its policy": (document(document_version=2), "missing overrides"),
            "version 1 with a policy": (
                document(overrides={"categories": {}}), "1 unexpected field",
            ),
            "an undeclared category": (
                document(document_version=2, overrides={"categories": {"magic": "blocked"}}),
                "unknown category 'magic'; this module declares endurance, supplies",
            ),
            "an unknown policy": (
                document(document_version=2,
                         overrides={"categories": {"supplies": "automatic"}}),
                "approval_required or blocked",
            ),
            "a policy with more fields": (
                document(document_version=2,
                         overrides={"categories": {}, "auto": True}),
                'exactly {"categories"',
            ),
            "a true version": (document(document_version=True), "must be 1 or 2"),
            "long guidance": (document(guidance="x" * 8001), "8000"),
            "not an object": ([], "JSON object"),
        }  # fmt: skip
        before = (self.fingerprint(), self.rules.revisions(), self.audit())
        for name, (content, shown) in cases.items():
            with self.subTest(name):
                refused = self.refusal(self.rules.import_document, content, HOST)
                self.assertEqual(refused.code, "invalid_document")
                self.assertIn(shown, " ".join(refused.extra["problems"]))
                self.assertNotIn(token, json.dumps(refused.extra))
        self.assertEqual(
            (self.fingerprint(), self.rules.revisions(), self.audit()), before
        )

    def test_a_module_without_settings_takes_guidance_only(self):
        with GameStore.create(
            self.directory / "dice.sqlite3", CATALOG.resolve("dice")
        ) as dice:
            rules = CampaignRules(dice)
            binding = {"id": "dice", "version": "1", "state_version": 1}
            for content in (document(bonus=0, module=binding),
                            document(module=binding, exceptions=[exception(
                                "c_ffffffffffffffff")])):  # fmt: skip
                with self.subTest(content=content):
                    with self.assertRaises(Refusal) as refused:
                        rules.import_document(content, HOST)
                    self.assertIn(
                        "no settings", " ".join(refused.exception.extra["problems"])
                    )
            result, _ = rules.import_document(document(module=binding), HOST)
            rules.activate(result["revision"], 0, HOST)
            self.assertIsNone(dice.effective_policy())
            self.assertEqual(
                rules.projection(),
                {"policy_revision": 1, "settings": {}, "guidance": "Walkers are locals.",
                 "requirements": None},
            )  # fmt: skip


class PolicyTests(RulesCase):
    def test_activation_makes_a_shown_approval_stale_and_completion_uses_it(self):
        ash, kit = self.draft(), self.draft(name="Kit")
        self.assertEqual(
            (self.record(ash).draft_revision, self.record(kit).draft_revision), (4, 4)
        )
        # An exception for Kit alone: Ash's effective settings do not change.
        content = document(exceptions=[exception(kit, bonus=2)])
        result, _ = self.rules.activate(self.imported(content), 0, HOST)
        [_, activation] = self.audit()
        self.assertEqual(json.loads(activation[3])["drafts"], [kit])
        self.assertEqual(
            (self.record(ash).draft_revision, self.record(kit).draft_revision), (4, 5)
        )

        stale, committed = self.store.complete_character(
            self.alice, new_request(), kit, 4
        )
        self.assertEqual(
            (stale["code"], stale["draft_revision"], committed),
            ("stale_draft", 5, False),
        )
        for character_id, revision in ((ash, 4), (kit, 5)):
            done, committed = self.store.complete_character(
                self.alice, new_request(), character_id, revision
            )
            self.assertTrue(committed, done)
        self.assertEqual(self.walker(ash)["max_stamina"], 5)  # 4 + 1 keen
        self.assertEqual(
            (self.walker(kit)["max_stamina"], self.walker(kit)["bonus"]), (7, 2)
        )  # 4 + 1 keen + 2 by its exception
        # Later settings never touch completed walkers.
        self.rules.activate(self.imported(document(bonus=1)), 1, HOST)
        self.assertEqual(
            (self.walker(ash)["max_stamina"], self.walker(kit)["max_stamina"]), (5, 7)
        )

    def test_each_character_sees_its_own_effective_policy(self):
        ash, kit = self.draft(), self.draft(name="Kit")
        content = document(bonus=1, exceptions=[exception(ash, bonus=2)])
        self.rules.activate(self.imported(content), 0, HOST)
        bonus = {
            who: self.store.effective_policy(who)["settings"]["starting_stamina_bonus"]
            for who in (ash, kit, None)
        }
        self.assertEqual(bonus, {ash: 2, kit: 1, None: 1})
        mine = self.rules.projection(kit)
        self.assertEqual(
            (
                mine["policy_revision"],
                mine["settings"],
                mine["exception"],
                mine["guidance"],
            ),
            (1, {"starting_stamina_bonus": 1}, None, "Walkers are locals."),
        )
        self.assertNotIn("mountain guide", json.dumps(mine))  # never another's reason
        self.assertEqual(self.rules.projection(ash)["exception"]["settings"],
                         {"starting_stamina_bonus": 2})  # fmt: skip

    def test_requirements_distinguish_mechanical_narrative_and_optional(self):
        ash = self.draft(name=None, trait="steady", roll=False)
        self.rules.activate(self.imported(document(bonus=1)), 0, HOST)
        entries = self.rules.projection(ash)["requirements"]
        self.assertEqual(
            [(e["id"], e["kind"], e["met"]) for e in entries],
            [("character.name", "mechanical", False), ("trait", "mechanical", True),
             ("stamina", "mechanical", False), ("background", "narrative", False),
             ("home", "optional", None)],
        )  # fmt: skip
        stamina = entries[2]
        self.assertEqual((stamina["method"], stamina["die"]), ("roll", "1d3+1"))
        self.assertIn("Approval adds 1 starting stamina", stamina["description"])
        self.assertEqual(
            entries[4]["method"], "trailhead.fact"
        )  # recorded, never required
        general = self.rules.projection()["requirements"]
        self.assertTrue(all("met" not in e for e in general))


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.seen = []

    def catalog(self, *, rules=None, settings="keep"):
        """Trailhead with its rules or settings replaced, and a creation step
        that records the policy its action was given."""
        characters = replace(TRAILHEAD_MODULE.characters, **(rules or {}))

        def note(action):
            self.seen.append(action.policy)
            return Outcome(action.state, {})

        characters = replace(
            characters,
            steps=(
                *characters.steps,
                Command("trailhead.note", lambda args: None, note),
            ),
        )
        module = replace(TRAILHEAD_MODULE, characters=characters)
        if characters.version == 1:  # overrides need version 2 rules
            module = replace(module, creation_overrides=None)
        if settings != "keep":
            module = replace(module, settings=settings)
        return Catalog([module], [DICE_PROVIDER])

    def run_rules(self, runtime, path=None) -> GameStore:
        """A store (new, or the one at `path`) where Alice has made Ash
        ready, with every creation step."""
        if path is None:
            store = GameStore.create(
                self.directory / f"{uuid.uuid4()}.sqlite3", runtime,
                randint=lambda low, high: 3,
            )  # fmt: skip
        else:
            store = GameStore.open_existing(
                path, runtime=runtime, randint=lambda low, high: 3
            )
        store = self.enterContext(store)
        token = new_token()
        store.add_participant("Alice", token)
        alice = store.authenticate(token)
        reply, _ = store.create_character(alice, "c-1", "Ash")
        ash = reply["result"]["character"]["character_id"]
        for step, args in (("trailhead.trait", {"trait": "quick"}),
                           ("trailhead.stamina", {}), ("trailhead.note", {})):  # fmt: skip
            raw = json.dumps({"version": 1, "type": "command", "request_id": step,
                              "expected_revision": store.revision(), "command": step,
                              "args": args})  # fmt: skip
            self.assertTrue(
                store.execute_command(alice, raw, actor=Actor(ash, "creation"))[1]
            )
        done, committed = store.complete_character(alice, "k-1", ash, 4)
        self.assertTrue(committed, done)
        return store

    def test_version_1_rules_without_settings_run_as_before(self):
        def problems(state, character_id):
            return TRAILHEAD_MODULE.characters.problems(state, character_id, None)

        def complete(state, character_id):
            return TRAILHEAD_MODULE.characters.complete(
                state, character_id, {"settings": {"starting_stamina_bonus": 0}}
            )

        catalog = self.catalog(
            rules={"version": 1, "problems": problems, "complete": complete,
                   "actions": None, "creation_actions": None},
            settings=None,
        )  # fmt: skip
        store = self.run_rules(catalog.resolve("trailhead"))
        self.assertEqual(self.seen, [None])  # no policy outside a policy-aware game
        self.assertIsNone(store.effective_policy())
        [walker] = store.inspect_state()["walkers"].values()
        self.assertEqual((walker["max_stamina"], "bonus" in walker), (4, False))

    def test_version_2_rules_need_settings_and_get_the_policy(self):
        with self.assertRaisesRegex(ModuleError, "needs campaign settings"):
            self.catalog(settings=None).resolve("trailhead")
        with self.assertRaisesRegex(ModuleError, "campaign settings version 2"):
            self.catalog(
                settings=replace(TRAILHEAD_MODULE.settings, version=2)
            ).resolve("trailhead")
        self.run_rules(self.catalog().resolve("trailhead"))
        self.assertEqual(
            self.seen, [{"revision": 0, "settings": {"starting_stamina_bonus": 0}}]
        )

    def test_activation_judges_a_candidate_against_the_game_now(self):
        def validate(settings, state):
            ready = any(w["ready"] for w in state["walkers"].values())
            return ["Not once a walker is on the trail."] if ready else []

        settings = replace(TRAILHEAD_MODULE.settings, validate=validate)
        runtime = self.catalog(settings=settings).resolve("trailhead")
        path = self.directory / "trail.sqlite3"
        with GameStore.create(path, runtime) as store:  # valid while nobody plays
            result, _ = CampaignRules(store).import_document(
                document(bonus=1), OPERATOR_DECIDER
            )
        store = self.run_rules(runtime, path)  # Ash is ready now
        rules = CampaignRules(store)
        with self.assertRaises(Refusal) as refused:
            rules.activate(result["revision"], 0, OPERATOR_DECIDER)
        self.assertEqual(
            refused.exception.extra["problems"],
            ["Campaign settings: Not once a walker is on the trail."],
        )
        self.assertEqual(store.policy_revision(), 0)

    def test_a_change_of_json_type_alone_makes_an_approval_stale(self):
        # A module may tell false from 0 (or 1.0 from 1); so does the host.
        settings = replace(TRAILHEAD_MODULE.settings, validate=lambda s, state: [])
        runtime = self.catalog(settings=settings).resolve("trailhead")
        store = self.enterContext(
            GameStore.create(self.directory / "trail.sqlite3", runtime)
        )
        token = new_token()
        store.add_participant("Alice", token)
        alice = store.authenticate(token)
        reply, _ = store.create_character(alice, "c-1", "Ash")
        ash = reply["result"]["character"]["character_id"]
        rules = CampaignRules(store)
        for revision, value in ((1, False), (2, 0.0)):  # from the default 0
            with self.subTest(value=value):
                result, _ = rules.import_document(
                    document(bonus=value), OPERATOR_DECIDER
                )
                rules.activate(result["revision"], revision - 1, OPERATOR_DECIDER)
                self.assertEqual(store.character(ash).draft_revision, 1 + revision)
        stale, committed = store.complete_character(alice, "k-1", ash, 1)
        self.assertEqual((stale["code"], committed), ("stale_draft", False))

    def test_a_broken_requirements_hook_is_caught(self):
        broken = CampaignSettings(1, lambda: {}, lambda s, st: [],
                                  lambda p, st, c: [{"id": "x", "kind": "class"}])  # fmt: skip
        runtime = self.catalog(settings=broken).resolve("trailhead")
        with self.assertRaises(TypeError):
            runtime.requirements({"revision": 0, "settings": {}}, {"walkers": {}}, None)
        zero = CampaignSettings(1, lambda: {}, lambda s, st: [], lambda p, st, c: [
            {"id": "x", "kind": "optional", "description": "x", "met": 0}])  # fmt: skip
        runtime = self.catalog(settings=zero).resolve("trailhead")
        with self.assertRaises(TypeError):  # 0 is not false
            runtime.requirements({"revision": 0, "settings": {}}, {"walkers": {}}, "c")

    def test_seeded_walkers_may_carry_a_starting_bonus(self):
        walker = {"trait": "keen", "rolled": 3, "stamina": 6, "max_stamina": 6,
                  "ready": True, "in_danger": False, "bonus": 2}  # fmt: skip
        owners = {"c_0123456789abcdef": "p_a1a1a1a1a1a1a1a1"}
        state = {"walkers": {"c_0123456789abcdef": walker}}
        TRAILHEAD.validate_character_seed(state, frozenset(owners.values()), owners)
        for bad in ({"bonus": 3}, {"bonus": 0}, {"max_stamina": 4}):
            with self.subTest(bad=bad), self.assertRaises(SeedError):
                walker_ = {**walker, **bad}
                TRAILHEAD.validate_character_seed(
                    {"walkers": {"c_0123456789abcdef": walker_}},
                    frozenset(owners.values()), owners,
                )  # fmt: skip


class RefereeContextTests(RulesCase):
    def test_a_job_sees_its_own_characters_policy_and_requirements_only(self):
        ash, fen = self.draft(), self.draft(self.bob, name="Fen")
        content = document(bonus=1, exceptions=[exception(fen, bonus=2) | {
            "reason": "Bob's private arrangement."}])  # fmt: skip
        self.rules.activate(self.imported(content), 0, HOST)
        referee = RefereeStore(self.store)
        referee.enable()
        submission = referee.submit(
            self.alice, "s-1", "Hello", False, actor=Actor(ash, "creation")
        )
        tools = BridgeTools(lambda: GameStore(self.path, runtime=TRAILHEAD))
        token = tools.grant(submission.job.job_id)
        context = tools.handle({"token": token, "tool": "get_context", "args": {}})[
            "result"
        ]
        self.assertEqual(
            context["policy"],
            {"revision": 1, "settings": {"starting_stamina_bonus": 1},
             "guidance": "Walkers are locals."},
        )  # fmt: skip
        self.assertEqual(context["requirements"][2]["id"], "stamina")
        self.assertTrue(context["requirements"][2]["met"])
        self.assertNotIn("private arrangement", json.dumps(context))
        actions = tools.handle({"token": token, "tool": "list_actions", "args": {}})
        self.assertTrue(actions["ok"], actions)

    def test_guidance_reaches_the_referee_without_module_settings(self):
        path = self.directory / "dice.sqlite3"
        with GameStore.create(path, CATALOG.resolve("dice")) as dice:
            token = new_token()
            dice.add_participant("Alice", token)
            alice = dice.authenticate(token)
            referee = RefereeStore(dice)
            referee.enable()
            tools = BridgeTools(lambda: GameStore(path))

            def context(request_id):
                job = referee.submit(alice, request_id, "Hello", False).job
                token = tools.grant(job.job_id)
                found = tools.handle(
                    {"token": token, "tool": "get_context", "args": {}}
                )
                tools.revoke()
                referee.finish(job.job_id, "failed", "malformed_reply")
                return found["result"]

            self.assertNotIn("policy", context("s-1"))  # nothing configured: as before
            binding = {"id": "dice", "version": "1", "state_version": 1}
            rules = CampaignRules(dice)
            result, _ = rules.import_document(
                document(module=binding), OPERATOR_DECIDER
            )
            rules.activate(result["revision"], 0, OPERATOR_DECIDER)
            found = context("s-2")
            self.assertEqual(
                (found["policy"], found["requirements"]),
                (
                    {"revision": 1, "settings": {}, "guidance": "Walkers are locals."},
                    None,
                ),
            )

    def test_a_setup_job_gets_the_campaigns_requirements(self):
        self.rules.activate(self.imported(document(bonus=2)), 0, HOST)
        referee = RefereeStore(self.store)
        referee.enable()
        submission = referee.submit(self.alice, "s-1", "Hi", False, actor=SETUP)
        tools = BridgeTools(lambda: GameStore(self.path, runtime=TRAILHEAD))
        token = tools.grant(submission.job.job_id)
        context = tools.handle({"token": token, "tool": "get_context", "args": {}})[
            "result"
        ]
        self.assertEqual(context["policy"]["settings"], {"starting_stamina_bonus": 2})
        self.assertTrue(all("met" not in e for e in context["requirements"]))


class OperatorTests(RulesCase):
    def tesserae(self, *args) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["game", "rules", *args, "--database", str(self.path)])
        return code, out.getvalue(), err.getvalue()

    def test_import_list_export_and_activate_from_files(self):
        self.store.close()
        wanted = self.directory / "rules.json"
        wanted.write_text(json.dumps(document(bonus=1)))
        code, out, _ = self.tesserae("import", "--file", str(wanted))
        self.assertEqual(code, 0)
        self.assertIn("Stored candidate revision 1; nothing live changed", out)
        code, out, _ = self.tesserae("activate", "1")
        self.assertEqual((code, out), (0, "Revision 1 is now the active settings.\n"))
        code, out, _ = self.tesserae("list")
        listed = json.loads(out)
        self.assertEqual(
            (listed["policy_revision"], listed["revisions"][0]["activated_by"]), (1, "operator")
        )  # fmt: skip
        exported = self.directory / "exported.json"
        self.assertEqual(self.tesserae("export", "--file", str(exported))[0], 0)
        self.assertEqual(json.loads(exported.read_text()), document(bonus=1))
        code, _, err = self.tesserae("export", "--file", str(exported))
        self.assertEqual(code, 1)  # never overwritten
        with GameStore.open_existing(self.path) as store:
            self.assertEqual(
                store.query("SELECT kind, host_id, player_id, via FROM host_audit"),
                [("import", None, "operator", "operator"), ("activation", None, "operator", "operator")],
            )  # fmt: skip

    def test_a_bad_file_is_refused_with_its_problems(self):
        self.store.close()
        bad = self.directory / "bad.json"
        bad.write_text(json.dumps(document(bonus=9)))
        code, out, err = self.tesserae("import", "--file", str(bad))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("cannot be used; nothing was changed", err)
        self.assertIn("- Campaign settings: The starting stamina bonus", err)
        bad.write_text("{not json")
        self.assertIn(
            "not a JSON document", self.tesserae("import", "--file", str(bad))[2]
        )
        code, _, err = self.tesserae("activate", "1")
        self.assertEqual((code, "no such revision" in err), (1, True))
        with GameStore.open_existing(self.path) as store:
            self.assertEqual(CampaignRules(store).revisions(), [])

    def test_the_operator_decider_is_distinct_from_any_host(self):
        self.assertNotEqual(OPERATOR_DECIDER.sender, HOST.sender)
        self.assertEqual(OPERATOR_DECIDER.via, "operator")


if __name__ == "__main__":
    unittest.main()
