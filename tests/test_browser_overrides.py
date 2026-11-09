"""The browser's controls for negotiated creation overrides (OVR-6, #178),
in a real browser against a real lobby host: request, counteroffer and
acceptance with nothing applied before it; denial and a deliberate
revision; an unrelated creation step keeping an offer usable and a relevant
change staling it, refused with nothing applied; a refresh finding every
offer again; authority moving with a transfer; another guest seeing nothing;
and an unused allowance expiring at approval. Trail is an original
Trailhead campaign; a scripted executor stands in for Codex.
docs/designs/creation_overrides_contract.md section 12."""

import asyncio
import json
import unittest

from tesserae.credentials import new_token
from tesserae.referee.tools import BridgeTools
from tesserae.registry import HostRegistry
from tesserae.store import GameStore

try:  # tests/ is on sys.path under `unittest discover -s tests`
    from test_browser_characters import CharacterCase, reply
except ImportError:  # running as `python -m unittest tests.test_...`
    from tests.test_browser_characters import CharacterCase, reply

try:
    from playwright.sync_api import expect
except ImportError:
    expect = None


def modifier(amount) -> dict:
    return {"changes": [{"quality_id": "max_stamina_modifier", "action_id": "set",
                         "parameters": {"amount": amount}}]}  # fmt: skip


class OverrideCase(CharacterCase):
    """Trail with Bob, who has no character, as its campaign host; Dora is
    a guest with no part in Alice's requests. The referee also answers
    `ask modifier N` by asking the host to set the job's draft's maximum
    stamina modifier to N, and `fact TEXT` by recording a background fact
    (an unrelated creation step)."""

    def setUp(self):
        super().setUp()
        with HostRegistry(self.registry_path) as registry:
            registry.set_host(self.campaign, self.players["bob"])
            self.tokens["dora"] = new_token()
            self.players["dora"] = registry.add_player("Dora", self.tokens["dora"])
            registry.grant(self.campaign, self.players["dora"])

    async def executor(self, job):
        if job.text.startswith("ask modifier "):
            amount = int(job.text.split()[-1])
            await asyncio.to_thread(self.bridge, job, "propose_host_decision", {
                "step_id": "p1", "kind": "creation_override", "scope": "target",
                "target": None, "payload": {"intent": modifier(amount)},
                "explanation": "A mountain guide by trade."})  # fmt: skip
            return reply(("sender", "I have asked the host; it waits for them."))
        if job.text.startswith("fact "):
            revision = await asyncio.to_thread(self.bridge, job, "act", {
                "step_id": "f1", "command": "trailhead.fact",
                "args": {"fact": "background", "text": job.text[5:],
                         "provenance": "chosen"}})  # fmt: skip
            self.host.lobby._services[self.campaign]._publish(revision)
            return reply(("sender", "Noted."))
        return await super().executor(job)

    def bridge(self, job, tool, args):
        service = self.host.lobby._services[self.campaign]
        # The host's own stores, as its referee opens them: each commit's
        # proposal notices reach the pages.
        tools = BridgeTools(service.open_store, **service.bridge_hooks())
        token = tools.grant(job.job_id)
        done = tools.handle({"token": token, "tool": tool, "args": args})
        tools.revoke()
        assert done["ok"], done
        return done["result"].get("result", {}).get("revision")

    def settings(self, page):
        page.locator('[data-screen="settings"]').click()

    def console(self, page):
        page.locator('[data-screen="console"]').click()

    def creating(self, page, amount=2) -> None:
        """Alice's new draft Ash, built, asking for a modifier of `amount`."""
        page.locator("#create-name").fill("Ash")
        page.locator("#character-create").click()
        self.expect_status(page, "Creating Ash (draft)")
        self.say(page, "build Ash keen")
        self.expect_answered(page, "Ash is keen; approve when you are ready.")
        self.say(page, f"ask modifier {amount}")
        self.expect_answered(page, "I have asked the host; it waits for them.")
        expect(page.locator("#console-overrides")).to_contain_text(
            "Waiting for the host"
        )

    def walker(self) -> dict:
        with GameStore.open_existing(self.trail) as store:
            [draft] = [c.character_id for c in store.inspect_characters()
                       if c.phase == "draft"]  # fmt: skip
            return store.inspect_state()["walkers"][draft], draft

    def host_item(self, page):
        return page.locator("#host-override-list li").first

    def offer(self, bob, counter=None, note="One is fair."):
        """Bob reviews the request, then offers it or counteroffers."""
        self.settings(bob)
        item = self.host_item(bob)
        item.get_by_role("button", name="Review").click()
        expect(item).to_contain_text("What it would do now:")
        if counter is None:
            item.get_by_role("button", name="Offer", exact=True).click()
        else:
            item.get_by_role("button", name="Counteroffer…").click()
            bob.locator("#host-counter-intent").fill(json.dumps(counter))
            bob.locator("#host-counter-review").click()
            expect(bob.locator("#host-counter-preview")).to_contain_text(
                "max_stamina_modifier"
            )
            bob.locator("#host-counter-note").fill(note)
            bob.locator("#host-counter-send").click()
        expect(bob.locator("#host-note")).to_contain_text(
            "Nothing applies until the player accepts."
        )


class NegotiationTests(OverrideCase):
    def test_request_counteroffer_and_acceptance_apply_only_on_acceptance(self):
        alice = self.entered("alice")
        self.creating(alice, 2)
        bob = self.entered("bob")
        self.settings(bob)
        item = self.host_item(bob)
        expect(item).to_contain_text("Waiting for the host. Alice asks for")
        expect(item).to_contain_text("“A mountain guide by trade.”")
        item.get_by_role("button", name="Review").click()
        expect(item).to_contain_text("max_stamina_modifier: 0 → 2")
        item.get_by_role("button", name="Counteroffer…").click()
        self.assertEqual(json.loads(bob.locator("#host-counter-intent").input_value()),
                         modifier(2))  # prefilled from the request  # fmt: skip
        self.offer(bob, counter=modifier(1))

        offered = alice.locator("#console-override-list li").first
        expect(offered).to_contain_text(
            "Offered, not applied yet (offer 1, a counteroffer)"
        )
        expect(offered).to_contain_text("The host's note: One is fair.")
        expect(offered).to_contain_text("max_stamina_modifier: 0 → 1")
        walker, _ = self.walker()
        self.assertNotIn("overrides", walker)  # an offer changes nothing
        offered.get_by_role("button", name="Accept…").click()
        expect(alice.locator("#override-accept-text")).to_contain_text(
            "Accept offer 1 exactly as shown?"
        )
        walker, _ = self.walker()
        self.assertNotIn("overrides", walker)  # nor does asking to confirm
        alice.locator("#override-accept-yes").click()
        expect(alice.locator("#console-overrides-note")).to_have_text(
            "Offer 1 is applied to your draft."
        )
        expect(alice.locator("#console-overrides")).to_contain_text("Applied")
        walker, _ = self.walker()
        self.assertEqual(walker["overrides"]["max_stamina_modifier"]["amount"], 1)
        self.assertFalse(walker["ready"])  # approving the character stays separate
        expect(alice.locator("#character-approve")).to_be_visible()
        self.assertEqual(self.errors, [])

    def test_denial_then_a_deliberate_revision(self):
        alice = self.entered("alice")
        self.creating(alice, 2)
        bob = self.entered("bob")
        self.settings(bob)
        item = self.host_item(bob)
        item.get_by_role("button", name="Deny").click()  # a denial needs a note
        expect(bob.locator("#host-note")).to_contain_text("write a note first")
        item.locator("input[data-override-note]").fill("Not on this trail.")
        item.get_by_role("button", name="Deny").click()
        expect(bob.locator("#host-note")).to_contain_text("is denied.")
        denied = alice.locator("#console-override-list li").first
        expect(denied).to_contain_text("Denied")
        expect(denied).to_contain_text("The host's note: Not on this trail.")
        denied.get_by_role("button", name="Revise").click()
        expect(alice.locator("#override-revise")).to_be_visible()
        self.assertEqual(json.loads(alice.locator("#override-revise-intent").input_value()),
                         modifier(2))  # fmt: skip
        alice.locator("#override-revise-intent").fill(json.dumps(modifier(1)))
        alice.locator("#override-revise-explanation").fill("Then just one.")
        alice.locator("#override-revise-send").click()
        expect(alice.locator("#console-overrides-note")).to_contain_text("Asked again")
        expect(self.host_item(bob)).to_contain_text("“Then just one.”")
        self.assertEqual(self.errors, [])

    def test_offers_hold_through_unrelated_steps_and_go_stale_on_relevant_ones(self):
        alice = self.entered("alice")
        self.creating(alice, 2)
        bob = self.entered("bob")
        self.offer(bob)
        offered = alice.locator("#console-override-list li").first
        expect(offered).to_contain_text("Offered, not applied yet (offer 1)")
        # An unrelated creation step: the offer stays usable.
        self.say(alice, "fact A shepherd's child.")
        self.expect_answered(alice, "Noted.")
        alice.reload()  # and a refresh finds it again, from the host
        self.entered("alice", alice)
        alice.locator("#character-list").get_by_role("button", name="Ash").click()
        offered = alice.locator("#console-override-list li").first
        expect(offered).to_contain_text("Offered, not applied yet (offer 1)")
        offered.get_by_role("button", name="Accept…").click()
        expect(alice.locator("#override-accept-confirm")).to_be_visible()
        # Before she confirms, the host sets the modifier directly: relevant.
        _, draft = self.walker()
        bob.locator("#host-override-character").fill(draft)
        bob.locator("#host-override-intent").fill(json.dumps(modifier(-1)))
        bob.locator("#host-override-review").click()
        expect(bob.locator("#host-override-preview")).to_contain_text("0 → -1")
        bob.locator("#host-override-reason").fill("The trail is hard.")
        bob.locator("#host-override-apply").click()
        expect(bob.locator("#host-note")).to_contain_text("Applied to")
        alice.locator("#override-accept-yes").click()
        expect(alice.locator("#console-overrides-note")).to_contain_text(
            "nothing was applied"
        )
        expect(alice.locator("#console-overrides")).to_contain_text(
            "Waiting for the host"
        )
        walker, _ = self.walker()
        self.assertEqual(walker["overrides"]["max_stamina_modifier"]["amount"], -1)
        self.assertEqual(self.errors, [])

    def test_authority_moves_and_other_guests_see_nothing(self):
        alice = self.entered("alice")
        self.creating(alice, 2)
        dora = self.entered("dora")
        self.settings(dora)
        expect(dora.locator("#host-panels")).to_be_hidden()
        self.assertEqual(dora.locator("#host-override-list li").count(), 0)
        self.assertEqual(dora.locator("#console-override-list li").count(), 0)
        self.assertNotIn("mountain guide", dora.content())
        bob = self.entered("bob")
        self.offer(bob)
        bob.locator("#host-transfer-player").fill(self.players["alice"])
        bob.locator("#host-transfer-ask").click()
        bob.locator("#host-transfer-yes").click()
        expect(bob.locator("#host-overrides")).to_be_hidden()
        self.assertEqual(bob.locator("#host-override-list li").count(), 0)
        # Alice, host now, sees her own request, its offer no longer current.
        offered = alice.locator("#console-override-list li").first
        expect(offered).to_contain_text("no longer holds (host_changed)")
        self.assertNotIn("mountain guide", dora.content())
        self.assertEqual(self.errors, [])

    def test_an_unused_allowance_expires_when_the_character_is_approved(self):
        alice = self.entered("alice")
        self.creating(alice, 2)
        _, draft = self.walker()
        bob = self.entered("bob")
        self.settings(bob)
        bob.locator("#host-override-character").fill(draft)
        bob.locator("#host-override-discover").click()
        expect(bob.locator("#host-override-qualities")).to_contain_text("starting_gear")
        picks = {"changes": [{"quality_id": "starting_gear", "action_id": "allow",
                              "parameters": {"count": 2}}]}  # fmt: skip
        bob.locator("#host-override-intent").fill(json.dumps(picks))
        bob.locator("#host-override-review").click()
        expect(bob.locator("#host-override-preview")).to_contain_text(
            "Allows, until approval"
        )
        bob.locator("#host-override-reason").fill("A gift from the hut.")
        bob.locator("#host-override-apply").click()
        expect(bob.locator("#host-note")).to_contain_text("Applied to")
        expect(alice.locator("#sheet-body")).to_contain_text("gear_picks")
        alice.locator("#character-approve").click()
        self.expect_status(alice, "Trail · Ash · Joined as")
        with GameStore.open_existing(self.trail) as store:
            walker = store.inspect_state()["walkers"][draft]
            kinds = [r[0] for r in store.query(
                "SELECT kind FROM override_records ORDER BY rowid")]  # fmt: skip
        # Its only override was the picks: none is left, and the record
        # of what applied stays.
        self.assertNotIn("gear_picks", walker.get("overrides", {}))
        self.assertEqual(kinds, ["direct", "expiry"])
        expect(alice.locator("#sheet-body")).not_to_contain_text("gear_picks")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
