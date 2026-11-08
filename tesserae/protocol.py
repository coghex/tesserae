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
        # Characters (docs/designs/character_contract.md section 9).
        "characters_unsupported",
        "character_unavailable",
        "character_changed",
        "mode_forbidden",
        "stale_draft",
        "character_incomplete",
        "character_busy",
        # Campaign hosts (docs/designs/governance_contract.md section 13).
        "not_campaign_host",
        "stale_policy",
        "invalid_document",
        "proposal_unavailable",
        "proposal_decided",
        # Negotiated creation overrides (creation overrides contract section 10).
        "override_unsupported",
        "override_blocked",
        "override_required",
        "invalid_intent",
        "stale_preview",
        "offer_unavailable",
        "offer_stale",
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
# Characters (docs/designs/character_contract.md section 9).
CHARACTER_SUPPORT = 1  # what `select`'s optional `characters` field declares
MODES = ("setup", "creation", "play")
_CHARACTER_FIELDS = {
    "characters": frozenset({"version", "type"}),
    "character_select": frozenset({"version", "type", "character_id"}),
    "character_create": frozenset({"version", "type", "request_id", "name"}),
    "character_complete": frozenset(
        {"version", "type", "request_id", "character_id", "draft_revision"}
    ),
    "character_archive": frozenset({"version", "type", "request_id", "character_id"}),
}
# Campaign hosts (governance contract section 13).
ROLES = ("host", "guest")
_HOST_FIELDS = {
    "host_view": frozenset({"version", "type"}),
    "host_messages": frozenset({"version", "type", "after_seq"}),
    "campaign_host_transfer": frozenset({"version", "type", "request_id", "player_id"}),
    "host_operations": frozenset({"version", "type"}),
    "host_audit": frozenset({"version", "type", "after"}),
    "host_command": frozenset(
        {"version", "type", "request_id", "expected_revision", "command", "args"}
    ),
}
# Campaign settings (governance contract sections 5 and 7); a field in
# brackets is optional.
_RULES_FIELDS = {
    "rules_view": (frozenset({"version", "type"}), "character_id"),
    "rules_list": (frozenset({"version", "type"}), None),
    "rules_export": (frozenset({"version", "type"}), "revision"),
    "rules_import": (frozenset({"version", "type", "request_id", "content"}), None),
    "rules_activate": (
        frozenset(
            {"version", "type", "request_id", "revision", "expected_policy_revision"}
        ),
        None,
    ),
    "rules_reject": (frozenset({"version", "type", "request_id", "revision"}), None),
}
RULES_TYPES = frozenset(_RULES_FIELDS)
# Guest requests for host decisions (governance contract section 9).
_PROPOSAL_FIELDS = {
    "requests": frozenset({"version", "type"}),
    "request_decide": frozenset(
        {"version", "type", "request_id", "proposal_id", "decision", "note"}
    ),
    "request_cancel": frozenset({"version", "type", "request_id", "proposal_id"}),
}


# Negotiated creation overrides (creation overrides contract section 10):
# each type's required fields, then its optional ones (absent reads null).
_OVERRIDE_FIELDS = {
    "override_categories": (frozenset(), frozenset()),
    "override_discover": (frozenset({"character_id", "query"}), frozenset()),
    "override_preview": (frozenset({"character_id", "intent"}), frozenset()),
    "override_request": (
        frozenset({"request_id", "character_id", "intent", "explanation"}),
        frozenset({"revises"}),
    ),
    "override_decide": (
        frozenset({"request_id", "proposal_id", "decision"}),
        frozenset({"preview_digest", "intent", "note"}),
    ),
    "override_withdraw": (frozenset({"request_id", "offer_id", "note"}), frozenset()),
    "override_respond": (
        frozenset({"request_id", "offer_id", "response"}),
        frozenset({"preview_digest", "note"}),
    ),
    "override_apply": (
        frozenset({"request_id", "character_id", "intent", "preview_digest", "reason"}),
        frozenset(),
    ),
    "override_requests": (frozenset(), frozenset({"character_id", "states", "after"})),
    "override_detail": (frozenset({"proposal_id"}), frozenset()),
    "override_offers": (frozenset({"proposal_id"}), frozenset({"after"})),
    "override_offer": (frozenset({"offer_id"}), frozenset()),
    "override_history": (frozenset({"character_id"}), frozenset({"after"})),
    "override_record": (frozenset({"record_id"}), frozenset()),
}
OVERRIDE_TYPES = frozenset(_OVERRIDE_FIELDS)
_OVERRIDE_IDS = ("character_id", "proposal_id", "offer_id", "record_id", "revises")
_OVERRIDE_TEXTS = ("explanation", "note", "reason", "preview_digest", "decision",
                   "response", "states", "after")  # fmt: skip


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
    # In a character game, the actor the client believes it has:
    # (character ID or None, mode). The host compares; it never acts on it.
    actor: tuple | None = None


@dataclass(frozen=True)
class CharacterRequest:
    """A character message (character contract section 9): `characters`,
    `character_select`, `character_create`, `character_complete` or
    `character_archive`, with whichever of its fields that type has."""

    kind: str
    request_id: str | None = None
    character_id: str | None = None
    name: str | None = None
    draft_revision: int | None = None


@dataclass(frozen=True)
class HostRequest:
    """A campaign host message (governance contract section 13):
    `host_view`, `host_messages`, `host_operations`, `host_audit`,
    `host_command` or `campaign_host_transfer`, with whichever of its fields
    that type has. Who may send it is the host's to judge, never the
    message's. A `host_command` keeps its `raw` text, which the store parses
    with the module's host operations."""

    kind: str
    request_id: str | None = None
    after_seq: int | None = None
    player_id: str | None = None
    after: int | None = None
    raw: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class RulesRequest:
    """A campaign settings message (governance contract section 13): only
    `rules_view` is for every player; the others are the campaign host's,
    which the host judges, never the message."""

    kind: str
    request_id: str | None = None
    character_id: str | None = None
    revision: int | None = None
    expected_policy_revision: int | None = None
    content: dict | None = None


@dataclass(frozen=True)
class ProposalRequest:
    """`requests` (anyone: their own, or every one for the campaign host),
    `request_decide` (the campaign host) or `request_cancel` (the requester),
    governance contract section 13. Who may is the host's to judge."""

    kind: str
    request_id: str | None = None
    proposal_id: str | None = None
    decision: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class OverrideRequest:
    """A creation override message (creation overrides contract section
    10): its type, its request ID if it has one, and its fields (an absent
    optional one None). Shapes only: who may, and what the values mean,
    are the host's and the module's to judge."""

    kind: str
    request_id: str | None = None
    fields: dict = field(default_factory=dict)


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
    characters: bool = False  # the client supports characters


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
    CommandRequest, JoinRequest, SyncRequest, SayRequest, RefereeStatusRequest,
    MessagesRequest, CharacterRequest or HostRequest.

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
    if isinstance(kind, str) and kind in _CHARACTER_FIELDS:
        return _parse_character(kind, data)
    if isinstance(kind, str) and kind in _HOST_FIELDS:
        return _parse_host(kind, data)
    if isinstance(kind, str) and kind in _RULES_FIELDS:
        return _parse_rules(kind, data)
    if isinstance(kind, str) and kind in _PROPOSAL_FIELDS:
        return _parse_proposal(kind, data)
    if isinstance(kind, str) and kind in _OVERRIDE_FIELDS:
        return _parse_override(kind, data)
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
        fields = _SELECT_FIELDS | ({"characters"} if "characters" in data else set())
        _check_session_fields(data, fields, "A select request")
        if "characters" in data and data["characters"] != CHARACTER_SUPPORT:
            raise ProtocolError(
                "invalid_request", f"'characters' is {CHARACTER_SUPPORT} when given."
            )
        if not is_valid_identifier(data["campaign_id"]):
            raise ProtocolError(
                "invalid_request",
                f"'campaign_id' must be a string of 1 to {MAX_IDENTIFIER_LENGTH} "
                "characters without whitespace.",
            )
        _check_session_version(data)
        return SelectRequest(data["campaign_id"], "characters" in data)
    return None


def _parse_say(data: dict) -> SayRequest:
    request_id = data.get("request_id")
    request_id = request_id if is_valid_request_id(request_id) else None
    fields = _SAY_FIELDS | ({"actor"} if "actor" in data else set())
    try:
        _check_fields(data, fields, "A say request")
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
    actor = None
    if "actor" in data:
        actor = data["actor"]
        if (
            not isinstance(actor, dict)
            or set(actor) != {"character_id", "mode"}
            or actor["mode"] not in MODES
            or not (
                actor["character_id"] is None or isinstance(actor["character_id"], str)
            )
        ):
            raise ProtocolError(
                "invalid_request",
                '\'actor\' is exactly {"character_id": ID or null, "mode": '
                '"setup", "creation" or "play"}.',
                request_id,
            )
        actor = (actor["character_id"], actor["mode"])
    return SayRequest(request_id, data["text"], data["private"], actor)


def _parse_character(kind: str, data: dict) -> CharacterRequest:
    """A character message's shape. Its values are the store's to judge: an
    unknown character is `character_unavailable`, a bad name invalid there."""
    request_id = data.get("request_id")
    request_id = request_id if is_valid_request_id(request_id) else None
    try:
        _check_fields(data, _CHARACTER_FIELDS[kind], f"A {kind} request")
    except ProtocolError as error:
        error.request_id = request_id
        raise
    if "request_id" in data and request_id is None:
        raise ProtocolError(
            "invalid_request",
            f"'request_id' must be a string of 1 to {MAX_REQUEST_ID_LENGTH} "
            "characters without whitespace.",
        )
    character_id = data.get("character_id")
    if "character_id" in data and not isinstance(character_id, str):
        if not (kind == "character_select" and character_id is None):
            raise ProtocolError(
                "invalid_request", "'character_id' must be a string.", request_id
            )
    name = data.get("name")
    if name is not None and not isinstance(name, str):
        raise ProtocolError(
            "invalid_request", "'name' is a string or null.", request_id
        )
    revision = data.get("draft_revision")
    if "draft_revision" in data and (not is_valid_revision(revision) or revision < 1):
        raise ProtocolError(
            "invalid_request",
            "'draft_revision' is a whole number, 1 or more.",
            request_id,
        )
    return CharacterRequest(kind, request_id, character_id, name, revision)


def _parse_host(kind: str, data: dict) -> HostRequest:
    request_id = data.get("request_id")
    request_id = request_id if is_valid_request_id(request_id) else None
    try:
        _check_fields(data, _HOST_FIELDS[kind], f"A {kind} request")
    except ProtocolError as error:
        error.request_id = request_id
        raise
    if kind == "host_messages":
        if not is_valid_revision(data["after_seq"]):
            raise ProtocolError(
                "invalid_request", "'after_seq' must be a whole number, 0 or more."
            )
        return HostRequest(kind, after_seq=data["after_seq"])
    if kind == "host_audit":
        if not is_valid_revision(data["after"]):
            raise ProtocolError(
                "invalid_request", "'after' must be a whole number, 0 or more."
            )
        return HostRequest(kind, after=data["after"])
    if kind == "host_command":
        if request_id is None:
            raise ProtocolError(
                "invalid_request",
                f"'request_id' must be a string of 1 to {MAX_REQUEST_ID_LENGTH} "
                "characters without whitespace.",
            )
        # The operation and its arguments are the store's to check, with the
        # module's host operations, exactly as a command's.
        command = {**data, "type": "command"}
        return HostRequest(kind, request_id, raw=json.dumps(command))
    if kind == "campaign_host_transfer":
        if request_id is None:
            raise ProtocolError(
                "invalid_request",
                f"'request_id' must be a string of 1 to {MAX_REQUEST_ID_LENGTH} "
                "characters without whitespace.",
            )
        if not is_valid_identifier(data["player_id"]):
            raise ProtocolError(
                "invalid_request",
                f"'player_id' must be a string of 1 to {MAX_IDENTIFIER_LENGTH} "
                "characters without whitespace.",
                request_id,
            )
        return HostRequest(kind, request_id, player_id=data["player_id"])
    return HostRequest(kind)


def _parse_rules(kind: str, data: dict) -> RulesRequest:
    request_id = data.get("request_id")
    request_id = request_id if is_valid_request_id(request_id) else None
    fields, optional = _RULES_FIELDS[kind]
    try:
        if optional is not None and optional in data:
            fields = fields | {optional}
        _check_fields(data, fields, f"A {kind} request")
        if "request_id" in fields and request_id is None:
            raise ProtocolError(
                "invalid_request",
                f"'request_id' must be a string of 1 to {MAX_REQUEST_ID_LENGTH} "
                "characters without whitespace.",
            )
        character_id = data.get("character_id")
        if character_id is not None and not is_valid_identifier(character_id):
            raise ProtocolError(
                "invalid_request", "'character_id' must be a character ID or absent."
            )
        revision = data.get("revision")
        if "revision" in data and (not is_valid_revision(revision) or revision < 1):
            raise ProtocolError(
                "invalid_request", "'revision' must be a whole number, 1 or more."
            )
        expected = data.get("expected_policy_revision")
        if "expected_policy_revision" in data and not is_valid_revision(expected):
            raise ProtocolError(
                "invalid_request",
                "'expected_policy_revision' must be a whole number, 0 or more.",
            )
        content = data.get("content")
        if "content" in data and not isinstance(content, dict):
            raise ProtocolError(
                "invalid_request", "'content' must be a settings document object."
            )
    except ProtocolError as error:
        error.request_id = request_id
        raise
    return RulesRequest(kind, request_id, character_id, revision, expected, content)


def _parse_proposal(kind: str, data: dict) -> ProposalRequest:
    request_id = data.get("request_id")
    request_id = request_id if is_valid_request_id(request_id) else None
    try:
        _check_fields(data, _PROPOSAL_FIELDS[kind], f"A {kind} request")
        if kind == "requests":
            return ProposalRequest(kind)
        if request_id is None:
            raise ProtocolError(
                "invalid_request",
                f"'request_id' must be a string of 1 to {MAX_REQUEST_ID_LENGTH} "
                "characters without whitespace.",
            )
        if not is_valid_identifier(data["proposal_id"]):
            raise ProtocolError(
                "invalid_request", "'proposal_id' must be a proposal ID."
            )
        if kind == "request_cancel":
            return ProposalRequest(kind, request_id, data["proposal_id"])
        if data["decision"] not in ("approve", "deny"):
            raise ProtocolError("invalid_request", "'decision' is approve or deny.")
        note = data["note"]
        if note is not None and (not isinstance(note, str) or len(note) > 1000):
            raise ProtocolError(
                "invalid_request", "'note' is null or up to 1000 characters."
            )
    except ProtocolError as error:
        error.request_id = request_id
        raise
    return ProposalRequest(
        kind, request_id, data["proposal_id"], data["decision"], note
    )


def _parse_override(kind: str, data: dict) -> OverrideRequest:
    request_id = data.get("request_id")
    request_id = request_id if is_valid_request_id(request_id) else None
    required, optional = _OVERRIDE_FIELDS[kind]
    try:
        present = frozenset(name for name in optional if name in data)
        _check_fields(
            data, {"version", "type"} | required | present, f"A {kind} request"
        )
        if "request_id" in required and request_id is None:
            raise ProtocolError(
                "invalid_request",
                f"'request_id' must be a string of 1 to {MAX_REQUEST_ID_LENGTH} "
                "characters without whitespace.",
            )
        fields = {
            name: data.get(name) for name in (required | optional) - {"request_id"}
        }
        for name in _OVERRIDE_IDS:
            value = fields.get(name)
            if value is not None and not is_valid_identifier(value):
                raise ProtocolError("invalid_request", f"'{name}' must be an ID.")
            if name in required and value is None:
                raise ProtocolError("invalid_request", f"'{name}' must be an ID.")
        for name in _OVERRIDE_TEXTS:
            value = fields.get(name)
            if value is not None and not isinstance(value, str):
                raise ProtocolError("invalid_request", f"'{name}' must be text.")
        for name in ("intent", "query"):
            if (
                name in fields
                and fields[name] is not None
                and not isinstance(fields[name], dict)
            ):
                raise ProtocolError("invalid_request", f"'{name}' must be an object.")
            if name in required and not isinstance(fields[name], dict):
                raise ProtocolError("invalid_request", f"'{name}' must be an object.")
    except ProtocolError as error:
        error.request_id = request_id
        raise
    return OverrideRequest(kind, request_id, fields)


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
    actor: dict | None = None,
    sheet: dict | None = None,
    role: str | None = None,
    policy_revision: int | None = None,
    override_categories: list | None = None,
) -> dict:
    """The recipient's view: the reply to a join or a sync.

    A game whose module projects views adds `module` and that participant's
    `view`, read with `revision`. Otherwise the snapshot is metadata only. A
    game in referee mode adds the recipient's `referee` projection, and an
    authenticated host the recipient's own `participant_name` (never a roster).
    A lobby host adds the selected `campaign`, `{"campaign_id", "name"}`. A
    character game adds the connection's `actor` and its character's `sheet`
    (null in setup), always both (character contract section 9). A lobby
    campaign with a designated campaign host adds the recipient's `role`,
    and a game whose module has campaign settings its `policy_revision`
    (governance contract section 13).
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
    if actor is not None:
        message["actor"] = actor
        message["sheet"] = sheet
    if role is not None:
        if role not in ROLES:
            raise ValueError("A role is host or guest.")
        message["role"] = role
    if policy_revision is not None:
        _check_revision(policy_revision)
        message["policy_revision"] = policy_revision
    if override_categories is not None:  # creation overrides section 10
        message["override_categories"] = override_categories
    return message


def role_message(role: str, request_id: str | None = None) -> dict:
    """The connection's campaign role: unsolicited when it changes, or the
    reply to `campaign_host_transfer` (with its `request_id`)."""
    if role not in ROLES:
        raise ValueError("A role is host or guest.")
    message = {"version": PROTOCOL_VERSION, "type": "role", "role": role}
    if request_id is not None:
        _check_request_id(request_id)
        message["request_id"] = request_id
    return message


def host_view_message(view: dict) -> dict:
    """The reply to `host_view`: everything the campaign host may inspect."""
    return {"version": PROTOCOL_VERSION, "type": "host_view", **view}


def rules_view_message(view: dict) -> dict:
    """The reply to `rules_view` (governance contract section 7)."""
    return {"version": PROTOCOL_VERSION, "type": "rules_view", **view}


def rules_list_message(policy_revision: int, revisions: list) -> dict:
    """The reply to `rules_list`: every revision's metadata, no content."""
    return {"version": PROTOCOL_VERSION, "type": "rules_list",
            "policy_revision": policy_revision, "revisions": list(revisions)}  # fmt: skip


def host_operations_message(revision: int, operations: list) -> dict:
    """The reply to `host_operations`: what the host may run now."""
    _check_revision(revision)
    return {"version": PROTOCOL_VERSION, "type": "host_operations",
            "revision": revision, "operations": list(operations)}  # fmt: skip


def host_audit_message(records: list, next_after: int | None) -> dict:
    """The reply to `host_audit`: one page of audit records and the `after`
    of the next page (None at the end)."""
    return {"version": PROTOCOL_VERSION, "type": "host_audit",
            "records": list(records), "next_after": next_after}  # fmt: skip


def host_messages_message(messages: list, next_after_seq: int | None) -> dict:
    """The reply to `host_messages`: one page of every message, and the
    `after_seq` of the next page (None at the end)."""
    return {"version": PROTOCOL_VERSION, "type": "host_messages",
            "messages": list(messages), "next_after_seq": next_after_seq}  # fmt: skip


def actor_object(mode: str, character: dict | None) -> dict:
    """The connection's actor: its mode and its character's summary
    (`character_id`, `name`, `phase`, `draft_revision`), null in setup."""
    if mode not in MODES or (character is None) != (mode == "setup"):
        raise ValueError("Only setup has no character.")
    return {"mode": mode, "character": None if character is None else dict(character)}


def characters_message(actor: dict, characters: list) -> dict:
    """The reply to `characters`: the caller's own characters and actor."""
    return {"version": PROTOCOL_VERSION, "type": "characters", "actor": actor,
            "characters": [dict(c) for c in characters]}  # fmt: skip


def character_message(
    request_id: str, revision: int, character: dict, actor: dict
) -> dict:
    """The reply to a lifecycle operation: the character after it, and the
    connection's actor."""
    _check_request_id(request_id)
    _check_revision(revision)
    return {"version": PROTOCOL_VERSION, "type": "character", "request_id": request_id,
            "revision": revision, "character": dict(character), "actor": actor}  # fmt: skip


def actor_notice(reason: str, actor: dict) -> dict:
    """Unsolicited: another connection completed or archived this
    connection's character."""
    if reason not in ("completed", "archived"):
        raise ValueError("An actor notice is for completed or archived.")
    return {
        "version": PROTOCOL_VERSION,
        "type": "actor",
        "reason": reason,
        "actor": actor,
    }


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


_NO_CHARACTERS = object()


def conversation_message(
    seq: int,
    sender_name: str | None,
    audience: str,
    text: str,
    *,
    character=_NO_CHARACTERS,
    creation: dict | None = None,
) -> dict:
    """One conversation message, as its recipients see it: from the referee
    (`sender_name` None) or a player, to the `table` or `private`.

    In a character game a player's `from` also names the `character` they
    spoke as (None in setup), and a creation exchange carries `creation`,
    `{"character_id", "name"}`, always private (character contract
    section 9)."""
    if audience not in ("table", "private"):
        raise ValueError("A message's audience is table or private.")
    if creation is not None and audience != "private":
        raise ValueError("A creation exchange is always private.")
    sender = (
        {"kind": "referee"}
        if sender_name is None
        else {"kind": "player", "name": sender_name}
    )
    if sender_name is not None and character is not _NO_CHARACTERS:
        sender["character"] = character
    message = {"version": PROTOCOL_VERSION, "type": "message", "seq": seq, "from": sender,
               "audience": audience, "text": text}  # fmt: skip
    if creation is not None:
        message["creation"] = dict(creation)
    return message


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
