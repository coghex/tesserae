"""`tesserae connect --lobby` for the campaign host and its guests (GOV-6,
#137): roles, rules and requirements, requests and exact decisions, host
inspection and confirmed administration with stale and busy refusals, retry
under the same authority only, and privilege lost to a transfer. Real client
processes against a lobby host on its own thread, with an original
Trailhead campaign and a scripted referee that proposes through the real
bridge tools. docs/designs/governance_contract.md section 15."""

import asyncio
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

from test_characters import use_legacy_trailhead
from test_client_characters import ALICE, MARA, TRAILHEAD, reply, seed
from test_referee_host import TIMEOUT, Client

from tesserae.credentials import new_token, write_token_file
from tesserae.lobby import LobbyHost
from tesserae.referee.store import RefereeStore
from tesserae.referee.tools import BridgeTools
from tesserae.registry import HostRegistry
from tesserae.seed import parse_seed
from tesserae.store import GameStore

PROPOSAL = re.compile(r"hp-[0-9a-f]{12}")


class GovernedTrail:
    """A lobby host on its own thread. Its referee answers `Heard: TEXT`;
    `build NAME TRAIT` makes the creation job's draft; `ask host N` asks the
    campaign host for a starting bonus of N for the job's character, through
    the bridge with this lobby's own host authority; `hold` waits until
    `release` is set."""

    def __init__(self, registry: Path, trail: Path):
        self.release = threading.Event()
        self.trail = trail
        ready = threading.Event()

        def executor_for(campaign_id):
            async def executor(job):
                if job.text == "hold":
                    await asyncio.to_thread(self.release.wait, TIMEOUT)
                if job.text.startswith("build "):
                    _, name, trait = job.text.split()
                    await asyncio.to_thread(self.tools, campaign_id, job, [
                        ("act", {"step_id": "s1", "command": "character.name",
                                 "args": {"name": name}}),
                        ("act", {"step_id": "s2", "command": "trailhead.trait",
                                 "args": {"trait": trait}}),
                        ("act", {"step_id": "s3", "command": "trailhead.stamina", "args": {}}),
                    ])  # fmt: skip
                    return reply(("sender", f"{name} is {trait}."))
                if job.text.startswith("ask host "):
                    bonus = int(job.text.split()[-1])
                    await asyncio.to_thread(self.tools, campaign_id, job, [
                        ("propose_host_decision", {
                            "step_id": "p1", "kind": "exception", "scope": "target",
                            "target": None, "payload": {"starting_stamina_bonus": bonus},
                            "explanation": "A guide by trade."}),
                    ])  # fmt: skip
                    return reply(("sender", "I have asked the campaign host."))
                audience = "sender" if job.private else "table"
                return reply((audience, f"Heard: {job.text}"))

            return executor

        def main():
            loop = asyncio.new_event_loop()
            self.loop = loop
            self.stop = loop.create_future()
            self.lobby = LobbyHost(registry, referee_executor_for=executor_for)

            def on_ready(uri):
                self.uri = uri
                ready.set()

            loop.run_until_complete(self.lobby.run(0, self.stop, on_ready=on_ready))
            loop.close()

        self.thread = threading.Thread(target=main, daemon=True)
        self.thread.start()
        ready.wait(TIMEOUT)

    def tools(self, campaign_id, job, calls):
        hooks = self.lobby._services[campaign_id].bridge_hooks()
        tools = BridgeTools(
            lambda: GameStore(self.trail, randint=lambda low, high: 2), **hooks
        )
        token = tools.grant(job.job_id)
        for tool, args in calls:
            done = tools.handle({"token": token, "tool": tool, "args": args})
            assert done["ok"], done
        tools.revoke()

    def close(self):
        self.release.set()
        self.loop.call_soon_threadsafe(
            lambda: self.stop.done() or self.stop.set_result(None)
        )
        self.thread.join(TIMEOUT)


class GovernanceClientTests(unittest.TestCase):
    """Trail is a Trailhead campaign in referee mode whose campaign host is
    Bob, who has no character; Alice, a guest, has Mara."""

    def setUp(self):
        use_legacy_trailhead(self)  # its requests are legacy draft requests
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry_path = self.root / "host.sqlite3"
        self.trail = self.root / "trail.sqlite3"
        self.files = {}
        with GameStore.create(self.trail, TRAILHEAD, seed=parse_seed(seed())) as store:
            RefereeStore(store).enable()
        with HostRegistry.create(self.registry_path) as registry:
            self.players = {}
            for name in ("alice", "bob"):
                token = new_token()
                self.players[name] = registry.add_player(name.title(), token)
                self.files[name] = self.root / f"{name}.token"
                write_token_file(self.files[name], token)
            self.campaign = registry.register_campaign(self.trail, "Trail",
                                                       self.players["bob"])  # fmt: skip
            registry.grant(self.campaign, self.players["alice"])
            registry.set_campaign_active(self.campaign, True)
            host_id = registry.host_id
        with GameStore.open_existing(self.trail) as store:
            store.link_host_player(host_id, self.players["alice"], ALICE)
        self.host = GovernedTrail(self.registry_path, self.trail)
        self.addCleanup(self.host.close)

    def entered(self, name, *extra) -> Client:
        client = Client(self, self.host.uri, self.files[name], "--lobby",
                        "--campaign", self.campaign, *extra)  # fmt: skip
        client.until("[status]")
        if "--character" in extra:
            client.until("[status]")
        return client

    def store(self) -> GameStore:
        store = GameStore.open_existing(self.trail)
        self.addCleanup(store.close)
        return store

    def asked(self, alice, bonus) -> str:
        """Alice's creation job asks the host; the request's ID."""
        alice.type(f"ask host {bonus}")
        lines = alice.until("[idle]")
        [notice] = [line for line in lines if line.startswith("[request]")]
        self.assertTrue(notice.endswith("is now pending."), lines)
        return PROPOSAL.search(notice).group()

    def test_a_host_with_no_character_decides_exact_requests(self):
        bob = self.entered("bob")
        bob.type("/role")
        self.assertEqual(
            bob.line(),
            "[role] You are this campaign's host. Its settings are at revision 0.",
        )
        alice = self.entered("alice")
        alice.type("/create")
        alice.until("[status]")
        alice.type("build Ash keen")
        alice.until("[idle]")
        first = self.asked(alice, 1)
        self.assertIn(f"[request] {first} is now pending.", bob.until("[request]"))

        bob.type("/requests")
        [listed] = bob.until(f"[request] {first}")[-1:]
        self.assertIn('asks for exception {"starting_stamina_bonus": 1}', listed)
        bob.type(f"/decide {first} approve Welcome, guide.")
        decided = bob.until(f"[request] {first} is now approved")[-1]
        self.assertIn('"policy_revision": 1', decided)
        self.assertIn(f"[request] {first} is now approved.", alice.until("[request]"))

        second = self.asked(alice, 2)
        bob.until(f"[request] {second} is now pending")
        bob.type(f"/decide {second} deny Too much.")
        bob.until(f"[request] {second} is now denied.")
        alice.until(f"[request] {second} is now denied.")
        alice.type("/requests")
        mine = alice.until(f"[request] {second}")
        self.assertIn(f"[request] {first} approved", " ".join(mine))
        self.assertEqual(alice.until("[request]")[-1], '[request]   note: "Too much."')
        alice.type("/rules")
        lines = alice.until("[requirement] home")
        self.assertIn(
            '[rules] Settings revision 1: {"starting_stamina_bonus": 1}', lines
        )
        self.assertTrue(
            any(line.startswith("[rules] Your exception:") for line in lines)
        )
        # A second decision is refused, and nothing is replayed under it.
        bob.type(f"/decide {first} deny")
        self.assertEqual(
            bob.until("[error]")[-1], "[error] That proposal was already approved."
        )
        # Handing the campaign over, after confirming, ends Bob's host commands.
        bob.type(f"/transfer {self.players['alice']}")
        self.assertTrue(
            bob.until("[role]")[-1].startswith("[role] Hand this campaign to")
        )
        bob.type(f"/transfer {self.players['alice']} confirm")
        self.assertEqual(
            bob.until("[role] You handed")[-1],
            "[role] You handed the campaign over; you are a guest here now.",
        )
        self.assertIn("[role] You are now this campaign's host.", alice.until("[role]"))
        bob.type(f"/decide {second} approve", "/inspect")
        self.assertEqual(
            bob.until("[error]")[-1], "[error] Only the campaign host can do that."
        )
        self.assertEqual((alice.finish(), bob.finish()), (0, 0))

    def test_a_host_edits_a_guests_walker_only_as_confirmed(self):
        bob = self.entered("bob")
        bob.type("/create Fen", "/create Gil")  # a host may have many characters
        bob.until("[status]")
        bob.until("[status]")
        bob.type("/admin")
        offered = bob.until("[admin] trailhead.prep_set")
        self.assertIn("[admin] trailhead.set_stamina (character_id, stamina): "
                      "Correct a ready walker's stamina, from 0 to its maximum.", offered)  # fmt: skip
        bob.type(
            f'/admin trailhead.set_stamina {{"character_id": "{MARA}", "stamina": 1}}'
        )
        prompt = bob.line()
        self.assertTrue(prompt.startswith("[admin] Run trailhead.set_stamina"), prompt)
        self.assertTrue(prompt.endswith("Type /admin confirm to run it."), prompt)

        # Another of Bob's connections changes the game meanwhile.
        other = self.entered("bob")
        other.type(
            '/admin trailhead.scene_set {"description": "Fog."}', "/admin confirm"
        )
        self.assertTrue(
            other.until("[admin] Done")[-1].startswith("[admin] Done at revision")
        )
        bob.type("/sync")  # Bob sees the new revision; his confirmation is still
        bob.until("[view]")  # for the one he was shown
        bob.type("/admin confirm")
        self.assertEqual(bob.until("[admin] The game moved on")[-2:], [
            f"[error] Expected revision {self.revision() - 1}, but the game is at "
            f"revision {self.revision()}.",
            "[admin] The game moved on since you looked; check it (/inspect) and type "
            "the change again.",
        ])  # fmt: skip
        self.assertEqual(self.store().inspect_state()["walkers"][MARA]["stamina"], 3)
        bob.type("/admin confirm")
        self.assertEqual(bob.line(), "[admin] There is nothing to confirm; type /admin "
                                     "COMMAND ARGS-JSON first.")  # fmt: skip
        bob.type(f'/admin trailhead.set_stamina {{"character_id": "{MARA}", "stamina": 1}}',
                 "/admin confirm", "/inspect Mara")  # fmt: skip
        self.assertTrue(
            bob.until("[admin] Done")[-1].startswith("[admin] Done at revision")
        )
        inspected = bob.until("[inspect]")[-1]
        self.assertTrue(
            inspected.startswith("[inspect] Mara (ready, Alice): "), inspected
        )
        self.assertIn('"stamina": 1', inspected)
        self.assertEqual(bob.finish(), 0)

    def test_a_guest_reads_her_rules_and_is_refused_every_host_command(self):
        alice = self.entered("alice", "--character", MARA)
        alice.type("/rules")
        lines = alice.until("[requirement] home")
        self.assertEqual(
            lines[0], '[rules] Settings revision 0: {"starting_stamina_bonus": 0}'
        )
        self.assertIn("[requirement] trait, mechanical (done): ", "\n".join(lines))
        refusal = [
            "[error] Only the campaign host can do that.",
            "[role] Only the campaign host can do that here; /role says what you are.",
        ]
        for typed in ("/inspect", "/history-all", "/admin", "/rules list", "/audit",
                      "/decide hp-0123456789ab approve"):  # fmt: skip
            with self.subTest(typed=typed):
                alice.type(typed)
                self.assertEqual([alice.line(), alice.line()], refusal)
        alice.type(
            '/admin trailhead.scene_set {"description": "Mine."}', "/admin confirm"
        )
        alice.line()  # the confirmation prompt: the client decides nothing
        self.assertEqual([alice.line(), alice.line()], refusal)
        self.assertNotIn("scene", self.store().inspect_state())
        self.assertEqual(alice.finish(), 0)

    def test_retry_resends_a_busy_edit_only_under_the_same_authority(self):
        bob, alice = self.entered("bob"), self.entered("alice", "--character", MARA)
        alice.type("hold")
        alice.until("[busy]")
        edit = '/admin trailhead.scene_set {"description": "Fog."}'
        bob.type(edit, "/admin confirm")
        self.assertEqual(bob.until("[retry]")[-1], "[retry] Nothing changed; /retry "
                         "sends exactly the same request once the referee is free.")  # fmt: skip
        self.host.release.set()
        bob.until("[idle]")
        bob.type("/retry")
        self.assertTrue(
            bob.until("[admin] Done")[-1].startswith("[admin] Done at revision")
        )
        [audit] = self.store().host_audit(0)[0]
        self.assertEqual(audit["detail"]["args"], {"description": "Fog."})

        # Busy again, then the operator makes Alice the host: the retry is
        # never resent under Bob's new role.
        self.host.release.clear()
        alice.type("hold")
        alice.until("[busy]")
        bob.type(
            '/admin trailhead.scene_set {"description": "Snow."}', "/admin confirm"
        )
        bob.until("[retry]")
        with HostRegistry(self.registry_path) as registry:
            registry.set_host(self.campaign, self.players["alice"])
        bob.type("/requests")  # its next request: then it is told
        bob.until("[role] You are now a guest")
        bob.type("/retry")
        self.assertEqual(  # the retry ended with Bob's role
            bob.until("[retry]")[-1], "[retry] There is no input to retry."
        )
        self.host.release.set()
        bob.until("[idle]")
        self.assertEqual(self.store().inspect_state()["scene"]["description"], "Fog.")
        self.assertEqual((alice.finish(), bob.finish()), (0, 0))

    def test_a_retry_dies_with_the_role_it_was_made_under(self):
        bob, alice = self.entered("bob"), self.entered("alice", "--character", MARA)
        alice.type("hold")
        alice.until("[busy]")
        bob.type('/admin trailhead.scene_set {"description": "Old."}', "/admin confirm")
        bob.until("[retry]")
        # Host, then guest, then host again: the old edit is not resent.
        for host, told in (("alice", "[role] You are now a guest"),
                           ("bob", "[role] You are now this campaign's host")):  # fmt: skip
            with HostRegistry(self.registry_path) as registry:
                registry.set_host(self.campaign, self.players[host])
            bob.type("/requests")
            bob.until(told)
        self.host.release.set()
        bob.until("[idle]")
        bob.type("/retry")
        self.assertEqual(
            bob.until("[retry]")[-1], "[retry] There is no input to retry."
        )
        self.assertNotIn("scene", self.store().inspect_state())
        self.assertEqual((alice.finish(), bob.finish()), (0, 0))

    def test_lost_access_leaves_nothing_of_the_campaign_behind(self):
        bob = self.entered("bob")
        with self.store() as store:  # Bob's participant link ends
            with HostRegistry(self.registry_path) as registry:
                store.unlink_host_player(registry.host_id, self.players["bob"])
        bob.type("/inspect")
        lines = bob.until("[lobby] You are back in the lobby")
        self.assertIn("[error] That campaign is no longer available to you.", lines)
        bob.type("/role")
        self.assertEqual(bob.line(), "[lobby] Choose a campaign first: /campaigns "
                                     "lists them, /join enters one.")  # fmt: skip
        bob.type("/join Trail")  # admitted again, still the campaign host
        bob.until("[joined] Trail")
        bob.until("[status]")
        bob.type("/role")
        self.assertEqual(bob.until("[role]")[-1], "[role] You are this campaign's "
                         "host. Its settings are at revision 0.")  # fmt: skip
        self.assertEqual(bob.finish(), 0)

    def test_json_output_keeps_messages_on_stdout_and_notes_on_stderr(self):
        bob = Client(self, self.host.uri, self.files["bob"], "--lobby", "--json",
                     "--campaign", self.campaign)  # fmt: skip
        bob.type("/requests", '/admin trailhead.scene_set {"description": "Fog."}',
                 "/admin confirm")  # fmt: skip
        replies = []
        while len(replies) < 2:
            message = json.loads(bob.line())
            if message["type"] in ("requests", "result"):
                replies.append(message)
        self.assertEqual(replies[0]["requests"], [])
        self.assertEqual(replies[1]["type"], "result")
        self.assertEqual(bob.finish(), 0)
        self.assertIn("[admin] Run trailhead.scene_set", bob.stderr)

    def revision(self) -> int:
        return self.store().revision()


if __name__ == "__main__":
    unittest.main()
