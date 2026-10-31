"""Version 1 host/client protocol: validation and message formatting.

Covers commands and the authenticated-session messages layered around them.
This module neither executes commands, stores state, verifies credentials, nor
opens connections. Command arguments are checked by the selected module's
validators (docs/modules.md). See docs/protocol.md for the contract.
"""

import copy
import json
import math
from dataclasses import dataclass, field

from .builtin import DICE_RUNTIME
from .dice import RollResult
from .modules import ArgumentError, Module, Runtime, describe_fields

PROTOCOL_VERSION = 1
MAX_REVISION = 2**63 - 1
MAX_REQUEST_ID_LENGTH = 128
MAX_IDENTIFIER_LENGTH = 128
MAX_TOKEN_LENGTH = 512
JOIN_TIMEOUT_SECONDS = 10
ERROR_CODES = frozenset(
    {
        "invalid_request",
        "unsupported_version",
        "unsupported_command",
        "invalid_expression",
        "action_rejected",
        "stale_revision",
        "request_conflict",
        "internal_error",
        # Authenticated sessions.
        "authentication_failed",
        "join_required",
        "join_timeout",
        "already_joined",
        "session_revoked",
    }
)
# One fixed message per session error, so failures never vary with the
# credential that was presented.
SESSION_ERROR_MESSAGES = {
    "authentication_failed": "Authentication failed.",
    "join_required": "Send a join message before anything else.",
    "join_timeout": f"No join message within {JOIN_TIMEOUT_SECONDS} seconds.",
    "already_joined": "This connection has already joined.",
    "session_revoked": "This session's credential is no longer valid.",
}
_REQUEST_FIELDS = frozenset(
    {"version", "type", "request_id", "expected_revision", "command", "args"}
)
_JOIN_FIELDS = frozenset({"version", "type", "token"})
_SYNC_FIELDS = frozenset({"version", "type"})


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


@dataclass(frozen=True)
class JoinRequest:
    """A well-formed join; the token is unverified and never shown in repr."""

    token: str = field(repr=False)


@dataclass(frozen=True)
class SyncRequest:
    """A read-only request for a fresh snapshot."""


def is_valid_request_id(value) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= MAX_REQUEST_ID_LENGTH
        and not any(character.isspace() for character in value)
    )


def is_valid_revision(value) -> bool:
    return _is_int(value) and 0 <= value <= MAX_REVISION


def is_valid_identifier(value) -> bool:
    """game_id and participant_id: 1 to 128 characters, no whitespace."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= MAX_IDENTIFIER_LENGTH
        and not any(character.isspace() for character in value)
    )


def parse_request(text: str | bytes, runtime: Runtime = DICE_RUNTIME) -> CommandRequest:
    """Validate one JSON command request without consuming randomness.

    Only `runtime`'s registered commands are accepted, checked by their own
    validators. The default is the `dice` compatibility module, whose only
    command is `roll`.
    """
    return _parse_command(_decode(text), runtime)


def parse_client_message(
    text: str | bytes, runtime: Runtime = DICE_RUNTIME
) -> CommandRequest | JoinRequest | SyncRequest:
    """Validate any client message in an authenticated session.

    Commands are validated exactly as by parse_request. Error messages never
    include a presented token.
    """
    data = _decode(text)
    if data.get("type") == "join":
        return _parse_join(data)
    if data.get("type") == "sync":
        return _parse_sync(data)
    return _parse_command(data, runtime)


def _decode(text: str | bytes) -> dict:
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
    if _has_lone_surrogate(data):
        raise ProtocolError(
            "invalid_request", "Request contains a string that is not valid Unicode."
        )
    return data


def _has_lone_surrogate(data) -> bool:
    """Whether any key or string value holds an unpaired surrogate like \\ud800.

    JSON escapes can produce these, but they cannot be encoded as UTF-8, so the
    store could never record such a request. Iterative, so depth is no issue.
    """
    pending = [data]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                return True
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return False


def _parse_join(data: dict) -> JoinRequest:
    _check_session_fields(data, _JOIN_FIELDS, "Join")
    if not isinstance(data["token"], str):
        raise ProtocolError("invalid_request", "'token' must be a string.")
    _check_session_version(data)
    token = data["token"]
    if not (
        1 <= len(token) <= MAX_TOKEN_LENGTH
        and not any(character.isspace() for character in token)
    ):
        # Indistinguishable from an unknown or revoked token.
        raise ProtocolError(
            "authentication_failed", SESSION_ERROR_MESSAGES["authentication_failed"]
        )
    return JoinRequest(token)


def _parse_sync(data: dict) -> SyncRequest:
    _check_session_fields(data, _SYNC_FIELDS, "Sync")
    _check_session_version(data)
    return SyncRequest()


def _check_session_fields(data: dict, fields: frozenset, subject: str):
    missing = sorted(fields - data.keys())
    extra = sorted(data.keys() - fields)
    if missing or extra:
        raise ProtocolError("invalid_request", describe_fields(missing, extra, subject))
    if not _is_int(data["version"]):
        raise ProtocolError("invalid_request", "'version' must be an integer.")


def _check_session_version(data: dict):
    if data["version"] != PROTOCOL_VERSION:
        raise ProtocolError(
            "unsupported_version", f"Protocol version {PROTOCOL_VERSION} only."
        )


def _parse_command(data: dict, runtime: Runtime) -> CommandRequest:
    request_id = data.get("request_id")
    if not is_valid_request_id(request_id):
        request_id = None

    def reject(code: str, message: str):
        raise ProtocolError(code, message, request_id)

    missing = sorted(_REQUEST_FIELDS - data.keys())
    extra = sorted(data.keys() - _REQUEST_FIELDS)
    if missing or extra:
        reject("invalid_request", describe_fields(missing, extra, "Request"))
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
    command = runtime.command(data["command"])
    deferred = None
    if command is not None:
        try:
            command.validate(args)
        except ArgumentError as error:
            if error.code == "invalid_request":
                reject(error.code, error.message)
            deferred = error  # a value error, reported after the version

    if data["version"] != PROTOCOL_VERSION:
        reject("unsupported_version", f"Protocol version {PROTOCOL_VERSION} only.")
    if command is None:
        reject("unsupported_command", f"Unknown command {data['command']!r}.")
    if deferred is not None:
        reject(deferred.code, deferred.message)

    return CommandRequest(
        version=data["version"],
        request_id=request_id,
        expected_revision=data["expected_revision"],
        command=data["command"],
        args=args,
    )


def result_message(request_id: str, revision: int, result: RollResult | dict) -> dict:
    """Wrap a dice result, or its recorded structured form, as a result message."""
    _check_request_id(request_id)
    _check_revision(revision)
    return {
        "version": PROTOCOL_VERSION,
        "type": "result",
        "request_id": request_id,
        "revision": revision,
        "result": (
            result.as_dict()
            if isinstance(result, RollResult)
            else copy.deepcopy(result)
        ),
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
    """Development mode's greeting."""
    _check_revision(revision)
    return {"version": PROTOCOL_VERSION, "type": "welcome", "revision": revision}


def snapshot_message(
    game_id: str,
    participant_id: str,
    revision: int,
    module: Module | None = None,
    view: dict | None = None,
) -> dict:
    """The recipient's view: the reply to a join or a sync.

    A game whose module projects views adds `module` and that participant's
    `view`, read with `revision`. Otherwise the snapshot is metadata only.
    """
    _check_identifier(game_id, "game_id")
    _check_identifier(participant_id, "participant_id")
    _check_revision(revision)
    message = {
        "version": PROTOCOL_VERSION,
        "type": "snapshot",
        "game_id": game_id,
        "participant_id": participant_id,
        "revision": revision,
    }
    if (module is None) != (view is None):
        raise ValueError("A snapshot has both a module and a view, or neither.")
    if view is not None:
        if not isinstance(view, dict):
            raise ValueError("A view must be a JSON object.")
        message["module"] = {
            "id": module.id,
            "version": module.version,
            "state_version": module.state_version,
        }
        message["view"] = view
    return message


def revision_message(game_id: str, revision: int) -> dict:
    """Unsolicited shared notification that the game's revision changed."""
    _check_identifier(game_id, "game_id")
    _check_revision(revision)
    return {
        "version": PROTOCOL_VERSION,
        "type": "revision",
        "game_id": game_id,
        "revision": revision,
    }


def session_error(code: str) -> dict:
    """A session error with its fixed message; no request_id or revision."""
    return error_message(code, SESSION_ERROR_MESSAGES[code])


def encode_message(message: dict) -> str:
    return json.dumps(message, allow_nan=False)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_request_id(value):
    if not is_valid_request_id(value):
        raise ValueError(f"Invalid request_id {value!r}.")


def _check_identifier(value, name: str):
    if not is_valid_identifier(value):
        raise ValueError(f"Invalid {name} {value!r}.")


def _check_revision(value):
    if not is_valid_revision(value):
        raise ValueError(f"Invalid revision {value!r}.")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")  # never echo client keys
        result[key] = value
    return result


def _reject_constant(name):
    raise ValueError(f"non-finite number {name}")


def _parse_finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"non-finite number {text}")
    return value
