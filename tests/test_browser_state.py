"""The player's state and pending question in the browser (#86, WEB-4): the
Relay panel, only one's own worker, refreshing on revisions, a question with
no revision change, its resolution, and the generic tree for an unfamiliar
module. The scripted referee acts through the real bridge tools.
docs/designs/browser_contract.md sections 7 and 10.

Like test_browser_shell, these skip without Playwright and Chromium unless
TESSERAE_BROWSER_TESTS=1."""

import unittest

from tesserae.builtin import CATALOG, DICE_PROVIDER
from tesserae.modules import Catalog, Module
from tesserae.referee.tools import BridgeTools
from tesserae.store import GameStore, ParticipantContext

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_browser_conversation import ConversationCase, reply
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_browser_conversation import ConversationCase, reply

try:
    from playwright.sync_api import expect
except ImportError:
    expect = None

RELAY = CATALOG.resolve("relay")


def asking(*messages, ask=(), resolve=()):
    answer = reply(*messages)
    answer["choices"] = {
        "ask": [{"participant_id": p, "question": q} for p, q in ask],
        "resolve": list(resolve),
    }
    return answer


class RelayCase(ConversationCase):
    runtime = RELAY

    def acting(self, *commands, answer):
        """A scripted job that runs `commands` for its sender through the
        bridge tools, as Codex would, then replies `answer`."""
        host = self.host

        def plan(job):
            tools = BridgeTools(
                lambda: GameStore(self.database, load_runtime=True),
                on_commit=lambda revision: host.loop.call_soon_threadsafe(
                    host.game._publish, revision
                ),
            )
            token = tools.grant(job.job_id)
            try:
                for number, (command, args) in enumerate(commands):
                    result = tools.handle({"token": token, "tool": "act", "args": {
                        "step_id": f"s{number}", "command": command, "args": args}})  # fmt: skip
                    assert result["ok"], result
            finally:
                tools.revoke()
            return answer

        return plan

    def stored_view(self, name) -> dict:
        with GameStore(self.database, load_runtime=True) as store:
            return store.participant_view(ParticipantContext(self.ids[name])).view

    def shown_worker(self, page) -> dict:
        panel = page.locator("#console-state-body dl")
        labels = panel.locator("dt").all_text_contents()
        values = panel.locator("dd").all_text_contents()
        return dict(zip(labels, values))

    def expect_revision(self, page, revision):
        expect(page.locator("#console-state-revision")).to_have_text(
            f"As of revision {revision}."
        )


class RelayStateTests(RelayCase):
    def test_each_player_sees_the_scene_and_only_their_own_worker(self):
        alice, bob = self.joined("alice"), self.joined("bob")
        for page in (alice, bob):
            panel = page.locator("#console-state-body")
            expect(panel).to_contain_text("The storm-damaged relay")
            expect(panel).to_contain_text("The relay is still offline.")
            expect(panel).to_contain_text("No worker yet.")
            self.expect_revision(page, 0)

        self.say(alice, "I sign on a worker.")
        self.job()
        self.replies.put(
            self.acting(
                ("relay.enlist", {"name": "Wren"}),
                answer=reply(("table", "Alice has a worker now.")),
            )
        )
        self.expect_revision(alice, 1)
        self.expect_revision(bob, 1)
        expect(alice.locator("#console-state-body dd").first).to_have_text("Wren")
        worker = self.stored_view("alice")["character"]
        self.assertEqual(
            self.shown_worker(alice),
            {
                "Name": "Wren",
                "Repair": str(worker["repair"]),
                "Effort": str(worker["effort"]),
            },
        )
        expect(bob.locator("#console-state-body")).to_contain_text("No worker yet.")
        self.assertIsNone(self.stored_view("bob")["character"])
        self.assertNotIn("Wren", bob.content())
        self.assertFalse([frame for frame in self.received[bob] if "Wren" in frame])
        for page in (alice, bob):  # commands and rules are not shown
            self.assertNotIn(
                "relay.enlist", page.locator("#console-state").inner_text()
            )
        self.assertEqual(self.errors, [])

    def test_a_question_is_its_players_alone_and_clears_once_answered(self):
        alice, bob = self.joined("alice"), self.joined("bob")
        self.say(alice, "I sign on a worker.")
        self.job()
        self.replies.put(
            self.acting(
                ("relay.enlist", {"name": "Wren"}), answer=reply(("table", "Done."))
            )
        )
        self.expect_revision(alice, 1)
        self.expect_answered(alice, "Done.")

        question = "Spend 1 effort for Wren to try the repair?"
        self.say(alice, "Can Wren fix the relay?")
        self.job()
        self.replies.put(
            asking(("table", "Wren can try."), ask=[(self.ids["alice"], question)])
        )
        expect(alice.locator("#console-question-text")).to_have_text(question)
        expect(alice.locator("#console-last-text")).to_have_text(
            "“Can Wren fix the relay?”"
        )
        expect(alice.locator("#console-last-state")).to_have_text("answered")
        self.expect_revision(alice, 1)  # a question changes no revision
        self.expect_answered(bob, "Wren can try.")
        expect(bob.locator("#console-question")).to_be_hidden()
        self.assertNotIn(question, bob.content())
        self.assertFalse([frame for frame in self.received[bob] if question in frame])

        self.say(alice, "Yes, go.")
        self.job()
        self.replies.put(
            self.acting(
                ("relay.repair", {}),
                answer=asking(
                    ("table", "Wren works on it."), resolve=[self.ids["alice"]]
                ),
            )
        )
        expect(alice.locator("#console-question")).to_be_hidden()
        self.expect_revision(alice, 2)
        self.expect_revision(bob, 2)
        expect(alice.locator("#console-last-text")).to_have_text("“Yes, go.”")
        stored = self.stored_view("alice")
        self.assertEqual(
            self.shown_worker(alice)["Effort"], str(stored["character"]["effort"])
        )
        self.assertEqual(stored["character"]["effort"], 2)
        restored = "restored." if stored["scene"]["restored"] else "still offline."
        expect(alice.locator(".relay-state")).to_have_text(f"The relay is {restored}")
        self.assertEqual(self.errors, [])


HARBOR_VIEW = {
    "harbor": {"name": "<b>Eastport</b>", "lights": [f"light {n}" for n in range(205)]},
    "you": {
        "coins": 3,
        "pet": None,
        "deep": {"a": {"b": {"c": {"d": {"e": "hidden"}}}}},
    },
    "empty": {},
}


def harbor():
    module = Module(
        id="harbor", version="1", state_version=1, commands=(),
        initial_state=lambda: {}, view=lambda state, participant_id: HARBOR_VIEW,
        requires=frozenset(), providers=(),
    )  # fmt: skip
    return Catalog([module], [DICE_PROVIDER]).resolve("harbor", providers=[])


class GenericStateTests(ConversationCase):
    runtime = harbor()

    def test_an_unfamiliar_view_is_a_read_only_tree_of_text(self):
        page = self.joined("alice")
        panel = page.locator("#console-state-body")
        expect(panel.locator("dt").first).to_have_text("harbor")
        expect(panel).to_contain_text("<b>Eastport</b>")  # text, never markup
        self.assertEqual(panel.locator("b").count(), 0)
        lights = panel.locator("ol.tree-list > li")
        expect(lights).to_have_count(201)
        expect(lights.last).to_have_text("5 more")
        expect(panel).to_contain_text("none")  # the pet, and the empty object
        self.assertNotIn("hidden", panel.inner_text())  # past six levels
        self.assertEqual(panel.locator("button, input, a").count(), 0)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
