"""Version 1 host/client command protocol: validation and message formatting.

This module neither executes commands, stores state, nor opens connections.
See docs/protocol.md for the contract.
"""

import copy
import json
import math
from dataclasses import dataclass

from .dice import DiceExpressionError, RollResult, parse_expression

PROTOCOL_VERSION = 1
MAX_REVISION = 2**63 - 1
MAX_REQUEST_ID_LENGTH = 128
ERROR_CODES = frozenset(
    {
        "invalid_request",
        "unsupported_version",
        "unsupported_command",
        "invalid_expression",
        "stale_revision",
        "request_conflict",
        "internal_error",
    }
)
_REQUEST_FIELDS = frozenset(
    {"version", "type", "request_id", "expected_revision", "command", "args"}
)


class ProtocolError(ValueError):
    """A request was rejected with a stable error code."""

    def __init__(self, code: str, message: str, request_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id

    def to_message(self, revision: int | None = None) -> dict:
        return error_message(
            self.code, self.message, request_id=self.request_id, revision=revision
        )


@dataclass(frozen=True)
class CommandRequest:
    version: int
    request_id: str
    expected_revision: int
    command: str
    args: dict

    def content(self) -> dict:
        """Replay identity content; excludes request_id and expected_revision."""
        return {
            "version": self.version,
            "type": "command",
            "command": self.command,
            "args": copy.deepcopy(self.args),
        }


def is_valid_request_id(value) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= MAX_REQUEST_ID_LENGTH
        and not any(character.isspace() for character in value)
    )


def is_valid_revision(value) -> bool:
    return _is_int(value) and 0 <= value <= MAX_REVISION


def parse_request(text: str | bytes) -> CommandRequest:
    """Validate one JSON request without consuming randomness."""
    try:
        data = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_parse_finite_float,
        )
    except (ValueError, RecursionError) as error:
        raise ProtocolError("invalid_request", f"Request is not valid JSON: {error}")
    if not isinstance(data, dict):
        raise ProtocolError("invalid_request", "Request must be a JSON object.")

    request_id = data.get("request_id")
    if not is_valid_request_id(request_id):
        request_id = None

    def reject(code: str, message: str):
        raise ProtocolError(code, message, request_id)

    missing = sorted(_REQUEST_FIELDS - data.keys())
    extra = sorted(data.keys() - _REQUEST_FIELDS)
    if missing or extra:
        reject("invalid_request", _field_problem(missing, extra, "Request"))
    if not _is_int(data["version"]):
        reject("invalid_request", "'version' must be an integer.")
    if data["type"] != "command":
        reject("invalid_request", "'type' must be \"command\".")
    if request_id is None:
        reject(
            "invalid_request",
            f"'request_id' must be a string of 1 to {MAX_REQUEST_ID_LENGTH} "
            "characters without whitespace.",
        )
    if not is_valid_revision(data["expected_revision"]):
        reject(
            "invalid_request",
            f"'expected_revision' must be an integer from 0 to {MAX_REVISION}.",
        )
    if not isinstance(data["command"], str):
        reject("invalid_request", "'command' must be a string.")
    args = data["args"]
    if not isinstance(args, dict):
        reject("invalid_request", "'args' must be a JSON object.")
    if data["command"] == "roll":
        missing = sorted({"expression"} - args.keys())
        extra = sorted(args.keys() - {"expression"})
        if missing or extra:
            reject("invalid_request", _field_problem(missing, extra, "Roll args"))
        if not isinstance(args["expression"], str):
            reject("invalid_request", "'expression' must be a string.")

    if data["version"] != PROTOCOL_VERSION:
        reject("unsupported_version", f"Protocol version {PROTOCOL_VERSION} only.")
    if data["command"] != "roll":
        reject("unsupported_command", f"Unknown command {data['command']!r}.")
    try:
        parse_expression(args["expression"])
    except DiceExpressionError as error:
        reject("invalid_expression", str(error))

    return CommandRequest(
        version=data["version"],
        request_id=request_id,
        expected_revision=data["expected_revision"],
        command=data["command"],
        args=args,
    )


def result_message(request_id: str, revision: int, result: RollResult) -> dict:
    _check_request_id(request_id)
    _check_revision(revision)
    return {
        "version": PROTOCOL_VERSION,
        "type": "result",
        "request_id": request_id,
        "revision": revision,
        "result": result.as_dict(),
    }


def error_message(
    code: str,
    message: str,
    *,
    request_id: str | None = None,
    revision: int | None = None,
) -> dict:
    if code not in ERROR_CODES:
        raise ValueError(f"Unknown protocol error code {code!r}.")
    if not isinstance(message, str):
        raise ValueError("Error message must be a string.")
    if request_id is not None:
        _check_request_id(request_id)
    if revision is not None:
        _check_revision(revision)
    return {
        "version": PROTOCOL_VERSION,
        "type": "error",
        "request_id": request_id,
        "revision": revision,
        "code": code,
        "message": message,
    }


def welcome_message(revision: int) -> dict:
    _check_revision(revision)
    return {"version": PROTOCOL_VERSION, "type": "welcome", "revision": revision}


def encode_message(message: dict) -> str:
    return json.dumps(message, allow_nan=False)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_request_id(value):
    if not is_valid_request_id(value):
        raise ValueError(f"Invalid request_id {value!r}.")


def _check_revision(value):
    if not is_valid_revision(value):
        raise ValueError(f"Invalid revision {value!r}.")


def _field_problem(missing: list[str], extra: list[str], subject: str) -> str:
    parts = []
    if missing:
        parts.append("missing " + ", ".join(missing))
    if extra:
        parts.append("unexpected " + ", ".join(extra))
    return f"{subject} has {'; '.join(parts)}."


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(name):
    raise ValueError(f"non-finite number {name}")


def _parse_finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"non-finite number {text}")
    return value
