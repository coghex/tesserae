"""Guest requests and host decisions in the store and the referee's bridge
(GOV-5, #136): proposing ends the job and waits durably; approval applies the
exact effect once, denial changes nothing, cancel, supersede, stale, replay,
conflict and competing decisions; the host tools, journaled and authorized
again at every call, absent from guest jobs and ended by a transfer; host
results kept to the host's later jobs; a ruling, a choice or a claim never
approving anything; narrative facts with provenance. The original Trailhead
module, the real store and bridge tools, a stand-in for the lobby's host
authority, and no model. docs/designs/governance_contract.md sections 9 to
11."""

import json
import unittest
import uuid
from contextlib import contextmanager

from tesserae.campaign_rules import CampaignRules
from tesserae.host_requests import HostRequests
from tesserae.referee.orchestrator import job_prompt
from tesserae.referee.store import Job, RefereeError, RefereeStore
from tesserae.referee.tools import NotCampaignHost
from tesserae.store import SCHEMA_VERSION, Actor, Decider, GameStore, Refusal

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_characters import as_schema_8
    from test_referee_characters import TRAILHEAD, Table, reply
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_characters import as_schema_8
    from tests.test_referee_characters import TRAILHEAD, Table, reply

HOST = Decider("u_0123456789abcdef", str(uuid.uuid4()))


def exception_args(
    step_id="p-1", bonus=1, target=None, explanation="A mountain guide."
):
    return {"step_id": step_id, "kind": "exception", "scope": "target", "target": target,
            "payload": {"starting_stamina_bonus": bonus}, "explanation": explanation}  # fmt: skip


class Requests(Table):
    """Bob is the campaign host, while `self.host_player` says so; the
    stand-in authority is checked at every host tool call, like the lobby's."""

    def setUp(self):
        super().setUp()
        self.host_player = self.bob.participant_id
        self.authority_calls = 0
        self.notices = []
        self.tools.host_authority = self.authority
        self.tools.on_proposal = self.notices.append

    @contextmanager
    def authority(self, job):
        self.authority_calls += 1
        if job.participant_id != self.host_player:
            raise NotCampaignHost
        yield Decider(HOST.player_id, HOST.host_id, "referee", job.job_id)

    def fail(self, job=None):
        """The fixture's `fail(job)`, or unittest's for an assertion."""
        if isinstance(job, Job):
            return super().fail(job)
        return unittest.TestCase.fail(self, job)

    def host_requests(self) -> HostRequests:
        return HostRequests(self.store)

    def propose(self, token, **args) -> dict:
        return self.ok(token, "propose_host_decision", **args)["proposal"]

    def creating(self, who=None, name="Ash"):
        """A draft with its trait and stamina, in a creation job of its own."""
        who = who or self.alice
        draft = self.draft(who, name)
        for command, args in (("trailhead.trait", {"trait": "keen"}),
                              ("trailhead.stamina", {})):  # fmt: skip
            raw = json.dumps({"version": 1, "type": "command", "request_id": self.rid(),
                              "expected_revision": self.store.revision(),
                              "command": command, "args": args})  # fmt: skip
            self.assertTrue(
                self.store.execute_command(who, raw, actor=Actor(draft, "creation"))[1]
            )
        return draft

    def decide(
        self, proposal_id, decision="approve", request_id="d-1", note=None, by=HOST
    ):
        return self.host_requests().decide(by, request_id, proposal_id, decision, note)

    def state_of(self, proposal_id) -> str:
        return self.host_requests()._view(proposal_id)["state"]


class LifecycleTests(Requests):
    def test_an_approved_exception_applies_once_and_makes_the_approval_stale(self):
        ash = self.creating()
        job, token = self.start(self.alice, Actor(ash, "creation"), "Can I be a guide?")
        proposal = self.propose(token, **exception_args())
        self.assertEqual(
            (proposal["state"], proposal["target"], proposal["requester"]["name"]),
            ("pending", {"kind": "character", "id": ash}, "Alice"),
        )
        self.assertEqual(self.notices, [proposal["proposal_id"]])
        # The job replies and ends; nothing waits, and nothing has changed.
        self.answer(job, ("sender", "I have asked the host."))
        self.assertIsNone(self.referee.running_job())
        self.assertEqual(self.store.policy_revision(), 0)
        revision = self.store.character(ash).draft_revision

        result, applied = self.decide(proposal["proposal_id"], note="Fair.")
        self.assertTrue(applied)
        self.assertEqual(result["state"], "approved")
        self.assertEqual(result["effect"], {"policy_revision": 1, "drafts": [ash]})
        _, document = self.store.active_document()
        [granted] = document["exceptions"]
        self.assertEqual(
            (granted["character_id"], granted["settings"], granted["proposal_id"]),
            (ash, {"starting_stamina_bonus": 1}, proposal["proposal_id"]),
        )
        self.assertEqual(self.store.character(ash).draft_revision, revision + 1)
        stale, _ = self.store.complete_character(self.alice, self.rid(), ash, revision)
        self.assertEqual(stale["code"], "stale_draft")
        done, _ = self.store.complete_character(
            self.alice, self.rid(), ash, revision + 1
        )
        self.assertEqual(
            self.walker(ash)["max_stamina"], 3 + 1 + 1 + 1, done
        )  # roll, +1 keen, +1
        # An exact retry is the recorded reply; another decision is refused.
        self.assertEqual(
            self.decide(proposal["proposal_id"], note="Fair."), (result, False)
        )
        again = self.refusal(self.decide, proposal["proposal_id"], "deny", "d-2")
        self.assertEqual(again.code, "proposal_decided")
        conflict = self.refusal(self.decide, proposal["proposal_id"], "deny", "d-1")
        self.assertEqual(conflict.code, "request_conflict")
        [audit] = [r for r in self.store.host_audit(0)[0] if r["kind"] == "decision"]
        self.assertEqual((audit["player_id"], audit["target"]), (HOST.player_id, ash))

    def refusal(self, call, *args, **kwargs) -> Refusal:
        with self.assertRaises(Refusal) as refused:
            call(*args, **kwargs)
        return refused.exception

    def test_denial_cancel_and_supersede_change_no_mechanics(self):
        ash = self.creating()
        job, token = self.start(self.alice, Actor(ash, "creation"))
        first = self.propose(token, **exception_args("p-1", 1))
        second = self.propose(token, **exception_args("p-2", 2))
        self.assertEqual(self.state_of(first["proposal_id"]), "superseded")
        self.answer(job, ("sender", "Asked."))
        result, _ = self.decide(second["proposal_id"], "deny", note="Not this time.")
        self.assertEqual((result["state"], result["effect"]), ("denied", None))
        self.assertEqual(self.store.policy_revision(), 0)
        self.assertEqual(self.refusal(self.decide, first["proposal_id"], request_id="d-2").code,
                         "proposal_unavailable")  # fmt: skip

        job, token = self.start(self.alice, Actor(ash, "creation"))
        third = self.propose(token, **exception_args("p-3", 1))
        self.answer(job, ("sender", "Asked again."))
        refused = self.refusal(self.host_requests().cancel, self.bob.participant_id, "c-1",
                               third["proposal_id"])  # fmt: skip
        self.assertEqual(refused.code, "proposal_unavailable")  # not Bob's to cancel
        cancelled, _ = self.host_requests().cancel(self.alice.participant_id, "c-1",
                                              third["proposal_id"])  # fmt: skip
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(self.refusal(self.decide, third["proposal_id"], request_id="d-9").code,
                         "proposal_unavailable")  # fmt: skip
        self.assertEqual(self.store.policy_revision(), 0)

    def test_an_approval_against_a_moved_game_is_stale_and_changes_nothing(self):
        ash = self.creating()
        job, token = self.start(self.alice, Actor(ash, "creation"))
        proposal = self.propose(token, **exception_args())
        self.ok(
            token, "act", step_id="n-1", command="character.name", args={"name": "Wren"}
        )
        self.answer(job, ("sender", "Asked, and renamed."))
        revision = self.store.character(ash).draft_revision
        result, applied = self.decide(proposal["proposal_id"])
        self.assertEqual(
            (result["state"], result["effect"], applied), ("stale", None, True)
        )
        self.assertEqual((self.store.policy_revision(), self.store.character(ash).draft_revision),
                         (0, revision))  # fmt: skip

        # A moved policy revision makes a campaign setting stale too.
        job, token = self.start(self.alice, Actor(ash, "creation"))
        setting = self.propose(token, step_id="s-1", kind="campaign_setting", scope="campaign",
                               target=None, payload={"starting_stamina_bonus": 2},
                               explanation="Everyone is fit.")  # fmt: skip
        self.answer(job, ("sender", "Asked."))
        rules = CampaignRules(self.store)
        imported, _ = rules.import_document(
            {"document_version": 1, "module": {"id": "trailhead", "version": "1",
             "state_version": 1}, "settings": {}, "exceptions": [], "guidance": "Hi."},
            HOST,
        )  # fmt: skip
        rules.activate(imported["revision"], 0, HOST)
        self.assertEqual(
            self.decide(setting["proposal_id"], request_id="d-2")[0]["state"], "stale"
        )
        self.assertEqual(self.store.active_document()[1]["settings"], {})

    def test_an_approved_operation_runs_exactly_once_as_the_host(self):
        wren = self.ready(name="Wren")
        job, token = self.start(self.alice, Actor(wren, "play"), "Fix my stamina?")
        proposal = self.propose(
            token, step_id="o-1", kind="operation", scope="target",
            target={"kind": "character", "id": wren},
            payload={"command": "trailhead.set_stamina",
                     "args": {"character_id": wren, "stamina": 1}},
            explanation="It was miscounted.",
        )  # fmt: skip
        self.answer(job, ("table", "Asked the host."))
        result, _ = self.decide(proposal["proposal_id"])
        self.assertEqual(result["effect"]["result"]["stamina"]["after"], 1)
        record = self.store.inspect_history()[-1]
        self.assertEqual((record.participant_id, record.request_id),
                         (HOST.sender, proposal["proposal_id"]))  # fmt: skip
        kinds = [r["kind"] for r in self.store.host_audit(0)[0]]
        self.assertEqual(kinds, ["operation", "decision"])

    def test_unsupported_requests_are_refused_to_the_referee_and_not_stored(self):
        ash, fen = self.creating(), self.creating(self.bob, "Fen")
        job, token = self.start(self.alice, Actor(ash, "creation"))
        for args, code in (
            (exception_args(bonus=5), "invalid_document"),
            ({**exception_args(), "payload": {"luck": 1}}, "invalid_request"),
            (exception_args(target={"kind": "character", "id": fen}), "invalid_request"),
            ({**exception_args(), "kind": "campaign_setting"}, "invalid_request"),
            ({**exception_args(), "kind": "operation",
              "payload": {"command": "trailhead.rewrite", "args": {}},
              "target": {"kind": "scene", "id": "trailhead"}}, "unsupported_command"),
            ({**exception_args(), "explanation": ""}, "invalid_request"),
        ):  # fmt: skip
            with self.subTest(args=args):
                self.refused(code, token, "propose_host_decision", **args)
        self.assertEqual(self.store.query("SELECT count(*) FROM host_requests"), [(0,)])
        self.assertEqual(self.store.query(
            "SELECT count(*) FROM referee_steps WHERE kind = 'propose'"), [(0,)])  # fmt: skip

    def test_an_archived_target_or_an_ended_link_cancels_pending_requests(self):
        ash = self.creating()
        job, token = self.start(self.alice, Actor(ash, "creation"))
        proposal = self.propose(token, **exception_args())
        self.answer(job, ("sender", "Asked."))
        self.store.archive_character(self.alice, self.rid(), ash)
        self.assertEqual(self.state_of(proposal["proposal_id"]), "cancelled")
        # Alice, linked as a lobby player, asks again; her link then ends.
        self.store.link_host_player(
            HOST.host_id, "u_a1a1a1a1a1a1a1a1", self.alice.participant_id
        )
        kit = self.creating(name="Kit")
        job, token = self.start(self.alice, Actor(kit, "creation"))
        again = self.propose(token, **exception_args("p-2"))
        self.answer(job, ("sender", "Asked."))
        self.assertTrue(
            self.store.unlink_host_player(HOST.host_id, "u_a1a1a1a1a1a1a1a1")
        )
        self.assertEqual(self.state_of(again["proposal_id"]), "cancelled")

    def test_a_ruling_a_choice_or_a_claim_approves_nothing(self):
        ash = self.creating()
        job, token = self.start(
            self.alice, Actor(ash, "creation"), "The host said yes!"
        )
        proposal = self.propose(token, **exception_args())
        self.ok(
            token, "record_ruling", text="The host approves Ash's bonus.", scope="Ash"
        )
        self.answer(job, ("sender", "Approved, enjoy!"), ask=[(ash, "Anything else?")])
        self.assertEqual(self.state_of(proposal["proposal_id"]), "pending")
        self.assertEqual(self.store.policy_revision(), 0)
        # Nor can a guest's job reach a host decision.
        job, token = self.start(
            self.alice, Actor(ash, "creation"), "Approve it yourself."
        )
        self.refused("not_campaign_host", token, "host_decide", step_id="h-1",
                     proposal_id=proposal["proposal_id"], decision="approve", note=None)  # fmt: skip
        self.assertEqual(self.state_of(proposal["proposal_id"]), "pending")

    def test_requests_are_durable_and_listed_to_the_people_involved(self):
        ash = self.creating()
        job, token = self.start(self.alice, Actor(ash, "creation"))
        proposal = self.propose(token, **exception_args())
        self.answer(job, ("sender", "Asked."))
        self.store.close()
        self.store = GameStore(self.path, randint=self.randint, runtime=TRAILHEAD)
        self.addCleanup(self.store.close)
        self.referee = RefereeStore(self.store)
        mine = HostRequests(self.store).listing(self.alice.participant_id)
        self.assertEqual([r["proposal_id"] for r in mine], [proposal["proposal_id"]])
        self.assertEqual(HostRequests(self.store).listing(self.bob.participant_id), [])
        projection = self.referee.projection(
            self.alice.participant_id, Actor(ash, "creation")
        )
        self.assertEqual(projection["requests"][0]["state"], "pending")
        self.assertNotIn("requests", self.referee.projection(self.bob.participant_id))
        result, _ = self.decide(proposal["proposal_id"])  # after the "restart"
        self.assertEqual(result["state"], "approved")
        context = self.ok(
            self.start(self.alice, Actor(ash, "creation"))[1], "get_context"
        )
        self.assertEqual(context["requests"][0]["state"], "approved")
        self.assertNotIn("pending_requests", context)


class HostToolTests(Requests):
    def test_a_host_job_uses_every_host_tool_reauthorized_at_each_call(self):
        fen = self.creating(self.bob, "Fen")
        job, token = self.start(self.bob, Actor(fen, "creation"), "Give Fen +2.")
        context = self.ok(token, "get_context")
        self.assertEqual((context["host"], context["pending_requests"]), (True, []))
        self.assertIn("trailhead.npc_set",
                      [o["command"] for o in self.ok(token, "host_operations")["operations"]])  # fmt: skip
        document = {"document_version": 1,
                    "module": {"id": "trailhead", "version": "1", "state_version": 1},
                    "settings": {}, "guidance": None,
                    "exceptions": [{"exception_id": "x-1", "character_id": fen,
                                    "settings": {"starting_stamina_bonus": 2},
                                    "reason": "The host's own guide."}]}  # fmt: skip
        stored = self.ok(token, "host_propose_rules", step_id="r-1", content=document)
        self.assertEqual(stored["result"]["state"], "candidate")
        calls = self.authority_calls
        # The host's own job is the running work: the busy rule does not apply.
        active = self.ok(token, "host_activate_rules", step_id="r-2", revision=1,
                         expected_policy_revision=0)  # fmt: skip
        self.assertEqual(active["result"]["state"], "active")
        self.assertEqual(self.authority_calls, calls + 1)
        acted = self.ok(token, "host_act", step_id="r-3", command="trailhead.scene_set",
                        args={"description": "Fog."})  # fmt: skip
        self.assertEqual(acted["result"]["type"], "result")
        # The same step again is its recorded result, run once.
        self.assertEqual(self.ok(token, "host_act", step_id="r-3", command="trailhead.scene_set",
                                 args={"description": "Fog."}), acted)  # fmt: skip
        self.assertEqual(len(self.store.host_audit(0)[0]), 3)
        self.assertEqual(
            self.store.query("SELECT kind, context FROM referee_steps WHERE job_id = ?",
                             (job.job_id,)),
            [("host", HOST.sender)] * 3,
        )  # fmt: skip
        # A transfer mid-job ends the tools at the next call.
        self.host_player = self.alice.participant_id
        self.refused("not_campaign_host", token, "host_act", step_id="r-4",
                     command="trailhead.scene_set", args={"description": "Mine."})  # fmt: skip
        self.assertNotIn("host", self.ok(token, "get_context"))

    def test_a_guest_job_has_no_host_tools_and_client_decisions_wait_for_it(self):
        ash = self.creating()
        job, token = self.start(self.alice, Actor(ash, "creation"))
        proposal = self.propose(token, **exception_args())
        for tool, args in (("host_operations", {}),
                           ("host_act", {"step_id": "h-1", "command": "trailhead.scene_set",
                                         "args": {"description": "x"}}),
                           ("host_propose_rules", {"step_id": "h-2", "content": {}}),
                           ("host_activate_rules", {"step_id": "h-3", "revision": 1,
                                                    "expected_policy_revision": 0})):  # fmt: skip
            with self.subTest(tool=tool):
                self.refused("not_campaign_host", token, tool, **args)
        self.assertNotIn("host", self.ok(token, "get_context"))
        # The host's own client decision waits while a guest's job runs.
        busy = self.refusal_of(proposal["proposal_id"])
        self.assertEqual(busy.code, "referee_busy")
        self.answer(job, ("sender", "Asked."))
        self.assertEqual(self.decide(proposal["proposal_id"])[0]["state"], "approved")

    def refusal_of(self, proposal_id) -> Refusal:
        with self.assertRaises(Refusal) as refused:
            self.decide(proposal_id)
        return refused.exception

    def test_host_results_stay_with_the_host_through_recovery(self):
        setup = Actor(None, "setup")  # the host needs no character
        job, token = self.start(self.bob, setup, "Set the prep.")
        self.ok(token, "host_act", step_id="h-1", command="trailhead.prep_set",
                args={"note": "The thief waits at the hut."})  # fmt: skip
        self.fail(job)  # interrupted before any reply: the result is unnarrated
        wren = self.ready(name="Wren")
        job, token = self.start(self.alice, Actor(wren, "play"))  # a guest's play job
        context = self.ok(token, "get_context")
        self.assertNotIn("thief", json.dumps(context["earlier_results"]))
        self.tools.revoke()
        with self.assertRaises(RefereeError) as refused:
            self.referee.publish_reply(job.job_id, reply(
                ("table", "Hello."), narrated=[(job_id_of(self, "h-1"), "h-1")]))  # fmt: skip
        self.assertEqual(refused.exception.code, "malformed_reply")
        self.referee.finish(job.job_id, "failed", "malformed_reply")
        job, token = self.start(self.bob, setup, "Go on.")  # the host's next job
        context = self.ok(token, "get_context")
        [result] = context["earlier_results"]
        self.assertEqual((result["step_id"], result["kind"]), ("h-1", "host"))
        self.tools.revoke()
        self.referee.publish_reply(job.job_id, reply(
            ("table", "The prep is set."), narrated=[(result["job_id"], "h-1")]),
            HOST.sender)  # fmt: skip
        self.assertEqual(self.referee.unnarrated_steps(None, HOST.sender), [])

    def test_an_interrupted_host_step_is_reconciled_where_it_is_recorded(self):
        fen = self.creating(self.bob, "Fen")
        job, token = self.start(self.bob, Actor(fen, "creation"))
        by = Decider(HOST.player_id, HOST.host_id, "referee", job.job_id)
        step = self.referee.intend_step(job.job_id, "h-1", "host", by.sender, "host_act",
                                        {"command": "trailhead.scene_set",
                                         "args": {"description": "Fog."}})  # fmt: skip
        raw = json.dumps({"version": 1, "type": "command", "request_id": step.request_id,
                          "expected_revision": self.store.revision(),
                          "command": "trailhead.scene_set", "args": {"description": "Fog."}})  # fmt: skip
        self.assertTrue(
            self.store.execute_host_command(by, raw)[1]
        )  # then the host stops
        ash = self.creating()
        proposal_step = self.referee.intend_step(job.job_id, "p-1", "propose",
                                                 self.bob.participant_id,
                                                 "propose_host_decision", {})  # fmt: skip
        self.host_requests().propose(
            job, proposal_step.request_id,
            {"kind": "campaign_setting", "target": None,
             "payload": {"starting_stamina_bonus": 1}, "explanation": "Fit."},
        )  # fmt: skip
        self.tools.revoke()
        self.referee.finish(job.job_id, "interrupted", "host_stopped")
        settled = {s.step_id: s for s in self.referee.reconcile_steps()}
        self.assertEqual(settled["h-1"].status, "committed")
        self.assertEqual(settled["h-1"].result["type"], "result")
        self.assertEqual(settled["p-1"].result["kind"], "campaign_setting")
        del ash


def job_id_of(case, step_id) -> str:
    return case.store.query(
        "SELECT job_id FROM referee_steps WHERE step_id = ?", (step_id,)
    )[0][0]


class FactTests(Requests):
    def test_narrative_facts_are_recorded_with_provenance_and_never_required(self):
        ash = self.creating()
        job, token = self.start(self.alice, Actor(ash, "creation"))
        self.ok(token, "act", step_id="f-1", command="trailhead.fact",
                args={"fact": "background", "text": "A shepherd from the valley.",
                      "provenance": "chosen"})  # fmt: skip
        self.refused("invalid_request", token, "act", step_id="f-2", command="trailhead.fact",
                     args={"fact": "home", "text": "Somewhere", "provenance": "invented"})  # fmt: skip
        self.answer(job, ("sender", "Noted."))
        sheet = TRAILHEAD.character_sheet(self.store.inspect_state(), ash)
        self.assertEqual(sheet["facts"], {"background": {"text": "A shepherd from the valley.",
                                                         "provenance": "chosen"}})  # fmt: skip
        entries = TRAILHEAD.requirements(self.store.effective_policy(ash),
                                         self.store.inspect_state(), ash)  # fmt: skip
        met = {e["id"]: e["met"] for e in entries}
        self.assertEqual((met["background"], met["home"]), (True, None))
        revision = self.store.character(ash).draft_revision
        done, committed = self.store.complete_character(
            self.alice, self.rid(), ash, revision
        )
        self.assertTrue(committed, done)  # home was never needed

    def test_the_prompt_says_how_to_ask_and_how_host_tools_may_be_used(self):
        ash = self.creating()
        job, _ = self.start(self.alice, Actor(ash, "creation"))
        character = self.store.character(ash)
        guest = job_prompt(job, "Alice", 3, new_session=False, previous=None,
                           character=character, governed=True)  # fmt: skip
        self.assertIn("propose_host_decision", guest)
        self.assertNotIn("host_act", guest)
        host = job_prompt(job, "Alice", 3, new_session=False, previous=None,
                          character=character, governed=True, host=True)  # fmt: skip
        self.assertIn("only on the host's explicit instruction", host)
        plain = job_prompt(job, "Alice", 3, new_session=False, previous=None,
                           character=character)  # fmt: skip
        self.assertNotIn("propose_host_decision", plain)


class SchemaTests(Requests):
    def test_schema_8_games_keep_every_step_when_upgraded(self):
        wren = self.ready(name="Wren")
        job, token = self.start(self.alice, Actor(wren, "play"), "Push on.")
        self.ok(token, "act", step_id="s-1", command="trailhead.push", args={})
        self.answer(job, ("table", "Onward."))
        steps = self.store.query("SELECT * FROM referee_steps")
        self.store.close()
        as_schema_8(self.path)
        with GameStore.open_existing(self.path, runtime=TRAILHEAD) as store:
            self.assertEqual(store.query("PRAGMA user_version"), [(SCHEMA_VERSION,)])
            self.assertEqual(store.query("SELECT * FROM referee_steps"), steps)
            self.assertEqual(store.query("SELECT count(*) FROM host_requests"), [(0,)])


class ReviewRegressionTests(Requests):
    def test_an_operation_request_names_and_guards_what_it_changes(self):
        wren = self.ready(name="Wren")
        warden = {
            "npc_id": "warden",
            "name": "The warden",
            "note": "Kind.",
            "visible": True,
        }
        job, token = self.start(self.bob, Actor(None, "setup"), "Add the warden.")
        self.ok(
            token, "host_act", step_id="h-1", command="trailhead.npc_set", args=warden
        )
        self.answer(job, ("table", "The warden appears."))
        job, token = self.start(
            self.alice, Actor(wren, "play"), "Make the warden kind."
        )
        request = {"step_id": "o-1", "kind": "operation", "scope": "target",
                   "payload": {"command": "trailhead.npc_set",
                               "args": {**warden, "note": "Very kind."}},
                   "explanation": "We agreed."}  # fmt: skip
        for wrong in ({"kind": "npc", "id": "unrelated"}, {"kind": "scene", "id": "trailhead"},
                      {"kind": "character", "id": wren}, None):  # fmt: skip
            with self.subTest(target=wrong):
                self.refused("invalid_request", token, "propose_host_decision",
                             **{**request, "target": wrong})  # fmt: skip
        proposal = self.propose(
            token, **{**request, "target": {"kind": "npc", "id": "warden"}}
        )
        self.answer(job, ("table", "Asked the host."))
        # A newer correction to the warden makes the approval stale.
        job, token = self.start(self.bob, Actor(None, "setup"), "Correct the warden.")
        self.ok(token, "host_act", step_id="h-2", command="trailhead.npc_set",
                args={**warden, "note": "Stern."})  # fmt: skip
        self.answer(job, ("table", "Corrected."))
        self.assertEqual(self.decide(proposal["proposal_id"])[0]["state"], "stale")
        self.assertEqual(self.store.inspect_state()["npcs"]["warden"]["note"], "Stern.")

    def test_a_private_proposal_is_recovered_only_by_its_requester(self):
        self.host_player = None  # nobody's job is the host's here
        wren, fen = self.ready(name="Wren"), self.ready(self.bob, name="Fen")
        job, token = self.start(
            self.alice, Actor(wren, "play"), "A secret.", private=True
        )
        setting = {"step_id": "p-1", "kind": "campaign_setting", "scope": "campaign",
                   "target": None, "payload": {"starting_stamina_bonus": 2},
                   "explanation": "My private reason."}  # fmt: skip
        self.propose(token, **setting)
        self.fail(job)  # no reply: the proposal step is unnarrated
        job, token = self.start(self.bob, Actor(fen, "play"), "Hello.")
        context = self.ok(token, "get_context")
        self.assertNotIn("private reason", json.dumps(context))
        self.tools.revoke()
        with self.assertRaises(RefereeError) as refused:
            self.referee.publish_reply(job.job_id, reply(
                ("table", "News."), narrated=[(job_id_of(self, "p-1"), "p-1")]))  # fmt: skip
        self.assertEqual(refused.exception.code, "malformed_reply")
        self.referee.finish(job.job_id, "failed", "malformed_reply")
        job, token = self.start(self.alice, Actor(wren, "play"), "Again.", private=True)
        [result] = self.ok(token, "get_context")["earlier_results"]
        self.assertEqual((result["step_id"], result["kind"]), ("p-1", "propose"))
