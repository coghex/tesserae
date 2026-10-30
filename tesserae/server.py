"""Foreground, loopback-only WebSocket adapter for the game store.

The host runs in one of two explicit modes and never falls back between them:

- development: every connection acts as the fixed participant local-development;
- authenticated: each connection must first join with a personal token and then
  acts as that token's participant.

Dice, replay, revisions, credentials, and persistence all live in GameStore;
this module only moves messages between sockets and that service. See
docs/protocol.md and docs/game_store.md.
"""

import asyncio
import logging
import signal
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .protocol import (
    JOIN_TIMEOUT_SECONDS,
    JoinRequest,
    ProtocolError,
    SyncRequest,
    encode_message,
    error_message,
    parse_client_message,
    parse_request,
    session_error,
    snapshot_message,
    welcome_message,
)
from .store import GameStore, ParticipantContext, StoreBusy, StoreError

HOST = "127.0.0.1"
DEVELOPMENT_PARTICIPANT = ParticipantContext("local-development")
WORKER_THREADS = 8
SESSION_CLOSE_CODE = 1008  # policy violation: the session cannot continue
_BINARY_REPLY = error_message("invalid_request", "Send requests as text JSON frames.")

logger = logging.getLogger("tesserae.server")


class GameHost:
    """Serves one game store to any number of local connections.

    Store calls run on worker threads, each opening its own short-lived
    GameStore, so the event loop never waits on SQLite and no SQLite connection
    is shared between threads.
    """

    def __init__(
        self,
        database,
        *,
        authenticated: bool = False,
        randint=None,
        join_timeout: float = JOIN_TIMEOUT_SECONDS,
    ):
        self.database = Path(database).resolve()
        self.authenticated = authenticated
        self._randint = randint
        self._join_timeout = join_timeout
        with self._open() as store:
            self.game_id = store.game_id
        self._executor = ThreadPoolExecutor(
            WORKER_THREADS, thread_name_prefix="tesserae-store"
        )

    async def run(self, port: int, stop: asyncio.Future, on_ready=None):
        """Serve until `stop` completes, then close connections and the store."""
        try:
            async with serve(self._handle, HOST, port) as server:
                address, bound_port = server.sockets[0].getsockname()[:2]
                if on_ready is not None:
                    on_ready(f"ws://{address}:{bound_port}/")
                await stop
        finally:
            # Finish in-flight store calls: each commits or rolls back whole.
            self._executor.shutdown(wait=True)

    async def _handle(self, connection: ServerConnection):
        try:
            if self.authenticated:
                await self._serve_authenticated(connection)
            else:
                await self._serve_development(connection)
        except ConnectionClosed:
            pass

    async def _serve_development(self, connection: ServerConnection):
        await connection.send(encode_message(await self._welcome()))
        async for message in connection:
            if isinstance(message, bytes):
                reply = _BINARY_REPLY
            else:
                reply = await self._execute(DEVELOPMENT_PARTICIPANT, message)
            await connection.send(encode_message(reply))

    async def _serve_authenticated(self, connection: ServerConnection):
        context = await self._join(connection)
        if context is None:
            return
        async for message in connection:
            reply = await self._session_reply(context, message)
            await connection.send(encode_message(reply))
            if reply["type"] == "error" and reply["code"] == "session_revoked":
                await connection.close(SESSION_CLOSE_CODE, "session_revoked")
                return

    async def _join(self, connection: ServerConnection) -> ParticipantContext | None:
        """Admit a connection, or send one session error and close it.

        Nothing describing the game is sent before a successful join.
        """
        try:
            first = await asyncio.wait_for(connection.recv(), self._join_timeout)
        except TimeoutError:
            return await self._refuse(connection, session_error("join_timeout"))
        if isinstance(first, bytes):
            return await self._refuse(connection, _BINARY_REPLY)
        try:
            message = parse_client_message(first)
        except ProtocolError as error:
            return await self._refuse(connection, error.to_message())
        if not isinstance(message, JoinRequest):
            return await self._refuse(connection, session_error("join_required"))
        try:
            admitted = await self._call(_admit, message.token)
        except Exception:
            logger.exception("Could not check a join")  # the token is never logged
            return await self._refuse(
                connection,
                error_message("internal_error", "The host could not check the join."),
            )
        if admitted is None:
            return await self._refuse(
                connection, session_error("authentication_failed")
            )
        context, revision = admitted
        snapshot = snapshot_message(self.game_id, context.participant_id, revision)
        await connection.send(encode_message(snapshot))
        return context

    async def _refuse(self, connection: ServerConnection, reply: dict) -> None:
        await connection.send(encode_message(reply))
        await connection.close(SESSION_CLOSE_CODE, reply["code"])
        return None

    async def _session_reply(self, context: ParticipantContext, message) -> dict:
        """Exactly one reply for one message from a joined connection."""
        if isinstance(message, bytes):
            return _BINARY_REPLY
        try:
            parsed = parse_client_message(message)
        except ProtocolError as error:
            return await self._rejection(context, error)
        if isinstance(parsed, JoinRequest):
            return session_error("already_joined")
        if isinstance(parsed, SyncRequest):
            return await self._sync(context)
        return await self._execute(context, message)

    async def _rejection(self, context: ParticipantContext, error: ProtocolError):
        """An invalid message's error, carrying the revision only if still joined."""
        try:
            revision = await self._call(GameStore.session_revision, context)
        except Exception:
            return self._failure(error.request_id)
        if revision is None:
            return session_error("session_revoked")
        return error.to_message(revision=revision)

    async def _sync(self, context: ParticipantContext) -> dict:
        try:
            revision = await self._call(GameStore.session_revision, context)
        except Exception:
            return self._failure(None)
        if revision is None:
            return session_error("session_revoked")
        return snapshot_message(self.game_id, context.participant_id, revision)

    async def _welcome(self) -> dict:
        try:
            return welcome_message(await self._call(GameStore.revision))
        except Exception:
            logger.exception("Could not read the game revision")
            return error_message("internal_error", "The host could not read the game.")

    async def _execute(self, context: ParticipantContext, raw: str) -> dict:
        try:
            return await self._call(GameStore.execute, context, raw)
        except Exception:
            return self._failure(_request_id(raw))

    def _failure(self, request_id) -> dict:
        """Report the exception being handled as internal_error, logging it."""
        if isinstance(sys.exception(), StoreBusy):
            logger.warning("Game store busy; request not executed")
            message = "The game is busy; nothing was changed. Retry the same request."
        else:
            logger.exception("Request failed inside the game store")
            message = (
                "The host failed; nothing was acknowledged. Retry the same request "
                "to get its recorded outcome."
            )
        return error_message("internal_error", message, request_id=request_id)

    async def _call(self, method, *args):
        def work():
            with self._open() as store:
                if store.game_id != self.game_id:
                    raise StoreError("The database now holds a different game.")
                return method(store, *args)

        return await asyncio.get_running_loop().run_in_executor(self._executor, work)

    def _open(self) -> GameStore:
        return GameStore(self.database, randint=self._randint)


def _admit(store: GameStore, token: str):
    """(context, revision) for an active token, else None. Runs on a worker."""
    context = store.authenticate(token)
    if context is None:
        return None
    revision = store.session_revision(context)
    return None if revision is None else (context, revision)


def run_foreground(database: str, port: int, *, authenticated: bool = False) -> int:
    """The `tesserae serve` command: run until Ctrl-C, then exit cleanly."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        host = GameHost(database, authenticated=authenticated)
    except (StoreError, sqlite3.Error, OSError) as error:
        return _fail(f"cannot open database {database}: {error}")

    def ready(uri):
        if authenticated:
            who = "mode: authenticated, join with a personal token"
        else:
            who = f"participant: {DEVELOPMENT_PARTICIPANT.participant_id}"
        print(
            f"Tesserae {'authenticated' if authenticated else 'development'} host "
            f"listening on {uri} (database: {host.database}, {who}). "
            "Press Ctrl-C to stop.",
            flush=True,
        )

    async def main():
        loop = asyncio.get_running_loop()
        stop = loop.create_future()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                signum, lambda: stop.done() or stop.set_result(None)
            )
        await host.run(port, stop, on_ready=ready)

    try:
        asyncio.run(main())
    except OSError as error:
        return _fail(f"cannot listen on {HOST}:{port}: {error.strerror or error}")
    print("Tesserae host stopped.", flush=True)
    return 0


def _fail(message: str) -> int:
    print(f"tesserae serve: {message}", file=sys.stderr)
    return 1


def _request_id(raw):
    try:
        return parse_request(raw).request_id
    except ProtocolError as error:
        return error.request_id
