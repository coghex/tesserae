"""The browser page for a campaign host and its guests (GOV-7, #138): a host
with no character inspects everything and edits another player's walker and
the world after reviewing exactly what runs; a stale edit needs review; a
guest's request waits in its Console while the host decides it in Settings;
host data stays in the host's Settings and is cleared when the role, the
connection or the identity changes. Chromium, against a lobby host with
Trail (Trailhead) and a scripted referee that proposes through the real
bridge tools. docs/designs/governance_contract.md section 15.

Like test_browser_shell, these skip without Playwright and Chromium unless
TESSERAE_BROWSER_TESTS=1."""

import asyncio
import json
import unittest

from tesserae.referee.tools import BridgeTools
from tesserae.registry import HostRegistry
from tesserae.store import GameStore

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_browser_characters import MARA, CharacterCase, reply
    from test_characters import use_legacy_trailhead
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_browser_characters import MARA, CharacterCase, reply
    from tests.test_characters import use_legacy_trailhead

try:
    from playwright.sync_api import expect
except ImportError:
    expect = None


class GovernanceCase(CharacterCase):
    """Trail with Bob, who has no character, as its campaign host. The
    referee also answers `ask host N` by asking the host for a starting
    bonus of N for the job's character."""

    def setUp(self):
        use_legacy_trailhead(self)  # its requests are legacy draft requests
        super().setUp()
        with HostRegistry(self.registry_path) as registry:
            registry.set_host(self.campaign, self.players["bob"])

    async def executor(self, job):
        if job.text.startswith("ask host "):
            await asyncio.to_thread(self.ask_host, job, int(job.text.split()[-1]))
            return reply(("sender", "I have asked the campaign host."))
        return await super().executor(job)

    def ask_host(self, job, bonus):
        hooks = self.host.lobby._services[self.campaign].bridge_hooks()
        tools = BridgeTools(lambda: GameStore(self.trail), **hooks)
        token = tools.grant(job.job_id)
        done = tools.handle({"token": token, "tool": "propose_host_decision", "args": {
            "step_id": "p1", "kind": "exception", "scope": "target", "target": None,
            "payload": {"starting_stamina_bonus": bonus},
            "explanation": "A guide by trade."}})  # fmt: skip
        tools.revoke()
        assert done["ok"], done

    def settings(self, page):
        page.locator('[data-screen="settings"]').click()

    def console(self, page):
        page.locator('[data-screen="console"]').click()

    def run_admin(self, page, command, args):
        """Choose, fill and review an operation; the confirmation is shown."""
        if page.locator("#host-admin-form").is_hidden():
            page.locator("#host-admin-load").click()
        page.locator("#host-admin-operation").select_option(command)
        page.locator("#host-admin-args").fill(json.dumps(args))
        page.locator("#host-admin-review").click()
        expect(page.locator("#host-admin-confirm")).to_be_visible()

    def stamina(self) -> int:
        with GameStore.open_existing(self.trail) as store:
            return store.inspect_state()["walkers"][MARA]["stamina"]


class HostTests(GovernanceCase):
    def test_a_host_with_no_character_inspects_and_edits_after_review(self):
        alice = self.entered("alice")
        self.play(alice, "Mara")
        bob = self.entered("bob")  # no character, and needs none
        self.settings(bob)
        expect(bob.locator("#settings-role")).to_have_text(
            "You are this campaign's host."
        )
        bob.locator("#host-inspect-load").click()
        expect(bob.locator("#host-inspect-body")).to_contain_text("Mara, ready, Alice")

        self.run_admin(
            bob, "trailhead.set_stamina", {"character_id": MARA, "stamina": 1}
        )
        expect(bob.locator("#host-admin-confirm-text")).to_contain_text(
            "Run trailhead.set_stamina at revision"
        )
        self.assertEqual(self.stamina(), 3)  # nothing runs before Run it
        bob.locator("#host-admin-run").click()
        expect(bob.locator("#host-note")).to_contain_text("Done at revision")
        self.assertEqual(self.stamina(), 1)
        expect(alice.locator("#sheet-body")).to_contain_text("1")  # her sheet follows

        self.run_admin(bob, "trailhead.scene_set", {"description": "Snow on the pass."})
        bob.locator("#host-admin-run").click()
        expect(bob.locator("#host-note")).to_contain_text("Done at revision")
        expect(alice.locator("#console-state-body")).to_contain_text(
            "Snow on the pass."
        )
        self.assertEqual(self.errors, [])

    def test_an_edit_reviewed_before_the_game_moved_needs_review_again(self):
        bob = self.entered("bob")
        self.settings(bob)
        self.run_admin(
            bob, "trailhead.set_stamina", {"character_id": MARA, "stamina": 0}
        )
        other = self.entered("bob")  # another of Bob's pages moves the game on
        self.settings(other)
        self.run_admin(other, "trailhead.scene_set", {"description": "Fog."})
        other.locator("#host-admin-run").click()
        expect(other.locator("#host-note")).to_contain_text("Done at revision")
        bob.locator("#host-admin-run").click()
        expect(bob.locator("#host-note")).to_have_text(
            "The game moved on since you reviewed this, so nothing changed. Review it again."
        )
        self.assertEqual(self.stamina(), 3)
        self.assertEqual(self.errors, [])


class HandoverTests(GovernanceCase):
    def test_the_host_reads_its_audit_and_hands_the_campaign_over(self):
        alice = self.entered("alice")
        bob = self.entered("bob")
        self.settings(bob)
        self.run_admin(bob, "trailhead.scene_set", {"description": "Fog."})
        bob.locator("#host-admin-run").click()
        expect(bob.locator("#host-note")).to_contain_text("Done at revision")
        bob.locator("#host-audit-load").click()
        expect(bob.locator("#host-audit-list")).to_contain_text(
            f"operation by {self.players['bob']} (client)"
        )
        bob.locator("#host-transfer-player").fill(self.players["alice"])
        bob.locator("#host-transfer-ask").click()
        expect(bob.locator("#host-transfer-text")).to_contain_text(
            f"Hand this campaign to {self.players['alice']}?"
        )
        bob.locator("#host-transfer-yes").click()
        expect(bob.locator("#host-note")).to_contain_text("you are a guest here now")
        expect(bob.locator("#host-requests")).to_be_hidden()
        expect(bob.locator("#host-audit-list")).to_have_text("")
        expect(bob.locator("#settings-role")).to_contain_text("You are a guest here.")
        self.settings(alice)  # told at once, with its controls
        expect(alice.locator("#settings-role")).to_have_text(
            "You are this campaign's host."
        )
        expect(alice.locator("#host-requests")).to_be_visible()
        self.assertEqual(self.errors, [])


class ReviewRegressionTests(GovernanceCase):
    def test_a_rules_reply_for_an_earlier_actor_is_never_shown(self):
        page, held, released = self.open(), [], []

        def route(socket):
            server = socket.connect_to_server()

            def from_host(message):
                kind = json.loads(message)["type"]
                if self.holding or (released and kind == "rules_view"):
                    held.append(message)
                else:
                    socket.send(message)

            server.on_message(from_host)
            socket.on_message(server.send)
            self.page_socket = socket

        self.holding = False
        page.route_web_socket(f"{self.host.uri}**", route)
        alice = self.entered("alice", page)
        self.settings(alice)
        expect(alice.locator("#settings-rules-body")).to_contain_text(
            "trait (mechanical):"
        )
        self.console(alice)
        self.holding = True
        alice.locator("#character-list").get_by_role("button", name="Play Mara").click()
        self.settings(alice)  # still in setup here: its rules are asked for again
        alice.locator("#settings-rules-refresh").click()
        while len(held) < 2:
            alice.wait_for_timeout(20)
        self.holding = False
        released.append(True)  # Mara's selection and setup's rules; Mara's rules wait
        for message in held[:2]:
            self.page_socket.send(message)
        del held[:2]
        self.expect_status(alice, "Trail · Mara · Joined as Alice")
        alice.wait_for_timeout(200)
        expect(alice.locator("#settings-rules-body")).to_have_text("")  # never setup's
        while not held:
            alice.wait_for_timeout(20)
        released.clear()
        for message in held:
            self.page_socket.send(message)
        expect(alice.locator("#settings-rules-body")).to_contain_text(
            "trait (mechanical, done)"
        )
        self.assertEqual(self.errors, [])

    def test_the_audit_is_read_a_page_at_a_time(self):
        with GameStore.open_existing(self.trail) as store, store.transaction() as db:
            db.executemany(
                "INSERT INTO host_audit (kind, host_id, player_id, via, detail, "
                "revision, created_at) VALUES ('operation', NULL, ?, 'client', ?, 0, 'now')",
                [(self.players["bob"], json.dumps({"n": n})) for n in range(205)],
            )
        bob = self.entered("bob")
        self.settings(bob)
        bob.locator("#host-audit-load").click()
        expect(bob.locator("#host-audit-more")).to_be_visible()  # a page, not all
        first = bob.locator("#host-audit-list li").count()
        self.assertLess(first, 205)
        for _ in range(3):  # pages of 200 rows or 32 KiB: a few at most
            if not bob.locator("#host-audit-more").is_visible():
                break
            shown = bob.locator("#host-audit-list li").count()
            bob.locator("#host-audit-more").click()
            expect(bob.locator("#host-audit-list li")).not_to_have_count(shown)
        expect(bob.locator("#host-audit-list li")).to_have_count(205)
        expect(bob.locator("#host-audit-list li").last).to_contain_text('{"n":204}')
        expect(bob.locator("#host-audit-more")).to_be_hidden()
        self.assertEqual(self.errors, [])


class RequestTests(GovernanceCase):
    def test_a_guest_waits_while_the_host_decides_her_request(self):
        alice = self.entered("alice")
        alice.locator("#create-name").fill("Ash")
        alice.locator("#character-create").click()
        self.expect_status(alice, "Creating Ash (draft)")
        self.say(alice, "build Ash keen")
        self.expect_answered(alice, "Ash is keen; approve when you are ready.")
        self.say(alice, "ask host 1")
        self.expect_answered(alice, "I have asked the campaign host.")
        expect(alice.locator("#console-requests")).to_contain_text(
            'pending: exception {"starting_stamina_bonus":1}'
        )
        self.settings(alice)  # a guest: the rules and the privacy, nothing of the host
        expect(alice.locator("#settings-role")).to_contain_text(
            "The campaign host can read your private creation and conversation"
        )
        expect(alice.locator("#settings-rules-body")).to_contain_text(
            "trait (mechanical, done)"
        )
        expect(alice.locator("#host-panels")).to_be_hidden()
        self.console(alice)

        bob = self.entered("bob")
        self.settings(bob)
        request = bob.locator("#host-request-list li").first
        expect(request).to_contain_text(
            'Alice asks for exception {"starting_stamina_bonus":1} on character'
        )
        expect(request).to_contain_text("“A guide by trade.”")
        request.locator('input[type="text"]').fill("Welcome, guide.")
        request.get_by_role("button", name="Approve").click()
        expect(bob.locator("#host-note")).to_contain_text("is now approved.")
        expect(alice.locator("#console-requests")).to_contain_text(
            "approved: exception"
        )
        expect(alice.locator("#console-requests")).to_contain_text(
            "The host's note: Welcome, guide."
        )
        expect(alice.locator("#sheet-revision")).to_contain_text("Draft revision 5")
        self.assertEqual(self.errors, [])


class PrivilegeTests(GovernanceCase):
    def test_host_data_is_cleared_when_the_role_or_the_connection_changes(self):
        alice = self.entered("alice")
        self.play(alice, "Mara")
        alice.locator("#composer-private").check()
        self.say(alice, "A secret plan.")
        self.expect_answered(alice, "Heard: A secret plan.")
        bob = self.entered("bob")
        self.settings(bob)
        bob.locator("#host-inspect-load").click()
        bob.locator("#host-messages-load").click()
        expect(bob.locator("#host-message-list")).to_contain_text("A secret plan.")
        expect(bob.locator("#host-message-list")).to_contain_text("To Alice")
        expect(bob.locator("#conversation")).not_to_contain_text("A secret plan.")

        # A reconnect clears it all until the host says Bob is still its host;
        # nothing is read or run again by itself.
        port = self.host.port
        self.host.stop()
        self.console(bob)
        bob.locator("#console-reconnect").wait_for()
        self.host.start(port)
        bob.locator("#console-reconnect").click()
        self.expect_status(bob, "Trail · No character · Joined as Bob")
        self.settings(bob)
        expect(bob.locator("#host-requests")).to_be_visible()
        expect(bob.locator("#host-message-list")).to_have_text("")
        expect(bob.locator("#host-inspect-body")).to_have_text("")

        bob.locator("#host-inspect-load").click()
        expect(bob.locator("#host-inspect-body")).to_contain_text("Mara, ready, Alice")
        with HostRegistry(self.registry_path) as registry:
            registry.set_host(self.campaign, self.players["alice"])
        bob.locator("#host-requests-refresh").click()  # refused: Bob is a guest now
        expect(bob.locator("#host-note")).to_be_visible()
        expect(bob.locator("#host-requests")).to_be_hidden()
        expect(bob.locator("#host-inspect-body")).to_have_text("")
        expect(bob.locator("#settings-role")).to_contain_text("You are a guest here.")
        self.assertNotIn("A secret plan.", bob.content())
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
