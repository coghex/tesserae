"""`tesserae connect --lobby` (HOST-3, #98): logging in to a lobby host,
listing, joining and leaving campaigns from the CLI, and retries that stay in
their campaign. Real client processes against a lobby host on its own thread,
with a scripted referee per campaign. docs/designs/lobby_contract.md."""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path

from test_referee_host import TIMEOUT, Client, reply

from tesserae.builtin import DICE_RUNTIME
from tesserae.credentials import new_token, write_token_file
from tesserae.lobby import LobbyHost
from tesserae.referee.store import RefereeStore
from tesserae.registry import HostRegistry
from tesserae.server import GameHost
from tesserae.store import GameStore


class LobbyThread:
    """A lobby host on its own thread and loop, for client processes. Every
    campaign's referee answers `Heard: TEXT` (privately to private input);
    input `hold` waits until `release` is set."""

    def __init__(self, registry: Path):
        self.release = threading.Event()
        ready = threading.Event()

        async def executor(job):
            if job.text == "hold":
                await asyncio.to_thread(self.release.wait, TIMEOUT)
            return reply(("sender" if job.private else "table", f"Heard: {job.text}"))

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

    def close(self):
        self.release.set()
        self.loop.call_soon_threadsafe(
            lambda: self.stop.done() or self.stop.set_result(None)
        )
        self.thread.join(TIMEOUT)


class LobbyClientTests(unittest.TestCase):
    """Harbor and Bridge are referee campaigns, Dice a plain one. Alice may
    enter all three; Bob none."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry_path = self.root / "host.sqlite3"
        self.files, self.campaigns = {}, {}
        with HostRegistry.create(self.registry_path) as registry:
            self.players = {}
            for name in ("alice", "bob"):
                token = new_token()
                self.players[name] = registry.add_player(name.title(), token)
                self.files[name] = self.root / f"{name}.token"
                write_token_file(self.files[name], token)
            for name, referee in (("harbor", True), ("bridge", True), ("dice", False)):
                path = self.root / f"{name}.sqlite3"
                with GameStore.create(path, DICE_RUNTIME) as store:
                    if referee:
                        RefereeStore(store).enable()
                campaign = registry.register_campaign(path, name.title())
                registry.set_campaign_active(campaign, True)
                registry.grant(campaign, self.players["alice"])
                self.campaigns[name] = campaign
        self.host = LobbyThread(self.registry_path)
        self.addCleanup(self.host.close)

    def client(self, name, *extra) -> Client:
        return Client(self, self.host.uri, self.files[name], "--lobby", *extra)

    def registry(self) -> HostRegistry:
        return HostRegistry(self.registry_path)

    def jobs(self, campaign) -> list:
        with GameStore(self.root / f"{campaign}.sqlite3") as store:
            return store.query("SELECT state FROM referee_jobs ORDER BY started_at")

    def test_login_list_join_talk_leave_and_switch(self):
        alice = self.client("alice")
        self.assertEqual(alice.until("[lobby] /join"), [
            "[lobby] Alice: 3 campaigns open to you",
            f"[lobby] 1. Bridge  {self.campaigns['bridge']}",
            f"[lobby] 2. Dice  {self.campaigns['dice']}",
            f"[lobby] 3. Harbor  {self.campaigns['harbor']}",
            "[lobby] /join N enters campaign N; /help lists commands.",
        ])  # fmt: skip
        alice.type("Hello?")  # words in the lobby go nowhere
        self.assertEqual(
            alice.line(),
            "[lobby] Choose a campaign first: /campaigns lists them, /join enters one.",
        )
        alice.type("/join 3", "Hello, harbor.")  # the second line waits for the join
        self.assertEqual(alice.until("[idle]"), [
            "[joined] Harbor: type to talk to the referee; /leave returns to the "
            "lobby; /help lists commands.",
            "[status] idle; nothing waiting for you; last message 0",
            "[table] Alice: Hello, harbor.",
            "[busy] The referee is answering.",
            "[table] Referee: Heard: Hello, harbor.",
            "[idle] The referee is ready.",
        ])  # fmt: skip
        alice.type("/campaigns")
        self.assertIn(
            f"[lobby] 3. Harbor (you are here)  {self.campaigns['harbor']}",
            alice.until("[lobby] 3."),
        )
        alice.type("/join bridge")
        self.assertEqual(alice.line(), "[lobby] You are in Harbor; /leave it first.")
        alice.type("/leave", "/join Bridge", "/history")
        lines = alice.until("[history]")
        self.assertEqual(lines[0], "[lobby] Alice: 3 campaigns open to you")
        self.assertIn("[joined] Bridge: type to talk to the referee; /leave returns "
                      "to the lobby; /help lists commands.", lines)  # fmt: skip
        self.assertEqual(lines[-1], "[history] Nothing yet.")  # nothing of Harbor's
        alice.type("/leave", "/join 2", '{"version": 1, "type": "command", '
                   '"request_id": "r1", "expected_revision": 0, "command": "roll", '
                   '"args": {"expression": "1d6"}}')  # fmt: skip
        alice.until("[joined] Dice: send JSON commands, one per line;")
        alice.line()  # its snapshot, as JSON
        result = json.loads(alice.until('{"version": 1, "type": "result"')[-1])
        self.assertEqual((result["request_id"], result["revision"]), ("r1", 1))
        self.assertEqual(alice.finish(), 0)
        self.assertEqual(
            (self.jobs("harbor"), self.jobs("bridge")), ([("completed",)], [])
        )
        self.assertNotIn(str(self.root), alice.stderr)

    def test_retry_belongs_to_the_campaign_it_was_sent_in(self):
        alice = self.client("alice", "--campaign", self.campaigns["harbor"])
        alice.until("[status]")
        alice.type("hold")
        alice.until("[busy]")
        alice.type("/leave", "/join Bridge", "/retry")
        self.assertEqual(
            alice.until("[retry]")[-1], "[retry] There is no input to retry."
        )
        self.host.release.set()
        alice.type("/leave", "/join Harbor")
        alice.until("[your last input]")
        alice.type("/retry")  # Harbor's "hold", replayed there and never run again
        self.assertEqual(alice.until("[table] Referee: Heard: hold")[-1],
                         "[table] Referee: Heard: hold")  # fmt: skip
        self.assertEqual(alice.finish(), 0)
        self.assertEqual(
            (self.jobs("harbor"), self.jobs("bridge")), ([("completed",)], [])
        )

    def test_empty_catalogs_reserved_commands_and_help(self):
        bob = self.client("bob")
        self.assertEqual(
            bob.line(), "[lobby] Bob: no campaigns are open to you yet; ask the host"
        )
        bob.type("/join 1")
        self.assertEqual(bob.line(), "[lobby] There is no campaign 1; see /campaigns.")
        bob.type(f"/join {self.campaigns['harbor']}")  # not granted
        self.assertEqual(bob.line(), "[error] That campaign is not available to you.")
        bob.type("/character", "/leave", "/retry", "/help")
        self.assertEqual(bob.until("[help] /leave"), [
            "[lobby] Characters come in a later version; /help lists what works now.",
            "[lobby] You are not in a campaign.",
            "[lobby] Choose a campaign first: /campaigns lists them, /join enters one.",
            "[help] /campaigns     the campaigns open to you, and which one you are in",
            "[help] /join N        enter campaign N from /campaigns (or its ID or name)",
            "[help] /leave         return to the lobby; anything you sent stays in that "
            "campaign",
        ])  # fmt: skip
        self.assertEqual(bob.finish(), 0)

    def test_lost_access_returns_to_the_lobby_and_a_revoked_token_ends(self):
        alice = self.client("alice", "--campaign", self.campaigns["harbor"])
        alice.until("[status]")
        with self.registry() as registry:
            registry.revoke_access(self.campaigns["harbor"], self.players["alice"])
        alice.type("Still here?")
        self.assertEqual(alice.until("[lobby] You are back"), [
            "[error] That campaign is no longer available to you.",
            "[lobby] You are back in the lobby, out of Harbor; /campaigns lists what "
            "is open to you.",
        ])  # fmt: skip
        alice.type("/campaigns")
        self.assertEqual(alice.line(), "[lobby] Alice: 2 campaigns open to you")
        with self.registry() as registry:
            registry.revoke_credential(self.players["alice"])
        alice.type("/campaigns")
        self.assertEqual(
            alice.until("[error]")[-1],
            "[error] This session's credential is no longer valid.",
        )
        alice.process.wait(TIMEOUT)  # the host closed it; stdin is still open
        self.assertEqual(alice.finish(), 1)
        self.assertIn("session_revoked", alice.stderr)

    def test_a_left_notice_from_a_delivery(self):
        alice = self.client("alice", "--campaign", self.campaigns["harbor"])
        alice.until("[status]")
        with (
            self.registry() as registry,
            GameStore(self.root / "harbor.sqlite3") as store,
        ):
            store.unlink_host_player(registry.host_id, self.players["alice"])
        other = Client(self, self.host.uri, self._second_player(), "--lobby",
                       "--campaign", self.campaigns["harbor"])  # fmt: skip
        other.until("[status]")
        other.type("Anyone?")  # its delivery finds Alice's link gone
        self.assertEqual(alice.until("[lobby] You are back"), [
            "[left] That campaign is no longer available to you.",
            "[lobby] You are back in the lobby, out of Harbor; /campaigns lists what "
            "is open to you.",
        ])  # fmt: skip
        self.host.release.set()
        self.assertEqual((alice.finish(), other.finish()), (0, 0))

    def _second_player(self) -> Path:
        token, path = new_token(), self.root / "carol.token"
        with self.registry() as registry:
            carol = registry.add_player("Carol", token)
            registry.grant(self.campaigns["harbor"], carol)
        write_token_file(path, token)
        return path

    def test_json_output_and_refused_logins(self):
        alice = self.client("alice", "--json", "--campaign", self.campaigns["dice"])
        lobby = json.loads(alice.line())
        self.assertEqual(
            [c["name"] for c in lobby["campaigns"]], ["Bridge", "Dice", "Harbor"]
        )
        snapshot = json.loads(alice.line())
        self.assertEqual(snapshot["campaign"]["campaign_id"], self.campaigns["dice"])
        self.assertEqual(alice.finish(), 0)

        stranger = Client(self, self.host.uri, self._token_file(new_token()), "--lobby")
        self.assertEqual(stranger.finish(), 1)
        self.assertIn("refused the login: authentication_failed", stranger.stderr)
        for extra, message in (
            (("--campaign", "x"), "--campaign needs --lobby"),
            (("--lobby", "--game-id", "x"), "--game-id is for one game"),
        ):
            with self.subTest(extra=extra):
                client = Client(self, self.host.uri, self.files["alice"], *extra)
                self.assertEqual(client.finish(), 1)
                self.assertIn(message, client.stderr)

    def _token_file(self, token) -> Path:
        path = self.root / f"t-{token[-6:]}.token"
        write_token_file(path, token)
        return path


class CompatibilityTests(unittest.TestCase):
    def test_each_host_explains_the_other_kind_of_token(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        registry = root / "host.sqlite3"
        HostRegistry.create(registry).close()
        lobby = LobbyThread(registry)
        self.addCleanup(lobby.close)
        token_file = root / "game.token"
        write_token_file(token_file, new_token())
        joiner = Client(self, lobby.uri, token_file)  # no --lobby
        self.assertEqual(joiner.finish(), 1)
        self.assertIn("this host serves campaigns: connect with --lobby", joiner.stderr)

        game = root / "game.sqlite3"
        with GameStore(game):
            pass
        ready, holder = threading.Event(), {}

        def serve():
            loop = asyncio.new_event_loop()
            holder["loop"], holder["stop"] = loop, loop.create_future()
            host = GameHost(game, authenticated=True)

            def on_ready(uri):
                holder["uri"] = uri
                ready.set()

            loop.run_until_complete(host.run(0, holder["stop"], on_ready=on_ready))
            loop.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        ready.wait(TIMEOUT)
        self.addCleanup(lambda: (holder["loop"].call_soon_threadsafe(
            holder["stop"].set_result, None), thread.join(TIMEOUT)))  # fmt: skip
        logger = Client(self, holder["uri"], token_file, "--lobby")
        self.assertEqual(logger.finish(), 1)
        self.assertIn("this looks like a single-game host", logger.stderr)


if __name__ == "__main__":
    unittest.main()
