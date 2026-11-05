"""`tesserae connect --lobby` in a campaign with characters (CHR-5, #115):
listing, selecting, creating with the referee, approving and archiving
characters from the CLI, the actor every input names, retries that stay with
their actor, and the notices another connection sends. Real client processes
against a lobby host on its own thread, with an original Trailhead campaign
and a scripted referee that builds drafts through the real bridge tools.
docs/designs/character_contract.md section 15."""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path

from test_referee_host import TIMEOUT, Client

from tesserae.builtin import CATALOG, DICE_RUNTIME
from tesserae.credentials import new_token, write_token_file
from tesserae.lobby import LobbyHost
from tesserae.referee.store import RefereeStore
from tesserae.referee.tools import BridgeTools
from tesserae.registry import HostRegistry
from tesserae.seed import parse_seed
from tesserae.store import Actor, GameStore, ParticipantContext

TRAILHEAD = CATALOG.resolve("trailhead")
ALICE = "p_a1a1a1a1a1a1a1a1"
MARA = "c_0123456789abcdef"


def reply(*messages, ask=()):
    return {"messages": [{"audience": a, "text": t} for a, t in messages],
            "choices": {"ask": [{"actor": a, "question": q} for a, q in ask],
                        "resolve": []},
            "narrated": []}  # fmt: skip


def seed() -> str:
    walker = {"trait": "quick", "rolled": 3, "stamina": 3, "max_stamina": 3,
              "ready": True, "in_danger": False}  # fmt: skip
    return json.dumps({
        "seed_version": 2,
        "module": {"id": "trailhead", "version": "1", "state_version": 1,
                   "providers": [{"operation": "dice.roll.v1", "provider": "builtin.dice",
                                  "version": "1"}]},
        "participants": [{"participant_id": ALICE, "name": "Alice"}],
        "characters": [{"character_id": MARA, "participant_id": ALICE, "name": "Mara"}],
        "state": {"walkers": {MARA: walker}},
        "provenance": {"source": "test"},
    })  # fmt: skip


class TrailThread:
    """A lobby host on its own thread. Its referee answers `Heard: TEXT`
    (privately to private input); `build NAME TRAIT` names the creation
    job's draft, sets its trait and rolls its stamina through the bridge;
    `ask me` asks the job's own character a question; `hold` waits until
    `release` is set."""

    def __init__(self, registry: Path, trail: Path):
        self.release = threading.Event()
        self.trail = trail
        ready = threading.Event()

        async def executor(job):
            if job.text == "hold":
                await asyncio.to_thread(self.release.wait, TIMEOUT)
            if job.text.startswith("build "):
                _, name, trait = job.text.split()
                await asyncio.to_thread(self.build, job, name, trait)
                return reply(
                    ("sender", f"{name} is {trait}; approve when you are ready.")
                )
            audience = "sender" if job.private else "table"
            if job.text == "ask me":
                return reply(
                    (audience, "Well?"), ask=[(job.character_id, "Cross now?")]
                )
            return reply((audience, f"Heard: {job.text}"))

        def main():
            loop = asyncio.new_event_loop()
            self.loop = loop
            self.stop = loop.create_future()
            host = LobbyHost(registry, referee_executor_for=lambda _: executor)

            def on_ready(uri):
                self.uri = uri
                ready.set()

            loop.run_until_complete(host.run(0, self.stop, on_ready=on_ready))
            loop.close()

        self.thread = threading.Thread(target=main, daemon=True)
        self.thread.start()
        ready.wait(TIMEOUT)

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

    def close(self):
        self.release.set()
        self.loop.call_soon_threadsafe(
            lambda: self.stop.done() or self.stop.set_result(None)
        )
        self.thread.join(TIMEOUT)


class CharacterClientTests(unittest.TestCase):
    """Trail is a Trailhead campaign in referee mode: Alice is linked to her
    seeded participant, who has Mara; Bob is new. Dice has no characters."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry_path = self.root / "host.sqlite3"
        self.trail = self.root / "trail.sqlite3"
        self.files, self.campaigns = {}, {}
        with GameStore.create(self.trail, TRAILHEAD, seed=parse_seed(seed())) as store:
            RefereeStore(store).enable()
        with GameStore.create(self.root / "dice.sqlite3", DICE_RUNTIME) as store:
            RefereeStore(store).enable()
        with HostRegistry.create(self.registry_path) as registry:
            host_id = registry.host_id
            self.players = {}
            for name in ("alice", "bob"):
                token = new_token()
                self.players[name] = registry.add_player(name.title(), token)
                self.files[name] = self.root / f"{name}.token"
                write_token_file(self.files[name], token)
            for name in ("trail", "dice"):
                campaign = registry.register_campaign(
                    self.root / f"{name}.sqlite3", name.title()
                )
                registry.set_campaign_active(campaign, True)
                for player in self.players.values():
                    registry.grant(campaign, player)
                self.campaigns[name] = campaign
        with GameStore.open_existing(self.trail) as store:
            store.link_host_player(host_id, self.players["alice"], ALICE)
        self.host_id = host_id
        self.host = TrailThread(self.registry_path, self.trail)
        self.addCleanup(self.host.close)

    def client(self, name, *extra) -> Client:
        return Client(self, self.host.uri, self.files[name], "--lobby", *extra)

    def entered(self, name, *extra) -> Client:
        client = self.client(name, "--campaign", self.campaigns["trail"], *extra)
        client.until("[status]")
        if "--character" in extra:  # and the status after selecting it
            client.until("[status]")
        return client

    def store(self) -> GameStore:
        store = GameStore.open_existing(self.trail)
        self.addCleanup(store.close)
        return store

    def wren(self) -> str:
        """Another ready character for Alice's participant."""
        alice = ParticipantContext(ALICE)
        with GameStore.open_existing(self.trail, randint=lambda low, high: 2) as store:
            made, _ = store.create_character(alice, "make-wren", "Wren")
            cid = made["result"]["character"]["character_id"]
            for step, (command, args) in enumerate((("trailhead.trait", {"trait": "steady"}),
                                                    ("trailhead.stamina", {}))):  # fmt: skip
                raw = json.dumps({"version": 1, "type": "command", "request_id": f"w-{step}",
                                  "expected_revision": store.revision(), "command": command,
                                  "args": args})  # fmt: skip
                assert store.execute_command(alice, raw, actor=Actor(cid, "creation"))[
                    1
                ]
            assert store.complete_character(alice, "done-wren", cid, 3)[1]
        return cid

    def test_a_newcomer_creates_approves_and_plays(self):
        with HostRegistry(self.registry_path) as registry:  # Bob is a guest here
            registry.set_host(self.campaigns["trail"], self.players["alice"])
        bob = self.client("bob", "--campaign", self.campaigns["trail"])
        self.assertEqual(bob.until("[status]"), [
            "[lobby] Bob: 2 campaigns open to you",
            f"[lobby] 1. Dice  {self.campaigns['dice']}",
            f"[lobby] 2. Trail  {self.campaigns['trail']}",
            "[lobby] /join N enters campaign N; /help lists commands.",
            "[joined] Trail: type to talk to the referee; /leave returns to the lobby; "
            "/help lists commands.",
            "[characters] You have no characters here yet: /create [NAME] starts one "
            "with the referee, privately. Until then you can ask setup questions.",
            "[status] idle; nothing waiting for you; last message 0",
        ])  # fmt: skip
        alice = self.entered("alice", "--character", MARA)

        bob.type("/create")
        lines = bob.until("[status]")
        self.assertEqual(lines[0], "[character] started an unnamed draft (draft)")
        self.assertIn(
            "[character] creating an unnamed draft (draft), revision 1; private to "
            "you and the campaign host. /sheet shows it, /approve completes it.",
            lines,
        )
        bob.type("build Ash keen")
        self.assertEqual(bob.until("[idle]"), [
            "[private · creating a draft] Bob: build Ash keen",
            "[busy] The referee is answering.",
            "[private · creating Ash] Referee: Ash is keen; approve when you are ready.",
            "[idle] The referee is ready.",
        ])  # fmt: skip
        bob.type("/private hello")
        self.assertEqual(bob.line(), "[error] Everything you say while creating is "
                                     "already private; just type it.")  # fmt: skip
        bob.type("/characters")  # learns revision 4, but shows no sheet
        self.assertIn(
            "draft, revision 4 (selected)", bob.until("[characters] /character N")[1]
        )
        bob.type("/approve")  # the sheet last shown was revision 1
        lines = []  # the refreshed sheet and the status after the attempt, in either order
        while not (any(x.startswith("[sheet]") for x in lines)
                   and any(x.startswith("[status]") for x in lines)):  # fmt: skip
            lines.append(bob.line())
        self.assertIn(
            "[error] That draft has changed since you saw it; review it again.", lines
        )
        self.assertIn('[sheet] Ash (draft), revision 4: {"trait": "keen", "stamina": 3, '
                      '"max_stamina": null, "in_danger": false}', lines)  # fmt: skip
        bob.type("/approve")
        lines = bob.until("[character] playing as")
        self.assertEqual(
            lines[-2:], ["[character] completed Ash", "[character] playing as Ash"]
        )
        bob.until("[status]")  # Ash's own status follows
        bob.type("Hello, everyone.")
        self.assertIn("[table] Bob as Ash: Hello, everyone.", bob.until("[idle]"))

        seen = alice.until("[table] Bob as Ash: Hello, everyone.")
        self.assertNotIn("build Ash keen", "\n".join(seen))  # creation stays private
        self.assertNotIn("approve when", "\n".join(seen))
        self.assertEqual((bob.finish(), alice.finish()), (0, 0))

    def test_flags_select_a_character_for_a_piped_session(self):
        alice = self.client("alice", "--campaign", self.campaigns["trail"],
                            "--character", MARA)  # fmt: skip
        alice.type("Mara looks around.")
        lines = alice.until("[idle]")
        self.assertIn("[character] playing as Mara", lines)
        self.assertIn("[table] Alice as Mara: Mara looks around.", lines)
        self.assertEqual(alice.finish(), 0)
        [stored] = self.store().query("SELECT character_id, mode FROM referee_inputs")
        self.assertEqual(stored, (MARA, "play"))

    def test_incomplete_approval_lists_the_missing_steps(self):
        bob = self.entered("bob")
        bob.type("/create")
        bob.until("[status]")
        bob.type("/approve")
        self.assertEqual(
            bob.line(),
            "[character] Not finished yet: Give the character a name. "
            "Choose a trait. Roll stamina.",
        )
        self.assertEqual(bob.finish(), 0)

    def test_retry_stays_with_its_actor(self):
        alice = self.entered("alice", "--character", MARA)
        alice.type("hold")
        alice.until("[busy]")
        self.assertEqual(alice.finish(), 0)  # it left before the reply
        self.host.release.set()
        alice = self.client("alice", "--campaign", self.campaigns["trail"],
                            "--character", MARA)  # fmt: skip
        alice.type("/retry")  # piped: it waits for Mara's status first
        lines = alice.until("[table] Referee: Heard: hold")
        self.assertTrue(
            any(line.startswith('[your last input] "hold"') for line in lines)
        )
        alice.type("/character none", "/retry")
        lines = alice.until("[retry]")
        self.assertIn("[character] no character selected; ask setup questions, "
                      "/character N to choose one, or /create", lines)  # fmt: skip
        self.assertEqual(lines[-1], "[retry] There is no input to retry.")  # not Mara's
        self.assertEqual(alice.finish(), 0)
        self.assertEqual(
            self.store().query("SELECT count(*) FROM referee_jobs"), [(1,)]
        )

    def test_another_characters_question_is_not_this_ones(self):
        self.wren()
        alice = self.entered("alice", "--character", MARA)
        alice.type("ask me")
        alice.until("[idle]")
        alice.type("/status")
        self.assertEqual(
            alice.line(), '[status] idle; waiting for you: "Cross now?"; last message 2'
        )
        alice.type("/character Wren", "/status")
        lines = alice.until("[status]")  # the status after switching
        lines = alice.until("[status]")  # the one asked for
        self.assertEqual(
            lines[-1], "[status] idle; nothing waiting for you; last message 2"
        )
        self.assertEqual(alice.finish(), 0)

    def test_archive_asks_first_and_tells_the_other_connection(self):
        other = self.entered("alice", "--character", MARA)
        alice = self.entered("alice")
        alice.type("/characters")
        self.assertEqual(alice.until("[characters] /character N"), [
            "[characters] 1 character:",
            f"[characters] 1. Mara, ready  {MARA}",
            "[characters] /character N plays (or resumes) one; /create starts another.",
        ])  # fmt: skip
        alice.type("/archive 1")
        self.assertEqual(alice.line(), "[character] Archive Mara? It leaves your list "
                                       "for good. Type /archive 1 confirm to archive it.")  # fmt: skip
        alice.type("/archive 1 confirm")
        self.assertEqual(alice.line(), "[character] archived Mara")
        self.assertEqual(other.until("[character] no character"), [
            "[character] Your character was archived on another connection.",
            "[character] no character selected; ask setup questions, /character N to "
            "choose one, or /create",
        ])  # fmt: skip
        self.assertEqual(self.store().character(MARA).phase, "archived")
        self.assertEqual((alice.finish(), other.finish()), (0, 0))

    def test_mistakes_get_useful_answers(self):
        alice = self.entered("alice")
        alice.type("/character 5")
        self.assertEqual(
            alice.line(), "[character] There is no character 5; see /characters."
        )
        alice.type("/approve")
        self.assertEqual(
            alice.line(), "[character] Only a draft is approved; select one first."
        )
        alice.type("/character c_ffffffffffffffff")
        lines = alice.until("[characters]")
        self.assertEqual(lines[0], "[error] That character is not available to you.")
        alice.type("/leave", "/join Dice", "/characters")
        self.assertEqual(
            alice.until("[character]")[-1], "[character] Dice has no characters."
        )
        self.assertEqual(alice.finish(), 0)

    def test_a_relinked_player_is_refused_their_old_character(self):
        with GameStore.open_existing(self.trail) as store:
            store.unlink_host_player(self.host_id, self.players["alice"])
        alice = self.client("alice", "--campaign", self.campaigns["trail"],
                            "--character", MARA)  # fmt: skip
        lines = alice.until("[error]")
        self.assertIn("[characters] You have no characters here yet: /create [NAME] "
                      "starts one with the referee, privately. Until then you can ask "
                      "setup questions.", lines)  # fmt: skip
        self.assertEqual(lines[-1], "[error] That character is not available to you.")
        self.assertEqual(alice.finish(), 0)

    def test_json_output_keeps_messages_whole(self):
        alice = self.client("alice", "--json", "--campaign", self.campaigns["trail"])
        alice.type("/characters")
        messages = []
        while (
            not messages
            or messages[-1].get("type") != "characters"
            or len(messages) < 3
        ):
            messages.append(json.loads(alice.line()))
        listed = [m for m in messages if m["type"] == "characters"][-1]
        self.assertEqual([c["character_id"] for c in listed["characters"]], [MARA])
        alice.type("/create Ash")
        while True:
            message = json.loads(alice.line())
            if (
                message["type"] == "snapshot"
                and message.get("actor", {}).get("mode") == "creation"
            ):
                break
        alice.type("/approve")
        while True:
            message = json.loads(alice.line())
            if message["type"] == "error":
                break
        self.assertEqual(message["code"], "character_incomplete")
        self.assertEqual(message["message"], "That character is not finished yet.")
        self.assertEqual(message["problems"], ["Choose a trait.", "Roll stamina."])
        self.assertEqual(alice.finish(), 0)

    def test_help_lists_the_character_commands(self):
        alice = self.entered("alice")
        alice.type("/help")
        lines = alice.until("[help] //TEXT")
        self.assertIn("[help] /create [NAME] start a new character with the referee "
                      "(privately)", lines)  # fmt: skip
        self.assertEqual(alice.finish(), 0)


if __name__ == "__main__":
    unittest.main()
