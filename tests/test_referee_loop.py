"""The referee loop (#69, REF-6): accepted input through Codex, the bridge and
back to the players, with a scripted stand-in for Codex (no model, no
account). docs/designs/referee_contract.md sections 4, 6, 10 and 11."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from websockets.asyncio.client import connect

from tesserae import server
from tesserae.builtin import CATALOG
from tesserae.credentials import new_token
from tesserae.referee import bridge, runner
from tesserae.referee.orchestrator import CodexReferee
from tesserae.referee.store import RefereeStore
from tesserae.server import GameHost
from tesserae.store import GameStore, ParticipantContext

FAKE = Path(__file__).resolve().parent / "fixtures" / "scripted_referee.py"
STATUS = json.dumps({"version": 1, "type": "referee_status"})


def say(request_id, text, private=False):
    return json.dumps({"version": 1, "type": "say", "request_id": request_id, "text": text,
                       "private": private})  # fmt: skip


def call(tool, **args):
    return {"tool": tool, "args": args}


def act(step_id, command, **args):
    return call("act", step_id=step_id, command=command, args=args)


def reply(*messages, ask=(), resolve=()):
    return {"messages": [{"audience": a, "text": t} for a, t in messages],
            "choices": {"ask": [{"participant_id": p, "question": q} for p, q in ask],
                        "resolve": list(resolve)},
            "narrated": []}  # fmt: skip


class LoopCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Short paths: a Unix socket path is limited to about 100 bytes.
        directory = tempfile.TemporaryDirectory(dir="/tmp", prefix="tl")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.folder = runner.setup(root / "r")
        self.fake = root / "f"
        self.fake.mkdir()
        patch = mock.patch.dict(os.environ, {"FAKE_REFEREE_DIR": str(self.fake)})
        patch.start()
        self.addCleanup(patch.stop)
        self.path = root / "game.sqlite3"
        self.tokens = {"alice": new_token(), "bob": new_token()}
        with GameStore.create(self.path, CATALOG.resolve("relay")) as store:
            self.game_id = store.game_id
            self.ids = {
                n: store.add_participant(n.title(), t) for n, t in self.tokens.items()
            }
            RefereeStore(store).enable()
        self.scripts = []

    def script(self, *jobs):
        self.scripts += jobs
        (self.fake / "script.json").write_text(json.dumps(self.scripts))

    def runs(self) -> list:
        log = self.fake / "log.jsonl"
        return (
            [json.loads(line) for line in log.read_text().splitlines()]
            if log.exists()
            else []
        )

    def store(self) -> GameStore:
        store = GameStore(self.path)
        self.addCleanup(store.close)
        return store

    def jobs(self) -> list:
        return self.store().query(
            "SELECT j.state, j.error FROM referee_jobs AS j ORDER BY j.input_id"
        )

    def host(self) -> GameHost:
        referee = CodexReferee(self.folder, self.game_id, codex=(sys.executable, str(FAKE)),
                               timeout=30, grace=1, lock_retry=0.1)  # fmt: skip
        return GameHost(self.path, authenticated=True, referee_executor=referee,
                        randint=lambda low, high: 4)  # fmt: skip

    async def start_host(self):
        loop = asyncio.get_running_loop()
        stop, ready = loop.create_future(), loop.create_future()
        host = self.host()
        task = asyncio.create_task(host.run(0, stop, on_ready=ready.set_result))

        async def shutdown():
            if not stop.done():
                stop.set_result(None)
            await asyncio.wait_for(task, 20)

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
        return json.loads(await asyncio.wait_for(connection.recv(), 20))

    async def until(self, connection, kind: str, **fields) -> dict:
        while True:
            message = await self.next(connection)
            if message["type"] == kind and all(
                message.get(k) == v for k, v in fields.items()
            ):
                return message

    async def exchange(self, connection, request_id, text, private=False) -> list:
        """Say something; what follows its reply, up to `idle`."""
        await connection.send(say(request_id, text, private))
        while (message := await self.next(connection)).get("request_id") != request_id:
            pass  # from before this input
        self.assertEqual(message["type"], "accepted", message)
        seen = [message]
        while seen[-1] != {"version": 1, "type": "referee", "state": "idle"}:
            seen.append(await self.next(connection))
        return seen

    async def paused(self):
        while not (self.fake / "paused").exists():
            await asyncio.sleep(0.02)
        (self.fake / "paused").unlink()


def texts(messages) -> list:
    return [m["text"] for m in messages if m["type"] == "message"]


class ConversationTests(LoopCase):
    async def test_questions_change_nothing_and_later_jobs_resume_the_session(self):
        self.script(
            {"calls": [call("get_context"), call("list_actions")],
             "reply": reply(("table", "A storm-damaged hilltop relay."))},
            {"calls": [call("get_context")], "reply": reply(("table", "Still windy."))},
        )  # fmt: skip
        await self.start_host()
        alice, _ = await self.joined("alice")
        seen = await self.exchange(alice, "a-1", "What is this place?")
        self.assertEqual(
            texts(seen), ["What is this place?", "A storm-damaged hilltop relay."]
        )
        await self.exchange(alice, "a-2", 'Is it "cold"?')
        first, second = self.runs()
        self.assertEqual((first["resume"], second["resume"]), (None, "thread-1"))
        self.assertIn(f"from Alice ({self.ids['alice']}), to the table:\n"
                      '"What is this place?"\n', first["prompt"])  # fmt: skip
        self.assertIn("Game revision now: 0.", first["prompt"])
        self.assertIn("start of the game's referee conversation", first["prompt"])
        self.assertNotIn("start of the game's referee conversation", second["prompt"])
        self.assertIn('"Is it \\"cold\\"?"', second["prompt"])  # quoted as one value
        store = self.store()
        self.assertEqual(
            (store.revision(), RefereeStore(store).session()), (0, ("thread-1", "ok"))
        )
        self.assertEqual(self.jobs(), [("completed", None)] * 2)
        # Every bridge call is in the job's diagnostics.
        diagnostics = sorted(self.folder.jobs(self.game_id).glob("*.jsonl"))
        tools = [json.loads(line).get("tool") for path in diagnostics
                 for line in path.read_text().splitlines()
                 if line.startswith('{"tesserae": "bridge"')]  # fmt: skip
        self.assertEqual(sorted(tools), ["get_context", "get_context", "list_actions"])

    async def test_a_supported_action_commits_and_everyone_hears_of_it(self):
        self.script({"calls": [call("get_context"), act("s1", "relay.enlist", name="Wren")],
                     "reply": reply(("table", "Wren signs up."))})  # fmt: skip
        await self.start_host()
        alice, _ = await self.joined("alice")
        bob, _ = await self.joined("bob")
        seen = await self.exchange(
            alice, "a-1", "I'd like to sign up a worker called Wren."
        )
        self.assertIn("Wren signs up.", texts(seen))
        self.assertEqual((await self.until(bob, "revision"))["revision"], 1)
        await self.until(bob, "message", text="Wren signs up.")
        state = self.store().inspect_state()
        self.assertEqual(state["workers"][self.ids["alice"]]["name"], "Wren")

    async def test_unauthorized_attempts_change_nothing(self):
        self.script({"calls": [act("s1", "relay.repair"),
                               call("referee_act", step_id="s2", command="relay.enlist",
                                    args={"name": "Odo"}),
                               act("s3", "relay.explode")],
                     "reply": reply(("table", "{results}"))})  # fmt: skip
        await self.start_host()
        alice, _ = await self.joined("alice")
        seen = await self.exchange(alice, "a-1", "Fix it, and enlist Odo for Bob")
        results = json.loads(texts(seen)[-1])
        self.assertEqual([(r["error"], r["result"]["code"]) for r in results],
                         [(True, "action_rejected"), (True, "unsupported_command"),
                          (True, "unsupported_command")])  # fmt: skip
        self.assertEqual(self.store().revision(), 0)

    async def test_private_replies_reach_only_the_sender(self):
        self.script({"calls": [call("get_view", participant_id=self.ids["alice"])],
                     "reply": reply(("sender", "Only what anyone can see."))})  # fmt: skip
        await self.start_host()
        alice, _ = await self.joined("alice")
        bob, _ = await self.joined("bob")
        seen = await self.exchange(
            bob, "b-1", "What is Alice's worker like?", private=True
        )
        self.assertEqual([m["audience"] for m in seen if m["type"] == "message"],
                         ["private", "private"])  # fmt: skip
        self.assertIn("privately to you", self.runs()[0]["prompt"])
        heard = [await self.next(alice), await self.next(alice)]
        self.assertEqual(
            [m["type"] for m in heard], ["referee", "referee"]
        )  # busy, idle

    async def test_a_pending_choice_waits_for_its_player(self):
        alice_id = self.ids["alice"]
        self.script(
            {"calls": [act("s1", "relay.enlist", name="Wren")],
             "reply": reply(("table", "Wren can try: 1d6 plus repair 1, needing 5. Try now?"),
                            ask=[(alice_id, "Try the repair now?")])},
            {"calls": [call("get_context")], "reply": reply(("table", "Odo is not here yet."))},
            {"calls": [act("s1", "relay.repair")], "reply": reply(("table", "{results}"),
                                                                  resolve=[alice_id])},
        )  # fmt: skip
        await self.start_host()
        alice, _ = await self.joined("alice")
        bob, _ = await self.joined("bob")
        await self.exchange(alice, "a-1", "Sign up Wren. Can Wren fix the relay?")
        await alice.send(STATUS)
        status = await self.until(alice, "referee_status")
        self.assertEqual(
            [c["question"] for c in status["choices"]], ["Try the repair now?"]
        )
        await self.exchange(bob, "b-1", "Where is Odo?")
        await alice.send(STATUS)
        self.assertEqual(len((await self.until(alice, "referee_status"))["choices"]), 1)
        seen = await self.exchange(alice, "a-2", "Yes, go.")
        [result] = json.loads(texts(seen)[-1])
        check = result["result"]["result"]["result"]["check"]
        self.assertEqual((check["roll"]["total"], check["repair"], check["total"],
                          check["success"]), (4, 1, 5, True))  # fmt: skip
        await alice.send(STATUS)
        self.assertEqual((await self.until(alice, "referee_status"))["choices"], [])
        self.assertTrue(self.store().inspect_state()["relay"]["restored"])

    async def test_an_exact_retry_returns_the_recorded_reply_without_a_run(self):
        self.script({"calls": [], "reply": reply(("table", "Hello."))})
        await self.start_host()
        alice, _ = await self.joined("alice")
        await self.exchange(alice, "a-1", "Hello?")
        await alice.send(say("a-1", "Hello?"))
        self.assertEqual((await self.next(alice))["type"], "accepted")
        self.assertEqual((await self.next(alice))["text"], "Hello.")
        self.assertEqual(len(self.runs()), 1)


class FailureTests(LoopCase):
    async def test_malformed_output_publishes_nothing_and_its_steps_reach_the_next_job(
        self,
    ):
        self.script(
            {"calls": [act("s1", "relay.enlist", name="Wren")], "reply": "malformed"},
            {"calls": [call("get_context")], "reply": reply(("table", "{results}"))},
        )  # fmt: skip
        await self.start_host()
        alice, _ = await self.joined("alice")
        seen = await self.exchange(alice, "a-1", "Sign up Wren")
        self.assertEqual(texts(seen), ["Sign up Wren"])  # nothing from the referee
        self.assertEqual(self.jobs(), [("failed", "malformed_reply")])
        self.assertEqual(self.store().revision(), 1)  # the committed step stays
        seen = await self.exchange(alice, "a-2", "Well?")
        self.assertIn(
            "The previous job ended without a reply (failed)", self.runs()[1]["prompt"]
        )
        [context] = json.loads(texts(seen)[-1])
        self.assertEqual([(s["step_id"], s["command"]) for s in context["result"]["earlier_results"]],
                         [("s1", "relay.enlist")])  # fmt: skip

    async def test_codex_dying_after_a_commit_keeps_it_and_ends_its_access(self):
        self.script(
            {"calls": [act("s1", "relay.enlist", name="Wren")], "exit": 3},
            {"calls": [call("get_context")], "reply": reply(("table", "{results}"))},
        )  # fmt: skip
        await self.start_host()
        alice, _ = await self.joined("alice")
        seen = await self.exchange(alice, "a-1", "Sign up Wren")
        self.assertEqual(texts(seen), ["Sign up Wren"])
        self.assertEqual(
            (self.jobs(), self.store().revision()), ([("failed", "runner_failed")], 1)
        )
        self.assertFalse(
            runner.JobLock.held(self.folder.lock_path(self.game_id))
        )  # nothing left
        old = {"TESSERAE_BRIDGE_SOCKET": str(self.folder.socket_path(self.game_id)),
               "TESSERAE_BRIDGE_TOKEN": (self.fake / "token").read_text()}  # fmt: skip
        with mock.patch.dict(os.environ, old):  # the dead run's token no longer works
            refused = await asyncio.to_thread(bridge.call_host, "get_context", {})
        self.assertEqual(refused["error"]["code"], "job_ended")
        seen = await self.exchange(alice, "a-2", "Did it work?")
        [context] = json.loads(texts(seen)[-1])
        self.assertEqual(
            [s["step_id"] for s in context["result"]["earlier_results"]], ["s1"]
        )

    async def test_a_lost_final_reply_and_junk_output_lines(self):
        self.script(
            {"calls": [call("get_context")], "reply": None},  # exits 0 with no reply
            {"calls": [], "noise": True, "reply": reply(("table", "Despite the noise."))},
        )  # fmt: skip
        await self.start_host()
        alice, _ = await self.joined("alice")
        self.assertEqual(texts(await self.exchange(alice, "a-1", "Hello?")), ["Hello?"])
        self.assertEqual(self.jobs(), [("failed", "malformed_reply")])
        seen = await self.exchange(alice, "a-2", "Hello again")
        self.assertIn("Despite the noise.", texts(seen))
        self.assertEqual(self.jobs()[-1], ("completed", None))
        self.assertEqual(RefereeStore(self.store()).session(), ("thread-1", "ok"))

    async def test_shutdown_before_a_commit_interrupts_with_nothing_changed(self):
        self.script({"calls": [act("s1", "relay.enlist", name="Wren")], "pause_before": 0,
                     "reply": reply(("table", "never"))})  # fmt: skip
        shutdown = await self.start_host()
        alice, _ = await self.joined("alice")
        await alice.send(say("a-1", "Sign up Wren"))
        await self.paused()
        await shutdown()
        self.assertEqual(self.jobs(), [("interrupted", "shutdown")])
        self.assertEqual(self.store().revision(), 0)
        self.assertFalse(runner.JobLock.held(self.folder.lock_path(self.game_id)))

    async def test_an_operator_stop_after_a_commit_keeps_it_for_the_next_job(self):
        self.script(
            {"calls": [act("s1", "relay.enlist", name="Wren")], "pause_before": 1,
             "reply": reply(("table", "never"))},
            {"calls": [call("get_context")], "reply": reply(("table", "{results}"))},
        )  # fmt: skip
        await self.start_host()
        alice, _ = await self.joined("alice")
        await alice.send(say("a-1", "Sign up Wren"))
        await self.paused()
        stopped = await asyncio.to_thread(subprocess.run, [
            sys.executable, "-m", "tesserae", "referee", "stop", "--database", str(self.path),
            "--referee-dir", str(self.folder.root)], capture_output=True, text=True, timeout=20)  # fmt: skip
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertIn("Asked the host to stop", stopped.stdout)
        await self.until(alice, "referee", state="idle")
        self.assertEqual(self.jobs(), [("interrupted", "operator")])
        self.assertEqual(self.store().revision(), 1)
        seen = await self.exchange(alice, "a-2", "Did it work?")
        self.assertIn("ended without a reply (interrupted)", self.runs()[1]["prompt"])
        [context] = json.loads(texts(seen)[-1])
        self.assertEqual(
            [s["step_id"] for s in context["result"]["earlier_results"]], ["s1"]
        )

    async def test_a_crashed_hosts_job_waits_for_its_codex_then_is_recovered(self):
        # A host died while its job ran: the job is still `running`, one step
        # committed but was never journaled, and its Codex still holds the lock.
        with GameStore(self.path, randint=lambda low, high: 4) as store:
            referee = RefereeStore(store)
            job = referee.submit(ParticipantContext(self.ids["alice"]), "a-0",
                                 "Sign up Wren", False, "h-dead").job  # fmt: skip
            step = referee.intend_step(job.job_id, "s1", "act", self.ids["alice"],
                                       "relay.enlist", {"name": "Wren"})  # fmt: skip
            raw = json.dumps({"version": 1, "type": "command", "request_id": step.request_id,
                              "expected_revision": 0, "command": "relay.enlist",
                              "args": {"name": "Wren"}})  # fmt: skip
            store.execute_command(ParticipantContext(self.ids["alice"]), raw)
        orphan = runner.JobLock(self.folder.lock_path(self.game_id))
        self.assertTrue(orphan.try_acquire())
        self.script(
            {"calls": [call("get_context")], "reply": reply(("table", "{results}"))}
        )
        await self.start_host()
        alice, snapshot = await self.joined("alice")
        self.assertEqual(snapshot["referee"]["state"], "busy")
        await alice.send(say("a-1", "Hello?"))
        self.assertEqual((await self.until(alice, "error"))["code"], "referee_busy")
        orphan.release()  # its Codex exits
        await self.until(alice, "referee", state="idle")
        self.assertEqual(self.jobs(), [("interrupted", "host_stopped")])
        seen = await self.exchange(alice, "a-2", "Hello again")
        [context] = json.loads(texts(seen)[-1])
        self.assertEqual([(s["step_id"], s["result"]["revision"])
                          for s in context["result"]["earlier_results"]], [("s1", 1)])  # fmt: skip
        self.assertEqual(len(self.runs()), 1)  # never a second live Codex

    async def test_a_missing_session_needs_an_operator_rebuild(self):
        with GameStore(self.path) as store:
            RefereeStore(store).set_session("thread-lost")
        self.script({"calls": [], "reply": reply(("table", "unused"))},
                    {"calls": [call("get_context")], "reply": reply(("table", "Back."))})  # fmt: skip
        shutdown = await self.start_host()
        alice, _ = await self.joined("alice")
        await self.exchange(alice, "a-1", "Hello?")
        self.assertEqual(self.jobs(), [("failed", "session_missing")])
        await alice.send(say("a-2", "Hello??"))
        self.assertEqual(
            (await self.until(alice, "error"))["code"], "referee_needs_rebuild"
        )
        rebuild = [sys.executable, "-m", "tesserae", "referee", "rebuild", "--database",
                   str(self.path), "--referee-dir", str(self.folder.root)]  # fmt: skip
        refused = await asyncio.to_thread(
            subprocess.run, rebuild, capture_output=True, text=True
        )
        self.assertEqual(refused.returncode, 1)
        self.assertIn("host is running", refused.stderr)
        await shutdown()
        rebuilt = await asyncio.to_thread(
            subprocess.run, rebuild, capture_output=True, text=True
        )
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
        await self.start_host()
        alice, _ = await self.joined("alice")
        self.assertIn("Back.", texts(await self.exchange(alice, "a-3", "Hello again")))
        run = self.runs()[-1]
        self.assertIsNone(run["resume"])
        self.assertIn("start of the game's referee conversation", run["prompt"])
        sessions = [row[0] for row in RefereeStore(self.store()).sessions()]
        self.assertEqual(sessions, ["thread-lost", "thread-2"])

    async def test_a_job_waits_while_an_earlier_runs_group_holds_the_lock(self):
        self.script({"calls": [], "reply": reply(("table", "Here now."))})
        await self.start_host()
        held = runner.JobLock(self.folder.lock_path(self.game_id))
        self.assertTrue(held.try_acquire())
        alice, _ = await self.joined("alice")
        await alice.send(say("a-1", "Anyone?"))
        await self.until(alice, "referee", state="busy")
        await asyncio.sleep(0.3)
        self.assertEqual(self.runs(), [])  # still waiting, still busy
        held.release()
        await self.until(alice, "message", text="Here now.")


class HostLifecycleTests(LoopCase):
    async def test_a_second_host_leaves_the_first_hosts_sockets_alone(self):
        self.script(
            {"calls": [call("get_context")], "reply": reply(("table", "Still here."))}
        )
        await self.start_host()
        sockets = [
            self.folder.socket_path(self.game_id),
            self.folder.control_path(self.game_id),
        ]
        before = [path.stat().st_ino for path in sockets]
        loop = asyncio.get_running_loop()
        with self.assertRaisesRegex(runner.RunnerError, "another host"):
            await asyncio.wait_for(self.host().run(0, loop.create_future()), 10)
        served = await asyncio.to_thread(subprocess.run, [
            sys.executable, "-m", "tesserae", "serve", "--database", str(self.path), "--port", "0",
            "--authenticated", "--referee-dir", str(self.folder.root)],
            capture_output=True, text=True, timeout=20)  # fmt: skip
        self.assertEqual(served.returncode, 1)
        self.assertIn("another host is already serving", served.stderr)
        self.assertEqual([path.stat().st_ino for path in sockets], before)
        alice, _ = await self.joined(
            "alice"
        )  # the first host still works, bridge and all
        self.assertIn("Still here.", texts(await self.exchange(alice, "a-1", "Hello?")))
        stopped = await asyncio.to_thread(subprocess.run, [
            sys.executable, "-m", "tesserae", "referee", "stop", "--database", str(self.path),
            "--referee-dir", str(self.folder.root)], capture_output=True, text=True, timeout=20)  # fmt: skip
        self.assertEqual(stopped.stdout.strip(), "The host has no referee job running.")

    async def test_shutdown_during_an_acceptance_leaves_nothing_running(self):
        self.script({"calls": [call("get_context")], "pause_before": 0,
                     "reply": reply(("table", "never"))})  # fmt: skip
        submit, entered, release = server._submit, threading.Event(), threading.Event()

        def slow_submit(*args):
            outcome = submit(*args)  # the job is stored `running`
            entered.set()
            release.wait(20)
            return outcome

        shutdown = await self.start_host()
        alice, _ = await self.joined("alice")
        with mock.patch.object(server, "_submit", slow_submit):
            await alice.send(say("a-1", "Hello?"))
            self.assertTrue(await asyncio.to_thread(entered.wait, 20))
            stopping = asyncio.create_task(shutdown())
            await asyncio.sleep(0.3)  # shutdown has begun while it is accepted
            release.set()
            await stopping
        self.assertEqual(self.jobs(), [("interrupted", "shutdown")])
        self.assertFalse(runner.JobLock.held(self.folder.lock_path(self.game_id)))

    async def test_shutdown_ends_a_job_of_this_run_whose_task_never_ran(self):
        loop = asyncio.get_running_loop()
        stop, ready = loop.create_future(), loop.create_future()
        host = self.host()
        task = asyncio.create_task(host.run(0, stop, on_ready=ready.set_result))
        await asyncio.wait_for(ready, 5)
        with GameStore(self.path) as store:  # stored, but no task will run it
            RefereeStore(store).submit(ParticipantContext(self.ids["alice"]), "a-1",
                                       "Hello?", False, host._host_run)  # fmt: skip
        stop.set_result(None)
        await asyncio.wait_for(task, 20)
        self.assertEqual(self.jobs(), [("interrupted", "shutdown")])
        self.assertEqual(self.runs(), [])


class ServeTests(unittest.TestCase):
    def test_serve_needs_a_set_up_referee_folder_for_a_referee_game(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "game.sqlite3"
        with GameStore.create(path, CATALOG.resolve("relay")) as store:
            RefereeStore(store).enable()
        served = subprocess.run(
            [sys.executable, "-m", "tesserae", "serve", "--database", str(path), "--port", "0",
             "--authenticated", "--referee-dir", str(Path(directory.name) / "none")],
            capture_output=True, text=True, timeout=20,
        )  # fmt: skip
        self.assertEqual(served.returncode, 2)
        self.assertIn("tesserae referee setup", served.stderr)


class StopCommandTests(LoopCase):
    def stop(self, *extra):
        return subprocess.run(
            [sys.executable, "-m", "tesserae", "referee", "stop", "--database", str(self.path),
             "--referee-dir", str(self.folder.root), *extra],
            capture_output=True, text=True, timeout=20,
        )  # fmt: skip

    def test_without_a_host_stop_touches_only_what_it_can_prove(self):
        stopped = self.stop()
        self.assertEqual((stopped.returncode, stopped.stdout.strip()),
                         (0, "Nothing is running for this game."))  # fmt: skip
        held = runner.JobLock(self.folder.lock_path(self.game_id))
        self.assertTrue(held.try_acquire())
        held.write({"state": "starting", "job_id": "j-1", "host_run": "h-1"})
        refused = self.stop()
        self.assertEqual(refused.returncode, 1)
        self.assertIn("still starting", refused.stderr)
        held.release()
        cleared = self.stop("--clear-stuck")
        self.assertEqual((cleared.returncode, cleared.stdout.strip()),
                         (0, "The record is not stuck; nothing to clear."))  # fmt: skip
