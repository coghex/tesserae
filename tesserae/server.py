"""Foreground, loopback-only WebSocket adapter for the game store.

The host runs in one of two explicit modes and never falls back between them:

- development: every connection acts as the fixed participant local-development;
- authenticated: each connection must first join with a personal token and then
  acts as that token's participant.

Dice, replay, revisions, credentials, and persistence all live in GameStore;
this module only moves messages between sockets and that service, and tells
joined connections when the shared revision changes. See docs/protocol.md and
docs/game_store.md.
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

from .builtin import DICE_RUNTIME
from .protocol import (
    JOIN_TIMEOUT_SECONDS,
    JoinRequest,
    ProtocolError,
    SyncRequest,
    encode_message,
    error_message,
    parse_client_message,
    parse_request,
    revision_message,
    session_error,
    snapshot_message,
    welcome_message,
)
from .store import GameStore, ParticipantContext, StoreBusy, StoreError
from .transport import TransportConfigError, check_server, server_context

HOST = "127.0.0.1"  # the default; see tesserae.transport for what else is allowed
DEVELOPMENT_PARTICIPANT = ParticipantContext("local-development")
WORKER_THREADS = 8
SESSION_CLOSE_CODE = 1008  # policy violation: the session cannot continue
SLOW_CLOSE_CODE = 1013  # try again later: reconnect and join to resynchronize
FAILURE_CLOSE_CODE = 1011  # internal error: reconnect and join to resynchronize
WRITE_TIMEOUT_SECONDS = 10.0
# Waits between retries when a notification's credential check fails, e.g.
# StoreBusy. After the last, the connection is closed rather than left behind.
NOTIFY_RETRY_DELAYS = (0.1, 0.5)
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
        host: str = HOST,
        ssl_context=None,
        randint=None,
        join_timeout: float = JOIN_TIMEOUT_SECONDS,
        write_timeout: float = WRITE_TIMEOUT_SECONDS,
    ):
        self.database = Path(database).resolve()
        self.authenticated = authenticated
        self.host = host
        self._ssl = ssl_context
        self._randint = randint
        # Messages are parsed with the game's own commands, so a command the
        # store would accept is never refused first. Stores refuse a game
        # bound to anything else. Only `dice` until #41.
        self._runtime = DICE_RUNTIME
        self._join_timeout = join_timeout
        self._write_timeout = write_timeout
        # Joined connections, subscribed to revision notifications. Only this
        # process's commits are announced; other writers are not observed.
        self._sessions: set[_Session] = set()
        with self._open() as store:
            self.game_id = store.game_id
        self._executor = ThreadPoolExecutor(
            WORKER_THREADS, thread_name_prefix="tesserae-store"
        )

    async def run(self, port: int, stop: asyncio.Future, on_ready=None):
        """Serve until `stop` completes, then close connections and the store."""
        try:
            async with serve(self._handle, self.host, port, ssl=self._ssl) as server:
                address, bound_port = server.sockets[0].getsockname()[:2]
                scheme = "ws" if self._ssl is None else "wss"
                if on_ready is not None:
                    on_ready(f"{scheme}://{address}:{bound_port}/")
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
        except (ConnectionClosed, _SlowConsumer):
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
        session = await self._join(connection)
        if session is None:
            return
        notifier = asyncio.create_task(self._deliver_notifications(session))
        try:
            async for message in connection:
                reply = await self._session_reply(session.context, message)
                await session.send(reply)
                if reply["type"] == "error" and reply["code"] == "session_revoked":
                    await connection.close(SESSION_CLOSE_CODE, "session_revoked")
                    return
        finally:
            self._sessions.discard(session)
            notifier.cancel()

    async def _join(self, connection: ServerConnection) -> "_Session | None":
        """Admit a connection, or send one session error and close it.

        Nothing describing the game is sent before a successful join. The
        session subscribes to notifications before its snapshot is read, so a
        commit racing the join is covered by the snapshot or a notification.
        """
        try:
            first = await asyncio.wait_for(connection.recv(), self._join_timeout)
        except TimeoutError:
            return await self._refuse(connection, session_error("join_timeout"))
        if isinstance(first, bytes):
            return await self._refuse(connection, _BINARY_REPLY)
        try:
            message = parse_client_message(first, self._runtime)
        except ProtocolError as error:
            return await self._refuse(connection, error.to_message())
        if not isinstance(message, JoinRequest):
            return await self._refuse(connection, session_error("join_required"))
        session = None
        try:
            context = await self._call(GameStore.authenticate, message.token)
            if context is not None:
                session = _Session(connection, context, self._write_timeout)
                self._sessions.add(session)
                revision = await self._call(GameStore.session_revision, context)
        except Exception:
            logger.exception("Could not check a join")  # the token is never logged
            self._sessions.discard(session)
            return await self._refuse(
                connection,
                error_message("internal_error", "The host could not check the join."),
            )
        if session is None or revision is None:
            self._sessions.discard(session)
            return await self._refuse(
                connection, session_error("authentication_failed")
            )
        try:
            await session.send(self._view(context, revision))
        except BaseException:
            self._sessions.discard(session)  # it never became a joined session
            raise
        return session

    async def _refuse(self, connection: ServerConnection, reply: dict) -> None:
        await connection.send(encode_message(reply))
        await connection.close(SESSION_CLOSE_CODE, reply["code"])
        return None

    def _view(self, context: ParticipantContext, revision: int) -> dict:
        """The recipient's view: the one place that decides what a joined
        participant may see. Today that is only the allowlisted metadata.
        """
        return snapshot_message(self.game_id, context.participant_id, revision)

    def _publish(self, revision: int):
        """Announce a committed revision to every joined connection. Never waits."""
        for session in list(self._sessions):
            session.notify(revision)

    async def _deliver_notifications(self, session: "_Session"):
        """Send one session its newest pending revision, rechecking its credential.

        A revoked session is closed with 1008 and no error message, because no
        request is being answered.
        """
        try:
            while True:
                target = await session.next_pending()
                revision = await self._check_for_delivery(session)
                if revision is _UNCHECKED:
                    # Never leave a client silently behind: closing makes it
                    # reconnect and join for a fresh snapshot.
                    await session.connection.close(FAILURE_CLOSE_CODE, "internal error")
                    return
                if revision is None:
                    await session.connection.close(
                        SESSION_CLOSE_CODE, "session_revoked"
                    )
                    return
                await session.send(
                    revision_message(self.game_id, max(revision, target))
                )
        except (ConnectionClosed, _SlowConsumer):
            pass

    async def _check_for_delivery(self, session: "_Session"):
        """The session's current revision, None if revoked, or _UNCHECKED."""
        for delay in (*NOTIFY_RETRY_DELAYS, None):
            try:
                return await self._call(GameStore.session_revision, session.context)
            except Exception:
                logger.exception("Could not check a session for a notification")
                if delay is None:
                    return _UNCHECKED
                await asyncio.sleep(delay)

    async def _session_reply(self, context: ParticipantContext, message) -> dict:
        """Exactly one reply for one message from a joined connection."""
        if isinstance(message, bytes):
            return _BINARY_REPLY
        try:
            parsed = parse_client_message(message, self._runtime)
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
        return self._view(context, revision)

    async def _welcome(self) -> dict:
        try:
            return welcome_message(await self._call(GameStore.revision))
        except Exception:
            logger.exception("Could not read the game revision")
            return error_message("internal_error", "The host could not read the game.")

    async def _execute(self, context: ParticipantContext, raw: str) -> dict:
        try:
            reply, committed = await self._call(GameStore.execute_command, context, raw)
        except Exception:
            return self._failure(_request_id(raw, self._runtime))
        if committed and self.authenticated:
            self._publish(reply["revision"])
        return reply

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
        return GameStore(self.database, randint=self._randint, runtime=self._runtime)


_UNCHECKED = object()


class _SlowConsumer(Exception):
    """A connection stopped reading and was closed."""


class _Session:
    """One joined connection: its trusted context, and what it has been sent.

    Every outgoing message goes through send(), under one lock. Snapshots and
    notifications on a connection never go backwards. Pending notifications
    collapse to the newest revision, so a slow reader costs one integer, not a
    queue.
    """

    def __init__(self, connection, context: ParticipantContext, write_timeout: float):
        self.connection = connection
        self.context = context
        self._write_timeout = write_timeout
        self._lock = asyncio.Lock()
        self._sent_revision = -1  # highest revision in a snapshot or notification
        self._pending_revision = -1
        self._wake = asyncio.Event()

    def notify(self, revision: int):
        if revision > self._pending_revision:
            self._pending_revision = revision
            self._wake.set()

    async def next_pending(self) -> int:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._pending_revision > self._sent_revision:
                return self._pending_revision

    async def send(self, message: dict):
        async with self._lock:
            kind = message["type"]
            if kind == "revision" and message["revision"] <= self._sent_revision:
                return  # already covered by something this connection was sent
            if kind == "snapshot" and message["revision"] < self._sent_revision:
                # Read before a newer revision was announced; revisions only grow.
                message = {**message, "revision": self._sent_revision}
            try:
                await asyncio.wait_for(
                    self.connection.send(encode_message(message)), self._write_timeout
                )
            except TimeoutError:
                await self._drop()
                raise _SlowConsumer from None
            if kind in ("snapshot", "revision"):
                self._sent_revision = message["revision"]

    async def _drop(self):
        logger.warning("Closing a connection that stopped reading")
        try:
            await asyncio.wait_for(
                self.connection.close(SLOW_CLOSE_CODE, "slow consumer"), 1
            )
        except TimeoutError:
            self.connection.transport.abort()


def run_foreground(
    database: str,
    port: int,
    *,
    authenticated: bool = False,
    host: str = HOST,
    tls_cert=None,
    tls_key=None,
) -> int:
    """The `tesserae serve` command: run until Ctrl-C, then exit cleanly.

    The address and security settings are checked together before anything
    is opened; an unsafe or incomplete combination never starts a listener.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        transport = check_server(
            host, authenticated=authenticated, tls_cert=tls_cert, tls_key=tls_key
        )
    except TransportConfigError as error:
        return _fail(str(error), code=2)
    try:
        context = server_context(tls_cert, tls_key) if transport.tls else None
    except TransportConfigError as error:
        return _fail(str(error))
    try:
        game = GameHost(
            database,
            authenticated=authenticated,
            host=transport.host,
            ssl_context=context,
        )
    except (StoreError, sqlite3.Error, OSError) as error:
        return _fail(f"cannot open database {database}: {error}")

    def ready(uri):
        if authenticated:
            who = "mode: authenticated, join with a personal token"
        else:
            who = f"participant: {DEVELOPMENT_PARTICIPANT.participant_id}"
        encryption = "TLS" if transport.tls else "no encryption, this computer only"
        print(
            f"Tesserae {'authenticated' if authenticated else 'development'} host "
            f"listening on {uri} (database: {game.database}, {who}, {encryption}). "
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
        await game.run(port, stop, on_ready=ready)

    try:
        asyncio.run(main())
    except OSError as error:
        return _fail(
            f"cannot listen on {transport.host}:{port}: {error.strerror or error}"
        )
    print("Tesserae host stopped.", flush=True)
    return 0


def _fail(message: str, code: int = 1) -> int:
    print(f"tesserae serve: {message}", file=sys.stderr)
    return code


def _request_id(raw, runtime):
    try:
        return parse_request(raw, runtime).request_id
    except ProtocolError as error:
        return error.request_id
