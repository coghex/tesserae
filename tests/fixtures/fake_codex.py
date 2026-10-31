"""A scripted stand-in for `codex` in the runner tests (no model, no account).

FAKE_CODEX_MODE picks the behavior; FAKE_CODEX_LOG, if set, receives what it
was run with (argv, cwd, the referee environment and stdin) as JSON.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

argv = sys.argv[1:]
if argv == ["--version"]:
    print("codex-cli 0.0.0-fake")
    sys.exit(0)
if argv[:2] == ["login", "status"]:
    sys.exit(0 if (Path(os.environ["CODEX_HOME"]) / "auth.json").exists() else 1)

mode = os.environ.get("FAKE_CODEX_MODE", "reply")
prompt = sys.stdin.read()
if os.environ.get("FAKE_CODEX_LOG"):
    Path(os.environ["FAKE_CODEX_LOG"]).write_text(
        json.dumps(
            {
                "argv": argv,
                "cwd": os.getcwd(),
                "stdin": prompt,
                "env": {
                    k: os.environ.get(k)
                    for k in (
                        "CODEX_HOME",
                        "TESSERAE_BRIDGE_SOCKET",
                        "TESSERAE_BRIDGE_TOKEN",
                    )
                },
            }
        )
    )
resume = argv[2] if argv[:2] == ["exec", "resume"] else None
reply_path = Path(argv[argv.index("-o") + 1])

if mode == "crash_before_session":
    print("boom", file=sys.stderr)
    sys.exit(2)
if mode == "session_missing":
    print(
        f"Error: thread/resume: thread/resume failed: no rollout found for thread id {resume}",
        file=sys.stderr,
    )
    sys.exit(1)
print(
    json.dumps({"type": "thread.started", "thread_id": resume or "fake-thread-1"}),
    flush=True,
)
print("not json at all", flush=True)
if mode == "wait_for_file":  # holds until the test says go
    while not Path(os.environ["FAKE_CODEX_GO"]).exists():
        time.sleep(0.02)
if mode in ("slow", "slow_with_child"):
    if mode == "slow_with_child":  # a grandchild in the same group, like an MCP server
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        Path(os.environ["FAKE_CODEX_CHILD"]).write_text(str(child.pid))
    time.sleep(60)
if mode == "nonzero_after_session":
    sys.exit(3)
if mode == "malformed":
    reply_path.write_text("{not json")
elif mode == "wrong_shape":
    reply_path.write_text(json.dumps({"messages": "hello"}))
elif mode != "no_reply":
    reply_path.write_text(
        json.dumps(
            {
                "messages": [
                    {"audience": "table", "text": f"Heard: {prompt.strip()[:40]}"}
                ],
                "choices": {"ask": [], "resolve": []},
                "narrated": [],
            }
        )
    )
print(json.dumps({"type": "turn.completed"}), flush=True)
