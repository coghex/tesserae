"""The referee's only tools: a minimal MCP stdio server that Codex starts
(`python -m tesserae.referee.bridge`, from the referee folder's config).

It holds no game logic and opens no database. Each tool call becomes one JSON
line to the host's Unix socket, carrying the job token; the host decides
everything (tesserae/referee/tools.py). The socket path and token come from
TESSERAE_BRIDGE_SOCKET and TESSERAE_BRIDGE_TOKEN, which Codex passes through
from the runner; the token is never printed. See
docs/designs/referee_contract.md section 6.
"""

import json
import os
import socket
import sys

SERVER_INFO = {"name": "tesserae", "version": "1"}
DEFAULT_PROTOCOL = "2025-06-18"
SOCKET_TIMEOUT = 55.0  # inside the config's tool_timeout_sec of 60
# The host is trusted and pages its large reads; this only stops a runaway line.
# A request's own limit (tools.MAX_REQUEST) does not apply to responses.
MAX_RESPONSE = 64 * 1024 * 1024

_STEP = {
    "type": "object",
    "additionalProperties": False,
    "required": ["step_id", "command", "args"],
    "properties": {
        "step_id": {
            "type": "string",
            "description": "A new short ID for this call, no spaces; reuse it only to retry this exact call.",
        },
        "command": {"type": "string"},
        "args": {"type": "object"},
    },
}


def _schema(properties: dict) -> dict:
    return {"type": "object", "additionalProperties": False,
            "required": sorted(properties), "properties": properties}  # fmt: skip


TOOLS = [
    {
        "name": "get_context",
        "description": (
            "Call first. The game as the referee sees it: the module's referee context, the "
            "participants, this job's input and sender, every pending choice, the rulings, "
            "earlier results nobody has been told about yet, and the first transcript page."
        ),
        "inputSchema": _schema({}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_transcript",
        "description": "The next page of the complete conversation after a message seq.",
        "inputSchema": _schema({"after_seq": {"type": "integer", "minimum": 0}}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_view",
        "description": "What one participant can see of the game.",
        "inputSchema": _schema({"participant_id": {"type": "string"}}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "list_actions",
        "description": "The commands this job's sender may use now, with their stakes.",
        "inputSchema": _schema({}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "act",
        "description": (
            "Run one of the sender's commands as the sender, at the current game state. "
            "Use it only for what they have clearly chosen. The result is what happened."
        ),
        "inputSchema": _STEP,
    },
    {
        "name": "referee_act",
        "description": "Run one of the module's referee-only commands, if it has any.",
        "inputSchema": _STEP,
    },
    {
        "name": "record_ruling",
        "description": "Record a ruling you have adopted, with its scope. It changes no mechanics.",
        "inputSchema": _schema(
            {"text": {"type": "string"}, "scope": {"type": "string"}}
        ),
    },
    # Governance (docs/designs/governance_contract.md section 10).
    {
        "name": "propose_host_decision",
        "description": (
            "Ask the campaign host to decide one exact change the sender wants: an exception "
            "to a setting for one of the sender's characters (target null: this job's), a "
            "campaign-wide setting (scope campaign), or a host operation. The host decides "
            "later; reply and end the job. Nothing changes until the host approves."
        ),
        "inputSchema": _schema(
            {
                "step_id": _STEP["properties"]["step_id"],
                "kind": {"enum": ["exception", "campaign_setting", "operation"]},
                "scope": {"enum": ["target", "campaign"]},
                "target": {"type": ["object", "null"]},
                "payload": {"type": "object"},
                "explanation": {"type": "string"},
            }
        ),  # fmt: skip
    },
    {
        "name": "host_operations",
        "description": "Campaign host's job only: the host operations available now.",
        "inputSchema": _schema({}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "host_act",
        "description": (
            "Campaign host's job only, on the host's explicit instruction: run one host "
            "operation exactly as asked."
        ),
        "inputSchema": _STEP,
    },
    {
        "name": "host_decide",
        "description": (
            "Campaign host's job only, on the host's explicit instruction naming the exact "
            "proposal: approve or deny it."
        ),
        "inputSchema": _schema(
            {
                "step_id": _STEP["properties"]["step_id"],
                "proposal_id": {"type": "string"},
                "decision": {"enum": ["approve", "deny"]},
                "note": {"type": ["string", "null"]},
            }
        ),  # fmt: skip
    },
    {
        "name": "host_propose_rules",
        "description": (
            "Campaign host's job only: store a settings document as a candidate. It is not "
            "active until host_activate_rules."
        ),
        "inputSchema": _schema(
            {
                "step_id": _STEP["properties"]["step_id"],
                "content": {"type": "object"},
            }
        ),  # fmt: skip
    },
    {
        "name": "host_activate_rules",
        "description": (
            "Campaign host's job only, when the host explicitly asks: activate a candidate "
            "revision against the current policy revision."
        ),
        "inputSchema": _schema(
            {
                "step_id": _STEP["properties"]["step_id"],
                "revision": {"type": "integer", "minimum": 1},
                "expected_policy_revision": {"type": "integer", "minimum": 0},
            }
        ),  # fmt: skip
    },
]
_TOOL_NAMES = {tool["name"] for tool in TOOLS}


def call_host(tool: str, args: dict) -> dict:
    """One request to the host's socket; a host that cannot be reached is an
    error result, never a crash."""
    request = {
        "token": os.environ.get("TESSERAE_BRIDGE_TOKEN", ""),
        "tool": tool,
        "args": args,
    }
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(SOCKET_TIMEOUT)
            connection.connect(os.environ["TESSERAE_BRIDGE_SOCKET"])
            connection.sendall(json.dumps(request).encode() + b"\n")
            with connection.makefile("rb") as stream:
                line = stream.readline(MAX_RESPONSE + 1)
        if not line:
            raise ConnectionResetError("no answer")
    except (OSError, KeyError):
        return {"ok": False, "error": {"code": "host_unreachable",
                                       "message": "The game cannot be reached; this job may have ended."}}  # fmt: skip
    try:
        if len(line) > MAX_RESPONSE or not line.endswith(b"\n"):
            raise ValueError("incomplete response")
        return json.loads(line)
    except ValueError:
        return {"ok": False, "error": {"code": "internal_error",
                                       "message": "The game's answer was cut off; retry the same call."}}  # fmt: skip


def answer(message: dict) -> dict | None:
    """The JSON-RPC response to one message, or None for a notification."""
    method, message_id = message.get("method"), message.get("id")
    if message_id is None:
        return None
    params = message.get("params") or {}
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", DEFAULT_PROTOCOL),
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in _TOOL_NAMES:
            return _rpc_error(message_id, -32602, f"Unknown tool: {name}")
        response = call_host(name, args)
        body = response.get("result") if response.get("ok") else response.get("error")
        result = {
            "content": [{"type": "text", "text": json.dumps(body)}],
            "isError": not response.get("ok"),
        }
    else:
        return _rpc_error(message_id, -32601, f"Method not found: {method}")
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _rpc_error(message_id, code: int, text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": code, "message": text},
    }


def main():
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        response = answer(message)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
