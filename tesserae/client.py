"""Thin streaming client for an authenticated Tesserae host: `tesserae connect`.

Joins with a token from a file, then forwards each nonblank stdin line as one
message while printing every message the host sends, one JSON object per
stdout line. Diagnostics go to stderr. The host owns all validation and game
logic; the client never rewrites, retries, or judges a request.
See docs/protocol.md.
"""

import asyncio
import json
import os
import signal
import ssl
import stat
import sys
import threading
from collections import deque

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

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
):
    """Check the destination and trust first: plaintext to another computer is
    refused before anything is read, sent, or contacted."""
    try:
        ssl_context = check_client(uri, ca_file)
        token = _read_token(token_file)
    except (_Failure, TransportConfigError) as failure:
        return _diagnose(str(failure), EXIT_FAILED)
    client = _Client(uri, token, game_id, drain_timeout, ssl_context)
    return asyncio.run(client.main())


class _Client:
    def __init__(self, uri, token, game_id, drain_timeout, ssl_context=None):
        self.ssl_context = ssl_context
        self.uri = uri
        self.token = token
        self.game_id = game_id
        self.drain_timeout = drain_timeout
        self.unanswered = deque()  # lines sent, oldest first; replies come in order
        self.replied = asyncio.Event()

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
            connection = await self._first(connect(self.uri, **options))
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
        try:
            await connection.send(
                json.dumps({"version": 1, "type": "join", "token": self.token})
            )
        except ConnectionClosed:
            raise _Failure(f"the host closed the connection {_closed(connection)}")
        try:
            first = await self._first(
                asyncio.wait_for(connection.recv(), JOIN_REPLY_TIMEOUT_SECONDS)
            )
        except TimeoutError:
            raise _Failure("the host did not answer the join in time") from None
        except ConnectionClosed:
            raise _Failure(f"the host closed the connection {_closed(connection)}")
        message = _decode(first)
        _print(message)
        if message.get("type") == "welcome":
            raise _Failure("this is a development host; start it with --authenticated")
        if message.get("type") == "error":
            raise _Failure(f"the host refused to join: {message.get('code')}")
        if message.get("type") != "snapshot" or not _is_snapshot(message):
            raise _Failure("the host's first reply was not a valid snapshot")
        self.view = (message["game_id"], message["participant_id"])
        if self.game_id is not None and message.get("game_id") != self.game_id:
            raise _Failure(
                f"the host serves game {message.get('game_id')}, not {self.game_id}; "
                "nothing was sent"
            )

        lines = asyncio.Queue()
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

    async def _forward(self, connection, lines: asyncio.Queue, receiver):
        """Send each nonblank stdin line as it arrives, until stdin ends."""
        while (line := await self._until_closed(lines.get(), receiver)) is not None:
            text = _decode_line(line)
            if text is None:
                continue
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
                    _print(message)
                    continue
                if not self.unanswered:
                    raise _Failure("the host sent a reply to nothing")
                _check_reply(message, self.unanswered[0], self.view)
                _print(message)
                self.unanswered.popleft()
                self.replied.set()
        except ConnectionClosed:
            pass

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


def _check_reply(message: dict, sent: str, view: tuple):
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
        if not (
            _is_snapshot(message)
            and (message["game_id"], message["participant_id"]) == view
        ):
            raise _Failure("the host sent a snapshot that does not match this session")
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


def _is_snapshot(message: dict) -> bool:
    return (
        message.get("version") == 1
        and isinstance(message.get("game_id"), str)
        and isinstance(message.get("participant_id"), str)
        and _is_revision(message.get("revision"))
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
