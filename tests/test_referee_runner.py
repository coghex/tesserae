"""The Codex runner (#66, REF-3), against a scripted fake `codex`
(tests/fixtures/fake_codex.py): arguments, session capture, failures,
timeouts, shutdown, the job lock and orphan stopping. No model is run."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from tesserae.referee import runner
from tesserae.referee.runner import CodexRun, JobLock, RunnerError

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_codex.py"
CODEX = (sys.executable, str(FAKE))
GAME = "game-1"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class RunnerCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.folder = runner.setup(self.tmp / "referee")
        self.log = self.tmp / "fake.json"
        self.env = {"FAKE_CODEX_LOG": str(self.log)}

    def mode(self, mode, **more):
        self.env.update(FAKE_CODEX_MODE=mode, **more)

    def run_job(self, prompt="Hello", session_id=None, timeout=10.0, job_id="j-1", sessions=None,
                on_session=None, codex=CODEX):  # fmt: skip
        lock = JobLock(self.folder.lock_path(GAME))
        self.assertTrue(lock.try_acquire())
        self.addCleanup(lock.release)
        run = CodexRun(self.folder, game_id=GAME, job_id=job_id, host_run="run-1", lock=lock,
                       codex=codex, timeout=timeout, grace=0.5)  # fmt: skip
        if sessions is not None:
            on_session = sessions.append
        saved = os.environ.copy()
        os.environ.update(self.env)
        try:
            run.start(prompt, session_id=session_id, token="secret-token-1",
                      on_session=on_session)  # fmt: skip
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.run = run
        try:
            return run.wait()
        finally:
            lock.release()  # as the host does once the job is recorded

    def seen(self) -> dict:
        return json.loads(self.log.read_text())


class SetupTests(RunnerCase):
    def test_setup_writes_a_private_folder_and_a_restricted_config(self):
        for path in (
            self.folder.root,
            self.folder.codex_home,
            self.folder.workspace,
            self.folder.run,
        ):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700, path)
        config = tomllib.loads((self.folder.codex_home / "config.toml").read_text())
        self.assertEqual((config["sandbox_mode"], config["approval_policy"], config["web_search"]),
                         ("read-only", "never", "disabled"))  # fmt: skip
        self.assertEqual(set(config["features"]), set(runner.FEATURES_OFF))
        self.assertFalse(any(config["features"].values()))
        self.assertEqual(
            list(config["mcp_servers"]), ["tesserae"]
        )  # the only tool server
        bridge = config["mcp_servers"]["tesserae"]
        self.assertEqual(
            (bridge["command"], bridge["args"]),
            (sys.executable, ["-m", "tesserae.referee.bridge"]),
        )
        self.assertEqual(
            bridge["env_vars"], ["TESSERAE_BRIDGE_SOCKET", "TESSERAE_BRIDGE_TOKEN"]
        )
        self.assertIn(
            "Do not start other agents",
            (self.folder.codex_home / "AGENTS.md").read_text(),
        )
        self.assertEqual(
            json.loads(self.folder.schema.read_text()), runner.REPLY_SCHEMA
        )

    def test_the_reply_schema_is_the_contracts(self):
        import re

        contract = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "designs"
            / "referee_contract.md"
        ).read_text()
        block = re.search(
            r"### Reply schema.*?```json\n(.*?)```", contract, re.S
        ).group(1)
        self.assertEqual(json.loads(block), runner.REPLY_SCHEMA)

    def test_check_reports_version_config_and_login(self):
        report = runner.check(self.folder.root, codex=CODEX)
        self.assertEqual((report["codex_version"], report["config_current"], report["logged_in"]),
                         ("codex-cli 0.0.0-fake", True, False))  # fmt: skip
        (self.folder.codex_home / "auth.json").write_text("{}")  # the login step, faked
        (self.folder.codex_home / "config.toml").write_text("# edited\n")
        report = runner.check(self.folder.root, codex=CODEX)
        self.assertEqual((report["config_current"], report["logged_in"]), (False, True))
        runner.setup(
            self.folder.root
        )  # rerunning setup restores the config, keeps the login
        self.assertTrue(runner.check(self.folder.root, codex=CODEX)["config_current"])
        self.assertTrue((self.folder.codex_home / "auth.json").exists())


class RunTests(RunnerCase):
    def test_a_first_run_uses_the_contract_arguments_and_environment(self):
        sessions = []
        result = self.run_job("Can Wren fix the relay?", sessions=sessions)
        self.assertEqual(
            (result.outcome, result.session_id, sessions),
            ("completed", "fake-thread-1", ["fake-thread-1"]),
        )
        self.assertEqual(
            result.reply["messages"][0]["text"], "Heard: Can Wren fix the relay?"
        )
        seen = self.seen()
        argv = seen["argv"]
        self.assertEqual(argv[:2], ["exec", "--json"])
        self.assertEqual(argv[-1], "-")  # the prompt is on stdin, never an argument
        for flag in (
            "--skip-git-repo-check",
            "--ignore-rules",
            "--output-schema",
            "-o",
        ):
            self.assertIn(flag, argv)
        for absent in (
            "--last",
            "--ephemeral",
            "resume",
            "Can Wren fix the relay?",
            "secret-token-1",
        ):
            self.assertNotIn(absent, argv)
        self.assertEqual(seen["stdin"], "Can Wren fix the relay?")
        self.assertEqual(Path(seen["cwd"]).resolve(), self.folder.workspace.resolve())
        self.assertEqual(seen["env"], {"CODEX_HOME": str(self.folder.codex_home),
                                       "TESSERAE_BRIDGE_SOCKET": str(self.folder.socket_path(GAME)),
                                       "TESSERAE_BRIDGE_TOKEN": "secret-token-1"})  # fmt: skip
        self.assertEqual(result.codex_version, "codex-cli 0.0.0-fake")

    def test_a_later_run_resumes_the_given_session(self):
        result = self.run_job(session_id="thread-7")
        self.assertEqual(self.seen()["argv"][:3], ["exec", "resume", "thread-7"])
        self.assertEqual((result.outcome, result.session_id), ("completed", "thread-7"))

    def test_the_session_is_captured_while_codex_is_still_running(self):
        go = self.tmp / "go"
        self.mode("wait_for_file", FAKE_CODEX_GO=str(go))
        sessions = []
        lock = JobLock(self.folder.lock_path(GAME))
        self.assertTrue(lock.try_acquire())
        self.addCleanup(lock.release)
        run = CodexRun(
            self.folder,
            game_id=GAME,
            job_id="j-1",
            host_run="r",
            lock=lock,
            codex=CODEX,
        )
        os.environ.update(self.env)
        try:
            run.start("Hi", session_id=None, token="t", on_session=sessions.append)
        finally:
            for key in self.env:
                os.environ.pop(key)
        deadline = time.monotonic() + 10
        while not sessions and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual((sessions, run.process.poll()), (["fake-thread-1"], None))
        go.write_text("go")
        self.assertEqual(run.wait().outcome, "completed")

    def test_diagnostics_keep_raw_events_privately(self):
        self.run_job("Hi")
        lines = self.run.diagnostics.read_text().splitlines()
        self.assertIn("not json at all", lines)  # kept, not parsed as an event
        self.assertEqual(json.loads(lines[0])["tesserae"], "start")
        self.assertEqual(json.loads(lines[-1])["tesserae"], "end")
        self.assertNotIn("secret-token-1", self.run.diagnostics.read_text())


class FailureTests(RunnerCase):
    def test_failures_map_to_job_states(self):
        cases = [
            ("crash_before_session", None, ("failed", "runner_failed", None)),
            (
                "session_missing",
                "thread-gone",
                ("failed", "session_missing", "thread-gone"),
            ),
            (
                "nonzero_after_session",
                None,
                ("failed", "runner_failed", "fake-thread-1"),
            ),
            ("malformed", None, ("failed", "malformed_reply", "fake-thread-1")),
            ("wrong_shape", None, ("failed", "malformed_reply", "fake-thread-1")),
            ("no_reply", None, ("failed", "malformed_reply", "fake-thread-1")),
        ]
        for number, (mode, session, expected) in enumerate(cases):
            with self.subTest(mode):
                self.mode(mode)
                result = self.run_job(session_id=session, job_id=f"j-{number}")
                self.assertEqual(
                    (result.outcome, result.error, result.session_id), expected
                )
                self.assertIsNone(result.reply)

    def test_a_timeout_stops_the_whole_process_group(self):
        child_file = self.tmp / "child"
        self.mode("slow_with_child", FAKE_CODEX_CHILD=str(child_file))
        result = self.run_job(timeout=1.0)
        self.assertEqual((result.outcome, result.error), ("interrupted", "timeout"))
        child = int(child_file.read_text())
        deadline = time.monotonic() + 5
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(child))  # the grandchild went with the group

    def test_stop_ends_a_run_for_shutdown(self):
        self.mode("slow")
        lock = JobLock(self.folder.lock_path(GAME))
        self.assertTrue(lock.try_acquire())
        self.addCleanup(lock.release)
        run = CodexRun(
            self.folder,
            game_id=GAME,
            job_id="j-1",
            host_run="r",
            lock=lock,
            codex=CODEX,
        )
        os.environ.update(self.env)
        try:
            run.start("Hi", session_id=None, token="t")
        finally:
            for key in self.env:
                os.environ.pop(key)
        run.stop()
        result = run.wait()
        self.assertEqual((result.outcome, result.error), ("interrupted", "shutdown"))
        self.assertEqual(JobLock.read(self.folder.lock_path(GAME))["state"], "ended")


class ReviewRegressionTests(RunnerCase):
    """The review of 18b2625 (R76-1 to R76-4)."""

    def wait_for(self, path: Path):
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(path.exists())

    def stubborn(self, child_file: Path) -> int:
        self.wait_for(child_file)
        child = int(child_file.read_text())
        self.addCleanup(lambda: alive(child) and os.kill(child, signal.SIGKILL))
        return child

    def test_a_timeout_also_stops_a_helper_that_ignores_sigterm(self):  # R76-1
        child_file = self.tmp / "child"
        self.mode("stubborn_child", FAKE_CODEX_CHILD=str(child_file))
        result = self.run_job(timeout=2.0)
        child = self.stubborn(child_file)
        self.assertEqual((result.outcome, result.error), ("interrupted", "timeout"))
        self.assertFalse(alive(child))  # gone before wait() returned

    def test_stop_also_stops_a_helper_that_ignores_sigterm(self):  # R76-1
        child_file = self.tmp / "child"
        self.mode("stubborn_child", FAKE_CODEX_CHILD=str(child_file))
        lock = JobLock(self.folder.lock_path(GAME))
        self.assertTrue(lock.try_acquire())
        self.addCleanup(lock.release)
        run = CodexRun(self.folder, game_id=GAME, job_id="j-1", host_run="r", lock=lock,
                       codex=CODEX, grace=0.5)  # fmt: skip
        os.environ.update(self.env)
        try:
            run.start("Hi", session_id=None, token="t")
        finally:
            for key in self.env:
                os.environ.pop(key)
        child = self.stubborn(child_file)
        run.stop()
        self.assertFalse(alive(child))  # stop() returns only once the group is gone
        self.assertEqual(run.wait().error, "shutdown")

    def test_a_session_that_cannot_be_saved_fails_the_run(self):  # R76-2
        def cannot_save(thread_id):
            raise RuntimeError("Session persistence failed")

        uncaught = []
        hook = threading.excepthook
        threading.excepthook = uncaught.append
        self.addCleanup(setattr, threading, "excepthook", hook)
        self.mode("wait_for_file", FAKE_CODEX_GO=str(self.tmp / "never"))
        started = time.monotonic()
        result = self.run_job(timeout=30.0, on_session=cannot_save)
        self.assertEqual((result.outcome, result.error, result.session_id),
                         ("failed", "runner_failed", "fake-thread-1"))  # fmt: skip
        self.assertIn("Session persistence failed", result.details[0])
        self.assertIsNone(result.reply)
        self.assertLess(time.monotonic() - started, 10)  # stopped, not left to time out
        self.assertEqual(uncaught, [])

    def test_a_group_that_survives_cleanup_is_stuck_not_ended(self):  # R76-1a
        self.mode("reply")
        lock_path = self.folder.lock_path(GAME)
        with mock.patch.object(runner, "_group_alive", return_value=True):
            result = self.run_job()  # a valid reply, but cleanup is never confirmed
            self.assertEqual(
                (result.outcome, result.error, result.reply),
                ("failed", "cleanup_failed", None),
            )
            record = JobLock.read(lock_path)
            self.assertEqual((record["state"], record["pgid"], record["pid"]),
                             ("stuck", self.run.process.pid, self.run.process.pid))  # fmt: skip
            self.assertIn("started_at", record)  # kept for the operator
            # Every descriptor is closed, yet no next job can start...
            self.assertFalse(JobLock(lock_path).try_acquire())
            self.assertTrue(JobLock.held(lock_path))
            with self.assertRaisesRegex(RunnerError, "still survives SIGKILL"):
                runner.stop_orphan(lock_path)
        # ...until the group is really gone, which clears the record.
        self.assertFalse(JobLock.held(lock_path))
        self.assertEqual(JobLock.read(lock_path)["state"], "ended")

    def test_a_session_save_that_does_not_return_in_time_fails_the_run(self):  # R76-2a
        entered, release = threading.Event(), threading.Event()

        def slow_save(thread_id):
            entered.set()
            release.wait(30)

        uncaught = []
        hook = threading.excepthook
        threading.excepthook = uncaught.append
        self.addCleanup(setattr, threading, "excepthook", hook)
        self.addCleanup(release.set)
        self.mode("reply")  # Codex itself finishes normally
        result = self.run_job(timeout=2.0, on_session=slow_save)
        self.assertTrue(entered.is_set())
        self.assertEqual((result.outcome, result.error, result.session_id, result.reply),
                         ("failed", "runner_failed", "fake-thread-1", None))  # fmt: skip
        self.assertIn("Saving the session ID did not finish", result.details[0])
        log = self.run.diagnostics.read_text()
        release.set()  # the save returns late; its reader resumes on a closed log
        self.run._readers[0].join(5)
        self.assertFalse(self.run._readers[0].is_alive())
        self.assertEqual((uncaught, self.run.diagnostics.read_text()), ([], log))

    def test_a_missing_executable_is_a_runner_failure(self):  # R76-3
        result = self.run_job(codex=(str(self.tmp / "no-such-codex"),))
        self.assertEqual((result.outcome, result.error), ("failed", "runner_failed"))
        self.assertIn("could not be started", result.details[0])
        self.assertTrue(self.run._log.closed)
        self.assertEqual(
            JobLock.read(self.folder.lock_path(GAME)),
            {"state": "ended", "job_id": "j-1"},
        )
        self.assertEqual(
            json.loads(self.run.diagnostics.read_text().splitlines()[-1])["tesserae"],
            "end",
        )

    def test_replies_are_checked_against_the_whole_schema(self):  # R76-4
        message = {"audience": "table", "text": "Hi"}
        good = {
            "messages": [message],
            "choices": {"ask": [], "resolve": []},
            "narrated": [],
        }
        bad = {
            "null choice lists": {**good, "choices": {"ask": None, "resolve": False}},
            "no messages": {**good, "messages": []},
            "nine messages": {**good, "messages": [message] * 9},
            "message without text": {**good, "messages": [{"audience": "table"}]},
            "unknown audience": {**good, "messages": [{"audience": "everyone", "text": "Hi"}]},
            "empty text": {**good, "messages": [{"audience": "table", "text": ""}]},
            "text too long": {**good, "messages": [{"audience": "table", "text": "x" * 4001}]},
            "extra message key": {**good, "messages": [{**message, "to": "bob"}]},
            "ask without question": {**good, "choices": {"ask": [{"participant_id": "p"}], "resolve": []}},
            "question too long": {**good, "choices": {
                "ask": [{"participant_id": "p", "question": "x" * 1001}], "resolve": []}},
            "resolve of a number": {**good, "choices": {"ask": [], "resolve": [3]}},
            "narrated number": {**good, "narrated": [{"job_id": "j", "step_id": 1}]},
            "seventeen narrated": {**good, "narrated": [{"job_id": "j", "step_id": "s"}] * 17},
        }  # fmt: skip
        for number, (name, reply) in enumerate(bad.items()):
            with self.subTest(name):
                self.mode("custom", FAKE_CODEX_REPLY=json.dumps(reply))
                result = self.run_job(job_id=f"j-{number}")
                self.assertEqual(
                    (result.outcome, result.error), ("failed", "malformed_reply")
                )
        full = {**good, "choices": {"ask": [{"participant_id": "p", "question": "Go?"}],
                                    "resolve": ["q"]}, "narrated": [{"job_id": "j", "step_id": "s"}]}  # fmt: skip
        self.mode("custom", FAKE_CODEX_REPLY=json.dumps(full))
        self.assertEqual(self.run_job(job_id="j-full").reply, full)


HOST = """
import os, sys, time
from pathlib import Path
from tesserae.referee import runner
folder = runner.RefereeDir(Path(sys.argv[1]))
lock = runner.JobLock(folder.lock_path("game-1"))
assert lock.try_acquire()
run = runner.CodexRun(folder, game_id="game-1", job_id="j-9", host_run="crashing", lock=lock,
                      codex=(sys.executable, sys.argv[2]))
os.environ["FAKE_CODEX_MODE"] = "slow"
run.start("Hi", session_id=None, token="t")
Path(sys.argv[3]).write_text(str(run.process.pid))
time.sleep(60)
"""


class LockTests(RunnerCase):
    def test_a_killed_host_leaves_the_lock_held_until_the_job_is_stopped(self):
        started = self.tmp / "started"
        host = subprocess.Popen(
            [sys.executable, "-c", HOST, str(self.folder.root), str(FAKE), str(started)]
        )
        self.addCleanup(lambda: host.poll() is None and host.kill())
        deadline = time.monotonic() + 15
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        codex_pid = int(started.read_text())
        self.addCleanup(lambda: alive(codex_pid) and os.kill(codex_pid, signal.SIGKILL))
        host.send_signal(signal.SIGKILL)  # a crash, not a clean shutdown
        host.wait()
        path = self.folder.lock_path(GAME)
        self.assertTrue(alive(codex_pid))  # Codex outlived its host
        self.assertTrue(JobLock.held(path))  # so the next host must not start another
        self.assertFalse(JobLock(path).try_acquire())
        record = JobLock.read(path)
        self.assertEqual(
            (record["state"], record["pid"], record["pgid"]),
            ("running", codex_pid, codex_pid),
        )
        self.assertIn("Stopped", runner.stop_orphan(path))
        self.assertFalse(JobLock.held(path))
        self.assertFalse(alive(codex_pid))
        self.assertEqual(runner.stop_orphan(path), "Nothing is running for this game.")

    def test_stop_refuses_what_it_cannot_identify(self):
        path = self.folder.lock_path(GAME)
        holder = JobLock(path)
        self.assertTrue(holder.try_acquire())
        self.addCleanup(holder.release)
        bystander = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        self.addCleanup(bystander.kill)
        identity = runner.process_identity(bystander.pid)
        cases = [
            ({"state": "starting", "job_id": "j-1"}, "still starting"),
            ({"state": "ended", "job_id": "j-1"}, "record is 'ended'"),
            ({"state": "running", "pid": bystander.pid, "pgid": bystander.pid,
              "started_at": "Thu Jan  1 00:00:00 1970", "command": identity["command"]}, "not the job's Codex"),
            ({"state": "running", "pid": bystander.pid, "pgid": bystander.pid + 1, **identity}, "no usable process group"),
        ]  # fmt: skip
        for record, message in cases:
            with self.subTest(message):
                holder.write(record)
                with self.assertRaisesRegex(RunnerError, message):
                    runner.stop_orphan(path)
        self.assertTrue(alive(bystander.pid))  # never signalled

    def test_a_run_needs_the_lock(self):
        run = CodexRun(self.folder, game_id=GAME, job_id="j-1", host_run="r",
                       lock=JobLock(self.folder.lock_path(GAME)), codex=CODEX)  # fmt: skip
        with self.assertRaisesRegex(RunnerError, "job lock"):
            run.start("Hi", session_id=None, token="t")


class CommandLineTests(unittest.TestCase):
    def test_setup_and_check(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name) / "referee"
        run = lambda *a: subprocess.run([sys.executable, "-m", "tesserae", "referee", *a, "--referee-dir", str(root)],  # noqa: E731
                                        capture_output=True, text=True)  # fmt: skip
        setup = run("setup")
        self.assertEqual(setup.returncode, 0, setup.stderr)
        self.assertIn(f"CODEX_HOME={root / 'codex-home'} codex login", setup.stdout)
        self.assertTrue((root / "codex-home" / "config.toml").is_file())
        check = run("check")
        self.assertIn("Config current: yes", check.stdout)

    def test_check_without_codex_installed(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name) / "referee"
        runner.setup(root)
        report = runner.check(
            root, codex=(str(Path(directory.name) / "no-such-codex"),)
        )
        self.assertEqual(
            (report["codex_version"], report["logged_in"], report["config_current"]),
            (None, False, True),
        )


if __name__ == "__main__":
    unittest.main()
