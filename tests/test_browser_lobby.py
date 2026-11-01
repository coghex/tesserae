"""The browser page of a lobby host (#99, HOST-4): logging in with a host
token, the campaign picker and its empty state, playing in the selected
campaign, leaving and switching without anything crossing campaigns, lost
access, revocation, and reconnecting into the same campaign. A scripted
referee per campaign stands in for Codex. docs/designs/lobby_contract.md
section 10.

Like test_browser_shell, these skip without Playwright and Chromium unless
TESSERAE_BROWSER_TESTS=1."""

import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from websockets.sync.client import connect

from tesserae.builtin import DICE_RUNTIME
from tesserae.credentials import new_token, write_token_file
from tesserae.lobby import LobbyHost
from tesserae.referee.store import RefereeStore
from tesserae.registry import HostRegistry
from tesserae.store import GameStore
from tesserae.web import load_assets

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_browser_conversation import reply
    from test_browser_shell import BrowserCase
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_browser_conversation import reply
    from tests.test_browser_shell import BrowserCase

try:
    from playwright.sync_api import expect
except ImportError:
    expect = None


class BackgroundLobby:
    """A lobby host serving the browser page, on its own event loop thread.
    It can be stopped and started again on the same port."""

    def __init__(self, registry: Path, executor_for):
        self.registry = registry
        self.executor_for = executor_for
        self.task = None
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.start()

    def _call(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(10)

    def start(self, port=0):
        self.uri = self._call(self._start(port))
        self.port = int(self.uri.rsplit(":", 1)[1].strip("/"))
        self.page = "http://" + self.uri.removeprefix("ws://")

    async def _start(self, port):
        host = LobbyHost(
            self.registry,
            referee_executor_for=self.executor_for,
            browser_assets=load_assets(lobby=True),
        )
        self.stop_future, ready = self.loop.create_future(), self.loop.create_future()
        self.task = asyncio.create_task(
            host.run(port, self.stop_future, on_ready=ready.set_result)
        )
        return await ready

    def stop(self):
        async def end():
            self.stop_future.set_result(None)
            await self.task

        self._call(end())
        self.task = None

    def close(self):
        if self.task is not None:
            self.stop()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()


class Valve:
    """Routes a page's socket through the test, which can hold back what the
    host sends and then release it in order."""

    def __init__(self, page, uri):
        self.hold = False
        self.held = []
        page.route_web_socket(f"{uri}**", self.route)

    def route(self, socket):
        server = socket.connect_to_server()
        self.socket = socket

        def from_host(message):
            if self.hold:
                self.held.append(message)
            else:
                socket.send(message)

        server.on_message(from_host)
        socket.on_message(server.send)

    def release(self):
        self.hold = False
        for message in self.held:
            self.socket.send(message)
        self.held.clear()


class LobbyCase(BrowserCase):
    """Harbor and Bridge are referee campaigns and Dice a plain one, all open
    to Alice; Hidden is active but not granted to her and Closed is granted
    but inactive. Bob has no campaigns. Each referee answers "Heard in NAME:
    TEXT", privately to private input; input `hold` waits for `release`."""

    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry_path = self.directory / "host.sqlite3"
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.tokens, self.players, self.campaigns, self.names = {}, {}, {}, {}
        with HostRegistry.create(self.registry_path) as registry:
            self.host_id = registry.host_id
            for name in ("alice", "bob"):
                self.tokens[name] = new_token()
                self.players[name] = registry.add_player(
                    name.title(), self.tokens[name]
                )
            for name, referee, active, granted in (
                ("harbor", True, True, True),
                ("bridge", True, True, True),
                ("dice", False, True, True),
                ("hidden", True, True, False),
                ("closed", True, False, True),
            ):
                path = self.directory / f"{name}.sqlite3"
                with GameStore.create(path, DICE_RUNTIME) as store:
                    if referee:
                        RefereeStore(store).enable()
                campaign = registry.register_campaign(path, name.title())
                registry.set_campaign_active(campaign, active)
                if granted:
                    registry.grant(campaign, self.players["alice"])
                self.campaigns[name] = campaign
                self.names[campaign] = name.title()
        self.host = BackgroundLobby(self.registry_path, self.executor_for)
        self.addCleanup(self.host.close)
        self.errors, self.sockets, self.received = [], {}, {}

    def executor_for(self, campaign_id):
        where = self.names[campaign_id]

        async def executor(job):
            if job.text == "hold":
                await asyncio.to_thread(self.release.wait, 20)
            audience = "sender" if job.private else "table"
            return reply((audience, f"Heard in {where}: {job.text}"))

        return executor

    def registry(self) -> HostRegistry:
        return HostRegistry(self.registry_path)

    def store(self, name) -> GameStore:
        return GameStore(self.directory / f"{name}.sqlite3")

    def jobs(self, name) -> list:
        with self.store(name) as store:
            return store.query("SELECT state FROM referee_jobs ORDER BY started_at")

    def logged_in(self, name, page=None):
        page = page or self.open()
        page.locator("#console-token").fill(self.tokens[name])
        page.locator("#console-token").press("Enter")
        self.expect_status(page, f"Logged in as {name.title()}")
        return page

    def enter(self, page, name):
        page.locator("#campaign-list").get_by_role("button", name=name).click()
        self.expect_status(page, f"{name} · Joined as")

    def leave(self, page):
        page.locator("#console-leave").click()
        self.expect_status(page, "Logged in as")

    def say(self, page, text, private=False):
        if private:
            page.locator("#composer-private").check()
        page.locator("#composer-text").fill(text)
        page.locator("#composer-text").press("Enter")

    def expect_status(self, page, text):
        expect(page.locator("#console-status")).to_contain_text(text)

    def expect_answered(self, page, text):
        expect(
            page.locator("#conversation .message-text", has_text=text)
        ).to_have_count(1)
        self.expect_status(page, "The referee is ready.")

    def campaign_names(self, page) -> list:
        return page.locator("#campaign-list button").all_text_contents()

    def texts(self, page) -> list:
        return page.locator("#conversation .message-text").all_text_contents()

    def wait_for_jobs(self, name, states):
        deadline = time.monotonic() + 10
        while self.jobs(name) != states:
            if time.monotonic() > deadline:
                self.fail(f"{name}'s jobs are {self.jobs(name)}, not {states}")
            time.sleep(0.05)


class PickingTests(LobbyCase):
    def test_login_pick_play_leave_and_switch_on_one_connection(self):
        page = self.open()
        valve = Valve(page, self.host.uri)
        expect(page.locator("#console-join h2")).to_have_text("Log in")
        page.locator("#console-token").fill(self.tokens["alice"])
        page.locator("#console-join").get_by_role("button", name="Log in").click()
        self.expect_status(page, "Logged in as Alice")
        expect(page.locator("#console-token")).to_have_value("")
        # Only the active campaigns granted to her, by name.
        self.assertEqual(self.campaign_names(page), ["Bridge", "Dice", "Harbor"])
        expect(page.locator("#console-play")).to_be_hidden()

        self.enter(page, "Harbor")
        expect(page.locator("#composer-text")).to_be_focused()
        self.say(page, "Hello, harbor.")
        self.expect_answered(page, "Heard in Harbor: Hello, harbor.")
        self.say(page, "A secret.", private=True)
        self.expect_answered(page, "Heard in Harbor: A secret.")
        page.locator("#composer-text").fill("Words never sent")

        page.get_by_role("button", name="Settings").click()
        expect(page.locator("#settings-campaign")).to_have_text(
            f"Harbor ({self.campaigns['harbor']})"
        )
        expect(page.locator("#settings-player")).to_have_text(
            f"Alice ({self.players['alice']})"
        )
        page.locator("#settings-leave").click()
        expect(page.locator("#settings-campaign")).to_have_text(
            "None: choose one in the Console"
        )
        page.get_by_role("button", name="Console").click()
        self.expect_status(page, "Logged in as Alice")
        self.assertEqual(self.texts(page), [])  # nothing of Harbor's stays

        self.enter(page, "Bridge")
        expect(page.locator("#composer-text")).to_have_value("")
        self.assertEqual(self.texts(page), [])
        expect(page.locator("#console-last")).to_be_hidden()
        # Leave before Bridge's acknowledgment arrives: it, and everything
        # else Bridge sent before the lobby reply, is dropped.
        valve.hold = True
        self.say(page, "hold")
        self.wait_for_jobs("bridge", [("running",)])
        page.locator("#console-leave").click()
        valve.release()
        self.expect_status(page, "Logged in as")
        self.enter(page, "Harbor")  # Harbor's own history, read again
        expect(page.locator("#console-resend-target")).to_have_text("")
        expect(page.locator("#conversation .message-text")).to_have_text([
            "Hello, harbor.", "Heard in Harbor: Hello, harbor.",
            "A secret.", "Heard in Harbor: A secret.",
        ])  # fmt: skip
        self.release.set()
        self.wait_for_jobs("bridge", [("completed",)])
        time.sleep(0.3)  # long enough for a wrongly delivered reply to arrive
        self.assertNotIn("Bridge", " ".join(self.texts(page)))
        expect(page.locator("#console-last-text")).to_have_text("“A secret.” (private)")

        self.leave(page)
        self.enter(page, "Dice")  # no referee: its state only
        expect(page.locator("#console-no-referee")).to_be_visible()
        expect(page.locator("#console-composer")).to_be_hidden()
        expect(page.locator("#console-state-body")).not_to_be_empty()
        self.leave(page)

        [sent] = self.sockets[page]  # one connection all along
        self.assertEqual(sent[0], "login")
        self.assertNotIn("join", sent)
        self.assertEqual(sent.count("select"), 4)
        self.assertEqual(
            (self.jobs("harbor"), self.jobs("dice")), ([("completed",)] * 2, [])
        )
        self.assertNotIn(self.tokens["alice"], page.content())
        self.assertEqual(self.errors, [])

    def test_each_page_chooses_its_own_campaign(self):
        harbor = self.logged_in("alice")
        bridge = self.logged_in("alice")
        self.enter(harbor, "Harbor")
        self.enter(bridge, "Bridge")
        self.say(harbor, "Only in Harbor.")
        self.expect_answered(harbor, "Heard in Harbor: Only in Harbor.")
        self.leave(harbor)
        self.expect_status(bridge, "Bridge · Joined as Alice")
        self.assertEqual(self.texts(bridge), [])
        self.assertNotIn("Only in Harbor", " ".join(self.received[bridge]))
        self.assertEqual(self.errors, [])


class AccessTests(LobbyCase):
    def test_an_empty_catalog_a_token_file_and_refusals(self):
        page = self.open()
        page.locator("#console-token").fill("not-a-real-token")
        page.locator("#console-token").press("Enter")
        expect(page.locator("#console-notice")).to_contain_text("Use your host token")
        expect(page.locator("#console-lobby")).to_be_hidden()

        token_file = self.directory / "bob.token"
        write_token_file(token_file, self.tokens["bob"])
        page.locator("#console-token-file").set_input_files(str(token_file))
        page.locator("#console-join .join-button").click()
        self.expect_status(page, "Logged in as Bob")
        expect(page.locator("#lobby-empty")).to_be_visible()
        expect(page.locator("#lobby-refresh")).to_be_focused()
        with self.registry() as registry:
            registry.grant(self.campaigns["harbor"], self.players["bob"])
        page.locator("#lobby-refresh").click()
        expect(page.locator("#campaign-list button")).to_have_text(["Harbor"])
        expect(page.locator("#lobby-empty")).to_be_hidden()

        self.enter(page, "Harbor")
        with self.registry() as registry:
            registry.revoke_credential(self.players["bob"])
        self.say(page, "Am I still here?")
        expect(page.locator("#console-notice")).to_contain_text("no longer valid")
        expect(page.locator("#console-join")).to_be_visible()
        expect(page.locator("#console-play")).to_be_hidden()
        self.assertEqual(self.texts(page), [])
        self.assertEqual(self.jobs("harbor"), [])
        self.assertEqual(self.errors, [])

    def test_lost_access_returns_to_the_picker(self):
        page = self.logged_in("alice")
        self.enter(page, "Harbor")
        with self.registry() as registry:
            registry.revoke_access(self.campaigns["harbor"], self.players["alice"])
        self.say(page, "Still here?")
        expect(page.locator("#console-notice")).to_contain_text(
            "That campaign is no longer available to you."
        )
        expect(page.locator("#campaign-list button")).to_have_text(["Bridge", "Dice"])
        self.assertEqual(self.jobs("harbor"), [])

        # A delivery that finds her link ended sends `left`.
        self.enter(page, "Bridge")
        with self.registry() as registry, self.store("bridge") as store:
            store.unlink_host_player(registry.host_id, self.players["alice"])
            carol = new_token()
            registry.grant(
                self.campaigns["bridge"], registry.add_player("Carol", carol)
            )
        with connect(self.host.uri) as other:
            other.send(json.dumps({"version": 1, "type": "login", "token": carol}))
            other.recv()
            other.send(json.dumps({"version": 1, "type": "select",
                                   "campaign_id": self.campaigns["bridge"]}))  # fmt: skip
            other.recv()
            other.send(json.dumps({"version": 1, "type": "say", "request_id": "c1",
                                   "text": "Anyone?", "private": False}))  # fmt: skip
            expect(page.locator("#console-notice")).to_contain_text(
                "You no longer have access to Bridge."
            )
        self.expect_status(page, "Logged in as Alice")
        self.assertEqual(self.texts(page), [])
        self.assertEqual(self.errors, [])


class ReconnectTests(LobbyCase):
    def test_reconnect_enters_the_same_campaign_and_never_another_participant(self):
        page = self.logged_in("alice")
        self.enter(page, "Harbor")
        self.say(page, "A secret.", private=True)
        self.expect_answered(page, "Heard in Harbor: A secret.")
        page.locator("#composer-text").fill("Half typed")
        port = self.host.port

        self.host.stop()
        self.expect_status(page, "Disconnected")
        self.host.start(port)
        page.locator("#console-reconnect").click()
        self.expect_status(page, "Harbor · Joined as Alice")
        expect(page.locator("#composer-text")).to_have_value("Half typed")
        expect(page.locator("#conversation .message-text")).to_have_count(2)

        # Relinked while the host was down: a new participant, nothing kept.
        self.host.stop()
        with self.registry() as registry, self.store("harbor") as store:
            store.unlink_host_player(registry.host_id, self.players["alice"])
        self.host.start(port)
        page.locator("#console-reconnect").click()
        self.expect_status(page, "Harbor · Joined as Alice")
        expect(page.locator("#composer-text")).to_have_value("")
        expect(page.locator("#console-last")).to_be_hidden()
        self.assertNotIn("A secret.", " ".join(self.texts(page)))

        # Access ended while the host was down: back to the picker.
        self.host.stop()
        with self.registry() as registry:
            registry.revoke_access(self.campaigns["harbor"], self.players["alice"])
        self.host.start(port)
        page.locator("#console-reconnect").click()
        expect(page.locator("#console-notice")).to_contain_text("not available to you")
        self.expect_status(page, "Logged in as Alice")
        self.assertEqual(self.texts(page), [])
        self.assertEqual(
            [e for e in self.errors if "WebSocket connection" not in e], []
        )


if __name__ == "__main__":
    unittest.main()
