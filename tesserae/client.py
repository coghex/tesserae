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

from websockets.asyncio.client import connect
from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    InvalidStatus,
    InvalidURI,
)

from .credentials import read_token_file
from .transport import TransportConfigError, check_client

DRAIN_TIMEOUT_SECONDS = 10.0
JOIN_REPLY_TIMEOUT_SECONDS = 10.0
EXIT_OK = 0  # stdin ended and every request was answered
EXIT_FAILED = 1  # could not start, or the host ended the session
EXIT_UNCERTAIN = 3  # some requests were never answered; resend them unchanged
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
):
    """Check the destination and trust first: plaintext to another computer is
    refused before anything is read, sent, or contacted."""
    if campaign is not None and not lobby:
        return _diagnose("--campaign needs --lobby; nothing was sent", EXIT_FAILED)
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
        # Lobby mode (--lobby): the catalog last shown, the selected campaign,
        # and the retry state of each campaign and participant while the client
        # is elsewhere.
        self.lobby = False
        self.campaign_wanted = None
        self.catalog = []
        self.selection = None
        self.saved = {}
        self.entered = False  # set when a select's snapshot arrives

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
        _start_stdin_reader(asyncio.get_running_loop(), lines)
        receiver = asyncio.create_task(self._receive(connection))
        try:
            await self._forward(connection, lines, receiver)
            await asyncio.wait_for(
                self._until_closed(self._all_answered(), receiver), self.drain_timeout
            )
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

        In lobby mode a line that changes the selection is answered before the
        next line is read, so each line is understood in the campaign (or the
        lobby) it was typed in.
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
            if self.lobby and _sent_type(text) in ("select", "leave"):
                await self._until_closed(self._all_answered(), receiver)
                if self.entered and self.referee:
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
        self.unanswered.append(text)
        try:
            await connection.send(text)
        except ConnectionClosed:
            raise _HostClosed from None

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
                if not self.unanswered:
                    raise _Failure("the host sent a reply to nothing")
                sent = self.unanswered[0]
                _check_reply(message, sent, getattr(self, "session", None))
                if self.referee:  # every line sent was the client's own JSON
                    self._remember(message, sent)
                if self.lobby:
                    self._after_reply(message, sent)
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
        if command in ("/character", "/characters"):
            return self._note(
                "[lobby] Characters come in a later version; /help lists what works now."
            )
        if command == "/help":
            self._note(_LOBBY_HELP)
            if self.selection is not None and self.referee:
                self._note(_HELP)
            return None
        if self.selection is None:
            return self._note(
                "[lobby] Choose a campaign first: /campaigns lists them, /join enters one."
            )
        if self.referee:
            return self._translate(text)
        return text

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
        return json.dumps({"version": 1, "type": "select", "campaign_id": campaign_id})

    def _after_reply(self, message: dict, sent: str):
        """Show a reply in lobby mode and follow any change of selection."""
        kind, sent_type = message.get("type"), _sent_type(sent)
        if kind == "lobby":
            if sent_type == "leave":
                self._back_in_lobby(quiet=True)
            self._lobby_reply(message)
        elif sent_type == "select" and kind == "snapshot":
            self._enter(message)
        elif kind == "error" and message["code"] == "campaign_unavailable":
            self._show(message)
            if self.selection is not None and sent_type != "select":
                self._back_in_lobby()
        else:
            self._show(message)

    def _lobby_reply(self, message: dict):
        self.catalog = message["campaigns"]
        self._show(message)

    def _enter(self, snapshot: dict):
        campaign = snapshot["campaign"]
        self.selection = campaign
        self.session = _session_of(snapshot)
        self.referee = isinstance(snapshot.get("referee"), dict)
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
        """Retry state belongs to one campaign and one participant in it: an
        input is never resent as someone else, even after a relink."""
        return self.selection["campaign_id"], self.session[1]

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
        self.selection, self.referee = None, False
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
        said = {"request_id": request_id or "c-" + secrets.token_hex(6), "text": text,
                "private": private}  # fmt: skip
        self.sending[said["request_id"]] = said
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
            and (self.selection is None or message.get("type") in ("lobby", "left"))
        )
        if not readable or self.json_output:
            _print(message)
            return
        for line in _readable(message):
            print(line, flush=True)

    async def _all_answered(self):
        while self.unanswered:
            self.replied.clear()
            await self.replied.wait()

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
    if kind == "left":
        return ["[left] That campaign is no longer available to you."]
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
        return [f"[error] {message['message']}"]
    if kind == "snapshot":
        view = message.get("view")
        detail = "" if view is None else " " + json.dumps(view, separators=(", ", ": "))
        return [f"[view] revision {message['revision']}{detail}"]
    return []  # accepted (its words arrive as a message) and revisions


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
    return f"[{message['audience']}] {who}: {message['text']}"


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
    else:
        raise _Failure(
            f"the host answered a {kind or 'malformed'} line with a "
            f"{message.get('type')!r} message"
        )


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


def _start_stdin_reader(loop, lines: asyncio.Queue):
    """Read stdin on a daemon thread with os.read, which holds no Python locks,
    so an idle reader never prevents the client from exiting.
    """

    def deliver(item):
        try:
            loop.call_soon_threadsafe(lines.put_nowait, item)
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
