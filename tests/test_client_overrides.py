"""The streaming client's controls for negotiated creation overrides (OVR-5,
#177): focused parsing and rendering checks, and one scripted workflow of
two `tesserae connect --lobby` processes against a real lobby host: a guest
asks, the host reviews and counteroffers with no character, the guest
reviews and accepts the exact offer, the host applies a change directly, a
stale confirmation is refused with nothing applied, and history shows what
applied. Trail is an original Trailhead campaign; nothing calls a model.
docs/designs/creation_overrides_contract.md section 12."""

import json
import re
import tempfile
import unittest
from pathlib import Path

from test_client_characters import ALICE, TRAILHEAD, seed
from test_client_governance import GovernedTrail
from test_referee_host import Client

from tesserae import override_lines
from tesserae.client import _Client, _with_intent
from tesserae.credentials import new_token, write_token_file
from tesserae.referee.store import RefereeStore
from tesserae.registry import HostRegistry
from tesserae.seed import parse_seed
from tesserae.store import Decider, GameStore

PROPOSAL = re.compile(r"hp-[0-9a-f]{12}")
OTHER = Decider("u_ffffffffffffffff", "h_ffffffffffffffff")
OFFER = re.compile(r"ho-[0-9a-f]{12}")
ONE = '{"changes": [{"quality_id": "max_stamina_modifier", "action_id": "set", "parameters": {"amount": 1}}]}'
TWO = ONE.replace('"amount": 1', '"amount": 2')
ROPE = '{"changes": [{"quality_id": "starting_gear", "action_id": "grant", "parameters": {"item": "climbing rope"}}]}'


class ParsingTests(unittest.TestCase):
    def client(self, role="guest", draft="c_0123456789abcdef") -> _Client:
        client = _Client("ws://x", "t", None, 1.0)
        client.lobby = True
        client.selection = {"campaign_id": "g", "name": "Trail"}
        client.session = ("g", ALICE)
        client.role = role
        client.overrides_on = True
        client.categories = [{"category_id": "endurance", "label": "Endurance",
                              "policy": "approval_required"}]  # fmt: skip
        client.actor = {"mode": "creation", "character": {
            "character_id": draft, "name": "Ash", "phase": "draft",
            "draft_revision": 3}}  # fmt: skip
        return client

    def sent(self, client, line) -> dict:
        text = client._translate_lobby(line)
        self.assertIsNotNone(text, line)
        return json.loads(text)

    def test_commands_become_exact_messages(self):
        client = self.client()
        request = self.sent(client, f"/override request {ONE} A guide by trade.")
        self.assertEqual(
            {k: v for k, v in request.items() if k != "request_id"},
            {"version": 1, "type": "override_request",
             "character_id": "c_0123456789abcdef", "intent": json.loads(ONE),
             "explanation": "A guide by trade."},
        )  # fmt: skip
        revised = self.sent(client, f"/override request {ONE} Again. --revises hp-1")
        self.assertEqual(
            (revised["explanation"], revised["revises"]), ("Again.", "hp-1")
        )
        self.assertEqual(
            self.sent(client, "/overrides endurance")["query"],
            {"category_id": "endurance"},
        )
        self.assertEqual(
            self.sent(client, "/overrides c_1 starting_gear item rope")["query"],
            {"quality_id": "starting_gear", "parameter": "item", "match": "rope"},
        )
        self.assertEqual(
            self.sent(client, "/requests overrides open"),
            {"version": 1, "type": "override_requests", "states": "open"},
        )
        decline = self.sent(client, "/decline ho-1 Not that.")
        self.assertEqual(
            (decline["response"], decline["note"]), ("decline", "Not that.")
        )
        # A confirmation sends exactly what was reviewed, and nothing before.
        self.assertIsNone(client._translate_lobby("/accept ho-1 confirm"))
        client.pending = ("accept", "ho-1", "abc")
        accepted = self.sent(client, "/accept ho-1 confirm")
        self.assertEqual(
            (accepted["type"], accepted["offer_id"], accepted["preview_digest"]),
            ("override_respond", "ho-1", "abc"),
        )
        self.assertIsNone(client.pending)

    def test_host_verdicts_and_the_legacy_decide(self):
        client = self.client(role="host")
        client.override_ids.add("hp-0123456789ab")
        self.assertIsNone(client._translate_lobby("/decide hp-0123456789ab approve"))
        denied = self.sent(client, "/decide hp-0123456789ab deny Not here.")
        self.assertEqual((denied["type"], denied["decision"], denied["note"]),
                         ("override_decide", "deny", "Not here."))  # fmt: skip
        legacy = self.sent(client, "/decide hp-ffffffffffff approve")
        self.assertEqual(
            (legacy["type"], legacy["decision"]), ("request_decide", "approve")
        )
        client.pending = ("decide", "hp-0123456789ab", "counteroffer", "d1",
                          json.loads(TWO), "Two.")  # fmt: skip
        decided = self.sent(client, "/decide hp-0123456789ab confirm")
        self.assertEqual(
            (decided["decision"], decided["preview_digest"], decided["intent"]),
            ("counteroffer", "d1", json.loads(TWO)),
        )
        self.assertIsNone(client._translate_lobby("/override apply confirm"))
        self.assertIsNone(client._translate_lobby(f"/override apply {ROPE} Gift."))

    def test_intent_parsing(self):
        self.assertEqual(_with_intent(f"c_1 {ONE} why not"),
                         ("c_1", json.loads(ONE), "why not"))  # fmt: skip
        self.assertIsNone(_with_intent("c_1 {not json} x"))
        self.assertIsNone(_with_intent("no intent"))

    def test_lines_say_who_is_awaited_and_never_call_an_offer_applied(self):
        request = {"proposal_id": "hp-1", "target": {"kind": "character", "id": "c_1"},
                   "requester": {"participant_id": "p_1", "name": "Alice"},
                   "state": "awaiting_player", "categories": ["endurance"],
                   "categories_more": 0, "explanation": "A guide.",
                   "offer": {"offer_id": "ho-1", "offer_revision": 2, "state": "open",
                             "current": True, "stale_reason": None,
                             "headline": "Sets the modifier."}}  # fmt: skip
        guest = override_lines.request_line(request, "guest")
        self.assertIn("offer 2 waiting for you, not applied yet", guest)
        self.assertIn(
            "waiting for the player", override_lines.request_line(request, "host")
        )
        stale = dict(request, offer={**request["offer"], "current": False,
                                     "stale_reason": "host_changed"})  # fmt: skip
        self.assertIn("no longer holds (host_changed)",
                      override_lines.request_line(stale, "guest"))  # fmt: skip
        self.assertIn("waiting for the host", override_lines.request_line(
            dict(request, state="pending_host", offer=None), "guest"))  # fmt: skip
        error = {"code": "stale_preview", "preview": {
            "headline": "Adds a rope.", "effects": [], "waivers": [], "allowances": [],
            "later_steps": [], "consequences": [], "limitations": []},
            "preview_digest": "f" * 64}  # fmt: skip
        self.assertIn("nothing was applied", override_lines.error_lines(error)[0])


class WorkflowTests(unittest.TestCase):
    """Trail negotiates overrides: Bob is its campaign host with no
    character, Alice a guest with Mara and a new draft."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry_path = self.root / "host.sqlite3"
        self.trail = self.root / "trail.sqlite3"
        self.files = {}
        with GameStore.create(self.trail, TRAILHEAD, seed=parse_seed(seed())) as store:
            RefereeStore(store).enable()
        with HostRegistry.create(self.registry_path) as registry:
            players = {}
            for name in ("alice", "bob"):
                token = new_token()
                players[name] = registry.add_player(name.title(), token)
                self.files[name] = self.root / f"{name}.token"
                write_token_file(self.files[name], token)
            self.campaign = registry.register_campaign(self.trail, "Trail",
                                                       players["bob"])  # fmt: skip
            registry.grant(self.campaign, players["alice"])
            registry.set_campaign_active(self.campaign, True)
            host_id = registry.host_id
        with GameStore.open_existing(self.trail) as store:
            store.link_host_player(host_id, players["alice"], ALICE)
        self.host = GovernedTrail(self.registry_path, self.trail)
        self.addCleanup(self.host.close)

    def entered(self, name) -> Client:
        client = Client(self, self.host.uri, self.files[name], "--lobby",
                        "--campaign", self.campaign)  # fmt: skip
        client.until("[status]")
        return client

    def walker(self, character_id) -> dict:
        with GameStore.open_existing(self.trail) as store:
            return store.inspect_state()["walkers"][character_id]

    def test_ask_review_counteroffer_accept_apply_and_stale(self):
        bob = self.entered("bob")
        alice = self.entered("alice")
        alice.type("/create")
        alice.until("[status]")
        alice.type("build Ash keen")
        alice.until("[idle]")
        with GameStore.open_existing(self.trail) as store:
            [ash] = [c.character_id for c in store.inspect_characters()
                     if c.phase == "draft"]  # fmt: skip

        alice.type("/overrides")
        listed = alice.until("[overrides] starting_gear")
        self.assertTrue(any("max_stamina_modifier (endurance), now 0" in line
                            for line in listed))  # fmt: skip
        alice.type(f"/override preview {ONE}")
        self.assertTrue(
            alice.line().startswith("[preview] Sets the maximum stamina modifier")
        )
        alice.type(f"/override request {ONE} A mountain guide by trade.")
        asked = alice.until("[request]")[-1]
        self.assertTrue(asked.endswith("is now pending_host."), asked)
        proposal_id = PROPOSAL.search(asked).group()
        bob.until(f"[request] {proposal_id} is now pending_host.")
        self.assertNotIn("overrides", self.walker(ash))

        # The host, with no character, lists and reviews it.
        bob.type("/requests")
        lines = bob.until(f"[override] {proposal_id}")
        self.assertIn("[request] No requests.", lines)  # legacy, unchanged
        self.assertIn("waiting for the host: Alice asks for", lines[-1])
        bob.type(f"/decide {proposal_id} counter {TWO} One more is fair.")
        lines = bob.until("[request] Counteroffer this")
        self.assertTrue(any(line.startswith("[preview] Sets the maximum stamina "
                                            "modifier from +0 to +2") for line in lines))  # fmt: skip
        bob.type(f"/decide {proposal_id} confirm")
        decided = bob.until(f"[request] {proposal_id} is now awaiting_player")[-1]
        self.assertIn("nothing is applied yet", decided)
        offer_id = OFFER.search(decided).group()
        alice.until(f"[request] {proposal_id} is now awaiting_player.")
        self.assertNotIn("overrides", self.walker(ash))  # an offer is not applied

        # The guest reviews the exact offer, and only then accepts it.
        alice.type("/requests overrides")
        self.assertIn("offer 1 waiting for you, not applied yet",
                      alice.until(f"[override] {proposal_id}")[-1])  # fmt: skip
        alice.type(f"/accept {offer_id}")
        lines = alice.until("[offer] Accept offer 1")
        self.assertIn("(counteroffer), open, not applied yet", lines[0])
        self.assertIn('note: "One more is fair."', lines[0])
        alice.type(f"/accept {offer_id} confirm")
        applied = alice.until(f"[request] {proposal_id} is now applied")[-1]
        self.assertIn("Applied at revision", applied)
        self.assertEqual(
            self.walker(ash)["overrides"]["max_stamina_modifier"]["amount"], 2
        )

        # The host applies a change directly; a moved draft is refused stale.
        bob.type(f"/override apply {ash} {ROPE} A gift from the hut.")
        bob.until("[override] Apply this to")
        # Meanwhile another connection changes the draft's gear.
        with GameStore.open_existing(self.trail) as store:
            raw = json.dumps({"version": 1, "type": "command", "request_id": "g-1",
                              "expected_revision": store.revision(),
                              "command": "trailhead.set_gear",
                              "args": {"character_id": ash, "gear": ["map"]}})  # fmt: skip
            self.assertTrue(store.execute_host_command(OTHER, raw)[1])
        bob.type("/override apply confirm")
        lines = bob.until("[override] Nothing was applied")
        self.assertTrue(any("It would now do this instead" in line for line in lines))
        self.assertEqual(self.walker(ash)["gear"], ["map"])  # no rope: not resent
        bob.type(f"/override apply {ash} {ROPE} A gift from the hut.")
        bob.until("[override] Apply this to")
        bob.type("/override apply confirm")
        self.assertTrue(bob.until("[override] Applied to")[-1].startswith(
            f"[override] Applied to {ash}"))  # fmt: skip
        self.assertEqual(self.walker(ash)["gear"], ["map", "climbing rope"])

        alice.type("/override history")
        history = alice.until("[override] hr-")
        history += [alice.line()]
        self.assertEqual([line.split()[2] for line in history[-2:]],
                         ["accepted_offer", "direct"])  # fmt: skip
        # The guest cannot give a verdict; the host cannot accept for her.
        alice.type(f"/decide {proposal_id} deny No.")
        self.assertTrue(alice.line().startswith("[error]"))
        bob.type(f"/decline {offer_id} Not mine.")
        self.assertTrue(bob.line().startswith("[error]"))


if __name__ == "__main__":
    unittest.main()
