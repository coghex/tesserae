"""The referee bridge (#67, REF-4): the whitelisted tools, their authority,
the step journal around real commands, and the MCP script Codex starts.
docs/designs/referee_contract.md sections 6 and 9."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tesserae.builtin import CATALOG
from tesserae.credentials import new_token
from tesserae.modules import (
    ActionRejected,
    ArgumentError,
    Catalog,
    Command,
    Module,
    Outcome,
)
from tesserae.referee import runner
from tesserae.referee.store import REFEREE, RefereeStore
from tesserae.referee.tools import BridgeServer, BridgeTools
from tesserae.store import GameStore, ParticipantContext

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_codex.py"


def _validate_move(args):
    if set(args) != {"to"} or not isinstance(args["to"], str):
        raise ArgumentError('Move args are exactly {"to": PLACE}.')


def _move(action) -> Outcome:
    if action.participant_id != REFEREE:  # the module's own check, as the contract asks
        raise ActionRejected("Only the referee moves the keeper.")
    action.state["keeper"] = action.args["to"]
    return Outcome(action.state, {"keeper": action.args["to"]})


def _look(action) -> Outcome:
    return Outcome(action.state, {"keeper": action.state["keeper"]})


# An original test module with one referee-only command: a lighthouse keeper
# only the referee moves.
WATCH = Module(
    id="watch",
    version="1",
    state_version=1,
    commands=(Command("watch.look", lambda args: None, _look),),
    initial_state=lambda: {"keeper": "lamp room"},
    view=lambda state, participant_id: {"keeper": state["keeper"]},
    referee_commands=(Command("watch.keeper_move", _validate_move, _move),),
)


class BridgeCase(unittest.TestCase):
    runtime = CATALOG.resolve("relay")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "game.sqlite3"
        self.draws = []

        def randint(low, high):
            self.draws.append((low, high))
            return 4

        self.randint = randint
        self.store = GameStore.create(self.path, self.runtime, randint=randint)
        self.addCleanup(self.store.close)
        self.alice = ParticipantContext(
            self.store.add_participant("Alice", new_token())
        )
        self.bob = ParticipantContext(self.store.add_participant("Bob", new_token()))
        self.referee = RefereeStore(self.store)
        self.referee.enable()
        self.commits = []
        self.tools = BridgeTools(self.open, on_commit=self.commits.append)

    def open(self):
        return GameStore(self.path, randint=self.randint, runtime=self.runtime)

    def start_job(self, who=None, text="Hello", request_id="r-1", private=False):
        job = self.referee.submit(who or self.alice, request_id, text, private).job
        return job, self.tools.grant(job.job_id)

    def call(self, token, tool, **args):
        return self.tools.handle({"token": token, "tool": tool, "args": args})

    def ok(self, token, tool, **args) -> dict:
        response = self.call(token, tool, **args)
        self.assertTrue(response["ok"], response)
        return response["result"]

    def refused(self, code, token, tool, **args) -> dict:
        response = self.call(token, tool, **args)
        self.assertEqual(
            (response["ok"], response["error"]["code"]), (False, code), response
        )
        return response["error"]

    def enlist(self, token, step_id="s1", name="Wren") -> dict:
        return self.call(
            token, "act", step_id=step_id, command="relay.enlist", args={"name": name}
        )


class ActTests(BridgeCase):
    def test_a_player_action_runs_as_the_sender_through_the_store(self):
        job, token = self.start_job()
        result = self.ok(
            token, "act", step_id="s1", command="relay.enlist", args={"name": "Wren"}
        )
        self.assertEqual(
            (result["status"], result["result"]["revision"]), ("committed", 1)
        )
        self.assertEqual(
            self.store.inspect_state()["workers"][self.alice.participant_id]["name"],
            "Wren",
        )
        [record] = self.store.history(
            self.alice
        )  # Alice's own history, under the mapped ID
        self.assertEqual(record.request_id, f"ref:{job.job_id}:s1")
        self.assertEqual(self.commits, [1])
        self.assertEqual(
            self.referee._step(self.store, job.job_id, "s1").status, "committed"
        )

    def test_an_exact_retry_returns_the_recorded_result_and_a_changed_one_conflicts(
        self,
    ):
        _, token = self.start_job()
        first = self.enlist(token)
        self.assertEqual(self.enlist(token), first)  # no second commit
        self.assertEqual((self.store.revision(), self.commits), (1, [1]))
        self.refused("request_conflict", token, "act", step_id="s1", command="relay.enlist",
                     args={"name": "Odo"})  # fmt: skip
        self.refused("request_conflict", token, "referee_act", step_id="s1", command="relay.enlist",
                     args={"name": "Wren"})  # fmt: skip
        self.refused(
            "invalid_request",
            token,
            "act",
            step_id="first roll",
            command="relay.repair",
            args={},
        )

    def test_illegal_actions_are_refused_before_any_draw(self):
        _, token = self.start_job()
        error = self.refused(
            "action_rejected",
            token,
            "act",
            step_id="s1",
            command="relay.repair",
            args={},
        )
        self.assertIn("Enlist a worker", error["message"])
        self.assertEqual((self.draws, self.store.revision()), ([], 0))
        # The refusal is the step's recorded outcome: retrying gets it again.
        self.refused(
            "action_rejected",
            token,
            "act",
            step_id="s1",
            command="relay.repair",
            args={},
        )

    def test_a_stale_state_is_refused_and_the_same_step_can_be_retried(self):
        job, token = self.start_job()
        self.store.execute(self.bob, json.dumps({
            "version": 1, "type": "command", "request_id": "b-1", "expected_revision": 0,
            "command": "relay.enlist", "args": {"name": "Odo"}}))  # fmt: skip
        with mock.patch.object(
            GameStore, "revision", return_value=0
        ):  # read just before Bob's commit
            self.refused("stale_revision", token, "act", step_id="s1", command="relay.enlist",
                         args={"name": "Wren"})  # fmt: skip
        self.assertEqual(
            self.referee._step(self.store, job.job_id, "s1").status, "intended"
        )
        self.assertEqual(self.ok(token, "act", step_id="s1", command="relay.enlist",
                                 args={"name": "Wren"})["result"]["revision"], 2)  # fmt: skip

    def test_a_commit_whose_reply_was_lost_is_found_not_repeated(self):
        job, token = self.start_job()
        with (
            mock.patch.object(
                RefereeStore, "record_step", side_effect=OSError("disk gone")
            ),
            self.assertLogs("tesserae.referee.tools", "ERROR"),
        ):
            self.refused("internal_error", token, "act", step_id="s1", command="relay.enlist",
                         args={"name": "Wren"})  # fmt: skip
        # It committed, and players heard of it.
        self.assertEqual((self.store.revision(), self.commits), (1, [1]))
        # Within the job, the same step returns the committed result.
        self.assertEqual(self.enlist(token)["result"]["result"]["revision"], 1)
        self.assertEqual((self.store.revision(), self.commits), (1, [1]))
        # After a crash instead, reconciliation finds it and the next job is told.
        with (
            mock.patch.object(
                RefereeStore, "record_step", side_effect=OSError("disk gone")
            ),
            self.assertLogs("tesserae.referee.tools", "ERROR"),
        ):
            self.call(token, "act", step_id="s2", command="relay.repair", args={})
        self.referee.finish(job.job_id, "interrupted")
        [settled] = self.referee.reconcile_steps()
        self.assertEqual((settled.step_id, settled.status), ("s2", "committed"))
        _, token = self.start_job(self.bob, request_id="b-1")
        earlier = self.ok(token, "get_context")["earlier_results"]
        self.assertEqual([(s["step_id"], s["by"]) for s in earlier], [("s1", self.alice.participant_id),
                                                                      ("s2", self.alice.participant_id)])  # fmt: skip
        self.assertTrue(earlier[1]["result"]["result"]["check"]["success"])


class AuthorityTests(BridgeCase):
    def test_authority_cannot_be_forged(self):
        _, token = self.start_job()
        # No tool argument names an actor, and module arguments are exact.
        self.refused("invalid_request", token, "act", step_id="s1", command="relay.enlist",
                     args={"name": "Wren"}, participant_id=self.bob.participant_id)  # fmt: skip
        self.refused("invalid_request", token, "act", step_id="s2", command="relay.enlist",
                     args={"name": "Wren", "participant_id": self.bob.participant_id})  # fmt: skip
        self.refused(
            "unsupported_tool", token, "execute_sql", sql="DELETE FROM commands"
        )
        self.assertEqual(self.store.revision(), 0)
        # The store keeps the two contexts apart whatever the caller asks.
        raw = json.dumps({"version": 1, "type": "command", "request_id": "x", "expected_revision": 0,
                          "command": "relay.enlist", "args": {"name": "Wren"}})  # fmt: skip
        with self.assertRaises(ValueError):
            self.store.execute_command(ParticipantContext(REFEREE), raw)
        with self.assertRaises(ValueError):
            self.store.execute_command(self.alice, raw, referee=True)

    def test_job_access_ends_with_its_job(self):
        job, token = self.start_job()
        self.refused("job_ended", "not-the-token", "get_context")
        self.refused("job_ended", None, "get_context")
        self.referee.finish(job.job_id, "interrupted")
        self.refused(
            "job_ended",
            token,
            "act",
            step_id="s1",
            command="relay.enlist",
            args={"name": "Wren"},
        )
        _, newer = self.start_job(self.bob, request_id="b-1")
        self.refused("job_ended", token, "get_context")  # the old job's token
        self.assertEqual(self.ok(newer, "get_context")["input"]["from"]["name"], "Bob")
        self.tools.revoke()
        self.refused("job_ended", newer, "get_context")
        self.assertEqual(self.store.revision(), 0)


class RefereeCommandTests(BridgeCase):
    runtime = Catalog([WATCH], []).resolve("watch")

    def test_referee_commands_run_only_in_the_referee_context(self):
        _, token = self.start_job()
        self.refused("unsupported_command", token, "act", step_id="s1", command="watch.keeper_move",
                     args={"to": "shore"})  # fmt: skip
        self.refused(
            "unsupported_command",
            token,
            "referee_act",
            step_id="s2",
            command="watch.look",
            args={},
        )
        result = self.ok(
            token,
            "referee_act",
            step_id="s3",
            command="watch.keeper_move",
            args={"to": "shore"},
        )
        self.assertEqual(result["result"]["result"], {"keeper": "shore"})
        [record] = self.store.history(ParticipantContext(REFEREE))
        self.assertEqual(self.store.history(self.alice), [])
        self.assertTrue(record.request_id.endswith(":s3"))
        actions = self.ok(token, "list_actions")
        self.assertEqual((actions["actions"], actions["referee_commands"]),
                         ([{"command": "watch.look"}], ["watch.keeper_move"]))  # fmt: skip


class ReadTests(BridgeCase):
    def test_the_referee_context_stays_out_of_player_views(self):
        _, token = self.start_job(text="What is this place?")
        context = self.ok(token, "get_context")
        self.assertIn(
            "backup fuse", context["context"]["referee_only"]["maintenance_note"]
        )
        self.assertEqual((context["input"]["text"], context["revision"], context["pending_choices"]),
                         ("What is this place?", 0, []))  # fmt: skip
        self.assertEqual(
            [m["text"] for m in context["transcript"]["messages"]],
            ["What is this place?"],
        )
        view = self.ok(token, "get_view", participant_id=self.alice.participant_id)[
            "view"
        ]
        self.assertNotIn("fuse", json.dumps(view))
        self.refused(
            "invalid_request", token, "get_view", participant_id="p_0000000000000000"
        )
        [action] = self.ok(token, "list_actions")["actions"]
        self.assertEqual(action["command"], "relay.enlist")
        self.assertEqual(self.store.revision(), 0)  # reading changes nothing

    def test_rulings_are_recorded_and_shown_without_changing_the_game(self):
        _, token = self.start_job()
        self.ok(
            token, "record_ruling", text="Workers may swap tools.", scope="This scene"
        )
        self.assertEqual([r["text"] for r in self.ok(token, "get_context")["rulings"]],
                         ["Workers may swap tools."])  # fmt: skip
        self.assertEqual(self.store.revision(), 0)

    def test_the_transcript_pages(self):
        _, token = self.start_job()
        page = self.ok(token, "get_transcript", after_seq=0)
        self.assertEqual((len(page["messages"]), page["next_after_seq"]), (1, None))
        self.refused("invalid_request", token, "get_transcript", after_seq=-1)


class DefaultHookTests(BridgeCase):
    runtime = CATALOG.resolve("dice")

    def test_a_module_without_hooks_gets_the_minimal_default(self):
        _, token = self.start_job()
        context = self.ok(token, "get_context")["context"]
        self.assertEqual(
            context,
            {"views": {self.alice.participant_id: None, self.bob.participant_id: None}},
        )
        self.assertEqual(
            self.ok(token, "list_actions")["actions"], [{"command": "roll"}]
        )


class SocketTests(BridgeCase):
    def short_dir(self) -> Path:
        # A Unix socket path is limited to about 100 bytes.
        directory = tempfile.TemporaryDirectory(dir="/tmp", prefix="tb")
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def test_the_bridge_script_speaks_mcp_through_the_socket(self):
        _, token = self.start_job(text="I'd like to sign up Wren")
        socket_path = self.short_dir() / "game.sock"
        with BridgeServer(socket_path, self.tools):
            self.assertEqual(stat.S_IMODE(os.stat(socket_path).st_mode), 0o600)
            bridge = subprocess.Popen(
                [sys.executable, "-m", "tesserae.referee.bridge"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                env={**os.environ, "TESSERAE_BRIDGE_SOCKET": str(socket_path), "TESSERAE_BRIDGE_TOKEN": token},
                text=True,
            )  # fmt: skip
            self.addCleanup(lambda: bridge.poll() is None and bridge.kill())

            def rpc(number, method, params=None):
                bridge.stdin.write(json.dumps({"jsonrpc": "2.0", "id": number, "method": method,
                                               "params": params or {}}) + "\n")  # fmt: skip
                bridge.stdin.flush()
                return json.loads(bridge.stdout.readline())

            self.assertEqual(rpc(1, "initialize", {"protocolVersion": "2025-06-18"})["result"]["protocolVersion"],
                             "2025-06-18")  # fmt: skip
            names = [tool["name"] for tool in rpc(2, "tools/list")["result"]["tools"]]
            self.assertEqual(names, ["get_context", "get_transcript", "get_view", "list_actions", "act",
                                     "referee_act", "record_ruling"])  # fmt: skip
            acted = rpc(3, "tools/call", {"name": "act", "arguments": {
                "step_id": "s1", "command": "relay.enlist", "args": {"name": "Wren"}}})["result"]  # fmt: skip
            self.assertFalse(acted["isError"])
            self.assertEqual(
                json.loads(acted["content"][0]["text"])["status"], "committed"
            )
            refused = rpc(4, "tools/call", {"name": "act", "arguments": {
                "step_id": "s2", "command": "relay.enlist", "args": {"name": "Wren"}}})["result"]  # fmt: skip
            self.assertEqual((refused["isError"], json.loads(refused["content"][0]["text"])["code"]),
                             (True, "action_rejected"))  # fmt: skip
            self.assertEqual(
                rpc(5, "tools/call", {"name": "shell", "arguments": {}})["error"][
                    "code"
                ],
                -32602,
            )
            bridge.stdin.close()
            self.assertEqual(bridge.wait(10), 0)
        self.assertFalse(socket_path.exists())

    def test_a_runner_job_reaches_the_game_only_through_the_bridge(self):
        job, token = self.start_job(text="I'd like to sign up Wren")
        folder = runner.setup(self.short_dir() / "referee")
        with BridgeServer(folder.socket_path(self.store.game_id), self.tools):
            lock = runner.JobLock(folder.lock_path(self.store.game_id))
            with lock:
                run = runner.CodexRun(folder, game_id=self.store.game_id, job_id=job.job_id, host_run="h",
                                      lock=lock, codex=(sys.executable, str(FAKE)), timeout=30)  # fmt: skip
                with mock.patch.dict(os.environ, {"FAKE_CODEX_MODE": "mcp"}):
                    run.start("New input", session_id=None, token=token)
                result = run.wait()
        self.assertEqual(result.outcome, "completed", result.details)
        self.assertTrue(
            result.reply["messages"][0]["text"].startswith(
                "Wren signed up. Tools: get_context"
            )
        )
        self.assertEqual(
            self.store.inspect_state()["workers"][self.alice.participant_id]["name"],
            "Wren",
        )
        self.assertNotIn(token, run.diagnostics.read_text())


if __name__ == "__main__":
    unittest.main()
