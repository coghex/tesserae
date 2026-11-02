"""The browser page in a campaign with characters (CHR-6, #116): the
character picker, private creation with the referee and approval at the
revision shown, play as a character, two pages and two characters of one
player, boundaries that clear one actor's state, late replies, reconnecting to
the same character, archiving from Settings and from another page, and names
shown as text. An original Trailhead campaign; a scripted referee builds
drafts through the real bridge tools. docs/designs/character_contract.md
section 15.

Like test_browser_shell, these skip without Playwright and Chromium unless
TESSERAE_BROWSER_TESTS=1."""

import asyncio
import json
import tempfile
import threading
import time
from pathlib import Path

from tesserae.builtin import CATALOG, DICE_RUNTIME
from tesserae.credentials import new_token
from tesserae.referee.store import RefereeStore
from tesserae.referee.tools import BridgeTools
from tesserae.registry import HostRegistry
from tesserae.seed import parse_seed
from tesserae.store import GameStore, ParticipantContext

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_browser_lobby import BackgroundLobby, Valve
    from test_browser_shell import BrowserCase
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_browser_lobby import BackgroundLobby, Valve
    from tests.test_browser_shell import BrowserCase

try:
    from playwright.sync_api import expect
except ImportError:
    expect = None

TRAILHEAD = CATALOG.resolve("trailhead")
ALICE = "p_a1a1a1a1a1a1a1a1"
MARA = "c_0123456789abcdef"
WREN = "c_fedcba9876543210"


def reply(*messages, ask=()):
    return {"messages": [{"audience": a, "text": t} for a, t in messages],
            "choices": {"ask": [{"actor": a, "question": q} for a, q in ask],
                        "resolve": []},
            "narrated": []}  # fmt: skip


def seed() -> str:
    """Alice's participant already has Mara and Wren, both ready."""
    walker = {"trait": "quick", "rolled": 3, "stamina": 3, "max_stamina": 3,
              "ready": True, "in_danger": False}  # fmt: skip
    return json.dumps({
        "seed_version": 2,
        "module": {"id": "trailhead", "version": "1", "state_version": 1,
                   "providers": [{"operation": "dice.roll.v1", "provider": "builtin.dice",
                                  "version": "1"}]},
        "participants": [{"participant_id": ALICE, "name": "Alice"}],
        "characters": [{"character_id": MARA, "participant_id": ALICE, "name": "Mara"},
                       {"character_id": WREN, "participant_id": ALICE, "name": "Wren"}],
        "state": {"walkers": {MARA: walker, WREN: dict(walker, trait="steady")}},
        "provenance": {"source": "test"},
    })  # fmt: skip


class CharacterCase(BrowserCase):
    """Trail, a Trailhead campaign in referee mode, open to Alice (linked to
    her seeded participant: Mara and Wren) and to Bob, who is new. The
    referee answers "Heard: TEXT" (privately to private input); `build NAME
    TRAIT` names the creation job's draft, sets its trait and rolls its
    stamina through the bridge; `ask me` asks the job's character a
    question; `hold` waits for `release`."""

    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry_path = self.directory / "host.sqlite3"
        self.trail = self.directory / "trail.sqlite3"
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        with GameStore.create(self.trail, TRAILHEAD, seed=parse_seed(seed())) as store:
            RefereeStore(store).enable()
        with GameStore.create(self.directory / "dice.sqlite3", DICE_RUNTIME) as store:
            RefereeStore(store).enable()  # Dice: a campaign without characters
        self.tokens, self.players = {}, {}
        with HostRegistry.create(self.registry_path) as registry:
            host_id = registry.host_id
            for name in ("alice", "bob"):
                self.tokens[name] = new_token()
                self.players[name] = registry.add_player(
                    name.title(), self.tokens[name]
                )
            self.campaign = registry.register_campaign(self.trail, "Trail")
            registry.set_campaign_active(self.campaign, True)
            for player in self.players.values():
                registry.grant(self.campaign, player)
            dice = registry.register_campaign(self.directory / "dice.sqlite3", "Dice")
            registry.set_campaign_active(dice, True)
            registry.grant(dice, self.players["alice"])
        with GameStore.open_existing(self.trail) as store:
            store.link_host_player(host_id, self.players["alice"], ALICE)
        self.host = BackgroundLobby(self.registry_path, lambda _: self.executor)
        self.addCleanup(self.host.close)
        self.errors, self.sockets, self.received = [], {}, {}

    async def executor(self, job):
        if job.text == "hold":
            await asyncio.to_thread(self.release.wait, 20)
        if job.text.startswith("build "):
            _, name, trait = job.text.split()
            revision = await asyncio.to_thread(self.build, job, name, trait)
            # As the real bridge does on each commit: players hear of it.
            self.host.lobby._services[self.campaign]._publish(revision)
            return reply(("sender", f"{name} is {trait}; approve when you are ready."))
        audience = "sender" if job.private else "table"
        if job.text == "ask me":
            return reply((audience, "Well?"), ask=[(job.character_id, "Cross now?")])
        return reply((audience, f"Heard: {job.text}"))

    def build(self, job, name, trait):
        tools = BridgeTools(lambda: GameStore(self.trail, randint=lambda low, high: 2))
        token = tools.grant(job.job_id)
        for step, (command, args) in enumerate((("character.name", {"name": name}),
                                                ("trailhead.trait", {"trait": trait}),
                                                ("trailhead.stamina", {}))):  # fmt: skip
            done = tools.handle({"token": token, "tool": "act",
                                 "args": {"step_id": f"s{step}", "command": command, "args": args}})  # fmt: skip
            assert done["ok"], done
        tools.revoke()
        return done["result"]["result"]["revision"]

    def entered(self, name, page=None):
        """Logged in, inside Trail, with no character yet."""
        page = page or self.open()
        page.locator("#console-token").fill(self.tokens[name])
        page.locator("#console-token").press("Enter")
        self.expect_status(page, f"Logged in as {name.title()}")
        page.locator("#campaign-list").get_by_role("button", name="Trail").click()
        self.expect_status(page, "Trail · No character · Joined as")
        return page

    def play(self, page, name):
        page.locator("#character-list").get_by_role(
            "button", name=f"Play {name}"
        ).click()
        self.expect_status(page, f"Trail · {name} · Joined as")

    def say(self, page, text):
        page.locator("#composer-text").fill(text)
        page.locator("#composer-text").press("Enter")

    def expect_status(self, page, text):
        expect(page.locator("#console-status")).to_contain_text(text)

    def expect_answered(self, page, text):
        expect(
            page.locator("#conversation .message-text", has_text=text)
        ).to_have_count(1)
        self.expect_status(page, "The referee is ready.")

    def texts(self, page) -> list:
        return page.locator("#conversation .message-text").all_text_contents()

    def jobs(self) -> list:
        with GameStore.open_existing(self.trail) as store:
            return store.query("SELECT state FROM referee_jobs ORDER BY started_at")

    def wait_for_jobs(self, states):
        deadline = time.monotonic() + 10
        while self.jobs() != states:
            if time.monotonic() > deadline:
                self.fail(f"the jobs are {self.jobs()}, not {states}")
            time.sleep(0.05)


class CreationTests(CharacterCase):
    def test_a_newcomer_creates_privately_while_another_player_plays(self):
        bob = self.entered("bob")
        expect(bob.locator("#characters-empty")).to_be_visible()
        expect(bob.locator("#composer-label")).to_have_text(
            "Ask a setup or rules question (nothing in the game changes)"
        )
        alice = self.entered("alice")
        expect(alice.locator("#character-list button")).to_have_text(
            ["Play Mara", "Play Wren"]
        )
        self.play(alice, "Mara")
        expect(alice.locator("#console-characters")).to_be_hidden()
        expect(alice.locator("#sheet-title")).to_have_text("Mara")

        bob.locator("#create-name").fill("Ash")
        bob.locator("#character-create").click()
        self.expect_status(
            bob, "Trail · Creating Ash (draft) · private to you · Joined as Bob"
        )
        expect(bob.locator("#composer-private-label")).to_be_hidden()
        approve = bob.locator("#character-approve")
        expect(approve).to_have_text("Approve Ash (draft) (revision 1)")
        self.say(bob, "build Ash keen")
        self.expect_answered(bob, "Ash is keen; approve when you are ready.")
        expect(bob.locator("#conversation .audience").first).to_have_text(
            "Private · creating Ash"
        )
        expect(approve).to_have_text(
            "Approve Ash (draft) (revision 4)"
        )  # the sheet was re-read
        expect(bob.locator("#sheet-body")).to_contain_text("keen")

        # Between Bob's answers the referee is free: Alice plays.
        self.say(alice, "Mara looks down the trail.")
        self.expect_answered(alice, "Heard: Mara looks down the trail.")
        expect(
            bob.locator("#conversation .sender", has_text="Alice as Mara")
        ).to_have_count(1)

        approve.click()
        self.expect_status(bob, "Trail · Ash · Joined as Bob")
        expect(bob.locator("#sheet-note")).to_contain_text("Ash is ready.")
        expect(bob.locator("#character-approve")).to_be_hidden()
        self.say(bob, "Ash steps up.")
        self.expect_answered(bob, "Heard: Ash steps up.")
        expect(
            alice.locator("#conversation .sender", has_text="Bob as Ash")
        ).to_have_count(1)

        seen = " ".join(self.texts(alice) + self.received[alice])
        self.assertNotIn("build Ash keen", seen)  # creation stayed Bob's
        self.assertNotIn("approve when you are ready", seen)
        for page in (alice, bob):
            self.assertNotIn(self.tokens["alice"], page.content())
            self.assertNotIn(self.tokens["bob"], page.content())
        self.assertEqual(self.errors, [])

    def test_names_are_shown_as_text(self):
        bob = self.entered("bob")
        bob.locator("#create-name").fill('<img src=x onerror="window.hacked=1">Kit')
        bob.locator("#character-create").click()
        self.expect_status(
            bob, 'Creating <img src=x onerror="window.hacked=1">Kit (draft)'
        )
        expect(bob.locator("#sheet-title")).to_contain_text("<img src=x")
        self.assertEqual(bob.locator("img").count(), 0)
        self.assertIsNone(bob.evaluate("window.hacked"))
        self.assertEqual(self.errors, [])


class ActorTests(CharacterCase):
    def test_one_player_plays_two_characters_on_two_pages(self):
        mara = self.entered("alice")
        wren = self.entered("alice")
        self.play(mara, "Mara")
        self.play(wren, "Wren")
        self.say(mara, "ask me")
        expect(mara.locator("#console-question-text")).to_have_text("Cross now?")
        self.say(wren, "Wren listens.")
        self.expect_answered(wren, "Heard: Wren listens.")
        expect(wren.locator("#console-question")).to_be_hidden()  # Mara's question
        expect(wren.locator("#console-last-text")).to_have_text("“Wren listens.”")
        expect(mara.locator("#console-last-text")).to_have_text("“ask me”")

        # Switching on the second page clears Wren's state; Mara's question shows.
        wren.get_by_role("button", name="Settings").click()
        wren.locator("#settings-change-character").click()
        self.expect_status(wren, "Trail · No character")
        expect(wren.locator("#console-last")).to_be_hidden()
        self.play(wren, "Mara")
        expect(wren.locator("#console-question-text")).to_have_text("Cross now?")
        expect(wren.locator("#console-last-text")).to_have_text("“ask me”")
        self.assertEqual(self.errors, [])

    def test_a_late_reply_never_reaches_the_next_character(self):
        page = self.entered("alice")
        self.play(page, "Mara")
        self.say(page, "hold")
        self.wait_for_jobs([("running",)])
        page.locator("#composer-text").fill("Half-typed for Mara")
        page.get_by_role("button", name="Settings").click()
        page.locator("#settings-change-character").click()
        self.play(page, "Wren")
        expect(page.locator("#composer-text")).to_have_value("")  # Mara's draft is gone
        self.release.set()
        self.wait_for_jobs([("completed",)])
        self.expect_answered(page, "Heard: hold")  # the table's conversation stays
        time.sleep(0.3)
        expect(page.locator("#console-last")).to_be_hidden()  # not Wren's input
        expect(page.locator("#composer-note")).to_have_text("")
        self.assertEqual(self.errors, [])

    def test_reconnect_returns_to_the_same_character(self):
        page = self.open()
        valve = Valve(page, self.host.uri)  # before its socket opens
        self.entered("alice", page)
        self.play(page, "Mara")
        valve.hold = True
        self.say(page, "Unheard?")
        self.wait_for_jobs([("completed",)])  # accepted; its reply held back
        self.host.stop()
        expect(page.locator("#console-reconnect")).to_be_visible()
        expect(page.locator("#console-unconfirmed-text")).to_have_text("“Unheard?”")
        self.host.start(self.host.port)
        valve.hold = False
        page.locator("#console-reconnect").click()
        self.expect_status(page, "Trail · Mara · Joined as Alice")
        expect(page.locator("#console-unconfirmed")).to_be_hidden()  # it was accepted
        expect(page.locator("#console-last-text")).to_have_text("“Unheard?”")
        self.assertEqual(self.errors, [])

    def test_a_character_reply_never_follows_the_page_out_of_its_campaign(self):
        page = self.open()
        valve = Valve(page, self.host.uri)
        self.entered("alice", page)
        valve.hold = True  # Play Mara's reply arrives only after leaving Trail
        page.locator("#character-list").get_by_role("button", name="Play Mara").click()
        page.locator("#console-leave").click()
        valve.release()
        self.expect_status(page, "Logged in as Alice")
        page.locator("#campaign-list").get_by_role("button", name="Dice").click()
        self.expect_status(page, "Dice · Joined as Alice")
        expect(page.locator("#console-status")).not_to_contain_text("Mara")
        expect(page.locator("#console-sheet")).to_be_hidden()
        self.say(page, "Hello, dice.")
        self.expect_answered(page, "Heard: Hello, dice.")  # no stray actor refused it
        self.assertEqual(self.errors, [])

    def test_a_character_archived_while_away_is_explained_and_nothing_is_missed(self):
        alice = self.entered("alice")
        self.play(alice, "Mara")
        self.host.stop()
        expect(alice.locator("#console-reconnect")).to_be_visible()
        with GameStore.open_existing(self.trail) as store:
            archived, _ = store.archive_character(
                ParticipantContext(ALICE), "away", MARA
            )
            self.assertEqual(archived["type"], "result", archived)
        self.host.start(self.host.port)
        bob = self.entered("bob")
        self.say(bob, "Bob arrives.")  # while Alice is still away
        self.expect_answered(bob, "Heard: Bob arrives.")
        alice.locator("#console-reconnect").click()
        self.expect_status(alice, "Trail · No character · Joined as Alice")
        expect(alice.locator("#sheet-note")).to_have_text(
            "Mara cannot be played now: That character is not available to you."
        )
        expect(alice.locator("#conversation .message-text")).to_contain_text(
            ["Bob arrives.", "Heard: Bob arrives."]  # missed while away, then read
        )
        expect(alice.locator("#character-list button")).to_have_text(["Play Wren"])
        self.assertEqual(self.errors, [])

    def test_archiving_from_settings_and_from_another_page(self):
        here = self.entered("alice")
        there = self.entered("alice")
        self.play(here, "Mara")
        self.play(there, "Mara")
        here.get_by_role("button", name="Settings").click()
        expect(here.locator("#settings-character")).to_have_text(
            f"Mara ({MARA}, ready)"
        )
        here.locator("#settings-archive").click()
        expect(here.locator("#archive-confirm-text")).to_contain_text("Archive Mara?")
        here.locator("#archive-no").click()
        expect(here.locator("#archive-confirm")).to_be_hidden()
        here.locator("#settings-archive").click()
        here.locator("#archive-yes").click()
        expect(here.locator("#settings-character")).to_have_text("None: questions only")
        here.get_by_role("button", name="Console").click()
        expect(here.locator("#sheet-note")).to_have_text(
            "Mara is archived; its history stays."
        )
        expect(here.locator("#character-list button")).to_have_text(["Play Wren"])

        self.expect_status(there, "Trail · No character")  # told by the host
        expect(there.locator("#sheet-note")).to_have_text(
            "Your character was archived from another page or client."
        )
        expect(there.locator("#character-list button")).to_have_text(["Play Wren"])
        with GameStore.open_existing(self.trail) as store:
            self.assertEqual(store.character(MARA).phase, "archived")
        self.assertEqual(self.errors, [])

    def test_a_refresh_keeps_nothing(self):
        page = self.entered("alice")
        self.play(page, "Mara")
        page.reload()
        expect(page.locator("#console-join")).to_be_visible()
        stored = page.evaluate(
            "[localStorage.length, sessionStorage.length, document.cookie]"
        )
        self.assertEqual(stored, [0, 0, ""])
        self.entered("alice", page)  # logged in again: no character until chosen
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
