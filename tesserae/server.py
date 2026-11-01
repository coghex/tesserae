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
import secrets
import signal
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .browser import RequestPolicy
from .external import load_module_dir
from .modules import ModuleError
from .protocol import (
    JOIN_TIMEOUT_SECONDS,
    JoinRequest,
    MessagesRequest,
    ProtocolError,
    RefereeStatusRequest,
    SayRequest,
    SyncRequest,
    accepted_message,
    busy_message,
    conversation_message,
    encode_message,
    error_message,
    messages_message,
    parse_client_message,
    parse_request,
    referee_state_message,
    referee_status_message,
    revision_message,
    session_error,
    snapshot_message,
    welcome_message,
)
from .referee.orchestrator import CodexReferee, RefereeJobFailed
from .referee.runner import DEFAULT_DIR as DEFAULT_REFEREE_DIR
from .referee.runner import DEFAULT_TIMEOUT, RefereeDir, RunnerError
from .referee.store import REFEREE, RefereeError, RefereeStore
from .store import GameStore, ParticipantContext, StoreBusy, StoreError
from .transport import TransportConfigError, check_server, server_context
from .web import AssetsError, load_assets

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
        referee_executor=None,
        referee_dir=None,
        referee_timeout: float = DEFAULT_TIMEOUT,
        browser_assets=None,
    ):
        """`runtime`, if given, is an externally loaded module's runtime
        (tesserae.external). The game must then already exist and be bound to
        exactly that runtime; without one, the bundled catalog is used.

        A game in referee mode needs `referee_executor`: an async callable
        given each accepted Job, returning the reply to publish or raising
        RefereeJobFailed. The host stores and delivers what it returns.
        Without one, `referee_dir` (a folder made by `tesserae referee setup`)
        gives the game a CodexReferee (tesserae.referee.orchestrator), which
        the host also opens, lets recover an earlier run's job, and closes.

        `browser_assets` (tesserae.web.load_assets) also serves the browser
        page from this port: plain loopback, authenticated and in referee mode
        only (docs/designs/browser_contract.md sections 2 and 3)."""
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
            opened = GameStore(self.database, load_runtime=True)
        else:
            opened = GameStore.open_existing(self.database, runtime=runtime)
        with opened as store:
            self.game_id = store.game_id
            # Exactly the game's bound module and providers, or StoreError:
            # never a fallback. Messages are parsed with its commands, and
            # every worker's store executes with it.
            self._runtime = store.runtime()
            self.referee_mode = RefereeStore(store).enabled()
        self.module = self._runtime.module
        if self.module.requires_authentication and not authenticated:
            raise HostConfigError(
                f"this game's module, {self.module.id}, requires --authenticated, "
                "even on this computer"
            )
        if self.referee_mode and not authenticated:
            raise HostConfigError(
                "this game is in referee mode, which requires --authenticated"
            )
        if browser_assets is not None:
            if not authenticated:
                raise HostConfigError("--browser requires --authenticated")
            if not self.referee_mode:
                raise HostConfigError(
                    "--browser needs a game in referee mode (tesserae referee enable)"
                )
            if ssl_context is not None or host != HOST:
                raise HostConfigError(
                    f"--browser serves this computer only, on {HOST} without TLS"
                )
        self._requests = RequestPolicy(browser_assets)
        if self.referee_mode and referee_executor is None and referee_dir is not None:
            folder = RefereeDir(Path(referee_dir).expanduser())
            if not (folder.codex_home / "config.toml").is_file():
                raise HostConfigError(
                    f"this game is in referee mode, and {folder.root} is not a referee "
                    "folder yet (run tesserae referee setup, then log Codex in)"
                )
            referee_executor = CodexReferee(
                folder, self.game_id, timeout=referee_timeout
            )
        if self.referee_mode and referee_executor is None:
            raise HostConfigError(
                "this game is in referee mode, and this host has no referee runner"
            )
        self._referee_executor = referee_executor
        # The full referee (open, recover, close), as opposed to a bare
        # executor such as a test's.
        self._referee = (
            referee_executor if isinstance(referee_executor, CodexReferee) else None
        )
        self._host_run = "h-" + secrets.token_hex(6)
        self._jobs: set[asyncio.Task] = set()
        self._closing = False  # shutting down: no new referee input is accepted
        self._outbox = None  # ordered unsolicited deliveries, while running
        # Held across each referee transition (accepting an input, settling a
        # job) and the outbox items it queues, so the outbox order is the
        # store's order.
        self._lifecycle = None
        self._executor = ThreadPoolExecutor(
            WORKER_THREADS, thread_name_prefix="tesserae-store"
        )

    async def run(self, port: int, stop: asyncio.Future, on_ready=None):
        """Serve until `stop` completes, then close connections and the store."""
        self._outbox = asyncio.Queue()
        self._lifecycle = asyncio.Lock()
        deliveries = asyncio.create_task(self._deliver_outbox())
        recovery = None
        try:
            if self._referee is not None:
                loop = asyncio.get_running_loop()
                self._referee.open(
                    self._host_run,
                    self._open,
                    lambda revision: loop.call_soon_threadsafe(self._publish, revision),
                )
                recovery = asyncio.create_task(self._recover_referee())
            async with serve(
                self._handle,
                self.host,
                port,
                ssl=self._ssl,
                process_request=self._requests.process_request,
            ) as server:
                address, bound_port = server.sockets[0].getsockname()[:2]
                self._requests.bind(bound_port)
                scheme = "ws" if self._ssl is None else "wss"
                if on_ready is not None:
                    on_ready(f"{scheme}://{address}:{bound_port}/")
                await stop
                await self._end_jobs()
        finally:
            deliveries.cancel()
            if recovery is not None:
                recovery.cancel()
                await asyncio.gather(recovery, return_exceptions=True)
            if self._referee is not None:  # its sockets take a moment to close
                await asyncio.get_running_loop().run_in_executor(
                    None, self._referee.close
                )
            # Finish in-flight store calls: each commits or rolls back whole.
            self._executor.shutdown(wait=True)

    async def _end_jobs(self):
        """Close admission, then end every job of this host run `interrupted`
        before the store closes."""
        self._closing = True
        async with self._lifecycle:
            pass  # an acceptance under way has scheduled its job by now
        for job in list(self._jobs):
            job.cancel()
        await asyncio.gather(*self._jobs, return_exceptions=True)
        if self.referee_mode:
            try:  # a job cancelled before its task ever ran never ended itself
                await self._call(_end_unfinished, self._host_run)
            except Exception:
                logger.exception("Could not end this host's referee job")

    async def _recover_referee(self):
        """Finish a job an earlier host run left `running` once nothing of it
        is alive; until then the game stays busy (contract section 4)."""
        loop = asyncio.get_running_loop()
        while True:
            async with self._lifecycle:
                try:
                    outcome = await loop.run_in_executor(
                        self._executor, self._referee.recover
                    )
                except Exception:
                    logger.exception("Could not recover an earlier referee job")
                    outcome = "waiting"
                if outcome == "finished":
                    self._outbox.put_nowait(("state", referee_state_message("idle")))
            if outcome != "waiting":
                return
            await asyncio.sleep(self._referee.lock_retry)

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
        referee = None
        if self.referee_mode:
            referee = await self._call(_referee_projection, context)
            if referee is None:
                return None
        return snapshot_message(
            self.game_id,
            context.participant_id,
            current.revision,
            None if current.view is None else self.module,
            current.view,
            referee,
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
                if isinstance(
                    parsed, (SayRequest, RefereeStatusRequest, MessagesRequest)
                ):
                    if not self.referee_mode:
                        reply = error_message(
                            "referee_off",
                            "This game is not in referee mode.",
                            request_id=getattr(parsed, "request_id", None),
                        )
                    elif isinstance(parsed, SayRequest):
                        return await self._say(session, parsed)
                    else:
                        reply = await self._referee_read(context, parsed)
                elif isinstance(parsed, JoinRequest):
                    reply = session_error("already_joined")
                elif self.referee_mode:
                    # Every player action goes through the referee, busy or not.
                    reply = error_message(
                        "referee_mode",
                        "This game has a referee: say what you want to do, and the "
                        "referee runs the game's commands.",
                        request_id=parsed.request_id,
                    )
                else:
                    reply = await self._execute(context, message)
        await session.send(reply)
        return reply

    async def _say(self, session: "_Session", request: SayRequest) -> dict:
        """Accept, replay or refuse one input and send its reply; an accepted
        input's job is running, and its message and `busy` queued, before
        that reply is sent, so a failed send never strands the job. Holding
        the sender's send lock throughout keeps the reply ahead of them."""
        replay = []

        async def decide() -> dict:
            async with self._lifecycle:
                if self._closing:
                    return error_message(
                        "internal_error",
                        "The host is shutting down; nothing was stored. Say it again "
                        "once it is back.",
                        request_id=request.request_id,
                    )
                try:
                    outcome = await self._call(
                        _submit, session.context, request, self._host_run
                    )
                except Exception:
                    return self._failure(request.request_id)
                kind = outcome[0]
                if kind == "busy":
                    return busy_message(request.request_id, outcome[2], outcome[1])
                if kind == "error":
                    code, text = outcome[1], outcome[2]
                    if code == "session_revoked":
                        return session_error(code)
                    return error_message(code, text, request_id=request.request_id)
                submission, said, names = outcome[1:]
                if kind == "accepted":
                    self._outbox.put_nowait(("conversation", ([said], names)))
                    self._outbox.put_nowait(("state", referee_state_message("busy")))
                    job = asyncio.create_task(self._run_job(submission.job))
                    self._jobs.add(job)
                    job.add_done_callback(self._jobs.discard)
                else:  # replayed: its recorded reply, to this connection only
                    replay.extend(_wire(m, names) for m in submission.messages)
                return accepted_message(
                    request.request_id, submission.job.job_id, said["seq"]
                )

        reply = await session.send_read(decide)
        for message in replay:
            await session.send(message)
        return reply

    async def _run_job(self, job):
        """Run one job with the executor, then store and deliver how it ended."""
        reply, state, error = None, None, None
        try:
            reply = await self._referee_executor(job)
        except RefereeJobFailed as failed:
            state, error = failed.state, failed.error
        except asyncio.CancelledError:
            state, error = "interrupted", "shutdown"
        except Exception:
            logger.exception("A referee job failed")
            state, error = "failed", "runner_failed"
        async with self._lifecycle:  # no new job is accepted until this is queued
            try:
                stored, names = await self._call(
                    _settle, job.job_id, reply, state, error
                )
            except Exception:
                logger.exception("Could not record how a referee job ended")
                stored, names = (), {}
            if stored:
                self._outbox.put_nowait(("conversation", (list(stored), names)))
            self._outbox.put_nowait(("state", referee_state_message("idle")))

    async def _referee_read(self, context: ParticipantContext, request) -> dict:
        """`referee_status` or `messages`, for the caller only."""
        try:
            if isinstance(request, RefereeStatusRequest):
                projection = await self._call(_referee_projection, context)
                if projection is not None:
                    return referee_status_message(projection)
            else:
                visible = await self._call(
                    _visible_messages, context, request.after_seq
                )
                if visible is not None:
                    return messages_message(visible)
        except Exception:
            return self._failure(None)
        return session_error("session_revoked")

    async def _deliver_outbox(self):
        """Deliver unsolicited referee messages in the order they were queued.
        Conversation goes only to its audience, each session rechecked first."""
        while True:
            kind, payload = await self._outbox.get()
            sends = []
            for session in list(self._sessions):
                if kind == "state":
                    sends.append(self._send_all(session, [payload], check=False))
                    continue
                messages, names = payload
                pid = session.context.participant_id
                visible = [
                    _wire(m, names)
                    for m in messages
                    if m["audience"] == "table" or pid in m["audience"]
                ]
                if visible:
                    sends.append(self._send_all(session, visible, check=True))
            await asyncio.gather(*sends, return_exceptions=True)

    async def _send_all(self, session: "_Session", messages: list, *, check: bool):
        try:
            if check:
                revision = await self._check_for_delivery(session)
                if revision is _UNCHECKED:
                    await session.connection.close(FAILURE_CLOSE_CODE, "internal error")
                    return
                if revision is None:  # a revoked credential gets nothing
                    await session.connection.close(
                        SESSION_CLOSE_CODE, "session_revoked"
                    )
                    return
            for message in messages:
                await session.send(message)
        except (ConnectionClosed, _SlowConsumer):
            pass

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


def _submit(store: GameStore, context, request: SayRequest, host_run: str) -> tuple:
    referee = RefereeStore(store)
    try:
        submission = referee.submit(
            context, request.request_id, request.text, request.private, host_run
        )
    except RefereeError as error:
        if error.code == "referee_busy":
            running = referee.running_job()
            names = _names(store)
            name = (
                "someone"
                if running is None
                else names.get(running.participant_id, "someone")
            )
            return ("busy", name, store.revision())
        return ("error", error.code, str(error))
    said = referee.input_message(submission.job.job_id)
    return (submission.outcome, submission, said, _names(store))


def _end_unfinished(store: GameStore, host_run: str):
    referee = RefereeStore(store)
    job = referee.running_job()
    if job is not None and job.host_run == host_run:
        referee.finish(job.job_id, "interrupted", "shutdown")


def _settle(store: GameStore, job_id: str, reply, state, error) -> tuple:
    """Publish a job's reply, or record why it ended without one."""
    referee = RefereeStore(store)
    if reply is not None:
        try:
            return referee.publish_reply(job_id, reply), _names(store)
        except RefereeError as refused:  # nothing from it is published
            state, error = "failed", refused.code
    referee.finish(job_id, state, error)
    return (), {}


def _referee_projection(store: GameStore, context) -> dict | None:
    if store.session_revision(context) is None:
        return None
    return RefereeStore(store).projection(context.participant_id)


def _visible_messages(store: GameStore, context, after_seq: int) -> list | None:
    if store.session_revision(context) is None:
        return None
    names = _names(store)
    return [
        _wire(m, names)
        for m in RefereeStore(store).messages_for(context.participant_id, after_seq)
    ]


def _names(store: GameStore) -> dict:
    return {p.participant_id: p.name for p in store.participants()}


def _wire(message: dict, names: dict) -> dict:
    """A stored message as its recipients see it."""
    sender = message["sender"]
    return conversation_message(
        message["seq"],
        None if sender == REFEREE else names.get(sender, sender),
        "table" if message["audience"] == "table" else "private",
        message["text"],
    )


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
    referee_dir=None,
    referee_timeout: float = DEFAULT_TIMEOUT,
    browser: bool = False,
) -> int:
    """The `tesserae serve` command: run until Ctrl-C, then exit cleanly.

    The address and security settings are checked together before anything
    is opened; an unsafe or incomplete combination never starts a listener.
    With `module_dir`, its package is loaded once, before the game is opened,
    and the game must already exist bound to it.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if browser:
        if not authenticated:
            return _fail("--browser requires --authenticated; nothing was started", 2)
        if tls_cert is not None or tls_key is not None or host != HOST:
            return _fail(
                f"--browser serves this computer only, on {HOST} without TLS "
                "(browser certificate trust is later work); nothing was started",
                2,
            )
        try:
            assets = load_assets()
        except AssetsError as error:
            return _fail(str(error))
    else:
        assets = None
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
            referee_dir=referee_dir or DEFAULT_REFEREE_DIR,
            referee_timeout=referee_timeout,
            browser_assets=assets,
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
        if browser:
            page = "http://" + uri.removeprefix("ws://")
            print(
                f"Browser: {page} (this computer only; join with a personal token)",
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
    except RunnerError as error:
        return _fail(str(error))
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
