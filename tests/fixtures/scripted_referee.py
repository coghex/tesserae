"""A scripted stand-in for `codex` in the referee loop tests (no model, no
account). It starts the configured bridge as Codex does and follows one job
script per run, from FAKE_REFEREE_DIR/script.json:

    {"calls": [{"tool": "act", "args": {...}}, ...],
     "pause_before": 1,          # optional: stop before call 1 and wait
     "reply": {...} or "malformed" or null}

Reply texts may contain {results}, replaced by the calls' results as JSON.
Each run is logged to log.jsonl (argv, the prompt, its resume ID); sessions
it has started are kept in sessions.txt, so resuming any other ID fails as
Codex does ("no rollout found").
"""

import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

argv = sys.argv[1:]
if argv == ["--version"]:
    print("codex-cli 0.0.0-scripted")
    sys.exit(0)
state = Path(os.environ["FAKE_REFEREE_DIR"])
prompt = sys.stdin.read()
resume = argv[2] if argv[:2] == ["exec", "resume"] else None
reply_path = Path(argv[argv.index("-o") + 1])
log = state / "log.jsonl"
runs = len(log.read_text().splitlines()) if log.exists() else 0
with log.open("a") as stream:
    stream.write(json.dumps({"argv": argv, "prompt": prompt, "resume": resume}) + "\n")
script = json.loads((state / "script.json").read_text())[runs]

sessions = state / "sessions.txt"
known = sessions.read_text().split() if sessions.exists() else []
if resume is not None and resume not in known:
    print(f"Error: thread/resume failed: no rollout found for thread id {resume}",
          file=sys.stderr)  # fmt: skip
    sys.exit(1)
thread = resume or f"thread-{runs + 1}"
if resume is None:
    with sessions.open("a") as stream:
        stream.write(thread + "\n")
print(json.dumps({"type": "thread.started", "thread_id": thread}), flush=True)

results = []
if script.get("calls") or "pause_before" in script:
    config = tomllib.loads((Path(os.environ["CODEX_HOME"]) / "config.toml").read_text())
    server = config["mcp_servers"]["tesserae"]
    bridge = subprocess.Popen(
        [server["command"], *server["args"]],
        env={name: os.environ[name] for name in server["env_vars"]}
        | {"PATH": os.environ.get("PATH", "")},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )  # fmt: skip

    def rpc(number, method, params):
        message = {"jsonrpc": "2.0", "id": number, "method": method, "params": params}
        bridge.stdin.write(json.dumps(message) + "\n")
        bridge.stdin.flush()
        return json.loads(bridge.stdout.readline())["result"]

    rpc(0, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "scripted"}})  # fmt: skip
    for number, call in enumerate(script.get("calls", [])):
        if script.get("pause_before") == number:
            (state / "paused").write_text(str(number))
            time.sleep(60)  # until the test stops it
        result = rpc(number + 1, "tools/call",
                     {"name": call["tool"], "arguments": call["args"]})  # fmt: skip
        results.append({"error": result["isError"],
                        "result": json.loads(result["content"][0]["text"])})  # fmt: skip
    if script.get("pause_before") == len(script.get("calls", [])):
        (state / "paused").write_text(str(len(results)))
        time.sleep(60)
    bridge.stdin.close()
    bridge.wait()

reply = script.get("reply")
if reply == "malformed":
    reply_path.write_text("{not json")
elif reply is not None:
    text = json.dumps(reply).replace("{results}", json.dumps(json.dumps(results))[1:-1])
    reply_path.write_text(text)
print(json.dumps({"type": "turn.completed"}), flush=True)
