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
import stat
import sys
import threading
from collections import deque
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from .credentials import read_token_file

DRAIN_TIMEOUT_SECONDS = 10.0
JOIN_REPLY_TIMEOUT_SECONDS = 10.0
EXIT_OK = 0  # stdin ended and every request was answered
EXIT_FAILED = 1  # could not start, or the host ended the session
EXIT_UNCERTAIN = 3  # some requests were never answered; resend them unchanged
EXIT_INTERRUPTED = 130  # Ctrl-C


class _Failure(Exception):
    """Stop with a diagnostic; never includes the token."""


def run(uri: str, token_file, game_id=None, drain_timeout=DRAIN_TIMEOUT_SECONDS):
    try:
        _check_uri(uri)
        token = _read_token(token_file)
    except _Failure as failure:
        return _diagnose(str(failure), EXIT_FAILED)
    return asyncio.run(_Client(uri, token, game_id, drain_timeout).main())


class _Client:
    def __init__(self, uri, token, game_id, drain_timeout):
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
        try:
            connection = await self._first(
                connect(self.uri, open_timeout=JOIN_REPLY_TIMEOUT_SECONDS, proxy=None)
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
            return _diagnose(str(failure), EXIT_FAILED)
        finally:
            await connection.close()

    async def _session(self, connection) -> int:
        await connection.send(
            json.dumps({"version": 1, "type": "join", "token": self.token})
        )
        try:
            first = await self._first(
                asyncio.wait_for(connection.recv(), JOIN_REPLY_TIMEOUT_SECONDS)
            )
        except TimeoutError:
            raise _Failure("the host did not answer the join in time") from None
        except ConnectionClosed:
            raise _Failure(f"the host closed the connection {_closed(connection)}")
        message = _emit(first)
        if message.get("type") == "welcome":
            raise _Failure("this is a development host; start it with --authenticated")
        if message.get("type") == "error":
            raise _Failure(f"the host refused to join: {message.get('code')}")
        if message.get("type") != "snapshot":
            raise _Failure("the host's first reply was not a snapshot")
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
        """Print everything the host sends; ends quietly when it closes."""
        try:
            async for raw in connection:
                if isinstance(raw, bytes):
                    raise _Failure("the host sent a binary frame")
                message = _emit(raw)
                if message.get("type") != "revision" and self.unanswered:
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


def _emit(raw) -> dict:
    """Print one host message as one JSON line."""
    try:
        message = json.loads(raw)
    except ValueError:
        raise _Failure("the host sent something that is not JSON") from None
    if not isinstance(message, dict):
        raise _Failure("the host sent a JSON value that is not an object")
    print(json.dumps(message), flush=True)
    return message


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


def _check_uri(uri: str):
    parts = urlsplit(uri)
    if parts.scheme not in ("ws", "wss") or not parts.hostname:
        raise _Failure("the host URI must look like ws://127.0.0.1:PORT/")
    if parts.query or parts.fragment or parts.username or parts.password:
        # Do not echo it: it might be carrying a secret.
        raise _Failure(
            "the host URI must not carry a query, fragment, or credentials; "
            "the token comes only from --token-file"
        )


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
    try:
        request_id = json.loads(line).get("request_id")
    except (ValueError, AttributeError):
        request_id = None
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
