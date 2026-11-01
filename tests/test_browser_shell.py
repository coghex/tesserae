"""The packaged page in a real browser (#84, WEB-2): Console by default, the
two-item navigation, collapsing it by keyboard, a narrow window, and no
connection or script error. docs/designs/browser_contract.md sections 4 and 5.

Needs the `browser-test` extra and Chromium (`python -m playwright install
chromium`). Without them these tests skip; with TESSERAE_BROWSER_TESTS=1 (CI's
browser job) they fail instead."""

import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path

from tesserae.credentials import new_token
from tesserae.referee.store import RefereeStore
from tesserae.server import GameHost
from tesserae.store import GameStore
from tesserae.web import load_assets

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

REQUIRED = os.environ.get("TESSERAE_BROWSER_TESTS") == "1"


async def never(job):
    raise AssertionError("no referee job runs in these tests")


class BackgroundHost:
    """A browser-mode host on its own event loop thread, so the synchronous
    Playwright API can drive the page from the test's thread."""

    def __init__(self, database: Path):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.uri = self._call(self._start(database))
        self.page = "http://" + self.uri.removeprefix("ws://")

    def _call(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(10)

    async def _start(self, database):
        host = GameHost(
            database,
            authenticated=True,
            referee_executor=never,
            browser_assets=load_assets(),
        )
        self.stop, ready = self.loop.create_future(), self.loop.create_future()
        self.task = asyncio.create_task(
            host.run(0, self.stop, on_ready=ready.set_result)
        )
        return await ready

    def close(self):
        async def end():
            self.stop.set_result(None)
            await self.task

        self._call(end())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()


class BrowserCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            if REQUIRED:
                raise RuntimeError("TESSERAE_BROWSER_TESTS=1 but Playwright is missing")
            raise unittest.SkipTest("the browser-test extra is not installed")
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        database = Path(directory.name) / "game.sqlite3"
        with GameStore(database) as store:
            store.add_participant("Alice", new_token())
            RefereeStore(store).enable()
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        try:
            cls.browser = cls.playwright.chromium.launch()
        except PlaywrightError:
            if REQUIRED:
                raise
            raise unittest.SkipTest("Chromium is not installed for Playwright")
        cls.addClassCleanup(cls.browser.close)
        cls.host = BackgroundHost(database)
        cls.addClassCleanup(cls.host.close)

    def open(self, width=1024, height=768):
        """A fresh page on the host, recording script errors and sockets."""
        context = self.browser.new_context(viewport={"width": width, "height": height})
        self.addCleanup(context.close)
        page = context.new_page()
        self.errors, self.sockets = [], []
        page.on("console", lambda m: m.type == "error" and self.errors.append(m.text))
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("websocket", lambda s: self.sockets.append(s.url))
        page.goto(self.host.page)
        return page

    def assert_quiet(self):
        """No script error or CSP violation, and no connection was opened."""
        self.assertEqual(self.errors, [])
        self.assertEqual(self.sockets, [])


class ShellTests(BrowserCase):
    def test_opens_on_console_and_switches_screens_without_connecting(self):
        page = self.open()
        console = page.get_by_role("button", name="Console")
        settings = page.get_by_role("button", name="Settings")
        expect(page.get_by_role("heading", name="Console")).to_be_visible()
        expect(console).to_have_attribute("aria-current", "page")
        expect(settings).not_to_have_attribute("aria-current", "page")
        expect(page.locator("#screen-settings")).to_be_hidden()
        expect(page.get_by_role("status")).to_have_text("Not connected")
        expect(
            page.locator("#screen-console").get_by_role("button", name="Join")
        ).to_be_disabled()
        self.assertEqual(
            page.get_by_role("navigation").get_by_role("button").count(), 3
        )

        settings.click()
        expect(page.get_by_role("heading", name="Settings")).to_be_visible()
        expect(page.locator("#screen-console")).to_be_hidden()
        expect(settings).to_have_attribute("aria-current", "page")
        expect(console).not_to_have_attribute("aria-current", "page")
        port = self.host.uri.rsplit(":", 1)[1].strip("/")
        expect(page.locator("#settings-host")).to_have_text(f"127.0.0.1:{port}")

        console.click()
        expect(page.get_by_role("heading", name="Console")).to_be_visible()
        settings.click()
        page.reload()  # never the last screen used
        expect(page.get_by_role("heading", name="Console")).to_be_visible()
        expect(page.locator("#screen-settings")).to_be_hidden()
        self.assertEqual(page.url, self.host.page)  # no screen in the URL
        self.assert_quiet()

    def test_navigation_collapses_and_expands_by_keyboard_keeping_names(self):
        page = self.open()
        toggle = page.locator("#nav-toggle")
        nav = page.get_by_role("navigation", name="Main")
        expect(toggle).to_have_attribute("aria-expanded", "true")
        wide = nav.bounding_box()["width"]

        page.keyboard.press("Tab")
        expect(toggle).to_be_focused()
        page.keyboard.press("Enter")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(page.get_by_role("button", name="Expand navigation")).to_be_focused()
        self.assertLess(nav.bounding_box()["width"], wide / 2)

        page.keyboard.press("Tab")
        console = page.get_by_role("button", name="Console")
        expect(console).to_be_focused()
        tooltip = console.locator(".label")
        self.assertGreater(tooltip.bounding_box()["width"], 20)  # shown on focus
        page.keyboard.press("Tab")
        settings = page.get_by_role("button", name="Settings")
        expect(settings).to_be_focused()
        self.assertLess(tooltip.bounding_box()["width"], 2)  # hidden again
        page.keyboard.press("Space")
        expect(settings).to_have_attribute("aria-current", "page")
        expect(page.get_by_role("heading", name="Settings")).to_be_visible()

        page.keyboard.press("Shift+Tab")
        page.keyboard.press("Shift+Tab")
        expect(toggle).to_be_focused()
        page.keyboard.press("Enter")
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(page.get_by_role("button", name="Collapse navigation")).to_be_visible()
        self.assertEqual(nav.bounding_box()["width"], wide)
        expect(settings).to_have_attribute("aria-current", "page")  # unchanged
        self.assert_quiet()

    def test_a_narrow_window_starts_collapsed_and_fits(self):
        page = self.open(width=320, height=640)
        toggle = page.locator("#nav-toggle")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(page.get_by_role("button", name="Console")).to_have_attribute(
            "aria-current", "page"
        )
        overflow = "document.documentElement.scrollWidth > window.innerWidth"
        self.assertFalse(page.evaluate(overflow))
        page.get_by_role("button", name="Settings").click()
        expect(page.get_by_role("heading", name="Settings")).to_be_visible()
        self.assertFalse(page.evaluate(overflow))

        page.set_viewport_size({"width": 1024, "height": 768})
        self.assertEqual(toggle.get_attribute("aria-expanded"), "false")  # player's
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        self.assert_quiet()


if __name__ == "__main__":
    unittest.main()
