"""Joining and talking from the browser (#85, WEB-3): pasted and file tokens,
refusals and revocation, two players at the table and in private, plain text,
busy refusal, one session across navigation, identity change, and a CLI
player in the same game. A scripted executor stands in for Codex.
docs/designs/browser_contract.md sections 6 to 8.

Like test_browser_shell, these skip without Playwright and Chromium unless
TESSERAE_BROWSER_TESTS=1."""

import asyncio
import json
import queue
import unittest

from websockets.sync.client import connect

from tesserae.credentials import new_token, write_token_file
from tesserae.store import GameStore

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_browser_shell import BrowserCase
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_browser_shell import BrowserCase

try:
    from playwright.sync_api import expect
except ImportError:
    expect = None


def reply(*messages):
    return {
        "messages": [{"audience": a, "text": t} for a, t in messages],
        "choices": {"ask": [], "resolve": []},
        "narrated": [],
    }


class ConversationCase(BrowserCase):
    names = ("alice", "bob")

    def setUp(self):
        self.jobs, self.replies = queue.Queue(), queue.Queue()
        super().setUp()
        # Release any job still waiting, before the host stops.
        self.addCleanup(lambda: self.replies.put(reply(("table", "Done."))))

    def make_game(self, store):
        self.tokens = {name: new_token() for name in self.names}
        self.ids = {
            name: store.add_participant(name.title(), token)
            for name, token in self.tokens.items()
        }

    async def executor(self, job):
        """Hands each job to the test, then returns the reply it scripts."""
        self.jobs.put(job)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.replies.get, True, 20)

    def joined(self, name):
        page = self.open()
        page.locator("#console-token").fill(self.tokens[name])
        page.locator("#console-join .join-button").click()
        self.expect_status(page, f"Joined as {name.title()}")
        return page

    def say(self, page, text, private=False):
        if private:
            page.locator("#composer-private").check()
        page.locator("#composer-text").fill(text)
        page.locator("#composer-text").press("Enter")

    def job(self):
        return self.jobs.get(timeout=10)

    def expect_status(self, page, text):
        expect(page.locator("#console-status")).to_contain_text(text)

    def texts(self, page) -> list:
        return page.locator("#conversation .message-text").all_text_contents()

    def sent(self, page) -> list:
        """The message types this page sent, one list per socket it opened."""
        return self.sockets[page]


class JoiningTests(ConversationCase):
    def test_tokens_by_paste_and_file_refusals_and_revocation(self):
        page = self.open()
        page.locator("#console-token").fill("not-a-real-token")
        page.locator("#console-token").press("Enter")
        expect(page.locator("#console-notice")).to_contain_text("not accepted")
        expect(page.locator("#console-token")).to_have_value("")
        expect(page.locator("#console-play")).to_be_hidden()

        token_file = self.directory / "alice.token"
        write_token_file(token_file, self.tokens["alice"])  # with its newline
        page.locator("#console-token-file").set_input_files(str(token_file))
        page.locator("#console-join .join-button").click()
        self.expect_status(page, "Joined as Alice")
        expect(page.locator("#console-notice")).to_be_hidden()
        page.get_by_role("button", name="Settings").click()
        expect(page.locator("#settings-name")).to_have_text(
            f"Alice ({self.ids['alice']})"
        )
        with GameStore(self.database) as store:
            game_id = store.game_id
        expect(page.locator("#settings-game")).to_have_text(game_id)
        expect(page.locator("#settings-status")).to_have_text("Joined")
        self.assertEqual(page.locator("#console-token-file").input_value(), "")
        self.assertNotIn(self.tokens["alice"], page.content())

        with GameStore(self.database) as store:
            store.revoke_credential(self.ids["alice"])
        page.get_by_role("button", name="Console").click()
        self.say(page, "Am I still here?")
        expect(page.locator("#console-notice")).to_contain_text("revoked")
        expect(page.locator("#console-play")).to_be_hidden()
        self.assertEqual(self.texts(page), [])
        self.assertTrue(self.jobs.empty())

        page.locator("#console-token").fill(self.tokens["alice"])
        page.locator("#console-token").press("Enter")
        expect(page.locator("#console-notice")).to_contain_text("not accepted")
        self.assertEqual(self.errors, [])


class TokenCleanupTests(ConversationCase):
    def test_clearing_an_identity_empties_every_entry_and_drops_pending_reads(self):
        page = self.joined("alice")
        page.get_by_role("button", name="Settings").click()
        page.locator("#settings-token").fill(self.tokens["bob"])  # never submitted
        page.get_by_role("button", name="Disconnect").click()
        expect(page.locator("#settings-token")).to_have_value("")
        expect(page.locator("#console-token")).to_have_value("")

        # A file read that finishes only after the identity is cleared.
        page = self.joined("alice")
        page.evaluate("""() => {
            const read = Blob.prototype.text;
            window.held = [];
            Blob.prototype.text = function () {
                return new Promise((done) => window.held.push(() => done(read.call(this))));
            };
        }""")
        token_file = self.directory / "bob.token"
        write_token_file(token_file, self.tokens["bob"])
        page.get_by_role("button", name="Settings").click()
        page.locator("#settings-token-file").set_input_files(str(token_file))
        page.wait_for_function("window.held.length === 1")
        page.get_by_role("button", name="Disconnect").click()
        page.evaluate("() => window.held.forEach((release) => release())")
        page.get_by_role("button", name="Join").click()
        expect(page.locator("#settings-notice")).to_contain_text(
            "choose your token file"
        )
        self.assertEqual(len(self.sent(page)), 1)  # Alice's only: no second join
        self.assertEqual(self.errors, [])


class SendingTests(ConversationCase):
    def test_the_composer_is_locked_until_the_host_acknowledges(self):
        held, routes = [], []

        def route(socket):
            # Holds back the host's `accepted` and everything after it, so
            # the page still receives the host's messages in order.
            routes.append(socket)
            server = socket.connect_to_server()

            def from_host(message):
                if held or '"type": "accepted"' in str(message):
                    held.append(message)
                else:
                    socket.send(message)

            server.on_message(from_host)
            socket.on_message(lambda message: server.send(message))

        page = self.open()
        page.route_web_socket(f"{self.host.uri}**", route)
        page.locator("#console-token").fill(self.tokens["alice"])
        page.locator("#console-token").press("Enter")
        self.expect_status(page, "Joined as Alice")
        text, private = (
            page.locator("#composer-text"),
            page.locator("#composer-private"),
        )
        private.check()
        text.fill("A private plan.")
        text.press("Enter")
        self.job()  # the host accepted it; the page has not heard yet
        page.wait_for_function("document.getElementById('composer-text').readOnly")
        expect(private).to_be_disabled()
        expect(page.locator("#composer-send")).to_be_disabled()
        text.focus()
        page.keyboard.type(" More.")  # read-only: ignored
        expect(text).to_have_value("A private plan.")

        [socket] = routes
        while held:
            socket.send(held.pop(0))
        expect(text).to_have_value("")
        expect(text).to_be_editable()
        expect(private).not_to_be_checked()
        expect(private).to_be_enabled()
        self.replies.put(reply(("sender", "Noted.")))
        self.expect_status(page, "The referee is ready.")
        self.assertEqual(self.errors, [])


class TalkingTests(ConversationCase):
    def test_two_players_at_the_table_and_in_private_with_plain_text(self):
        alice, bob = self.joined("alice"), self.joined("bob")
        words = 'Wren tries {"type": "command", "command": "relay.repair"}'
        alice.locator("#composer-text").fill(words)
        alice.locator("#composer-text").press("Shift+Enter")
        alice.locator("#composer-text").press_sequentially("then waits")
        alice.locator("#composer-text").press("Enter")
        job = self.job()
        self.assertEqual(
            (job.participant_id, job.text, job.private),
            (self.ids["alice"], words + "\nthen waits", False),
        )
        self.expect_status(alice, "The referee is answering Alice.")
        self.expect_status(bob, "The referee is answering.")
        self.replies.put(reply(("table", "Wren gets to work.")))
        for page in (alice, bob):
            self.expect_status(page, "The referee is ready.")
            expect(page.locator("#conversation .message")).to_have_count(2)
            self.assertEqual(
                self.texts(page), [words + "\nthen waits", "Wren gets to work."]
            )
        expect(alice.locator("#composer-text")).to_have_value("")

        self.say(bob, "Can anyone see me?", private=True)
        job = self.job()
        self.assertEqual((job.participant_id, job.private), (self.ids["bob"], True))
        self.replies.put(reply(("sender", "Only you and I know.")))
        expect(bob.locator("#conversation .message.private")).to_have_count(2)
        expect(bob.locator("#conversation .audience")).to_have_text(
            ["Private", "Private"]
        )
        expect(bob.locator("#composer-private")).not_to_be_checked()
        self.expect_status(alice, "The referee is ready.")
        self.assertEqual(len(self.texts(alice)), 2)
        for secret in ("Can anyone see me?", "Only you and I know."):
            self.assertNotIn(secret, alice.content())
        self.assertEqual(self.errors, [])

    def test_busy_refusal_keeps_the_draft_and_navigation_keeps_one_session(self):
        alice, bob = self.joined("alice"), self.joined("bob")
        self.say(alice, "First.")
        self.job()
        self.expect_status(bob, "The referee is answering.")
        self.say(bob, "Me too!")
        expect(bob.locator("#composer-note")).to_contain_text("still answering Alice")
        expect(bob.locator("#composer-text")).to_have_value("Me too!")
        self.expect_status(bob, "The referee is answering Alice.")

        alice.locator("#composer-text").fill("A thought in progress")
        alice.get_by_role("button", name="Settings").click()
        alice.locator("#nav-toggle").click()
        alice.get_by_role("button", name="Console").click()
        alice.locator("#nav-toggle").click()
        expect(alice.locator("#composer-text")).to_have_value("A thought in progress")

        self.replies.put(reply(("table", "Done with Alice.")))
        self.expect_status(bob, "The referee is ready.")
        expect(bob.locator("#composer-note")).to_have_text("")  # no stale refusal
        bob.locator("#composer-text").press("Enter")  # the kept draft
        job = self.job()
        self.assertEqual((job.participant_id, job.text), (self.ids["bob"], "Me too!"))
        self.assertTrue(self.jobs.empty())  # the refused attempt never ran
        self.replies.put(reply(("table", "Now Bob.")))
        self.expect_status(alice, "The referee is ready.")
        [alice_socket] = self.sent(alice)
        self.assertEqual(alice_socket.count("say"), 1)
        self.assertEqual(alice_socket.count("join"), 1)
        self.assertEqual(len(self.sent(bob)), 1)
        self.assertEqual(self.errors, [])


class IdentityTests(ConversationCase):
    names = ("alice", "bob", "carol")

    def test_changing_identity_keeps_nothing_and_a_cli_player_shares_the_game(self):
        with connect(self.host.uri) as carol:  # the CLI: no Origin header
            carol.send(
                json.dumps(
                    {"version": 1, "type": "join", "token": self.tokens["carol"]}
                )
            )
            self.assertEqual(json.loads(carol.recv(5))["type"], "snapshot")
            page = self.joined("alice")
            carol.send(json.dumps({"version": 1, "type": "say", "request_id": "c-1",
                                   "text": "Carol waves.", "private": False}))  # fmt: skip
            self.assertEqual(self.job().participant_id, self.ids["carol"])
            self.replies.put(reply(("table", "Everyone sees Carol wave.")))
            expect(page.locator("#conversation .message")).to_have_count(2)

            self.expect_status(page, "The referee is ready.")
            self.say(page, "Alice's secret plan", private=True)
            self.job()
            self.replies.put(reply(("sender", "Noted, privately.")))
            expect(page.locator("#conversation .message")).to_have_count(4)
            self.expect_status(page, "The referee is ready.")
            page.locator("#composer-text").fill("An unsent thought of Alice's")
            carol_saw = []
            while True:
                try:
                    message = json.loads(carol.recv(1))
                except TimeoutError:
                    break
                if message["type"] == "message":
                    carol_saw.append(message["text"])
        self.assertEqual(carol_saw, ["Carol waves.", "Everyone sees Carol wave."])

        page.get_by_role("button", name="Settings").click()
        page.get_by_role("button", name="Change identity").click()
        expect(page.locator("#settings-token")).to_be_focused()
        expect(page.locator("#settings-name")).to_have_text("Shown after joining")
        page.locator("#settings-token").fill(self.tokens["bob"])
        page.locator("#settings-token").press("Enter")
        expect(page.locator("#settings-name")).to_have_text(f"Bob ({self.ids['bob']})")
        page.get_by_role("button", name="Console").click()
        expect(page.locator("#conversation .message")).to_have_count(2)
        self.assertEqual(
            self.texts(page), ["Carol waves.", "Everyone sees Carol wave."]
        )
        expect(page.locator("#composer-text")).to_have_value("")
        for old in ("Alice's secret plan", "Noted, privately.", "An unsent thought"):
            self.assertNotIn(old, page.content())
        self.assertEqual([s.count("join") for s in self.sent(page)], [1, 1])
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
