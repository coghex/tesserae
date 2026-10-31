"""Foreground, loopback-only WebSocket adapter for the game store.

The host runs in one of two explicit modes and never falls back between them:

- development: every connection acts as the fixed participant local-development;
- authenticated: each connection must first join with a personal token and then
  acts as that token's participant.

The game's module, replay, revisions, credentials, and persistence all live in
GameStore; this module only moves messages between sockets and that service,
and tells joined connections when the shared revision changes. See
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

from .external import load_module_dir
from .modules import ModuleError
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


class HostConfigError(ValueError):
    """The game cannot be served in the requested mode; nothing was started."""


class GameHost:
    """Serves one game store to any number of local connections.

    Store calls run on worker threads, each opening its own short-lived
    GameStore, so the event loop never waits on SQLite and no SQLite connection
    is shared between threads. The game's module is loaded once, at startup.
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
        runtime=None,
    ):
        """`runtime`, if given, is an externally loaded module's runtime
        (tesserae.external). The game must then already exist and be bound to
        exactly that runtime; without one, the bundled catalog is used."""
        self.database = Path(database).resolve()
        self.authenticated = authenticated
        self.host = host
        self._ssl = ssl_context
        self._randint = randint
        self._join_timeout = join_timeout
        self._write_timeout = write_timeout
        # Joined connections, subscribed to revision notifications. Only this
        # process's commits are announced; other writers are not observed.
        self._sessions: set[_Session] = set()
        if runtime is None:
            opened = GameStore(self.database)
        else:
            opened = GameStore.open_existing(self.database, runtime=runtime)
        with opened as store:
            self.game_id = store.game_id
            # Exactly the game's bound module and providers, or StoreError:
            # never a fallback. Messages are parsed with its commands, and
            # every worker's store executes with it.
            self._runtime = store.runtime()
        self.module = self._runtime.module
        if self.module.requires_authentication and not authenticated:
            raise HostConfigError(
                f"this game's module, {self.module.id}, requires --authenticated, "
                "even on this computer"
            )
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
                reply = await self._answer(session, message)
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
        try:
            context = await self._call(GameStore.authenticate, message.token)
        except Exception:
            logger.exception("Could not check a join")  # the token is never logged
            return await self._refuse(connection, _JOIN_FAILED)
        if context is None:
            return await self._refuse(
                connection, session_error("authentication_failed")
            )
        session = _Session(connection, context, self._write_timeout)
        self._sessions.add(session)
        try:
            reply = await session.send_read(lambda: self._join_snapshot(context))
        except BaseException:
            self._sessions.discard(session)  # it never became a joined session
            raise
        if reply["type"] != "snapshot":
            self._sessions.discard(session)
            await connection.close(SESSION_CLOSE_CODE, reply["code"])
            return None
        return session

    async def _join_snapshot(self, context: ParticipantContext) -> dict:
        try:
            snapshot = await self._snapshot(context)
        except Exception:
            logger.exception("Could not check a join")
            return _JOIN_FAILED
        return snapshot or session_error("authentication_failed")

    async def _refuse(self, connection: ServerConnection, reply: dict) -> None:
        await connection.send(encode_message(reply))
        await connection.close(SESSION_CLOSE_CODE, reply["code"])
        return None

    async def _snapshot(self, context: ParticipantContext) -> dict | None:
        """The recipient's snapshot, or None if revoked: the one place that
        decides what a joined participant may see.

        The credential check, revision and module state are read together, and
        the module projects this participant's view from that state. A `dice`
        game has no view, so its snapshot is metadata only.
        """
        current = await self._call(GameStore.participant_view, context)
        if current is None:
            return None
        return snapshot_message(
            self.game_id,
            context.participant_id,
            current.revision,
            None if current.view is None else self.module,
            current.view,
        )

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

    async def _answer(self, session: "_Session", message) -> dict:
        """Send exactly one reply to one message from a joined connection."""
        context = session.context
        if isinstance(message, bytes):
            reply = _BINARY_REPLY
        else:
            try:
                parsed = parse_client_message(message, self._runtime)
            except ProtocolError as error:
                reply = await self._rejection(context, error)
            else:
                if isinstance(parsed, SyncRequest):
                    return await session.send_read(lambda: self._sync(context))
                if isinstance(parsed, JoinRequest):
                    reply = session_error("already_joined")
                else:
                    reply = await self._execute(context, message)
        await session.send(reply)
        return reply

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
            snapshot = await self._snapshot(context)
        except Exception:
            return self._failure(None)
        return snapshot or session_error("session_revoked")

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
_JOIN_FAILED = error_message("internal_error", "The host could not check the join.")


class _SlowConsumer(Exception):
    """A connection stopped reading and was closed."""


class _Session:
    """One joined connection: its trusted context, and what it has been sent.

    Every outgoing message is sent under one lock. Snapshots are read and
    sent while holding it, so no notification lands between reading a view
    and sending it: a snapshot always carries the revision its view was read
    with, and revisions on a connection never go backwards. Pending
    notifications collapse to the newest revision, so a slow reader costs one
    integer, not a queue.
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
            await self._send(message)

    async def send_read(self, read) -> dict:
        """Await `read()` and send the message it returns, holding back
        notifications from before the read until after the send."""
        async with self._lock:
            message = await read()
            await self._send(message)
            return message

    async def _send(self, message: dict):
        kind = message["type"]
        if kind == "revision" and message["revision"] <= self._sent_revision:
            return  # already covered by something this connection was sent
        if kind == "snapshot" and message["revision"] < self._sent_revision:
            # Every revision sent was committed before this snapshot was read,
            # under the lock. Never relabel a view with another revision.
            raise RuntimeError("A snapshot was older than its connection's revision.")
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
    module_dir=None,
) -> int:
    """The `tesserae serve` command: run until Ctrl-C, then exit cleanly.

    The address and security settings are checked together before anything
    is opened; an unsafe or incomplete combination never starts a listener.
    With `module_dir`, its package is loaded once, before the game is opened,
    and the game must already exist bound to it.
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
    runtime = None
    if module_dir is not None:
        try:
            runtime = load_module_dir(module_dir)
        except ModuleError as error:
            return _fail(f"cannot load the module package: {error}")
    try:
        game = GameHost(
            database,
            authenticated=authenticated,
            host=transport.host,
            ssl_context=context,
            runtime=runtime,
        )
    except HostConfigError as error:
        return _fail(f"{error}; nothing was started", code=2)
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
            f"listening on {uri} (database: {game.database}, module: "
            f"{game.module.id} {game.module.version}, {who}, {encryption}). "
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
