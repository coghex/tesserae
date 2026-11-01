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
        # Referee mode (docs/designs/referee_contract.md section 7).
        "referee_busy",
        "referee_mode",
        "referee_off",
        "referee_needs_rebuild",
        # Lobby hosts (docs/designs/lobby_contract.md section 3).
        "login_required",
        "already_logged_in",
        "no_campaign",
        "campaign_selected",
        "campaign_unavailable",
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
# A lobby host's fixed messages (docs/designs/lobby_contract.md section 3).
LOBBY_ERROR_MESSAGES = {
    "authentication_failed": SESSION_ERROR_MESSAGES["authentication_failed"],
    "session_revoked": SESSION_ERROR_MESSAGES["session_revoked"],
    "login_required": "This host serves campaigns: log in with a host token first.",
    "join_timeout": f"No login message within {JOIN_TIMEOUT_SECONDS} seconds.",
    "already_logged_in": "This connection has already logged in.",
    "no_campaign": "Select a campaign first.",
    "campaign_selected": "Leave this campaign before selecting another.",
    "campaign_unavailable": "That campaign is not available to you.",
}
_REQUEST_FIELDS = frozenset(
    {"version", "type", "request_id", "expected_revision", "command", "args"}
)
_JOIN_FIELDS = frozenset({"version", "type", "token"})
_SYNC_FIELDS = frozenset({"version", "type"})
_SAY_FIELDS = frozenset({"version", "type", "request_id", "text", "private"})
_MESSAGES_FIELDS = frozenset({"version", "type", "after_seq"})
_LOGIN_FIELDS = frozenset({"version", "type", "token"})
_SELECT_FIELDS = frozenset({"version", "type", "campaign_id"})
LOBBY_TYPES = frozenset({"login", "lobby", "select", "leave"})


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


@dataclass(frozen=True)
class SayRequest:
    """Player input for the referee: words, never a command. The store checks
    the text's length and characters."""

    request_id: str
    text: str
    private: bool


@dataclass(frozen=True)
class RefereeStatusRequest:
    """A read of the caller's referee projection."""


@dataclass(frozen=True)
class MessagesRequest:
    """A read of the conversation the caller can see, after a message seq."""

    after_seq: int


@dataclass(frozen=True)
class LoginRequest:
    """A well-formed lobby login; the host token is unverified and never shown."""

    token: str = field(repr=False)


@dataclass(frozen=True)
class LobbyRequest:
    """A read of the caller's catalog and selection."""


@dataclass(frozen=True)
class SelectRequest:
    campaign_id: str


@dataclass(frozen=True)
class LeaveRequest:
    """Return to the lobby from the selected campaign."""


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


def parse_client_message(text: str | bytes, runtime: Runtime = DICE_RUNTIME):
    """Validate any client message in an authenticated session: a
    CommandRequest, JoinRequest, SyncRequest, SayRequest, RefereeStatusRequest
    or MessagesRequest.

    Commands are validated exactly as by parse_request. Error messages never
    include a presented token.
    """
    data = _decode(text)
    kind = data.get("type")
    if kind == "join":
        return _parse_join(data)
    if kind == "sync":
        return _parse_sync(data)
    if kind == "say":
        return _parse_say(data)
    if kind == "referee_status":
        _check_fields(data, _SYNC_FIELDS, "A referee_status request")
        return RefereeStatusRequest()
    if kind == "messages":
        _check_fields(data, _MESSAGES_FIELDS, "A messages request")
        if not is_valid_revision(data["after_seq"]):
            raise ProtocolError(
                "invalid_request", "'after_seq' must be a whole number, 0 or more."
            )
        return MessagesRequest(data["after_seq"])
    return _parse_command(data, runtime)


def parse_lobby_message(text: str | bytes):
    """A lobby host's own message (login, lobby, select or leave), or None for
    any other well-formed JSON object, which belongs to the selected campaign.

    Raises ProtocolError for malformed JSON and malformed lobby messages.
    Error messages never include a presented token.
    """
    data = _decode(text)
    kind = data.get("type")
    if not isinstance(kind, str):
        raise ProtocolError("invalid_request", "'type' must be a string.")
    if kind == "login":
        return LoginRequest(_parse_join(data, "Login").token)
    if kind in ("lobby", "leave"):
        _check_session_fields(data, _SYNC_FIELDS, f"A {kind} request")
        _check_session_version(data)
        return LobbyRequest() if kind == "lobby" else LeaveRequest()
    if kind == "select":
        _check_session_fields(data, _SELECT_FIELDS, "A select request")
        if not is_valid_identifier(data["campaign_id"]):
            raise ProtocolError(
                "invalid_request",
                f"'campaign_id' must be a string of 1 to {MAX_IDENTIFIER_LENGTH} "
                "characters without whitespace.",
            )
        _check_session_version(data)
        return SelectRequest(data["campaign_id"])
    return None


def _parse_say(data: dict) -> SayRequest:
    request_id = data.get("request_id")
    request_id = request_id if is_valid_request_id(request_id) else None
    try:
        _check_fields(data, _SAY_FIELDS, "A say request")
    except ProtocolError as error:
        error.request_id = request_id
        raise
    if request_id is None:
        raise ProtocolError(
            "invalid_request",
            f"'request_id' must be a string of 1 to {MAX_REQUEST_ID_LENGTH} "
            "characters without whitespace.",
        )
    if not isinstance(data["text"], str) or not isinstance(data["private"], bool):
        raise ProtocolError(
            "invalid_request",
            "'text' is a string and 'private' true or false.",
            request_id,
        )
    return SayRequest(request_id, data["text"], data["private"])


def _check_fields(data: dict, fields: frozenset, subject: str):
    """Exactly these fields, at protocol version 1."""
    missing = sorted(fields - data.keys())
    extra = sorted(data.keys() - fields)
    if missing or extra:
        raise ProtocolError("invalid_request", describe_fields(missing, extra, subject))
    if data["version"] != PROTOCOL_VERSION:
        raise ProtocolError(
            "unsupported_version", f"Protocol version {PROTOCOL_VERSION} only."
        )


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


def _parse_join(data: dict, subject: str = "Join") -> JoinRequest:
    _check_session_fields(data, _JOIN_FIELDS, subject)
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
    referee: dict | None = None,
    participant_name: str | None = None,
    campaign: dict | None = None,
) -> dict:
    """The recipient's view: the reply to a join or a sync.

    A game whose module projects views adds `module` and that participant's
    `view`, read with `revision`. Otherwise the snapshot is metadata only. A
    game in referee mode adds the recipient's `referee` projection, and an
    authenticated host the recipient's own `participant_name` (never a roster).
    A lobby host adds the selected `campaign`, `{"campaign_id", "name"}`.
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
    if participant_name is not None:
        if not isinstance(participant_name, str):
            raise ValueError("A participant name is a string.")
        message["participant_name"] = participant_name
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
    if referee is not None:
        message["referee"] = referee
    if campaign is not None:
        message["campaign"] = _campaign_summary(campaign)
    return message


def lobby_message(
    player_id: str, player_name: str, campaigns: list, selected: dict | None
) -> dict:
    """A lobby host's catalog for the caller: only campaign IDs and names."""
    _check_identifier(player_id, "player_id")
    if not isinstance(player_name, str):
        raise ValueError("A player name is a string.")
    return {"version": PROTOCOL_VERSION, "type": "lobby", "player_id": player_id,
            "player_name": player_name,
            "campaigns": [_campaign_summary(c) for c in campaigns],
            "selected": None if selected is None else _campaign_summary(selected)}  # fmt: skip


def left_message(campaign_id: str, reason: str) -> dict:
    """Unsolicited: the host returned this connection to the lobby."""
    _check_identifier(campaign_id, "campaign_id")
    if reason != "campaign_unavailable":
        raise ValueError("Unknown reason for leaving a campaign.")
    return {"version": PROTOCOL_VERSION, "type": "left", "campaign_id": campaign_id,
            "reason": reason}  # fmt: skip


def lobby_error(code: str, message: str | None = None) -> dict:
    """A lobby host's error, with its fixed message unless one is given."""
    return error_message(code, message or LOBBY_ERROR_MESSAGES[code])


def _campaign_summary(campaign: dict) -> dict:
    _check_identifier(campaign["campaign_id"], "campaign_id")
    if not isinstance(campaign["name"], str):
        raise ValueError("A campaign name is a string.")
    return {"campaign_id": campaign["campaign_id"], "name": campaign["name"]}


def accepted_message(request_id: str, job_id: str, seq: int) -> dict:
    """The reply to a `say`: its job, and the seq of the player's own message."""
    _check_request_id(request_id)
    return {"version": PROTOCOL_VERSION, "type": "accepted", "request_id": request_id,
            "job_id": job_id, "seq": seq}  # fmt: skip


def busy_message(request_id: str, revision: int, for_name: str) -> dict:
    """`referee_busy`: another job is running, and nothing was stored."""
    message = error_message(
        "referee_busy",
        f"The referee is still answering {for_name}. Try again when it finishes.",
        request_id=request_id,
        revision=revision,
    )
    message["job"] = {"state": "running", "for": for_name}
    return message


def conversation_message(
    seq: int, sender_name: str | None, audience: str, text: str
) -> dict:
    """One conversation message, as its recipients see it: from the referee
    (`sender_name` None) or a player, to the `table` or `private`."""
    if audience not in ("table", "private"):
        raise ValueError("A message's audience is table or private.")
    sender = (
        {"kind": "referee"}
        if sender_name is None
        else {"kind": "player", "name": sender_name}
    )
    return {"version": PROTOCOL_VERSION, "type": "message", "seq": seq, "from": sender,
            "audience": audience, "text": text}  # fmt: skip


def messages_message(messages: list) -> dict:
    """The reply to `messages`: conversation messages the caller can see."""
    return {"version": PROTOCOL_VERSION, "type": "messages", "messages": list(messages)}


def referee_status_message(projection: dict) -> dict:
    """The reply to `referee_status`: the caller's referee projection."""
    return {"version": PROTOCOL_VERSION, "type": "referee_status", **projection}


def referee_state_message(state: str) -> dict:
    """Unsolicited, to everyone: a job started (`busy`) or ended (`idle`)."""
    if state not in ("busy", "idle"):
        raise ValueError("The referee is busy or idle.")
    return {"version": PROTOCOL_VERSION, "type": "referee", "state": state}


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
