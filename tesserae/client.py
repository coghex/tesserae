"""Thin streaming client for an authenticated Tesserae host: `tesserae connect`.

Joins with a token from a file, then forwards each nonblank stdin line as one
message while printing every message the host sends, one JSON object per
stdout line. Diagnostics go to stderr. The host owns all validation and game
logic; the client never rewrites, retries, or judges a request.

In a game with a referee (the join snapshot carries `referee`), each line is
instead the player's words, sent as a `say`; lines starting with `/` are
plumbing (/private, /status, /sync, /history, /retry, /help), and `//` sends a
line that starts with `/`. Output is readable lines, or JSON with --json.
See docs/protocol.md and docs/designs/referee_contract.md section 12.

With --lobby, the token is a host token for a lobby host (`serve
--registry`): the client logs in, shows the player's campaigns, and /join,
/leave and /campaigns choose among them. Inside a campaign it behaves as
above. Retry state belongs to the campaign it was sent in. See
docs/designs/lobby_contract.md.

In a campaign with characters the connection also has an actor: no
character (setup questions), a draft (private creation with the referee) or
a ready character (play). /characters, /character, /create, /sheet, /approve
and /archive manage it, every say names it, and retry state belongs to the
campaign, participant, character and mode it was sent as. See
docs/designs/character_contract.md section 15.
"""

import asyncio
import json
import os
import secrets
import signal
import ssl
import stat
import sys
import threading
from collections import deque
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    InvalidStatus,
    InvalidURI,
)

from . import override_lines
from .credentials import read_token_file
from .transport import TransportConfigError, check_client

DRAIN_TIMEOUT_SECONDS = 10.0
JOIN_REPLY_TIMEOUT_SECONDS = 10.0
EXIT_OK = 0  # stdin ended and every request was answered
EXIT_FAILED = 1  # could not start, or the host ended the session
EXIT_UNCERTAIN = 3  # some requests were never answered; resend them unchanged
# A request for a host decision (docs/designs/governance_contract.md section 9).
# A negotiated creation override has its own (creation overrides contract
# section 7).
PROPOSAL_STATES = tuple(
    dict.fromkeys(override_lines.LEGACY_STATES + override_lines.OVERRIDE_STATES)
)
EXIT_INTERRUPTED = 130  # Ctrl-C


class _Failure(Exception):
    """Stop with a diagnostic; never includes the token."""


def run(
    uri: str,
    token_file,
    game_id=None,
    drain_timeout=DRAIN_TIMEOUT_SECONDS,
    ca_file=None,
    json_output=False,
    lobby=False,
    campaign=None,
    character=None,
):
    """Check the destination and trust first: plaintext to another computer is
    refused before anything is read, sent, or contacted."""
    if campaign is not None and not lobby:
        return _diagnose("--campaign needs --lobby; nothing was sent", EXIT_FAILED)
    if character is not None and campaign is None:
        return _diagnose(
            "--character needs --campaign (and --lobby); nothing was sent", EXIT_FAILED
        )
    if game_id is not None and lobby:
        return _diagnose(
            "--game-id is for one game; with --lobby use --campaign. Nothing was sent",
            EXIT_FAILED,
        )
    try:
        ssl_context = check_client(uri, ca_file)
        token = _read_token(token_file)
    except (_Failure, TransportConfigError) as failure:
        return _diagnose(str(failure), EXIT_FAILED)
    client = _Client(uri, token, game_id, drain_timeout, ssl_context, json_output)
    client.lobby = lobby
    client.campaign_wanted = campaign
    client.character_wanted = character
    return asyncio.run(client.main())


class _Client:
    def __init__(
        self, uri, token, game_id, drain_timeout, ssl_context=None, json_output=False
    ):
        self.json_output = json_output
        self.referee = False  # set from the join snapshot
        self.sending = {}  # request ID to input, for says not yet answered
        self.last_say = None  # this client's last accepted input, for /retry
        self.reported_input = None  # `your_last_input` from the host, for /retry
        self.ssl_context = ssl_context
        self.uri = uri
        self.token = token
        self.game_id = game_id
        self.drain_timeout = drain_timeout
        self.unanswered = deque()  # lines sent, oldest first; replies come in order
        self.replied = asyncio.Event()
        self.deadline = None  # once stdin ends, when the host must have answered
        # Lobby mode (--lobby): the catalog last shown, the selected campaign,
        # and the retry state of each campaign and participant while the client
        # is elsewhere.
        self.lobby = False
        self.campaign_wanted = None
        self.catalog = []
        self.selection = None
        self.saved = {}
        self.entered = False  # set when a select's snapshot arrives
        # Characters (a campaign whose snapshots carry `actor`): the host's
        # word on this connection's actor, the list last shown (for numbers),
        # the draft revision of the sheet last printed (for /approve), and an
        # archive waiting for its confirmation.
        self.character_wanted = None
        self.actor = None
        # A campaign with a campaign host: "host" or "guest" here, else None
        # (docs/designs/governance_contract.md section 4).
        self.role = None
        self.roster = []
        self.shown_revision = None
        self.confirming = None
        self.created = None  # a draft just created, to select next
        self.refreshes = set()  # characters or sync requests the client sent itself
        # Governance (docs/designs/governance_contract.md section 15): the
        # game and settings revisions this client last saw, a host change
        # waiting for its confirmation, the one to resend with /retry after a
        # busy or failed host, and /inspect's character filters, in order.
        self.revision = None
        self.policy_revision = None
        self.pending = None
        self.mutation_retry = None
        self.inspecting = deque()
        # Creation overrides (creation overrides contract section 12): whether
        # this campaign negotiates them, its categories, what each read line
        # was sent for, lines to send right after the one typed, follow-up
        # reads a reply asks for, page cursors, and the requests known to be
        # negotiated ones.
        self.overrides_on = False
        self.categories = []
        self.contexts = {}
        self.followups = []
        self.queued = []
        self.cursors = {}
        self.override_ids = set()
        self.send_lock = asyncio.Lock()  # one line at a time, in deque order

    async def main(self) -> int:
        loop = asyncio.get_running_loop()
        self.interrupted = loop.create_future()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, self._interrupt)
        options = {"open_timeout": JOIN_REPLY_TIMEOUT_SECONDS, "proxy": None}
        if self.ssl_context is not None:
            options["ssl"] = self.ssl_context
        try:
            # TLS, including certificate and name checks, completes before the
            # join is sent, so a host that fails them never sees the token.
            connection = await self._first(
                _connect_without_redirects(self.uri, **options)
            )
        except _Redirected:
            return _diagnose(
                f"the host at {self.uri} answered with a redirect. Tesserae never "
                "follows redirects, because only the address you gave was "
                "checked; connect to the host's own address. Nothing was sent",
                EXIT_FAILED,
            )
        except ssl.SSLCertVerificationError as error:
            return _diagnose(
                f"cannot trust {self.uri}: {error.verify_message or 'verification failed'}. "
                "Check that --ca-file is this host's certificate and that the URI "
                "uses an address or name the certificate lists. Nothing was sent",
                EXIT_FAILED,
            )
        except (ssl.SSLError, ConnectionResetError) as error:
            # A host without TLS usually just drops the TLS handshake.
            if self.ssl_context is None:
                return _diagnose(f"cannot connect to {self.uri}: {_reason(error)}", 1)
            reason = getattr(error, "reason", None) or "the connection was reset"
            return _diagnose(
                f"cannot connect to {self.uri} securely: "
                f"{reason.replace('_', ' ').lower()}. "
                "Is the host running with --tls-cert and --tls-key? Nothing was sent",
                EXIT_FAILED,
            )
        except (OSError, InvalidHandshake, InvalidURI, TimeoutError) as error:
            return _diagnose(f"cannot connect to {self.uri}: {_reason(error)}", 1)
        except _Interrupted:
            return EXIT_INTERRUPTED
        try:
            return await self._session(connection)
        except _Interrupted:
            return self._stop("interrupted", EXIT_INTERRUPTED)
        except _Failure as failure:
            # Requests already sent may or may not have committed.
            code = EXIT_UNCERTAIN if self.unanswered else EXIT_FAILED
            return self._stop(str(failure), code)
        finally:
            await connection.close()

    async def _session(self, connection) -> int:
        if self.lobby:
            await self._login(connection)
        else:
            await self._join(connection)
        lines = asyncio.Queue()
        if self.campaign_wanted is not None:
            lines.put_nowait(f"/join {self.campaign_wanted}".encode())
        if self.character_wanted is not None:
            lines.put_nowait(f"/character {self.character_wanted}".encode())
        _start_stdin_reader(asyncio.get_running_loop(), lines, self._input_ended)
        receiver = asyncio.create_task(self._receive(connection))
        try:
            await self._forward(connection, lines, receiver)
            await self._until_closed(self._all_answered(), receiver)
        except TimeoutError:
            return self._stop(
                f"no reply within {self.drain_timeout:g} seconds after input ended",
                EXIT_UNCERTAIN,
            )
        except _HostClosed:
            return self._host_closed(connection)
        finally:
            receiver.cancel()
        return EXIT_OK

    async def _first_reply(self, connection, what: str) -> dict:
        try:
            first = await self._first(
                asyncio.wait_for(connection.recv(), JOIN_REPLY_TIMEOUT_SECONDS)
            )
        except TimeoutError:
            raise _Failure(f"the host did not answer the {what} in time") from None
        except ConnectionClosed:
            raise _Failure(f"the host closed the connection {_closed(connection)}")
        return _decode(first)

    async def _send_first(self, connection, message: dict):
        try:
            await connection.send(json.dumps(message))
        except ConnectionClosed:
            raise _Failure(f"the host closed the connection {_closed(connection)}")

    async def _login(self, connection):
        await self._send_first(
            connection, {"version": 1, "type": "login", "token": self.token}
        )
        message = await self._first_reply(connection, "login")
        if message.get("type") == "error" and _is_error(message):
            self._show(message)
            hint = (
                "; this looks like a single-game host: connect without --lobby, "
                "with a game token"
                if message["code"] in ("join_required", "invalid_request")
                else ""
            )
            raise _Failure(f"the host refused the login: {message['code']}{hint}")
        if message.get("type") != "lobby" or not _is_lobby(message):
            raise _Failure("the host's first reply was not a valid lobby")
        self._lobby_reply(message)

    async def _join(self, connection):
        await self._send_first(
            connection, {"version": 1, "type": "join", "token": self.token}
        )
        message = await self._first_reply(connection, "join")
        self.referee = isinstance(message.get("referee"), dict)
        if self.referee and not self.json_output:
            if message.get("type") == "snapshot":
                self._note(
                    "[joined] Type to talk to the referee; /help lists commands."
                )
        else:
            _print(message)
        if message.get("type") == "welcome":
            raise _Failure("this is a development host; start it with --authenticated")
        if message.get("type") == "error":
            if message.get("code") == "login_required":
                raise _Failure(
                    "this host serves campaigns: connect with --lobby and your host token"
                )
            raise _Failure(f"the host refused to join: {message.get('code')}")
        if message.get("type") != "snapshot" or not _is_snapshot(message):
            raise _Failure("the host's first reply was not a valid snapshot")
        self.session = _session_of(message)
        if self.game_id is not None and message.get("game_id") != self.game_id:
            raise _Failure(
                f"the host serves game {message.get('game_id')}, not {self.game_id}; "
                "nothing was sent"
            )

    async def _forward(self, connection, lines: asyncio.Queue, receiver):
        """Send each nonblank stdin line as it arrives, until stdin ends.

        In lobby mode a line that changes the selection (of a campaign or a
        character) is answered before the next line is read, so each line is
        understood in the campaign, and as the actor, it was typed in. After
        each, the client waits for the referee status of where it now is.
        """
        if self.referee:
            await self._recover(connection, receiver)
        while (line := await self._until_closed(lines.get(), receiver)) is not None:
            text = _decode_line(line)
            if text is not None and self.lobby:
                text = self._translate_lobby(text)
            elif text is not None and self.referee:
                text = self._translate(text)
            if text is None:
                continue
            await self._send_line(connection, text)
            while self.followups:  # a command that reads two things in order
                await self._send_line(connection, self.followups.pop(0))
            if self.lobby and _sent_type(text) in _BOUNDARIES:
                await self._until_closed(self._all_answered(), receiver)
                if self.created is not None:  # a new draft: select it next
                    created, self.created = self.created, None
                    await self._send_line(connection, _select_character(created))
                    await self._until_closed(self._all_answered(), receiver)
                if self.entered and self.actor is not None:
                    # A character campaign: say what this player has there.
                    await self._send_line(connection, _CHARACTERS)
                    await self._until_closed(self._all_answered(), receiver)
                if self.selection is not None and self.referee:
                    await self._recover(connection, receiver)
                self.entered = False

    async def _recover(self, connection, receiver):
        """Show the player's last input and the referee's state, and wait for
        them before reading on, so a /retry typed next can resend an input the
        host accepted before this connection."""
        await self._send_line(
            connection, json.dumps({"version": 1, "type": "referee_status"})
        )
        await self._until_closed(self._all_answered(), receiver)

    async def _send_line(self, connection, text: str):
        # Locked, so lines reach the host in the order replies are expected.
        async with self.send_lock:
            self.unanswered.append(text)
            try:
                await connection.send(text)
            except ConnectionClosed:
                raise _HostClosed from None

    def _refresh(self, connection, text: str):
        """Ask the host where this connection now is, after a reply that says
        the client's idea of its character was out of date."""

        async def send():
            try:
                await self._send_line(connection, text)
            except _HostClosed:
                pass

        task = asyncio.create_task(send())
        self.refreshes.add(task)
        task.add_done_callback(self.refreshes.discard)

    async def _receive(self, connection):
        """Print everything the host sends; ends quietly when it closes.

        A message counts as the reply to the oldest unanswered line only if it
        is a valid reply to it. Anything else stops the client, leaving that
        request reported as uncertain.
        """
        try:
            async for raw in connection:
                message = _decode(raw)
                if message.get("type") == "revision":
                    if not _is_revision(message.get("revision")):
                        raise _Failure("the host sent a malformed notification")
                    self._saw_revision(message["revision"])
                    self._show(message)
                    continue
                if message.get("type") in ("message", "referee"):
                    if not _is_unsolicited(message):
                        raise _Failure("the host sent a malformed referee message")
                    self._show(message)
                    continue
                if message.get("type") == "left" and self.lobby:
                    if not _is_left(message, self.selection):
                        raise _Failure("the host sent a malformed left message")
                    self._show(message)
                    self._back_in_lobby()
                    continue
                if message.get("type") == "role" and "request_id" not in message:
                    if not (
                        message.get("version") == 1
                        and message.get("role") in ("host", "guest")
                    ):
                        raise _Failure("the host sent a malformed role notice")
                    self._show(message)
                    if self.selection is not None:
                        self._set_role(message["role"])
                    continue
                if message.get("type") == "proposal" and "request_id" not in message:
                    if not (
                        message.get("version") == 1
                        and isinstance(message.get("proposal_id"), str)
                        and message.get("state") in PROPOSAL_STATES
                    ):
                        raise _Failure("the host sent a malformed proposal notice")
                    self._show(message)
                    continue
                if message.get("type") == "actor" and self.actor is not None:
                    if not (
                        message.get("version") == 1
                        and message.get("reason") in ("completed", "archived")
                        and _is_actor(message.get("actor"))
                    ):
                        raise _Failure("the host sent a malformed actor notice")
                    self._show(message)
                    self._set_actor(message["actor"])
                    continue
                if not self.unanswered:
                    raise _Failure("the host sent a reply to nothing")
                sent = self.unanswered[0]
                _check_reply(message, sent, getattr(self, "session", None))
                if self.referee:  # every line sent was the client's own JSON
                    self._remember(message, sent)
                if self.lobby:
                    self._after_reply(message, sent)
                    self._follow_up(connection, message)
                else:
                    self._show(message)
                self.unanswered.popleft()
                self.replied.set()
        except ConnectionClosed:
            pass

    # --- lobby mode -----------------------------------------------------------------

    def _translate_lobby(self, text: str) -> str | None:
        """A typed line in lobby mode: a lobby command, or else what the
        selected campaign makes of it. Nothing is sent from the lobby except
        lobby requests."""
        command, _, rest = text.partition(" ")
        rest = rest.strip()
        if command == "/campaigns":
            return json.dumps({"version": 1, "type": "lobby"})
        if command == "/join":
            return self._join_line(rest)
        if command == "/leave":
            if self.selection is None:
                return self._note("[lobby] You are not in a campaign.")
            return json.dumps({"version": 1, "type": "leave"})
        if command == "/help":
            self._note(_LOBBY_HELP)
            if self.selection is not None:
                self._note(_GOVERNANCE_HELP)
                if self.overrides_on:
                    self._note(_OVERRIDE_HELP)
                if self.role == "host":
                    self._note(_HOST_HELP)
                    if self.overrides_on:
                        self._note(_OVERRIDE_HOST_HELP)
            if self.actor is not None:
                self._note(_CHARACTER_HELP)
            if self.selection is not None and self.referee:
                self._note(_HELP)
            return None
        if self.selection is None:
            return self._note(
                "[lobby] Choose a campaign first: /campaigns lists them, /join enters one."
            )
        if command in _GOVERNANCE_COMMANDS:
            return self._governance_line(command, rest)
        if command in _OVERRIDE_COMMANDS:
            if not self.overrides_on:
                return self._note(
                    f"[override] {self.selection['name']} does not negotiate creation "
                    "overrides."
                )
            return self._override_line(command, rest)
        if command == "/retry" and self.mutation_retry is not None:
            return self._retry_mutation()
        if command in _CHARACTER_COMMANDS:
            if self.actor is None:
                return self._note(
                    f"[character] {self.selection['name']} has no characters."
                )
            return self._character_line(command, rest)
        if self.actor is not None and self.actor["mode"] == "creation":
            if command == "/private":
                return self._note(
                    "[error] Everything you say while creating is already private; "
                    "just type it."
                )
        if self.referee:
            return self._translate(text)
        return text

    def _character_line(self, command: str, rest: str) -> str | None:
        """One character command as its message, or None after a note
        (character contract section 15)."""
        if command == "/characters":
            return _CHARACTERS
        if command == "/sheet":
            return json.dumps({"version": 1, "type": "sync"})
        if command == "/create":
            return json.dumps({"version": 1, "type": "character_create",
                               "request_id": _new_id("k"), "name": rest or None})  # fmt: skip
        if command == "/approve":
            character = self.actor["character"]
            if self.actor["mode"] != "creation":
                return self._note(
                    "[character] Only a draft is approved; select one first."
                )
            if self.shown_revision is None:
                return self._note(
                    "[character] Look at the sheet first (/sheet); /approve completes "
                    "the revision you were shown."
                )
            return json.dumps({"version": 1, "type": "character_complete",
                               "request_id": _new_id("k"),
                               "character_id": character["character_id"],
                               "draft_revision": self.shown_revision})  # fmt: skip
        wanted, _, confirm = rest.partition(" ")
        if command == "/character":
            if not rest:
                return self._note(
                    "[character] Type /character and a number from /characters, or "
                    "/character none."
                )
            if rest == "none":
                return _select_character(None)
            found = self._character_id(rest)
            return None if found is None else _select_character(found)
        # /archive N, then /archive N confirm
        if not wanted:
            return self._note(
                "[character] Type /archive and a number from /characters."
            )
        found = self._character_id(wanted)
        if found is None:
            return None
        if confirm != "confirm" or self.confirming != found:
            self.confirming = found
            name = _label(self._known(found))
            return self._note(
                f"[character] Archive {name}? It leaves your list for good. Type "
                f"/archive {wanted} confirm to archive it."
            )
        self.confirming = None
        return json.dumps({"version": 1, "type": "character_archive",
                           "request_id": _new_id("k"), "character_id": found})  # fmt: skip

    # --- governance (docs/designs/governance_contract.md section 15) -----------

    def _governance_line(self, command: str, rest: str) -> str | None:
        """One governance command as its message, or None after a note. The
        host decides who may do what; the client only asks for exact
        confirmation before a consequential change."""
        if command == "/role":
            return self._note(_role_line(self.role, self.policy_revision))
        if command == "/requests":
            if rest.startswith("overrides"):
                if not self.overrides_on:
                    return self._note("[override] This campaign has no overrides.")
                return self._override_requests(rest[len("overrides") :].split())
            if self.overrides_on:  # then the first page of open override requests
                self.followups.append(self._read(
                    "override_requests", ("page", ("requests", None, "open")),
                    states="open",
                ))  # fmt: skip
            return _message("requests")
        if command == "/cancel":
            if not rest:
                return self._note("[request] Type /cancel and a request's ID.")
            return self._mutation("request_cancel", proposal_id=rest)
        if command == "/decide":
            proposal_id, _, more = rest.partition(" ")
            decision, _, note = more.strip().partition(" ")
            if self.overrides_on and (
                decision in ("offer", "counter", "confirm")
                or proposal_id in self.override_ids
            ):
                return self._override_decide(proposal_id, decision, note.strip())
            if not proposal_id or decision not in ("approve", "deny"):
                return self._note(
                    "[request] Type /decide ID approve or /decide ID deny, and an "
                    "optional note."
                )
            return self._mutation("request_decide", proposal_id=proposal_id,
                                  decision=decision, note=note.strip() or None)  # fmt: skip
        if command == "/rules":
            return self._rules_line(rest)
        if command == "/inspect":
            self.inspecting.append(rest or None)
            return _message("host_view")
        if command == "/history-all":
            after = rest or "0"
            if not after.isdigit():
                return self._note("[history] Type /history-all or /history-all N.")
            return _message("host_messages", after_seq=int(after))
        if command == "/audit":
            after = rest or "0"
            if not after.isdigit():
                return self._note("[audit] Type /audit or /audit N.")
            return _message("host_audit", after=int(after))
        if command == "/admin":
            return self._admin_line(rest)
        # /transfer PLAYER, then /transfer PLAYER confirm
        player, _, confirm = rest.partition(" ")
        if not player:
            return self._note("[role] Type /transfer and the player's ID (u_...).")
        if confirm != "confirm" or self.pending != ("transfer", player):
            self.pending = ("transfer", player)
            return self._note(
                f"[role] Hand this campaign to {player}? You become a guest at once. "
                f"Type /transfer {player} confirm to do it."
            )
        self.pending = None
        return self._mutation("campaign_host_transfer", player_id=player)

    # --- creation overrides (creation overrides contract section 12) ---------

    def _read(self, kind: str, context: tuple, **fields) -> str:
        """An override read, with what its reply is for: shown, a step of a
        confirmation, or a page to continue."""
        line = _message(kind, **fields)
        self.contexts.setdefault(line, deque()).append(context)
        return line

    def _draft(self) -> str | None:
        """The selected draft, the default CHARACTER where one is allowed."""
        if self.actor is not None and self.actor["mode"] == "creation":
            return self.actor["character"]["character_id"]
        return None

    def _override_line(self, command: str, rest: str) -> str | None:
        if command == "/overrides":
            return self._overrides_discover(rest.split())
        if command == "/override":
            action, _, rest = rest.partition(" ")
            return self._override_action(action, rest.strip())
        target, _, extra = rest.partition(" ")
        extra = extra.strip()
        if command == "/offer":
            if not target:
                return self._note("[offer] Type /offer and a request or offer ID.")
            if target.startswith("ho-"):
                return self._read("override_offer", ("show",), offer_id=target)
            return self._read("override_detail", ("detail",), proposal_id=target)
        if command == "/offers":
            if not target:
                return self._note("[offer] Type /offers and a request's ID.")
            fields = {"proposal_id": target}
            key = ("offers", target)
            if extra == "more":
                if self.cursors.get(key) is None:
                    return self._note("[offer] There are no more offers to show.")
                fields["after"] = self.cursors[key]
            return self._read("override_offers", ("page", key), **fields)
        if command == "/accept":
            if not target:
                return self._note("[offer] Type /accept and an offer's ID.")
            if extra == "confirm":
                if self.pending is None or self.pending[:2] != ("accept", target):
                    return self._note(
                        f"[offer] Look at it first: /accept {target} shows the offer."
                    )
                _, offer_id, digest = self.pending
                self.pending = None
                return self._mutation("override_respond", offer_id=offer_id,
                                      response="accept", preview_digest=digest)  # fmt: skip
            return self._read("override_offer", ("accept", target), offer_id=target)
        if command == "/decline":
            if not target:
                return self._note("[offer] Type /decline, an offer's ID, and a note.")
            return self._mutation("override_respond", offer_id=target,
                                  response="decline", note=extra or None)  # fmt: skip
        # /withdraw OFFER NOTE
        if not target or not extra:
            return self._note("[offer] Type /withdraw, an offer's ID, and why.")
        return self._mutation("override_withdraw", offer_id=target, note=extra)

    def _character_or_draft(self, words: list) -> tuple:
        """(character ID or None after a note, the words left), the first
        word being a CHARACTER unless the selected draft is meant."""
        draft = self._draft()
        if words and not words[0].startswith("{"):
            found = self._character_id(words[0])
            return found, words[1:]
        if draft is None:
            self._note("[override] Name the character: you have no draft selected.")
        return draft, words

    def _overrides_discover(self, words: list) -> str | None:
        if not words and self._draft() is None:
            return _message("override_categories")
        categories = {c["category_id"] for c in self.categories}
        if len(words) == 1 and words[0] in categories and self._draft() is not None:
            return self._read("override_discover", ("show",), character_id=self._draft(),
                              query={"category_id": words[0]})  # fmt: skip
        character, words = self._character_or_draft(words)
        if character is None:
            return None
        query = {}
        if len(words) == 1:
            query["category_id"] = words[0]
        elif len(words) >= 2:
            query.update(quality_id=words[0], parameter=words[1])
            if len(words) > 2:
                query["match"] = " ".join(words[2:])
        return self._read("override_discover", ("show",), character_id=character,
                          query=query)  # fmt: skip

    def _override_action(self, action: str, rest: str) -> str | None:
        if action == "record":
            if not rest:
                return self._note("[override] Type /override record and its ID.")
            return self._read("override_record", ("show",), record_id=rest)
        if action == "history":
            words = rest.split()
            more = words[-1:] == ["more"]
            words = words[:-1] if more else words
            character, _ = self._character_or_draft(words)
            if character is None:
                return None
            key = ("history", character)
            fields = {"character_id": character}
            if more:
                if self.cursors.get(key) is None:
                    return self._note("[override] There is no more history to show.")
                fields["after"] = self.cursors[key]
            return self._read("override_history", ("page", key), **fields)
        if action == "apply" and rest == "confirm":
            if self.pending is None or self.pending[0] != "apply":
                return self._note("[override] There is nothing to confirm; type /override "
                                  "apply CHARACTER INTENT-JSON REASON first.")  # fmt: skip
            _, character, intent, digest, reason = self.pending
            self.pending = None
            return self._mutation("override_apply", character_id=character,
                                  intent=intent, preview_digest=digest, reason=reason)  # fmt: skip
        if action not in ("preview", "request", "apply"):
            return self._note(
                "[override] /override preview|request|apply|history|record; /help "
                "lists them."
            )
        parsed = _with_intent(rest)
        if parsed is None:
            return self._note("[override] The intent must be one JSON object: "
                              '{"changes": [...]}.')  # fmt: skip
        before, intent, after = parsed
        if action == "apply" and not before:
            return self._note("[override] Name the character to change.")
        character, _ = self._character_or_draft(before.split())
        if character is None:
            return None
        if action == "preview":
            return self._read("override_preview", ("show",), character_id=character,
                              intent=intent)  # fmt: skip
        if action == "apply":
            if not after:
                return self._note("[override] Give a reason after the intent.")
            return self._read("override_preview", ("apply", character, intent, after),
                              character_id=character, intent=intent)  # fmt: skip
        revises = None
        if " --revises " in f" {after} ":
            after, _, revises = f" {after} ".partition(" --revises ")
            after, revises = after.strip(), revises.strip() or None
        if not after:
            return self._note("[override] Say why after the intent.")
        return self._mutation("override_request", character_id=character, intent=intent,
                              explanation=after,
                              **({"revises": revises} if revises else {}))  # fmt: skip

    def _override_requests(self, words: list) -> str | None:
        more = words[-1:] == ["more"]
        words = words[:-1] if more else words
        states = "all"
        if words and words[-1] in ("open", "terminal"):
            states = words.pop()
        character = None
        if words:
            character = self._character_id(words[0])
            if character is None:
                return None
        key = ("requests", character, states)
        fields = {"states": states}
        if character is not None:
            fields["character_id"] = character
        if more:
            if self.cursors.get(key) is None:
                return self._note("[override] There are no more requests to show.")
            fields["after"] = self.cursors[key]
        return self._read("override_requests", ("page", key), **fields)

    def _override_decide(self, proposal_id: str, decision: str, rest: str):
        if not proposal_id:
            return self._note("[request] Type /decide ID offer|counter|deny.")
        if decision == "confirm":
            if self.pending is None or self.pending[:2] != ("decide", proposal_id):
                return self._note(
                    f"[request] Review it first: /decide {proposal_id} offer or counter."
                )
            _, _, kind, digest, intent, note = self.pending
            self.pending = None
            fields = {"proposal_id": proposal_id, "decision": kind,
                      "preview_digest": digest, "note": note}  # fmt: skip
            if intent is not None:
                fields["intent"] = intent
            return self._mutation("override_decide", **fields)
        if decision == "approve":
            return self._note(
                f"[request] {proposal_id} is negotiated: /decide {proposal_id} offer "
                "offers it as asked; nothing applies until the player accepts."
            )
        if decision == "deny":
            if not rest:
                return self._note("[request] A denial says why: /decide ID deny NOTE.")
            return self._mutation("override_decide", proposal_id=proposal_id,
                                  decision="deny", note=rest)  # fmt: skip
        if decision == "offer":
            return self._read("override_detail", ("offer", proposal_id, rest or None),
                              proposal_id=proposal_id)  # fmt: skip
        if decision == "counter":
            parsed = _with_intent(rest)
            if parsed is None or parsed[0] or not parsed[2]:
                return self._note("[request] Type /decide ID counter INTENT-JSON NOTE.")
            _, intent, note = parsed
            return self._read("override_detail", ("counter", proposal_id, intent, note),
                              proposal_id=proposal_id)  # fmt: skip
        return self._note("[request] Type /decide ID offer|counter|deny.")

    def _override_reply(self, message: dict, sent: str, sent_type: str):
        """An override reply: remember what it says, then show it."""
        kind = message.get("type")
        waiting = self.contexts.get(sent)
        context = waiting.popleft() if waiting else ("show",)
        if waiting is not None and not waiting:
            del self.contexts[sent]
        if kind == "override_requests":
            self.override_ids.update(r["proposal_id"] for r in message["items"])
        if kind == "override_detail":
            self.override_ids.add(message["request"]["proposal_id"])
        if kind == "proposal" and sent_type in _OVERRIDE_MUTATIONS:
            self.override_ids.add(message["proposal_id"])
        if context[0] == "page" and kind == message.get("type") and kind != "error":
            self.cursors[context[1]] = message["next"]
        if self.json_output:
            _print(message)
        else:
            if kind == "error":
                for line in _readable(message) + override_lines.error_lines(message):
                    print(line, flush=True)
                hint = _OVERRIDE_HINTS.get(message["code"])
                if hint is not None:
                    self._note(hint)
            else:
                for line in override_lines.lines(message, self.role):
                    print(line, flush=True)
                if kind == "proposal":
                    for line in _readable(message):
                        print(line, flush=True)
            if context[0] == "page" and message.get("next"):
                self._note(_MORE[context[1][0]])
        if kind == "error":
            return
        self._confirmation(context, message)

    def _confirmation(self, context: tuple, message: dict):
        """After a review read: what confirming will send, exactly."""
        step = context[0]
        if step == "accept":
            offer = message["offer"]
            if offer["state"] != "open" or not offer.get("current"):
                return self._note(
                    f"[offer] {offer['offer_id']} cannot be accepted now."
                )
            self.pending = ("accept", offer["offer_id"], offer["preview_digest"])
            return self._note(
                f"[offer] Accept offer {offer['offer_revision']} exactly as shown? It "
                f"applies at once. Type /accept {offer['offer_id']} confirm."
            )
        if step == "detail" and message["offer"] is not None:
            self.queued.append(self._read("override_offer", ("show",),
                                          offer_id=message["offer"]["offer_id"]))  # fmt: skip
        if step in ("offer", "counter"):
            request = message["request"]
            if request["state"] not in ("pending_host", "awaiting_player"):
                return self._note(f"[request] {context[1]} is {request['state']}.")
            if step == "counter":
                self.queued.append(self._read(
                    "override_preview", ("counter-preview", *context[1:]),
                    character_id=request["target"]["id"], intent=context[2],
                ))  # fmt: skip
                return None
            current = request["current"]
            if current["status"] not in ("same", "changed"):
                return self._note(
                    f"[request] {context[1]} cannot be offered as asked now; deny it, "
                    "or counter with another intent."
                )
            if current["status"] == "same":
                print(*override_lines.preview_lines(current["preview"],
                                                    current["preview_digest"], "[now]"),
                      sep="\n", flush=True)  # fmt: skip
            self.pending = ("decide", context[1], "offer", current["preview_digest"],
                            None, context[2])  # fmt: skip
            return self._note(
                f"[request] Offer {context[1]} as it would apply now (shown)? Nothing "
                f"applies until the player accepts. Type /decide {context[1]} confirm."
            )
        if step == "counter-preview":
            _, proposal_id, intent, note = context
            self.pending = ("decide", proposal_id, "counteroffer",
                            message["preview_digest"], intent, note)  # fmt: skip
            return self._note(
                f"[request] Counteroffer this for {proposal_id}? Nothing applies until "
                f"the player accepts. Type /decide {proposal_id} confirm."
            )
        if step == "apply":
            _, character, intent, reason = context
            self.pending = ("apply", character, intent, message["preview_digest"],
                            reason)  # fmt: skip
            return self._note(
                f"[override] Apply this to {character} now, as host, with no request? "
                "Type /override apply confirm."
            )
        return None

    def _rules_line(self, rest: str) -> str | None:
        action, _, argument = rest.partition(" ")
        argument = argument.strip()
        if not action:
            character = None if self.actor is None else self.actor["character"]
            fields = {} if character is None else {
                "character_id": character["character_id"]}  # fmt: skip
            return _message("rules_view", **fields)
        if action == "list":
            return _message("rules_list")
        if action == "show":
            if argument and not argument.isdigit():
                return self._note("[rules] Type /rules show or /rules show N.")
            return _message(
                "rules_export", **({"revision": int(argument)} if argument else {})
            )
        if action == "import":
            if not argument:
                return self._note("[rules] Type /rules import and a JSON file.")
            try:
                with open(Path(argument).expanduser(), encoding="utf-8") as stream:
                    content = json.load(stream)
            except (OSError, ValueError) as error:
                return self._note(f"[rules] Cannot read {argument}: {_reason(error)}")
            if not isinstance(content, dict):
                return self._note(f"[rules] {argument} is not a JSON object.")
            return self._mutation("rules_import", content=content)
        if action in ("activate", "reject"):
            number, _, confirm = argument.partition(" ")
            if not number.isdigit():
                return self._note(
                    f"[rules] Type /rules {action} and a revision number."
                )
            if action == "reject":
                return self._mutation("rules_reject", revision=int(number))
            seen = self.policy_revision
            if seen is None:
                return self._note(
                    "[rules] Look at the rules first (/rules list); activation names "
                    "the settings revision you saw."
                )
            if confirm != "confirm" or self.pending != ("activate", int(number), seen):
                self.pending = ("activate", int(number), seen)
                return self._note(
                    f"[rules] Make revision {number} the campaign's settings, replacing "
                    f"revision {seen}? Drafts it changes must be approved again. Type "
                    f"/rules activate {number} confirm to do it."
                )
            self.pending = None
            return self._mutation("rules_activate", revision=int(number),
                                  expected_policy_revision=seen)  # fmt: skip
        return self._note(
            "[rules] /rules, /rules list, /rules show [N], /rules import FILE, "
            "/rules activate N, /rules reject N."
        )

    def _admin_line(self, rest: str) -> str | None:
        """/admin lists what the host may run; /admin COMMAND ARGS-JSON asks
        to confirm running it at the game revision seen now; /admin confirm
        runs exactly that."""
        if not rest:
            return _message("host_operations")
        if rest == "confirm":
            if self.pending is None or self.pending[0] != "admin":
                return self._note("[admin] There is nothing to confirm; type /admin "
                                  "COMMAND ARGS-JSON first.")  # fmt: skip
            _, command, args, revision = self.pending
            self.pending = None
            return self._mutation("host_command", expected_revision=revision,
                                  command=command, args=args)  # fmt: skip
        command, _, raw = rest.partition(" ")
        try:
            args = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return self._note("[admin] The arguments must be one JSON object.")
        if not isinstance(args, dict):
            return self._note("[admin] The arguments must be one JSON object.")
        if self.revision is None:
            return self._note("[admin] Look at the game first (/sync or /inspect).")
        self.pending = ("admin", command, args, self.revision)
        shown = json.dumps(args, separators=(", ", ": "))
        return self._note(
            f"[admin] Run {command} {shown} at revision {self.revision}? If the game "
            "moves on first, it is refused. Type /admin confirm to run it."
        )

    def _mutation(self, kind: str, **fields) -> str:
        """A host change or a request change, with its own request ID: once
        per attempt, reused only by /retry after a busy or failed host."""
        return json.dumps({"version": 1, "type": kind, "request_id": _new_id("h"),
                           **fields})  # fmt: skip

    def _retry_mutation(self) -> str | None:
        line, authority = self.mutation_retry
        if authority != self._authority():
            self.mutation_retry = None
            return self._note(
                "[retry] Not resent: your campaign or role has changed since. Look "
                "again and make the change anew if it is still yours to make."
            )
        return line

    def _authority(self) -> tuple:
        """Where a host change was made, and as whom: it is never resent in
        another campaign, session or role."""
        if self.selection is None:
            return (None,)
        return (self.selection["campaign_id"], self.session[1], self.role)

    def _governance_reply(self, message: dict, sent: str, sent_type):
        """Keep the governance state the reply tells of, and show it."""
        kind = message.get("type")
        if kind == "snapshot":
            self._saw_revision(message["revision"])
            if _is_revision(message.get("policy_revision")):
                self.policy_revision = message["policy_revision"]
        if kind in ("rules_view", "rules_list"):
            self.policy_revision = message["policy_revision"]
        if kind in ("host_view", "host_operations", "result") and _is_revision(
            message.get("revision")
        ):
            self._saw_revision(message["revision"])
        if kind == "role" and "request_id" in message:
            self._set_role(message["role"])
        if sent_type in _HOST_MUTATIONS + _OVERRIDE_MUTATIONS:
            retryable = kind == "error" and message["code"] in _RETRYABLE
            self.mutation_retry = (sent, self._authority()) if retryable else None
        if sent_type in _OVERRIDE_TYPES:
            return self._override_reply(message, sent, sent_type)
        if sent_type == "host_view":
            wanted = self.inspecting.popleft() if self.inspecting else None
            if kind == "host_view" and not self.json_output:
                for line in _inspect_lines(message, wanted):
                    print(line, flush=True)
                return
        if sent_type not in _GOVERNANCE_READS + _HOST_MUTATIONS:
            return
        if self.json_output:
            _print(message)
            return
        for line in _readable(message):  # readable here, whatever the campaign
            print(line, flush=True)
        if kind == "error":
            hint = _HINTS.get(message["code"])
            if hint is not None:
                self._note(hint)

    def _set_role(self, role):
        if role != self.role:
            # A confirmation or a retry was made under the old role, and is
            # never sent under any later one, even if it is the same again.
            self.pending, self.mutation_retry = None, None
        self.role = role

    def _saw_revision(self, revision: int):
        if self.revision is None or revision > self.revision:
            self.revision = revision

    def _character_id(self, wanted: str) -> str | None:
        """The character the player meant: a number from /characters, a name
        (if only one has it) or an ID; None after saying why not."""
        if wanted.isdigit():
            if 1 <= int(wanted) <= len(self.roster):
                return self.roster[int(wanted) - 1]["character_id"]
            return self._note(
                f"[character] There is no character {wanted}; see /characters."
            )
        named = [c for c in self.roster
                 if (c["name"] or "").casefold() == wanted.casefold()]  # fmt: skip
        if len(named) == 1:
            return named[0]["character_id"]
        if " " in wanted or len(named) > 1:
            return self._note(
                f"[character] Which {wanted}? Use its number from /characters."
            )
        return wanted  # an ID: the host decides

    def _known(self, character_id: str) -> dict | None:
        return next((c for c in self.roster if c["character_id"] == character_id), None)

    def _join_line(self, wanted: str) -> str | None:
        if self.selection is not None:
            return self._note(
                f"[lobby] You are in {self.selection['name']}; /leave it first."
            )
        if not wanted:
            return self._note("[lobby] Type /join and a number from /campaigns.")
        choice = _choose(self.catalog, wanted)
        campaign_id = wanted if choice is None else choice["campaign_id"]
        if choice is None and (wanted.isdigit() or " " in wanted):
            return self._note(f"[lobby] There is no campaign {wanted}; see /campaigns.")
        # Every select says the client handles characters: a character
        # campaign refuses one that does not (character contract section 9).
        return json.dumps({"version": 1, "type": "select", "campaign_id": campaign_id,
                           "characters": 1})  # fmt: skip

    def _after_reply(self, message: dict, sent: str):
        """Show a reply in lobby mode and follow any change of selection."""
        kind, sent_type = message.get("type"), _sent_type(sent)
        self._governance_reply(message, sent, sent_type)
        if sent_type in _GOVERNANCE_READS + _HOST_MUTATIONS + _OVERRIDE_TYPES:
            if (
                kind == "error"
                and message["code"] == "campaign_unavailable"
                and self.selection is not None
            ):
                self._back_in_lobby()  # its access is gone: nothing of it stays
            return
        if kind == "lobby":
            if sent_type == "leave":
                self._back_in_lobby(quiet=True)
            self._lobby_reply(message)
        elif sent_type == "select" and kind == "snapshot":
            self._enter(message)
        elif kind == "snapshot" and self.actor is not None:
            self._set_role(message.get("role"))
            self._show(message)
            self._set_actor(message["actor"])
            if message["actor"]["mode"] == "creation":
                # What /approve completes: the revision this sheet showed.
                self.shown_revision = message["actor"]["character"]["draft_revision"]
        elif kind == "characters":
            self.roster = message["characters"]
            self._show(message)
            self._set_actor(message["actor"])
        elif kind == "character":
            self._show(message)
            if (
                sent_type == "character_create"
                and message["character"]["phase"] == "draft"
            ):
                self.created = message["character"]["character_id"]
            self._set_actor(message["actor"])
        elif kind == "error" and message["code"] == "campaign_unavailable":
            self._show(message)
            if self.selection is not None and sent_type != "select":
                self._back_in_lobby()
        else:
            self._show(message)

    def _follow_up(self, connection, message: dict):
        """After a reply that shows the client's idea of its character was
        out of date, ask the host where it is now; and send the read a
        review step asked for next."""
        while self.queued:
            self._refresh(connection, self.queued.pop(0))
        if message.get("type") != "error":
            return
        if message["code"] in ("character_changed", "character_unavailable"):
            self._refresh(connection, _CHARACTERS)
        elif message["code"] == "stale_draft":
            self._note(
                "[character] The draft changed since you saw it; here it is now."
            )
            self._refresh(connection, json.dumps({"version": 1, "type": "sync"}))

    def _set_actor(self, actor: dict):
        """Follow the host's word on this connection's actor. Retry state
        belongs to one actor: the old one's is kept with it."""
        if actor == self.actor:
            return
        old = self.actor
        if old is not None:
            self.saved[self._retry_key()] = (
                self.sending,
                self.last_say,
                self.reported_input,
            )
        self.actor = actor
        self.sending, self.last_say, self.reported_input = self.saved.pop(
            self._retry_key(), ({}, None, None)
        )
        if old is None or _actor_key(old) != _actor_key(actor):
            self.shown_revision = None
        elif actor["mode"] == "creation":
            return  # the same draft, renamed or revised: no news
        self._note(_actor_line(actor, self.role))

    def _lobby_reply(self, message: dict):
        self.catalog = message["campaigns"]
        self._show(message)

    def _enter(self, snapshot: dict):
        campaign = snapshot["campaign"]
        self.selection = campaign
        self.session = _session_of(snapshot)
        self.referee = isinstance(snapshot.get("referee"), dict)
        self.actor = snapshot.get("actor")  # None: a campaign without characters
        self.role = snapshot.get("role")
        self.revision = snapshot["revision"]
        self.policy_revision = snapshot.get("policy_revision")
        self.pending, self.mutation_retry = None, None
        self.roster, self.shown_revision, self.confirming = [], None, None
        self.overrides_on = isinstance(snapshot.get("override_categories"), list)
        self.categories = snapshot.get("override_categories") or []
        self.cursors, self.override_ids = {}, set()
        self.sending, self.last_say, self.reported_input = self.saved.pop(
            self._retry_key(), ({}, None, None)
        )
        self.entered = True
        if self.json_output:
            _print(snapshot)
            return
        how = (
            "type to talk to the referee"
            if self.referee
            else "send JSON commands, one per line"
        )
        self._note(
            f"[joined] {campaign['name']}: {how}; /leave returns to the lobby; "
            "/help lists commands."
        )
        if not self.referee:
            _print(snapshot)

    def _retry_key(self) -> tuple:
        """Retry state belongs to one campaign, one participant in it, and in
        a character campaign one character and mode: an input is never
        resent as someone else, even after a relink, or as another actor."""
        return (self.selection["campaign_id"], self.session[1],
                *_actor_key(self.actor))  # fmt: skip

    def _back_in_lobby(self, quiet: bool = False):
        """Keep the campaign's retry state with it, and stop using it."""
        if self.selection is None:
            return
        campaign = self.selection
        self.saved[self._retry_key()] = (
            self.sending,
            self.last_say,
            self.reported_input,
        )
        self.sending, self.last_say, self.reported_input = {}, None, None
        self.selection, self.referee, self.actor = None, False, None
        self.role, self.revision, self.policy_revision = None, None, None
        self.pending, self.mutation_retry = None, None
        self.roster, self.shown_revision, self.confirming = [], None, None
        self.overrides_on, self.categories = False, []
        self.cursors, self.override_ids = {}, set()
        if not quiet:
            self._note(
                f"[lobby] You are back in the lobby, out of {campaign['name']}; "
                "/campaigns lists what is open to you."
            )

    # --- referee mode -------------------------------------------------------------

    def _translate(self, text: str) -> str | None:
        """One typed line as the message to send, or None if it was handled
        here. Anything that is not plumbing is the player's words, even if it
        looks like JSON or code."""
        if text.startswith("//"):
            return self._say(text[1:], private=False)
        if not text.startswith("/"):
            return self._say(text, private=False)
        command, _, rest = text[1:].partition(" ")
        if command == "private":
            if not rest.strip():
                return self._note("[error] Type /private and then what to say.")
            return self._say(rest, private=True)
        if command == "status":
            return json.dumps({"version": 1, "type": "referee_status"})
        if command == "sync":
            return json.dumps({"version": 1, "type": "sync"})
        if command == "history":
            return json.dumps({"version": 1, "type": "messages", "after_seq": 0})
        if command == "retry" and self.mutation_retry is not None:
            return self._retry_mutation()
        if command == "retry":
            target = self.last_say or self.reported_input
            if self.sending:  # one still unanswered is the one to retry
                target = list(self.sending.values())[-1]
            if target is None:
                return self._note("[retry] There is no input to retry.")
            return self._say(target["text"], target["private"], target["request_id"])
        if command == "help":
            return self._note(_HELP)
        return self._note(f"[error] There is no /{command}; /help lists commands.")

    def _say(self, text: str, private: bool, request_id: str | None = None) -> str:
        said = {"request_id": request_id or _new_id("c"), "text": text,
                "private": private}  # fmt: skip
        if self.actor is not None:  # the actor this client believes it has
            character = self.actor["character"]
            said["actor"] = {
                "character_id": None
                if character is None
                else character["character_id"],
                "mode": self.actor["mode"],
            }
        self.sending[said["request_id"]] = said
        self.mutation_retry = None  # /retry now means this input
        return json.dumps({"version": 1, "type": "say", **said})

    def _remember(self, message: dict, sent: str):
        """Track what /retry should resend."""
        line = json.loads(sent)
        if message.get("type") == "referee_status":
            self.reported_input = message.get("your_last_input")
        elif line.get("type") == "say":
            said = self.sending.pop(line["request_id"], None)
            if said is not None and message.get("type") == "accepted":
                self.last_say = said
            # A refused input (busy or error) is typed again, never retried,
            # and the last accepted input stays the one /retry resends.

    def _note(self, text: str) -> None:
        """A line from the client itself; stderr when stdout is JSON."""
        print(text, file=sys.stderr if self.json_output else sys.stdout, flush=True)

    def _show(self, message: dict):
        readable = self.referee or (
            self.lobby
            and (self.selection is None or message.get("type") in _LOBBY_READABLE)
        )
        if not readable or self.json_output:
            _print(message)
            return
        for line in _readable(message):
            print(line, flush=True)

    async def _all_answered(self):
        """Wait until every line sent is answered. Once stdin has ended, the
        host has the drain timeout from then to answer, whatever the client
        is waiting for (a join, a leave, its first status, or the last reply)."""
        while self.unanswered:
            self.replied.clear()
            if self.deadline is None:
                await self.replied.wait()
            else:
                left = self.deadline - asyncio.get_running_loop().time()
                await asyncio.wait_for(self.replied.wait(), max(left, 0))

    def _input_ended(self):
        self.deadline = asyncio.get_running_loop().time() + self.drain_timeout
        self.replied.set()  # a wait already in progress takes the deadline now

    async def _until_closed(self, awaitable, receiver):
        """Await something unless the host closes or Ctrl-C comes first."""
        task = asyncio.ensure_future(awaitable)
        done, _ = await asyncio.wait(
            {task, receiver, self.interrupted}, return_when=asyncio.FIRST_COMPLETED
        )
        if task in done:
            return task.result()
        task.cancel()
        if self.interrupted.done():
            raise _Interrupted
        if receiver.exception() is not None:
            raise receiver.exception()
        raise _HostClosed

    async def _first(self, awaitable):
        """Await something unless Ctrl-C comes first."""
        task = asyncio.ensure_future(awaitable)
        await asyncio.wait(
            {task, self.interrupted}, return_when=asyncio.FIRST_COMPLETED
        )
        if not task.done():
            task.cancel()
            raise _Interrupted
        return task.result()

    def _interrupt(self):
        if not self.interrupted.done():
            self.interrupted.set_result(None)

    def _host_closed(self, connection) -> int:
        reason = f"the host closed the connection {_closed(connection)}"
        if self.unanswered:
            return self._stop(reason, EXIT_UNCERTAIN)
        return _diagnose(reason, EXIT_FAILED)

    def _stop(self, reason: str, code: int) -> int:
        if not self.unanswered:
            return _diagnose(reason, code)
        ids = ", ".join(_request_id(line) for line in self.unanswered)
        return _diagnose(
            f"{reason}. {len(self.unanswered)} request(s) got no reply, so their "
            f"outcome is uncertain: {ids}. Run tesserae connect again and resend "
            "those lines unchanged; a request that committed returns its original "
            "result",
            code,
        )


class _Interrupted(Exception):
    pass


class _Redirected(Exception):
    """The host answered the handshake with an HTTP redirect."""


class _connect_without_redirects(connect):
    """websockets' connect, except that handshake redirects are never followed.

    check_client vetted the URI the user gave. A redirect could send the join
    token somewhere it never checked, such as plaintext to another computer.
    """

    def process_redirect(self, exc: Exception) -> Exception:
        if isinstance(exc, InvalidStatus) and 300 <= exc.response.status_code < 400:
            return _Redirected()
        return exc


class _HostClosed(Exception):
    pass


def _decode(raw) -> dict:
    if isinstance(raw, bytes):
        raise _Failure("the host sent a binary frame")
    try:
        message = json.loads(raw)
    except ValueError:
        raise _Failure("the host sent something that is not JSON") from None
    if not isinstance(message, dict):
        raise _Failure("the host sent a JSON value that is not an object")
    return message


def _print(message: dict):
    """Print one host message as one JSON line."""
    print(json.dumps(message), flush=True)


_LOBBY_HELP = """\
[help] /campaigns     the campaigns open to you, and which one you are in
[help] /join N        enter campaign N from /campaigns (or its ID or name)
[help] /leave         return to the lobby; anything you sent stays in that campaign"""

_CHARACTER_HELP = """\
[help] /characters    your characters here: number, name, ready or draft
[help] /character N   play character N, or resume creating draft N (number, name or ID)
[help] /character none  no character: ask setup and rules questions only
[help] /create [NAME] start a new character with the referee (privately)
[help] /sheet         the selected character's sheet
[help] /approve       complete the draft at the revision /sheet last showed
[help] /archive N     archive character N, after you confirm"""

_GOVERNANCE_HELP = """\
[help] /role          your role here: campaign host or guest
[help] /rules         the campaign's settings, and your character's requirements
[help] /requests      your requests for the host's decision (the host sees all)
[help] /cancel ID     withdraw your own pending request"""

_OVERRIDE_HELP = """\
[help] Creation overrides (a change to a draft is asked, offered, then accepted):
[help] /overrides [CHARACTER] [CATEGORY]   what can be changed on a draft
[help] /overrides CHARACTER QUALITY PARAM [MATCH]   a parameter's options
[help] /override preview [CHARACTER] INTENT-JSON   what it would do, exactly
[help] /override request [CHARACTER] INTENT-JSON WHY [--revises ID]   ask the host
[help] /requests overrides [CHARACTER] [open|terminal] [more]   your requests (host: all)
[help] /offer ID      a request (hp-...) and its offer, or one offer (ho-...)
[help] /offers ID [more]   every offer revision of a request
[help] /accept OFFER  shows the offer; /accept OFFER confirm applies it
[help] /decline OFFER [NOTE]
[help] /override history [CHARACTER] [more] | /override record ID   what was applied"""

_OVERRIDE_HOST_HELP = """\
[help] Campaign host, creation overrides (nothing applies until the player accepts):
[help] /decide ID offer [NOTE]   review it, then /decide ID confirm
[help] /decide ID counter INTENT-JSON NOTE   review it, then /decide ID confirm
[help] /decide ID deny NOTE
[help] /withdraw OFFER NOTE      withdraw an offer not yet accepted
[help] /override apply CHARACTER INTENT-JSON REASON   then /override apply confirm"""

_HOST_HELP = """\
[help] Campaign host:
[help] /inspect [CHARACTER]  every sheet, the module's view and preparation
[help] /history-all [N]      every message, private ones included, after N
[help] /decide ID approve|deny [NOTE]  decide a request exactly as made
[help] /rules list | show [N] | import FILE | activate N | reject N
[help] /admin                the operations you can run now
[help] /admin COMMAND ARGS   run one, after you confirm (/admin confirm)
[help] /audit [N]            what hosts have changed
[help] /transfer PLAYER      hand the campaign over, after you confirm"""

_HELP = """\
[help] Type to talk to the referee; everyone at the table sees it.
[help] /private TEXT  say something to the referee only
[help] /status        the referee's state, your pending choice, your last input
[help] /sync          your current view of the game
[help] /history       the conversation you can see
[help] /retry         resend your last input exactly (never a second job)
[help] //TEXT         say text that starts with /"""


def _readable(message: dict) -> list[str]:
    """A host message as lines for a person; the client adds no game rules."""
    kind = message.get("type")
    if kind == "lobby":
        return _lobby_lines(message)
    if kind == "characters":
        return _character_lines(message)
    if kind == "character":
        character = message["character"]
        done = {"draft": "started", "ready": "completed", "archived": "archived"}
        return [f"[character] {done[character['phase']]} {_label(character)}"]
    if kind == "actor":
        why = "completed" if message["reason"] == "completed" else "archived"
        return [f"[character] Your character was {why} on another connection."]
    if kind == "left":
        return ["[left] That campaign is no longer available to you."]
    if kind == "proposal":
        line = f"[request] {message['proposal_id']} is now {message['state']}."
        if message.get("offer_id") and message["state"] == "awaiting_player":
            line += (f" Offer {message['offer_id']} (revision "
                     f"{message.get('offer_revision')}) waits for the player; nothing "
                     "is applied yet.")  # fmt: skip
        if message.get("record_id"):
            line += (
                f" Applied at revision {message['revision']}: {message['record_id']}."
            )
        effect = message.get("effect")
        if effect:
            line += " Effect: " + json.dumps(effect, separators=(", ", ": "))
        return [line]
    if kind == "requests":
        return _request_lines(message["requests"])
    if kind == "rules_view":
        return _rules_view_lines(message)
    if kind == "rules_list":
        return _rules_list_lines(message)
    if kind == "rules":
        return _rules_lines(message)
    if kind == "host_messages":
        return _host_message_lines(message)
    if kind == "host_operations":
        return _operation_lines(message)
    if kind == "host_audit":
        return _audit_lines(message)
    if kind == "result":  # a host operation's
        return [f"[admin] Done at revision {message['revision']}: "
                + json.dumps(message["result"], separators=(", ", ": "))]  # fmt: skip
    if kind == "role" and "request_id" in message:
        return ["[role] You handed the campaign over; you are a guest here now."]
    if kind == "role":
        if message["role"] == "host":
            return ["[role] You are now this campaign's host."]
        return ["[role] You are now a guest in this campaign."]
    if kind == "message":
        return [_line(message)]
    if kind == "messages":
        return [_line(m) for m in message["messages"]] or ["[history] Nothing yet."]
    if kind == "referee":
        if message["state"] == "busy":
            return ["[busy] The referee is answering."]
        return ["[idle] The referee is ready."]
    if kind == "referee_status":
        choices = message["choices"]
        waiting = (
            f'waiting for you: "{choices[0]["question"]}"'
            if choices
            else "nothing waiting for you"
        )
        lines = [
            f"[status] {message['state']}; {waiting}; last message {message['last_seq']}"
        ]
        last = message["your_last_input"]
        if last is not None:
            privacy = " (private)" if last["private"] else ""
            lines.append(
                f'[your last input] "{last["text"]}"{privacy} {last["job_state"]}'
            )
        return lines
    if kind == "error":
        if message["code"] == "referee_busy":
            return [f"[busy] {message['message']}"]
        if message["code"] == "character_incomplete":
            problems = message.get("problems")
            if (
                isinstance(problems, list)
                and problems
                and all(isinstance(problem, str) for problem in problems)
            ):
                return ["[character] Not finished yet: " + " ".join(problems)]
        return [f"[error] {message['message']}"]
    if kind == "snapshot":
        view = message.get("view")
        detail = "" if view is None else " " + json.dumps(view, separators=(", ", ": "))
        lines = [f"[view] revision {message['revision']}{detail}"]
        sheet, actor = message.get("sheet"), message.get("actor")
        if sheet is not None and actor is not None:
            character = actor["character"]
            revision = (
                f", revision {character['draft_revision']}"
                if actor["mode"] == "creation"
                else ""
            )
            lines.append(
                f"[sheet] {_label(character)}{revision}: "
                + json.dumps(sheet, separators=(", ", ": "))
            )
        return lines
    return []  # accepted (its words arrive as a message) and revisions


def _role_line(role, policy_revision) -> str:
    if role == "host":
        who = "You are this campaign's host."
    elif role == "guest":
        who = "You are a guest in this campaign."
    else:
        who = "This campaign has no campaign host."
    if policy_revision is not None:
        who += f" Its settings are at revision {policy_revision}."
    return f"[role] {who}"


def _compact(value) -> str:
    return json.dumps(value, separators=(", ", ": "), ensure_ascii=False)


def _request_lines(requests: list) -> list[str]:
    if not requests:
        return ["[request] No requests."]
    lines = []
    for request in requests:
        target = request["target"]
        on = "the campaign" if target is None else f"{target['kind']} {target['id']}"
        who = request["requester"]["name"] or request["requester"]["participant_id"]
        lines.append(
            f"[request] {request['proposal_id']} {request['state']}: {who} asks for "
            f"{request['kind']} {_compact(request['payload'])} on {on}: "
            f"{_compact(request['explanation'])}"
        )
        if request.get("decision_note"):
            lines.append(f"[request]   note: {_compact(request['decision_note'])}")
    return lines


def _rules_view_lines(message: dict) -> list[str]:
    lines = [f"[rules] Settings revision {message['policy_revision']}: "
             + _compact(message["settings"])]  # fmt: skip
    if message.get("exception"):
        lines.append(f"[rules] Your exception: {_compact(message['exception'])}")
    if message.get("guidance"):
        lines.append(f"[rules] Guidance: {message['guidance']}")
    for category in message.get("override_categories") or []:
        lines.append(f"[rules] Override category {category['category_id']} "
                     f"({category['label']}): {category['policy']}")  # fmt: skip
    for entry in message.get("requirements") or []:
        state = ""
        if entry.get("met") is True:
            state = " (done)"
        elif entry.get("met") is False:
            state = " (to do)"
        lines.append(
            f"[requirement] {entry['id']}, {entry['kind']}{state}: {entry['description']}"
        )
    return lines


def _rules_list_lines(message: dict) -> list[str]:
    lines = [f"[rules] Active settings revision: {message['policy_revision']}"]
    for revision in message["revisions"]:
        lines.append(
            f"[rules] {revision['revision']} {revision['state']} ({revision['origin']}) "
            f"{revision['sha256'][:12]}"
        )
    return lines


def _rules_lines(message: dict) -> list[str]:
    if "content" in message:
        return [f"[rules] Revision {message['revision']} ({message['state']}): "
                + _compact(message["content"])]  # fmt: skip
    return [f"[rules] Revision {message['revision']} is {message['state']}."]


def _inspect_lines(message: dict, wanted: str | None) -> list[str]:
    """The host view as lines, or one character's (by ID or unique name)."""
    names = {p["participant_id"]: p["name"] for p in message["participants"]}
    characters = message["characters"]
    if wanted is not None:
        characters = [c for c in characters if c["character_id"] == wanted
                      or (c["name"] or "").casefold() == wanted.casefold()]  # fmt: skip
        if len(characters) != 1:
            return [f"[inspect] No single character {wanted}; /inspect lists them all."]
    lines = []
    if wanted is None:
        policy = message.get("policy")
        settings = "no settings" if policy is None else (
            f"settings revision {policy['revision']}")  # fmt: skip
        lines.append(
            f"[inspect] Revision {message['revision']}; {settings}; "
            f"{len(message['participants'])} participants"
        )
    for character in characters:
        owner = names.get(character["participant_id"], character["participant_id"])
        lines.append(
            f"[inspect] {_label(character)} ({character['phase']}, {owner}): "
            + _compact(character["sheet"])
        )
    if wanted is None:
        lines.append(f"[inspect] Module: {_compact(message['inspect'])}")
        for choice in message["pending_choices"]:
            lines.append(f"[inspect] Waiting: {_compact(choice)}")
    return lines


def _host_message_lines(message: dict) -> list[str]:
    lines = []
    for item in message["messages"]:
        line = _line(item)
        if item.get("to"):
            line += " (to " + ", ".join(t["name"] or t["participant_id"]
                                        for t in item["to"]) + ")"  # fmt: skip
        lines.append(line)
    if not lines:
        lines.append("[history] Nothing yet.")
    if message["next_after_seq"] is not None:
        lines.append(f"[history] More: /history-all {message['next_after_seq']}")
    return lines


def _operation_lines(message: dict) -> list[str]:
    lines = [f"[admin] At revision {message['revision']} you can run:"]
    for operation in message["operations"]:
        args = ", ".join(a.get("name", "?") for a in operation.get("args", [])
                         if isinstance(a, dict))  # fmt: skip
        lines.append(
            f"[admin] {operation['command']} ({args}): "
            f"{operation.get('description', '')}"
        )
    if len(lines) == 1:
        lines.append("[admin] Nothing in this campaign.")
    return lines


def _audit_lines(message: dict) -> list[str]:
    lines = []
    for record in message["records"]:
        on = "" if record["target"] is None else f" on {record['target']}"
        lines.append(
            f"[audit] {record['id']} {record['kind']} by {record['player_id']} "
            f"({record['via']}){on}: {_compact(record['detail'])}"
        )
    if not lines:
        lines.append("[audit] Nothing yet.")
    if message["next_after"] is not None:
        lines.append(f"[audit] More: /audit {message['next_after']}")
    return lines


def _lobby_lines(message: dict) -> list[str]:
    campaigns, selected = message["campaigns"], message["selected"]
    here = None if selected is None else selected["campaign_id"]
    if campaigns:
        head = (
            f"{len(campaigns)} campaign{'s' if len(campaigns) != 1 else ''} open to you"
        )
    else:
        head = "no campaigns are open to you yet; ask the host"
    lines = [f"[lobby] {message['player_name']}: {head}"]
    for number, campaign in enumerate(campaigns, 1):
        mark = " (you are here)" if campaign["campaign_id"] == here else ""
        lines.append(
            f"[lobby] {number}. {campaign['name']}{mark}  {campaign['campaign_id']}"
        )
    if here is None and campaigns:
        lines.append("[lobby] /join N enters campaign N; /help lists commands.")
    return lines


def _choose(catalog: list, wanted: str) -> dict | None:
    """The catalog entry the player meant: its number, ID, or unique name."""
    if wanted.isdigit() and 1 <= int(wanted) <= len(catalog):
        return catalog[int(wanted) - 1]
    for campaign in catalog:
        if campaign["campaign_id"] == wanted:
            return campaign
    named = [c for c in catalog if c["name"].casefold() == wanted.casefold()]
    return named[0] if len(named) == 1 else None


def _line(message: dict) -> str:
    sender = message["from"]
    who = "Referee" if sender.get("kind") == "referee" else sender.get("name", "?")
    if isinstance(sender.get("character"), str):
        who += f" as {sender['character']}"
    audience = message["audience"]
    creation = message.get("creation")
    if isinstance(creation, dict):
        audience = f"private · creating {creation.get('name') or 'a draft'}"
    return f"[{audience}] {who}: {message['text']}"


def _label(character) -> str:
    if character is None:
        return "that character"
    if character["phase"] == "draft":
        return f"{character['name'] or 'an unnamed draft'} (draft)"
    return character["name"] or "an unnamed character"


def _actor_line(actor: dict, role: str | None = None) -> str:
    """Where the connection now is (character contract section 15). A
    guest's creation is also the campaign host's to read."""
    character = actor["character"]
    if actor["mode"] == "play":
        return f"[character] playing as {character['name']}"
    if actor["mode"] == "creation":
        private = "you and the campaign host" if role == "guest" else "you"
        return (
            f"[character] creating {_label(character)}, revision "
            f"{character['draft_revision']}; private to {private}. /sheet shows "
            "it, /approve completes it."
        )
    return (
        "[character] no character selected; ask setup questions, /character N "
        "to choose one, or /create"
    )


def _character_lines(message: dict) -> list[str]:
    characters = message["characters"]
    if not characters:
        return [
            "[characters] You have no characters here yet: /create [NAME] starts "
            "one with the referee, privately. Until then you can ask setup "
            "questions."
        ]
    here = message["actor"]["character"]
    here = None if here is None else here["character_id"]
    count = f"{len(characters)} character{'s' if len(characters) != 1 else ''}"
    lines = [f"[characters] {count}:"]
    for number, character in enumerate(characters, 1):
        state = (
            "ready"
            if character["phase"] == "ready"
            else f"draft, revision {character['draft_revision']}"
        )
        mark = " (selected)" if character["character_id"] == here else ""
        lines.append(
            f"[characters] {number}. {character['name'] or 'Unnamed'}, {state}{mark}"
            f"  {character['character_id']}"
        )
    lines.append(
        "[characters] /character N plays (or resumes) one; /create starts another."
    )
    return lines


def _is_unsolicited(message: dict) -> bool:
    if message.get("version") != 1:
        return False
    if message["type"] == "referee":
        return message.get("state") in ("busy", "idle")
    return _is_conversation(message)


def _is_conversation(message: dict) -> bool:
    sender = message.get("from")
    return (
        _is_revision(message.get("seq"))
        and isinstance(sender, dict)
        and (sender.get("kind") == "referee" or isinstance(sender.get("name"), str))
        and message.get("audience") in ("table", "private")
        and isinstance(message.get("text"), str)
    )


def _check_reply(message: dict, sent: str, session: tuple):
    """Raise unless `message` is a well-formed reply to the line `sent`.

    A command is answered by its own result or an error, a sync by this
    session's snapshot or an error, and anything else only by an error.
    """
    try:
        line = json.loads(sent)
    except ValueError:
        line = None
    kind = line.get("type") if isinstance(line, dict) else None
    if message.get("version") != 1:
        raise _Failure("the host sent a reply with an unsupported version")
    if message.get("type") == "error":
        if not _is_error(message):
            raise _Failure("the host sent a malformed error")
    elif kind == "command" and message.get("type") == "result":
        if not (
            message.get("request_id") == line.get("request_id")
            and _is_revision(message.get("revision"))
            and isinstance(message.get("result"), dict)
        ):
            raise _Failure("the host sent a result that does not answer the request")
    elif kind == "sync" and message.get("type") == "snapshot":
        if not (_is_snapshot(message) and _session_of(message) == session):
            raise _Failure("the host sent a snapshot that does not match this session")
    elif kind == "select" and message.get("type") == "snapshot":
        campaign = message.get("campaign")
        if not (
            _is_snapshot(message)
            and _is_campaign(campaign)
            and campaign["campaign_id"] == line.get("campaign_id")
            and message["game_id"] == line.get("campaign_id")
        ):
            raise _Failure("the host sent a snapshot that does not answer the select")
    elif kind in ("lobby", "leave") and message.get("type") == "lobby":
        if not _is_lobby(message):
            raise _Failure("the host sent a malformed lobby")
    elif kind == "characters" and message.get("type") == "characters":
        if not (
            _is_actor(message.get("actor"))
            and isinstance(message.get("characters"), list)
            and all(_is_character(c) for c in message["characters"])
        ):
            raise _Failure("the host sent a malformed character list")
    elif kind == "character_select" and message.get("type") == "snapshot":
        actor = message.get("actor")
        wanted = line.get("character_id")
        if not (
            _is_snapshot(message)
            and _session_of(message) == session
            and _is_actor(actor)
            and (actor["character"] or {}).get("character_id") == wanted
        ):
            raise _Failure(
                "the host sent a snapshot that does not answer the selection"
            )
    elif kind in _LIFECYCLE and message.get("type") == "character":
        if not (
            message.get("request_id") == line.get("request_id")
            and _is_revision(message.get("revision"))
            and _is_character(message.get("character"))
            and _is_actor(message.get("actor"))
        ):
            raise _Failure("the host sent a character reply that does not answer it")
    elif kind == "say" and message.get("type") == "accepted":
        if not (
            message.get("request_id") == line.get("request_id")
            and isinstance(message.get("job_id"), str)
            and _is_revision(message.get("seq"))
        ):
            raise _Failure("the host sent an acceptance that does not answer the input")
    elif kind == "referee_status" and message.get("type") == "referee_status":
        last = message.get("your_last_input")
        if not (
            message.get("state") in ("idle", "busy")
            and isinstance(message.get("choices"), list)
            and _is_revision(message.get("last_seq"))
            and (last is None or isinstance(last, dict))
        ):
            raise _Failure("the host sent a malformed referee status")
    elif kind == "messages" and message.get("type") == "messages":
        if not (
            isinstance(message.get("messages"), list)
            and all(_is_conversation(m) for m in message["messages"])
        ):
            raise _Failure("the host sent malformed messages")
    elif (
        kind in _GOVERNANCE_REPLIES and message.get("type") == _GOVERNANCE_REPLIES[kind]
    ):
        if kind in _HOST_MUTATIONS + _OVERRIDE_MUTATIONS and message.get(
            "request_id"
        ) != line.get("request_id"):
            raise _Failure("the host sent a reply that does not answer the request")
        if not _GOVERNANCE_SHAPES[message["type"]](message):
            raise _Failure(f"the host sent a malformed {message['type']} reply")
    else:
        raise _Failure(
            f"the host answered a {kind or 'malformed'} line with a "
            f"{message.get('type')!r} message"
        )


def _is_character(value) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("character_id"), str)
        and (value.get("name") is None or isinstance(value["name"], str))
        and value.get("phase") in ("draft", "ready", "archived")
        and (
            value.get("draft_revision") is None or _is_revision(value["draft_revision"])
        )
    )


def _is_actor(value) -> bool:
    if not isinstance(value, dict) or value.get("mode") not in (
        "setup",
        "creation",
        "play",
    ):
        return False
    character = value.get("character")
    if value["mode"] == "setup":
        return character is None
    return _is_character(character)


def _actor_key(actor) -> tuple:
    """(character ID, mode), or (None, None) without characters."""
    if actor is None:
        return None, None
    character = actor["character"]
    return (None if character is None else character["character_id"]), actor["mode"]


def _select_character(character_id) -> str:
    return json.dumps(
        {"version": 1, "type": "character_select", "character_id": character_id}
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


_CHARACTERS = json.dumps({"version": 1, "type": "characters"})
_LIFECYCLE = ("character_create", "character_complete", "character_archive")
# Lines answered before the next is read: each changes where the client is.
_BOUNDARIES = ("select", "leave", "character_select", *_LIFECYCLE)
_CHARACTER_COMMANDS = (
    "/characters", "/character", "/create", "/sheet", "/approve", "/archive",
)  # fmt: skip
_LOBBY_READABLE = ("lobby", "left", "characters", "character", "actor", "proposal")
# Governance (docs/designs/governance_contract.md section 15).
_GOVERNANCE_COMMANDS = (
    "/role", "/rules", "/requests", "/cancel", "/decide", "/inspect",
    "/history-all", "/audit", "/admin", "/transfer",
)  # fmt: skip
_GOVERNANCE_READS = ("requests", "rules_view", "rules_list", "rules_export",
                     "host_view", "host_messages", "host_operations", "host_audit")  # fmt: skip
_HOST_MUTATIONS = ("rules_import", "rules_activate", "rules_reject", "request_decide",
                   "request_cancel", "host_command", "campaign_host_transfer")  # fmt: skip
# Creation overrides (creation overrides contract section 12).
_OVERRIDE_COMMANDS = ("/overrides", "/override", "/offer", "/offers", "/accept",
                      "/decline", "/withdraw")  # fmt: skip
_OVERRIDE_READS = ("override_categories", "override_discover", "override_preview",
                   "override_requests", "override_detail", "override_offers",
                   "override_offer", "override_history", "override_record")  # fmt: skip
_OVERRIDE_MUTATIONS = ("override_request", "override_decide", "override_withdraw",
                       "override_respond", "override_apply")  # fmt: skip
_OVERRIDE_TYPES = _OVERRIDE_READS + _OVERRIDE_MUTATIONS
_MORE = {
    "requests": "[override] More: /requests overrides [CHARACTER] [open|terminal] more",
    "offers": "[offer] More: /offers ID more",
    "history": "[override] More: /override history ... more",
}
_OVERRIDE_HINTS = {
    "stale_preview": "[override] Nothing was applied: review it again and confirm "
    "anew if you still want it.",
    "offer_stale": "[offer] That offer no longer holds; /offer shows the request now.",
    "offer_unavailable": "[offer] That offer is not open to you; /requests overrides "
    "lists yours.",
    "not_campaign_host": "[role] Only the campaign host can do that here; /role "
    "says what you are.",
    "referee_busy": "[retry] Nothing changed; /retry sends exactly the same "
    "request once the referee is free.",
    "internal_error": "[retry] Nothing changed; /retry sends exactly the same request.",
}
# Nothing was stored: the same request may be resent unchanged.
_RETRYABLE = ("referee_busy", "internal_error")
_HINTS = {
    "not_campaign_host": "[role] Only the campaign host can do that here; /role "
    "says what you are.",
    "stale_revision": "[admin] The game moved on since you looked; check it "
    "(/inspect) and type the change again.",
    "stale_policy": "[rules] The settings changed since you looked; check them "
    "(/rules list) and try again.",
    "referee_busy": "[retry] Nothing changed; /retry sends exactly the same "
    "request once the referee is free.",
    "internal_error": "[retry] Nothing changed; /retry sends exactly the same request.",
}


# What each governance message is answered by, besides an error.
_GOVERNANCE_REPLIES = {
    "requests": "requests", "rules_view": "rules_view", "rules_list": "rules_list",
    "rules_export": "rules", "host_view": "host_view", "host_messages": "host_messages",
    "host_operations": "host_operations", "host_audit": "host_audit",
    "rules_import": "rules", "rules_activate": "rules", "rules_reject": "rules",
    "request_decide": "proposal", "request_cancel": "proposal", "host_command": "result",
    "campaign_host_transfer": "role",
    **{kind: kind for kind in _OVERRIDE_READS},
    "override_request": "proposal", "override_decide": "proposal",
    "override_withdraw": "proposal", "override_respond": "proposal",
    "override_apply": "override_applied",
}  # fmt: skip


def _is_request(value) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("proposal_id"), str)
        and value.get("state") in PROPOSAL_STATES
        and isinstance(value.get("kind"), str)
        and (value.get("target") is None or isinstance(value["target"], dict))
        and isinstance(value.get("requester"), dict)
        and isinstance(value.get("explanation"), str)
    )


def _all(items, check) -> bool:
    return isinstance(items, list) and all(check(item) for item in items)


def _dicts(items) -> bool:
    return _all(items, lambda item: isinstance(item, dict))


# The shape each governance reply must have before it is shown.
_GOVERNANCE_SHAPES = {
    "requests": lambda m: _all(m.get("requests"), _is_request),
    "rules_view": lambda m: _is_revision(m.get("policy_revision"))
    and isinstance(m.get("settings"), dict)
    and (m.get("requirements") is None or _dicts(m["requirements"])),
    "rules_list": lambda m: _is_revision(m.get("policy_revision"))
    and _all(m.get("revisions"), lambda r: isinstance(r, dict)
             and _is_revision(r.get("revision")) and isinstance(r.get("sha256"), str)),
    "rules": lambda m: _is_revision(m.get("revision")) and isinstance(m.get("state"), str),
    "host_view": lambda m: _is_revision(m.get("revision"))
    and _all(m.get("characters"), _is_character)
    and _dicts(m.get("participants")) and _dicts(m.get("pending_choices")),
    "host_messages": lambda m: _all(m.get("messages"), _is_conversation)
    and (m.get("next_after_seq") is None or _is_revision(m["next_after_seq"])),
    "host_operations": lambda m: _is_revision(m.get("revision"))
    and _all(m.get("operations"), lambda o: isinstance(o, dict)
             and isinstance(o.get("command"), str)),
    "host_audit": lambda m: _all(m.get("records"), lambda r: isinstance(r, dict)
                                 and isinstance(r.get("kind"), str))
    and (m.get("next_after") is None or _is_revision(m["next_after"])),
    "proposal": lambda m: isinstance(m.get("proposal_id"), str)
    and m.get("state") in PROPOSAL_STATES,
    "result": lambda m: _is_revision(m.get("revision")) and isinstance(m.get("result"), dict),
    "role": lambda m: m.get("role") in ("host", "guest"),
    **override_lines.SHAPES,
}  # fmt: skip


def _message(kind: str, **fields) -> str:
    return json.dumps({"version": 1, "type": kind, **fields})


def _is_error(message: dict) -> bool:
    return (
        isinstance(message.get("code"), str)
        and isinstance(message.get("message"), str)
        and (
            message.get("request_id") is None or isinstance(message["request_id"], str)
        )
        and (message.get("revision") is None or _is_revision(message["revision"]))
    )


def _is_campaign(value) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("campaign_id"), str)
        and isinstance(value.get("name"), str)
    )


def _is_lobby(message: dict) -> bool:
    campaigns, selected = message.get("campaigns"), message.get("selected")
    return (
        message.get("version") == 1
        and isinstance(message.get("player_id"), str)
        and isinstance(message.get("player_name"), str)
        and isinstance(campaigns, list)
        and all(_is_campaign(c) for c in campaigns)
        and (selected is None or _is_campaign(selected))
    )


def _is_left(message: dict, selection) -> bool:
    return (
        message.get("version") == 1
        and selection is not None
        and message.get("campaign_id") == selection["campaign_id"]
        and isinstance(message.get("reason"), str)
    )


def _with_intent(text: str) -> tuple | None:
    """(words before, the JSON object, words after) of a command line whose
    intent is one JSON object; None if there is none."""
    start = text.find("{")
    if start < 0:
        return None
    try:
        intent, end = json.JSONDecoder().raw_decode(text, start)
    except ValueError:
        return None
    if not isinstance(intent, dict):
        return None
    return text[:start].strip(), intent, text[end:].strip()


def _sent_type(line: str):
    try:
        data = json.loads(line)
    except ValueError:
        return None
    return data.get("type") if isinstance(data, dict) else None


def _is_snapshot(message: dict) -> bool:
    """The metadata fields, plus either both `module` and `view` or neither.

    Only the shape is checked. The view belongs to the module and is printed
    unchanged; the client has no game rules.
    """
    if "module" in message or "view" in message:
        module = message.get("module")
        if not (
            isinstance(module, dict)
            and set(module) == {"id", "version", "state_version"}
            and isinstance(module["id"], str)
            and isinstance(module["version"], str)
            and _is_revision(module["state_version"])
            and isinstance(message.get("view"), dict)
        ):
            return False
    return (
        message.get("version") == 1
        and isinstance(message.get("game_id"), str)
        and isinstance(message.get("participant_id"), str)
        and _is_revision(message.get("revision"))
    )


def _session_of(snapshot: dict) -> tuple:
    """What every snapshot on one connection shares: its game, participant
    and module."""
    module = snapshot.get("module")
    return (
        snapshot["game_id"],
        snapshot["participant_id"],
        None if module is None else tuple(sorted(module.items())),
    )


def _is_revision(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _sent_request_id(line: str):
    try:
        data = json.loads(line)
    except ValueError:
        return None
    return data.get("request_id") if isinstance(data, dict) else None


def _decode_line(line: bytes):
    line = line.removesuffix(b"\r")
    if not line.strip():
        return None
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        _warn("skipped an input line that is not valid UTF-8")
        return None


def _start_stdin_reader(loop, lines: asyncio.Queue, ended):
    """Read stdin on a daemon thread with os.read, which holds no Python locks,
    so an idle reader never prevents the client from exiting. At the end it
    queues None and calls `ended` on the loop.
    """

    def deliver(item):
        try:
            loop.call_soon_threadsafe(lines.put_nowait, item)
            if item is None:
                loop.call_soon_threadsafe(ended)
        except RuntimeError:  # the client has already finished
            pass

    def read():
        pending = b""
        try:
            while chunk := os.read(sys.stdin.fileno(), 65536):
                *complete, pending = (pending + chunk).split(b"\n")
                for line in complete:
                    deliver(line)
            if pending:
                deliver(pending)
        except OSError:
            pass
        deliver(None)

    threading.Thread(target=read, name="tesserae-stdin", daemon=True).start()


def _read_token(path) -> str:
    try:
        token = read_token_file(path)
        mode = os.stat(path).st_mode
    except (OSError, UnicodeDecodeError) as error:
        raise _Failure(f"cannot read token file {path}: {_reason(error)}") from None
    if not token.strip():
        raise _Failure(f"token file {path} is empty")
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        _warn(f"token file {path} is readable by others; chmod 600 it")
    return token


def _request_id(line: str) -> str:
    request_id = _sent_request_id(line)
    return (
        repr(request_id)
        if isinstance(request_id, str)
        else "(a line with no request_id)"
    )


def _closed(connection) -> str:
    code, reason = connection.close_code, connection.close_reason
    if code is None:
        return "unexpectedly"
    return f"(code {code}{', ' + reason if reason else ''})"


def _reason(error: BaseException) -> str:
    return getattr(error, "strerror", None) or str(error) or type(error).__name__


def _diagnose(message: str, code: int) -> int:
    print(f"tesserae connect: {message}", file=sys.stderr, flush=True)
    return code


def _warn(message: str):
    print(f"tesserae connect: warning: {message}", file=sys.stderr, flush=True)
