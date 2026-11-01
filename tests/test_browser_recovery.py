"""Recovering browser play (#87, WEB-5): a dropped connection keeps the page
and offers Reconnect; an unanswered input is unconfirmed until the host's
`your_last_input` or an exact resend settles it; a refresh recovers the
accepted input and says the draft is gone; a busy refusal never replaces the
resend target; and a host restart. No resend ever starts a second job.
docs/designs/browser_contract.md sections 7 and 9.

Like test_browser_shell, these skip without Playwright and Chromium unless
TESSERAE_BROWSER_TESTS=1."""

import json
import time
import unittest

from websockets.sync.client import connect

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_browser_conversation import ConversationCase, reply
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_browser_conversation import ConversationCase, reply

try:
    from playwright.sync_api import expect
except ImportError:
    expect = None


class Wire:
    """Routes a page's sockets through the test, which can swallow the page's
    next `say`, hold back everything the host sends (keeping its order), and
    cut the connection at both ends."""

    def __init__(self, page, uri):
        self.ends = []
        self.drop_say = False
        self.dropped = []  # the swallowed `say` messages
        self.hold = False
        self.held = []
        page.route_web_socket(f"{uri}**", self.route)

    def route(self, socket):
        server = socket.connect_to_server()
        self.ends.append((socket, server))

        def from_host(message):
            if self.hold or self.held:
                self.held.append(message)
            else:
                socket.send(message)

        def from_page(message):
            if self.drop_say and json.loads(message).get("type") == "say":
                self.dropped.append(json.loads(message))
            else:
                server.send(message)

        server.on_message(from_host)
        socket.on_message(from_page)

    def cut(self):
        """The connection drops: nothing held is ever delivered."""
        socket, server = self.ends[-1]
        self.held.clear()
        self.hold = False
        server.close()
        socket.close(code=1001, reason="gone")


class RecoveryCase(ConversationCase):
    def wired(self, name):
        page = self.open()
        wire = Wire(page, self.host.uri)
        page.locator("#console-token").fill(self.tokens[name])
        page.locator("#console-token").press("Enter")
        self.expect_status(page, f"Joined as {name.title()}")
        return page, wire

    def expect_lost(self, page):
        self.expect_status(page, "Disconnected. What you see may be out of date.")
        expect(page.locator("#console-reconnect")).to_be_visible()

    def reconnect(self, page):
        page.locator("#console-reconnect").click()
        self.expect_status(page, "Joined as")

    def expect_no_job(self):
        time.sleep(0.3)  # long enough for a wrongly started job to arrive
        self.assertTrue(self.jobs.empty())

    def quiet(self):
        """No script error except a refused WebSocket while the host was down."""
        self.assertEqual(
            [e for e in self.errors if "WebSocket connection" not in e], []
        )


class UnconfirmedTests(RecoveryCase):
    def test_an_input_the_host_never_received_is_resent_exactly_once(self):
        page, wire = self.wired("alice")
        wire.drop_say = True
        self.say(page, "Hello?")
        page.wait_for_function("document.getElementById('composer-text').readOnly")
        wire.cut()
        self.expect_lost(page)
        expect(page.locator("#console-unconfirmed-text")).to_have_text("“Hello?”")
        expect(page.locator("#unconfirmed-resend")).to_be_disabled()
        expect(page.locator("#composer-text")).to_have_value("Hello?")
        self.assertTrue(self.jobs.empty())

        wire.drop_say = False
        self.reconnect(page)
        expect(page.locator("#console-unconfirmed")).to_be_visible()  # no record of it
        page.locator("#unconfirmed-resend").click()
        job = self.job()
        [original] = wire.dropped
        self.assertEqual(
            (job.request_id, job.text, job.private),
            (original["request_id"], "Hello?", False),
        )
        expect(page.locator("#console-unconfirmed")).to_be_hidden()
        expect(page.locator("#composer-text")).to_have_value("")
        self.replies.put(reply(("table", "Hello, Alice.")))
        self.expect_answered(page, "Hello, Alice.")
        self.expect_no_job()
        self.quiet()

    def test_an_accepted_input_is_confirmed_by_the_host_after_reconnecting(self):
        page, wire = self.wired("alice")
        wire.hold = True
        self.say(page, "Fix it.")
        self.job()  # accepted; its acknowledgment is held back, then lost
        wire.cut()
        self.expect_lost(page)
        expect(page.locator("#console-unconfirmed-text")).to_have_text("“Fix it.”")
        self.replies.put(reply(("table", "Wren fixes it.")))

        self.reconnect(page)
        expect(page.locator("#console-unconfirmed")).to_be_hidden()
        expect(page.locator("#composer-text")).to_have_value("")
        expect(page.locator("#composer-text")).to_be_editable()
        self.assertEqual(self.texts(page), ["Fix it.", "Wren fixes it."])
        expect(page.locator("#console-last-text")).to_have_text("“Fix it.”")
        expect(page.locator("#console-last-state")).to_have_text("answered")
        self.expect_no_job()
        self.quiet()

    def test_a_newer_input_from_elsewhere_keeps_it_unconfirmed_until_resent(self):
        page, wire = self.wired("alice")
        wire.hold = True
        self.say(page, "First.")
        self.job()
        wire.cut()
        self.expect_lost(page)
        self.replies.put(reply(("table", "One.")))
        with connect(self.host.uri) as cli:  # Alice again, from the CLI
            cli.send(
                json.dumps(
                    {"version": 1, "type": "join", "token": self.tokens["alice"]}
                )
            )
            self.assertEqual(json.loads(cli.recv(5))["type"], "snapshot")
            while True:  # wait until the first job has ended
                status = {"version": 1, "type": "referee_status"}
                cli.send(json.dumps(status))
                while (answer := json.loads(cli.recv(5)))["type"] != "referee_status":
                    pass
                if answer["state"] == "idle":
                    break
                time.sleep(0.05)
            cli.send(json.dumps({"version": 1, "type": "say", "request_id": "c-2",
                                 "text": "Second.", "private": False}))  # fmt: skip
            self.assertEqual(self.job().text, "Second.")
            self.replies.put(reply(("table", "Two.")))

        self.reconnect(page)
        expect(page.locator("#console-last-text")).to_have_text("“Second.”")
        expect(page.locator("#console-unconfirmed-text")).to_have_text("“First.”")
        expect(page.locator("#console-unconfirmed")).to_contain_text("not known")
        page.locator("#unconfirmed-resend").click()
        expect(page.locator("#console-unconfirmed")).to_be_hidden()
        expect(page.locator("#composer-text")).to_have_value("")
        self.expect_no_job()  # the recorded acceptance, not a second job
        self.assertEqual(self.texts(page), ["First.", "One.", "Second.", "Two."])
        self.quiet()


class RefreshTests(RecoveryCase):
    def test_a_refresh_recovers_the_accepted_input_and_says_the_draft_is_gone(self):
        page = self.joined("alice")
        self.say(page, "Hello.")
        self.job()
        self.replies.put(reply(("table", "Hello, Alice.")))
        self.expect_answered(page, "Hello, Alice.")
        page.locator("#composer-text").fill("Never sent.")

        page.reload()
        expect(page.locator("#console-join")).to_be_visible()
        page.locator("#console-token").fill(self.tokens["alice"])
        page.locator("#console-token").press("Enter")
        self.expect_status(page, "Joined as Alice")
        expect(page.locator("#console-last-text")).to_have_text("“Hello.”")
        expect(page.locator("#console-last-state")).to_have_text("answered")
        expect(page.locator("#composer-note")).to_contain_text("is not kept")
        expect(page.locator("#composer-text")).to_have_value("")  # never refilled
        expect(page.locator("#conversation .message")).to_have_count(2)
        [_, reloaded] = self.sent(page)
        self.assertNotIn("say", reloaded)

        page.locator("#console-resend").click()  # the host-reported input
        self.expect_no_job()
        expect(page.locator("#conversation .message")).to_have_count(2)  # merged
        self.assertEqual(reloaded.count("say"), 1)
        self.quiet()

    def test_a_busy_refusal_does_not_replace_the_resend_target(self):
        alice, bob = self.joined("alice"), self.joined("bob")
        self.say(alice, "First.")
        self.job()
        self.replies.put(reply(("table", "One.")))
        self.expect_answered(bob, "One.")
        self.say(bob, "My turn.")
        self.job()
        self.say(alice, "Second.")
        expect(alice.locator("#composer-note")).to_contain_text("still answering Bob")
        self.replies.put(reply(("table", "Bob's answer.")))
        self.expect_answered(alice, "Bob's answer.")

        expect(alice.locator("#console-last-text")).to_have_text("“First.”")
        alice.locator("#console-resend").click()
        self.expect_no_job()  # "First." again, recorded; "Second." was never sent
        expect(alice.locator("#composer-text")).to_have_value("Second.")  # untouched
        self.quiet()


class RestartTests(RecoveryCase):
    def test_a_host_restart_keeps_the_page_and_reconnect_recovers(self):
        alice, bob = self.joined("alice"), self.joined("bob")
        self.say(alice, "Before.")
        self.job()
        self.replies.put(reply(("table", "Noted before.")))
        for page in (alice, bob):  # both have it before the host stops
            self.expect_answered(page, "Noted before.")
        alice.locator("#composer-text").fill("Still typing")

        self.host.stop()
        for page in (alice, bob):
            self.expect_lost(page)
            expect(page.locator("#conversation .message")).to_have_count(2)
            expect(page.locator("#console-state-revision")).to_contain_text(
                "out of date"
            )
        expect(alice.locator("#composer-text")).to_have_value("Still typing")
        expect(alice.locator("#composer-send")).to_be_disabled()
        alice.locator("#console-reconnect").click()  # the host is still down
        expect(alice.locator("#console-notice")).to_contain_text(
            "Could not reach the host"
        )
        self.expect_lost(alice)

        self.host.start(self.host.port)
        for page in (alice, bob):
            self.reconnect(page)
            self.assertEqual(self.texts(page), ["Before.", "Noted before."])
        expect(alice.locator("#composer-text")).to_have_value("Still typing")
        expect(alice.locator("#console-state-revision")).not_to_contain_text(
            "out of date"
        )
        self.say(bob, "After.")
        self.job()
        self.replies.put(reply(("table", "Noted after.")))
        expect(alice.locator("#conversation .message")).to_have_count(4)
        self.assertEqual(len(self.sent(alice)), 3)  # join, the failed retry, rejoin
        self.quiet()


if __name__ == "__main__":
    unittest.main()
