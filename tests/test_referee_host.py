"""Referee mode on the host and the CLI (#68, REF-5): say, busy, retries,
audiences, status, revocation and the readable client. A scripted executor
stands in for Codex. docs/designs/referee_contract.md sections 7, 8 and 12."""

import asyncio
import json
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from tesserae.credentials import new_token, write_token_file
from tesserae.referee.store import RefereeStore
from tesserae.server import GameHost, HostConfigError, RefereeJobFailed
from tesserae.store import GameStore

TIMEOUT = 15


def say(request_id, text, private=False):
    return json.dumps({"version": 1, "type": "say", "request_id": request_id, "text": text,
                       "private": private})  # fmt: skip


def reply(*messages, ask=(), resolve=()):
    return {
        "messages": [{"audience": a, "text": t} for a, t in messages],
        "choices": {
            "ask": [{"participant_id": p, "question": q} for p, q in ask],
            "resolve": list(resolve),
        },  # fmt: skip
        "narrated": [],
    }


STATUS = json.dumps({"version": 1, "type": "referee_status"})
SYNC = json.dumps({"version": 1, "type": "sync"})


def history(after=0):
    return json.dumps({"version": 1, "type": "messages", "after_seq": after})


class RefereeGame(unittest.TestCase):
    def make_game(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "game.sqlite3"
        self.tokens = {"alice": new_token(), "bob": new_token()}
        with GameStore(self.path) as store:
            self.ids = {
                n: store.add_participant(n.title(), t) for n, t in self.tokens.items()
            }
            RefereeStore(store).enable()

    def jobs(self) -> list:
        with GameStore(self.path) as store:
            return store.query(
                "SELECT state, error FROM referee_jobs ORDER BY started_at"
            )


class HostCase(RefereeGame, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.make_game()
        self.script = None  # created on the host's loop
        self.started = None

    async def executor(self, job):
        """Hands each job to the test, then returns (or raises) what it scripts."""
        self.started.put_nowait(job)
        outcome = await self.script.get()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome(job) if callable(outcome) else outcome

    async def start_host(self):
        self.script, self.started = asyncio.Queue(), asyncio.Queue()
        loop = asyncio.get_running_loop()
        self.stop, ready = loop.create_future(), loop.create_future()
        host = GameHost(self.path, authenticated=True, referee_executor=self.executor)
        self.host_task = asyncio.create_task(
            host.run(0, self.stop, on_ready=ready.set_result)
        )

        async def shutdown():
            if not self.stop.done():
                self.stop.set_result(None)
            await asyncio.wait_for(self.host_task, 10)

        self.addAsyncCleanup(shutdown)
        self.uri = await asyncio.wait_for(ready, 5)
        return shutdown

    async def joined(self, name):
        connection = await connect(self.uri)
        self.addAsyncCleanup(connection.close)
        await connection.send(
            json.dumps({"version": 1, "type": "join", "token": self.tokens[name]})
        )
        snapshot = await self.next(connection)
        self.assertEqual(snapshot["type"], "snapshot", snapshot)
        return connection, snapshot

    async def next(self, connection) -> dict:
        return json.loads(await asyncio.wait_for(connection.recv(), 5))

    async def expect(self, connection, *types) -> list:
        messages = [await self.next(connection) for _ in types]
        self.assertEqual([m["type"] for m in messages], list(types), messages)
        return messages

    async def until(self, connection, kind: str, **fields) -> dict:
        """Skip messages until one of this type with these fields."""
        while True:
            message = await self.next(connection)
            if message["type"] == kind and all(
                message.get(k) == v for k, v in fields.items()
            ):
                return message

    async def reply_on(self, connection) -> dict:
        """The next reply, skipping unsolicited messages (which may come first)."""
        while (message := await self.next(connection))["type"] in (
            "message",
            "referee",
            "revision",
        ):
            pass
        return message

    async def quiet(self, connection, seconds=0.3):
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(connection.recv(), seconds)

    async def job(self):
        return await asyncio.wait_for(self.started.get(), 5)


class ConversationTests(HostCase):
    async def test_table_input_and_its_reply_reach_everyone(self):
        await self.start_host()
        alice, snapshot = await self.joined("alice")
        bob, _ = await self.joined("bob")
        self.assertEqual(snapshot["referee"],
                         {"state": "idle", "choices": [], "last_seq": 0, "your_last_input": None})  # fmt: skip
        await alice.send(say("a-1", "What is this place?"))
        accepted, said, busy = await self.expect(
            alice, "accepted", "message", "referee"
        )
        self.assertEqual(
            (accepted["request_id"], accepted["seq"], busy["state"]), ("a-1", 1, "busy")
        )
        self.assertEqual(said, {"version": 1, "type": "message", "seq": 1, "from": {"kind": "player", "name": "Alice"},
                                "audience": "table", "text": "What is this place?"})  # fmt: skip
        self.assertEqual((await self.expect(bob, "message", "referee"))[0], said)
        job = await self.job()
        self.assertEqual(
            (job.job_id, job.text), (accepted["job_id"], "What is this place?")
        )
        self.script.put_nowait(reply(("table", "A storm-damaged hilltop relay.")))
        for connection in (alice, bob):
            answer, idle = await self.expect(connection, "message", "referee")
            self.assertEqual((answer["from"], answer["audience"], answer["text"], idle["state"]),
                             ({"kind": "referee"}, "table", "A storm-damaged hilltop relay.", "idle"))  # fmt: skip

    async def test_private_exchanges_reach_only_the_sender_and_survive_reconnecting(
        self,
    ):
        await self.start_host()
        alice, _ = await self.joined("alice")
        bob, _ = await self.joined("bob")
        await bob.send(say("b-1", "Can I see Wren's sheet?", private=True))
        _, said, _ = await self.expect(bob, "accepted", "message", "referee")
        self.assertEqual(said["audience"], "private")
        await self.expect(alice, "referee")  # busy, and nothing of the words
        await self.job()
        self.script.put_nowait(
            reply(("sender", "Only what anyone can see: a worker named Wren."))
        )
        private, _ = await self.expect(bob, "message", "referee")
        self.assertEqual(
            (private["audience"], private["from"]), ("private", {"kind": "referee"})
        )
        await self.expect(alice, "referee")  # idle only
        await self.quiet(alice)
        # Bob reconnects: his status and history have it; Alice's have nothing.
        await bob.close()
        bob, snapshot = await self.joined("bob")
        last = snapshot["referee"]["your_last_input"]
        self.assertEqual(
            (last["request_id"], last["private"], last["job_state"]),
            ("b-1", True, "completed"),
        )
        await bob.send(history())
        [found] = await self.expect(bob, "messages")
        self.assertEqual(
            [m["audience"] for m in found["messages"]], ["private", "private"]
        )
        await alice.send(history())
        self.assertEqual((await self.expect(alice, "messages"))[0]["messages"], [])
        await alice.send(STATUS)
        status = (await self.expect(alice, "referee_status"))[0]
        self.assertEqual((status["last_seq"], status["your_last_input"]), (0, None))

    async def test_a_choice_is_shown_only_to_its_player(self):
        await self.start_host()
        alice, _ = await self.joined("alice")
        bob, _ = await self.joined("bob")
        await alice.send(say("a-1", "Can Wren fix the relay?"))
        await self.expect(alice, "accepted", "message", "referee")
        await self.expect(bob, "message", "referee")
        await self.job()
        self.script.put_nowait(lambda job: reply(("table", "1d6 plus repair against 5. Try now?"),
                                                 ask=[(self.ids["alice"], "Try the repair now?")]))  # fmt: skip
        await self.expect(alice, "message", "referee")
        await self.expect(bob, "message", "referee")
        for connection, expected in (
            (alice, [{"question": "Try the repair now?"}]),
            (bob, []),
        ):
            await connection.send(STATUS)
            self.assertEqual(
                (await self.expect(connection, "referee_status"))[0]["choices"],
                expected,
            )
            await connection.send(SYNC)
            self.assertEqual(
                (await self.expect(connection, "snapshot"))[0]["referee"]["choices"],
                expected,
            )


class BusyAndRetryTests(HostCase):
    async def test_simultaneous_inputs_one_job_and_exact_retries(self):
        await self.start_host()
        alice, _ = await self.joined("alice")
        bob, _ = await self.joined("bob")
        await asyncio.gather(
            alice.send(say("a-1", "I go first")), bob.send(say("b-1", "No, me"))
        )
        first = [await self.reply_on(alice), await self.reply_on(bob)]
        kinds = sorted(m["type"] for m in first)
        self.assertEqual(kinds, ["accepted", "error"], first)
        winner, loser = (alice, bob) if first[0]["type"] == "accepted" else (bob, alice)
        refused = first[1] if winner is alice else first[0]
        self.assertEqual(
            (refused["code"], refused["job"]["state"]), ("referee_busy", "running")
        )
        self.assertIn(refused["job"]["for"], ("Alice", "Bob"))
        job = await self.job()
        self.assertEqual(len(self.jobs()), 1)  # nothing stored for the refused input
        # While busy, the winner's exact retry is its recorded job, not a new one.
        retry = say(job.request_id, job.text)
        await winner.send(retry)
        self.assertEqual((await self.until(winner, "accepted"))["job_id"], job.job_id)
        self.script.put_nowait(reply(("table", "Done.")))
        await self.until(winner, "referee", state="idle")
        await self.until(loser, "referee", state="idle")
        # After the reply, a retry returns it again, to that connection only.
        await winner.send(retry)
        await self.until(winner, "accepted")
        replayed = await self.next(winner)
        self.assertEqual((replayed["type"], replayed["text"]), ("message", "Done."))
        await self.quiet(loser)
        self.assertEqual(len(self.jobs()), 1)
        # The refused input, sent again once idle, is a fresh job.
        await loser.send(
            say(refused["request_id"], "No, me" if loser is bob else "I go first")
        )
        await self.until(loser, "accepted")
        await self.job()
        self.assertEqual(len(self.jobs()), 2)
        self.script.put_nowait(reply(("table", "Your turn.")))

    async def test_direct_commands_are_refused_idle_or_busy(self):
        await self.start_host()
        alice, _ = await self.joined("alice")
        roll = json.dumps({"version": 1, "type": "command", "request_id": "r-1", "expected_revision": 0,
                           "command": "roll", "args": {"expression": "1d6"}})  # fmt: skip
        await alice.send(roll)
        [refused] = await self.expect(alice, "error")
        self.assertEqual(
            (refused["code"], refused["request_id"]), ("referee_mode", "r-1")
        )
        await alice.send(say("a-1", "Roll for me"))
        await self.expect(alice, "accepted", "message", "referee")
        await alice.send(roll)
        self.assertEqual((await self.expect(alice, "error"))[0]["code"], "referee_mode")
        await self.job()
        self.script.put_nowait(reply(("table", "Rolled nothing.")))
        with GameStore(self.path) as store:
            self.assertEqual((store.revision(), store.inspect_history()), (0, []))


class FailureTests(HostCase):
    async def test_failed_and_malformed_jobs_release_busy_and_publish_nothing(self):
        await self.start_host()
        alice, _ = await self.joined("alice")
        cases = [(RefereeJobFailed("failed", "runner_failed"), ("failed", "runner_failed")),
                 (reply(("sender", "Not to a table input.")), ("failed", "malformed_reply"))]  # fmt: skip
        for number, (outcome, expected) in enumerate(cases):
            await alice.send(say(f"a-{number}", "Hello"))
            await self.expect(alice, "accepted", "message", "referee")
            await self.job()
            self.script.put_nowait(outcome)
            self.assertEqual((await self.expect(alice, "referee"))[0]["state"], "idle")
            self.assertEqual(self.jobs()[-1], expected)

    async def test_revoked_sessions_get_nothing_more(self):
        await self.start_host()
        alice, _ = await self.joined("alice")
        bob, _ = await self.joined("bob")
        with GameStore(self.path) as store:
            store.revoke_credential(self.ids["alice"])
        await bob.send(say("b-1", "Hello, table"))
        await self.expect(bob, "accepted", "message", "referee")
        received = []
        with self.assertRaises(ConnectionClosed):
            while True:
                received.append(await self.next(alice))
        self.assertNotIn("message", [m["type"] for m in received])
        self.assertEqual(alice.close_code, 1008)
        await self.job()
        self.script.put_nowait(reply(("table", "Hello, Bob.")))

    async def test_status_sync_and_joins_answer_during_a_job_and_shutdown_interrupts_it(
        self,
    ):
        shutdown = await self.start_host()
        alice, _ = await self.joined("alice")
        await alice.send(say("a-1", "Think hard"))
        await self.expect(alice, "accepted", "message", "referee")
        await self.job()  # never answered
        bob, snapshot = await self.joined("bob")
        self.assertEqual(snapshot["referee"]["state"], "busy")
        await bob.send(STATUS)
        self.assertEqual((await self.expect(bob, "referee_status"))[0]["state"], "busy")
        await bob.send(SYNC)
        await self.expect(bob, "snapshot")
        await shutdown()
        self.assertEqual(self.jobs(), [("interrupted", "shutdown")])


class ModeTests(RefereeGame):
    def setUp(self):
        self.make_game()

    def test_referee_mode_needs_authentication_and_an_executor(self):
        async def executor(job):
            return None

        with self.assertRaisesRegex(HostConfigError, "requires --authenticated"):
            GameHost(self.path, referee_executor=executor)
        with self.assertRaisesRegex(HostConfigError, "no referee runner"):
            GameHost(self.path, authenticated=True)


class OrdinaryHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_game_without_a_referee_refuses_referee_messages(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path, token = Path(directory.name) / "plain.sqlite3", new_token()
        with GameStore(path) as store:
            store.add_participant("Alice", token)
        loop = asyncio.get_running_loop()
        stop, ready = loop.create_future(), loop.create_future()
        task = asyncio.create_task(
            GameHost(path, authenticated=True).run(0, stop, on_ready=ready.set_result)
        )
        try:
            async with connect(await asyncio.wait_for(ready, 5)) as alice:
                await alice.send(
                    json.dumps({"version": 1, "type": "join", "token": token})
                )
                self.assertNotIn("referee", json.loads(await alice.recv()))
                await alice.send(say("a-1", "Hello"))
                refused = json.loads(await asyncio.wait_for(alice.recv(), 5))
                self.assertEqual(
                    (refused["code"], refused["request_id"]), ("referee_off", "a-1")
                )
        finally:
            stop.set_result(None)
            await asyncio.wait_for(task, 10)


class HostThread:
    """A referee host on its own thread and loop, for client processes. Its
    executor answers `Heard: TEXT` (privately to private input); input `hold`
    waits until `release` is set."""

    def __init__(self, path: Path):
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
            host = GameHost(path, authenticated=True, referee_executor=executor)

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


class Client:
    def __init__(self, test, uri, token_file, *extra):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "tesserae", "connect", uri, "--token-file", str(token_file), *extra],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )  # fmt: skip
        self.lines = queue.Queue()
        threading.Thread(target=lambda: [self.lines.put(line.rstrip("\n")) for line in self.process.stdout],
                         daemon=True).start()  # fmt: skip
        test.addCleanup(self.kill)

    def type(self, *lines):
        self.process.stdin.write("".join(line + "\n" for line in lines))
        self.process.stdin.flush()

    def line(self) -> str:
        return self.lines.get(timeout=TIMEOUT)

    def until(self, wanted: str) -> list:
        """Lines up to and including the first that starts with `wanted`."""
        seen = []
        while not (seen and seen[-1].startswith(wanted)):
            seen.append(self.line())
        return seen

    def finish(self) -> int:
        self.process.stdin.close()
        code = self.process.wait(TIMEOUT)
        self.stderr = self.process.stderr.read()
        return code

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


class ClientTests(RefereeGame):
    def setUp(self):
        self.make_game()
        self.files = {}
        for name, token in self.tokens.items():
            self.files[name] = self.directory / f"{name}.token"
            write_token_file(self.files[name], token)
        self.host = HostThread(self.path)
        self.addCleanup(self.host.close)

    def client(self, name, *extra) -> Client:
        return Client(self, self.host.uri, self.files[name], *extra)

    def test_players_type_words_and_read_lines(self):
        alice = self.client("alice")
        self.assertEqual(alice.until("[status]"),
                         ["[joined] Type to talk to the referee; /help lists commands.",
                          "[status] idle; nothing waiting for you; last message 0"])  # fmt: skip
        bob = self.client("bob")
        bob.until("[status]")
        alice.type("Can Wren fix the relay?")
        self.assertEqual(alice.until("[idle]"), [
            "[table] Alice: Can Wren fix the relay?",
            "[busy] The referee is answering.",
            "[table] Referee: Heard: Can Wren fix the relay?",
            "[idle] The referee is ready.",
        ])  # fmt: skip
        self.assertIn(
            "[table] Referee: Heard: Can Wren fix the relay?", bob.until("[idle]")
        )
        # Text that looks like a command or JSON is still just words.
        command = '{"version": 1, "type": "command", "command": "roll"}'
        heard = []
        for typed in (command, "//help me", "/private Can I hide?"):
            alice.type(typed)
            heard += alice.until("[idle]")
        self.assertIn(f"[table] Referee: Heard: {command}", heard)
        self.assertIn("[table] Referee: Heard: /help me", heard)
        self.assertIn("[private] Referee: Heard: Can I hide?", heard)
        seen_by_bob = bob.until("[idle]") + bob.until("[idle]") + bob.until("[idle]")
        self.assertNotIn("Can I hide?", "\n".join(seen_by_bob))
        # Typing while the referee is busy is refused, never queued.
        alice.type("hold")
        alice.until("[busy] The referee is answering.")
        alice.type("Me again")
        self.assertEqual(
            alice.line(),
            "[busy] The referee is still answering Alice. Try again when it finishes.",
        )
        self.host.release.set()
        alice.until("[idle]")
        alice.type("/history")
        self.assertIn(
            "[private] Alice: Can I hide?", alice.until("[table] Referee: Heard: hold")
        )
        alice.type("/nonsense")
        self.assertEqual(
            alice.line(), "[error] There is no /nonsense; /help lists commands."
        )
        self.assertEqual((alice.finish(), bob.finish()), (0, 0))
        self.assertEqual(len(self.jobs()), 5)

    def test_a_new_client_shows_the_last_input_and_retry_never_repeats_it(self):
        alice = self.client("alice")
        alice.until("[status]")
        alice.type("hold")
        alice.until("[busy]")
        self.assertEqual(alice.finish(), 0)  # it left before the reply
        self.host.release.set()
        alice = self.client("alice")
        lines = alice.until("[your last input]")
        self.assertRegex(lines[-1], r'^\[your last input\] "hold" (running|completed)$')
        alice.type("/retry")
        self.assertIn(
            "[table] Referee: Heard: hold", alice.until("[table] Referee: Heard: hold")
        )
        self.assertEqual(alice.finish(), 0)
        self.assertEqual(self.jobs(), [("completed", None)])  # one job, never a second

    def test_json_output_for_scripts(self):
        alice = self.client("alice", "--json")
        snapshot = json.loads(alice.line())
        self.assertEqual(
            (snapshot["type"], snapshot["referee"]["state"]), ("snapshot", "idle")
        )
        self.assertEqual(json.loads(alice.line())["type"], "referee_status")
        alice.type("Hello")
        kinds = [json.loads(alice.line())["type"] for _ in range(5)]
        self.assertEqual(
            kinds, ["accepted", "message", "referee", "message", "referee"]
        )
        self.assertEqual(alice.finish(), 0)


if __name__ == "__main__":
    unittest.main()
