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
from dataclasses import replace
from pathlib import Path

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .browser import RequestPolicy
from .external import load_module_dir
from .modules import ModuleError
from .protocol import (
    JOIN_TIMEOUT_SECONDS,
    CharacterRequest,
    HostRequest,
    JoinRequest,
    MessagesRequest,
    ProtocolError,
    RefereeStatusRequest,
    SayRequest,
    SyncRequest,
    accepted_message,
    actor_notice,
    actor_object,
    busy_message,
    character_message,
    characters_message,
    conversation_message,
    encode_message,
    error_message,
    host_messages_message,
    host_view_message,
    messages_message,
    parse_client_message,
    parse_request,
    referee_state_message,
    referee_status_message,
    revision_message,
    role_message,
    session_error,
    snapshot_message,
    welcome_message,
)
from .referee.orchestrator import CodexReferee, RefereeJobFailed
from .referee.runner import DEFAULT_DIR as DEFAULT_REFEREE_DIR
from .referee.runner import DEFAULT_TIMEOUT, RefereeDir, RunnerError
from .referee.store import REFEREE, RefereeError, RefereeStore
from .store import (
    SETUP,
    Actor,
    GameStore,
    ParticipantContext,
    StoreBusy,
    StoreError,
)
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


class CampaignService:
    """One game store served to its joined sessions: replies, notifications,
    and in referee mode its jobs and conversation.

    Store calls run on worker threads, each opening its own short-lived
    GameStore, so the event loop never waits on SQLite and no SQLite connection
    is shared between threads. The game's module is loaded once, when the
    service is made. GameHost serves one of these on its own listener; a
    LobbyHost serves several, one per selected campaign.
    """

    def __init__(
        self,
        database,
        *,
        authenticated: bool = False,
        randint=None,
        write_timeout: float = WRITE_TIMEOUT_SECONDS,
        runtime=None,
        referee_executor=None,
        referee_dir=None,
        referee_timeout: float = DEFAULT_TIMEOUT,
        existing: bool = False,
        game_id: str | None = None,
        host_transfer=None,
    ):
        """`runtime`, if given, is an externally loaded module's runtime
        (tesserae.external). The game must then already exist and be bound to
        exactly that runtime; without one, the bundled catalog is used.
        `existing` never creates a game, even to refuse it, and `game_id`, if
        given, is the game the file must hold.

        A game in referee mode needs `referee_executor`: an async callable
        given each accepted Job, returning the reply to publish or raising
        RefereeJobFailed. The host stores and delivers what it returns.
        Without one, `referee_dir` (a folder made by `tesserae referee setup`)
        gives the game a CodexReferee (tesserae.referee.orchestrator), which
        the host also opens, lets recover an earlier run's job, and closes.

        A lobby host gives `host_transfer`, an async callable (session,
        HostRequest) returning (reply, transferred) for
        `campaign_host_transfer` (governance contract section 3); without
        it, as on a single-game host, there is no campaign host."""
        self.database = Path(database).resolve()
        self.authenticated = authenticated
        self._randint = randint
        self._write_timeout = write_timeout
        self._existing = existing
        # Joined connections, subscribed to revision notifications. Only this
        # process's commits are announced; other writers are not observed.
        self._sessions: set[_Session] = set()
        if runtime is not None:
            opened = GameStore.open_existing(self.database, runtime=runtime)
        elif existing:
            opened = GameStore.open_existing(self.database, load_runtime=True)
        else:
            opened = GameStore(self.database, load_runtime=True)
        with opened as store:
            if game_id is not None and store.game_id != game_id:
                raise StoreError("The database now holds a different game.")
            self.game_id = store.game_id
            # Exactly the game's bound module and providers, or StoreError:
            # never a fallback. Messages are parsed with its commands, and
            # every worker's store executes with it.
            self._runtime = store.runtime()
            self.referee_mode = RefereeStore(store).enabled()
        self.module = self._runtime.module
        # A character game (docs/designs/character_contract.md): each session
        # selects its own actor, and only a lobby host serves one.
        self.characters = self._runtime.characters is not None
        if self.module.requires_authentication and not authenticated:
            raise HostConfigError(
                f"this game's module, {self.module.id}, requires --authenticated, "
                "even on this computer"
            )
        if self.referee_mode and not authenticated:
            raise HostConfigError(
                "this game is in referee mode, which requires --authenticated"
            )
        self._check_serving()
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
        self._deliveries = None
        self._recovery = None
        self._notices: set[asyncio.Task] = set()  # actor and role notices
        self._host_transfer = host_transfer

    def _check_serving(self):
        """A subclass's own refusals, before any referee is set up."""

    # The interface a LobbyHost uses for its connections' sessions.

    def subscribe(self, session: "_Session"):
        self._sessions.add(session)

    def unsubscribe(self, session: "_Session"):
        self._sessions.discard(session)

    async def snapshot(self, session: "_Session") -> dict | None:
        return await self._snapshot(session)

    async def answer(self, session: "_Session", message) -> dict:
        return await self._answer(session, message)

    async def deliver_notifications(self, session: "_Session"):
        await self._deliver_notifications(session)

    def open_store(self) -> GameStore:
        return self._open()

    async def start(self):
        """Begin delivering, and open the referee (RunnerError if another host
        has this game). Call close() afterwards even if this raises."""
        self._outbox = asyncio.Queue()
        self._lifecycle = asyncio.Lock()
        self._deliveries = asyncio.create_task(self._deliver_outbox())
        if self._referee is not None:
            loop = asyncio.get_running_loop()
            self._referee.open(
                self._host_run,
                self._open,
                lambda revision: loop.call_soon_threadsafe(self._publish, revision),
            )
            self._recovery = asyncio.create_task(self._recover_referee())

    async def close(self):
        """Stop delivering, close the referee and finish in-flight store calls."""
        if self._deliveries is not None:
            self._deliveries.cancel()
        if self._recovery is not None:
            self._recovery.cancel()
            await asyncio.gather(self._recovery, return_exceptions=True)
        if self._referee is not None:  # its sockets take a moment to close
            await asyncio.get_running_loop().run_in_executor(None, self._referee.close)
        # Finish in-flight store calls: each commits or rolls back whole.
        self._executor.shutdown(wait=True)

    async def end_jobs(self):
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

    async def _snapshot(self, session: "_Session") -> dict | None:
        """The recipient's snapshot, or None if revoked: the one place that
        decides what a joined participant may see.

        The credential check, revision and module state are read together, and
        the module projects this participant's view from that state. A `dice`
        game has no view, so its snapshot is metadata only.
        """
        context = session.context
        # One guarded read of one committed state: the revision, the view, the
        # actor's sheet and the referee projection always agree.
        found = await self._call(
            _snapshot_parts,
            context,
            session.character_id,
            self.characters,
            self.referee_mode,
            session=session,
            role=True,
        )
        if found is None:
            return None
        current, view, referee, lost, role = found
        session.role = role  # what this connection is told, sent under its lock
        if lost:  # the selection is no longer the caller's: back in setup
            session.character_id = None
        actor, summary, sheet = view or (None, None, None)
        return snapshot_message(
            self.game_id,
            context.participant_id,
            current.revision,
            None if current.view is None else self.module,
            current.view,
            referee,
            current.name if self.authenticated else None,
            session.campaign,
            None if actor is None else actor_object(actor.mode, summary),
            sheet,
            role,
        )

    async def _actor(self, session: "_Session", *, keep_lost: bool = True):
        """The session's actor now, with its character's summary and sheet:
        (Actor, summary or None, sheet or None). A selection that is no longer
        the caller's (archived, relinked, unknown) ends here: the session is
        back in setup. With `keep_lost`, that returns None instead, so the
        request can say so (character_unavailable)."""
        found = await self._call(
            _actor_view, session.context, session.character_id, session=session
        )
        if found is None:
            session.character_id = None
            return None if keep_lost else (SETUP, None, None)
        return found

    def _publish(self, revision: int):
        """Announce a committed revision to every joined connection. Never waits."""
        for session in list(self._sessions):
            session.notify(revision)

    async def _deliver_notifications(self, session: "_Session"):
        """Send one session its newest pending revision, rechecking its credential.

        A revoked session is closed with 1008 and no error message, because no
        request is being answered (a lobby session may instead return to the
        lobby; see _Session.revoked).
        """
        try:
            while True:
                target = await session.next_pending()
                revision = await self._check_for_delivery(session)
                if revision is _UNCHECKED:
                    # Never leave a client silently behind: closing makes it
                    # reconnect and join for a fresh snapshot.
                    await session.unreachable()
                    return
                if revision is None:
                    await session.revoked()
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
                return await self._call(
                    GameStore.session_revision, session.context, session=session
                )
            except Exception:
                logger.exception("Could not check a session for a notification")
                if delay is None:
                    return _UNCHECKED
                await asyncio.sleep(delay)

    async def _answer(self, session: "_Session", message) -> dict:
        """Send exactly one reply to one message from a joined connection.

        The reply is read and sent under the session's send lock, so nothing
        sent to the connection (a notification, or a lobby session leaving
        its campaign) lands between that read and its reply. A `say` does the
        same in _say, which also sends its replayed messages after it.
        """
        parsed = None
        if not isinstance(message, bytes):
            try:
                parsed = parse_client_message(message, self._runtime)
            except ProtocolError as error:
                parsed = error
            if isinstance(parsed, SayRequest) and self.referee_mode:
                return await self._say(session, parsed)
        return await session.send_read(lambda: self._reply(session, message, parsed))

    async def _reply(self, session: "_Session", message, parsed) -> dict:
        """The reply to one message other than an accepted `say`."""
        checked = False  # whether the reply came from a store call
        if parsed is None:
            reply = _BINARY_REPLY
        elif isinstance(parsed, ProtocolError):
            reply = await self._rejection(session, parsed)
            checked = True
        elif isinstance(parsed, SyncRequest):
            return await self._sync(session)
        elif isinstance(parsed, HostRequest):
            return await self._host(session, parsed)
        elif isinstance(parsed, CharacterRequest) and self.characters:
            return await self._character(session, parsed)
        elif isinstance(parsed, CharacterRequest) or (
            isinstance(parsed, SayRequest)
            and parsed.actor is not None
            and not self.characters
        ):
            reply = _no_characters(parsed.request_id)
        elif isinstance(parsed, (SayRequest, RefereeStatusRequest, MessagesRequest)):
            if not self.referee_mode:
                reply = error_message(
                    "referee_off",
                    "This game is not in referee mode.",
                    request_id=getattr(parsed, "request_id", None),
                )
            else:
                reply = await self._referee_read(session, parsed)
                checked = True
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
        elif self.characters:
            found = await self._actor(session)
            if found is None:
                reply = _unavailable(parsed.request_id)
            else:
                reply = await self._execute(session.context, message, session, found[0])
            checked = True
        else:
            reply = await self._execute(session.context, message, session)
            checked = True
        if not checked and session.guard is not None:
            # A lobby session's every request rechecks access (lobby
            # contract section 3), even one answered without the store.
            reply = await self._recheck(session, reply)
        return reply

    async def _recheck(self, session: "_Session", reply: dict) -> dict:
        """`reply`, or session_revoked if the session's access has gone."""
        try:
            revision = await self._call(
                GameStore.session_revision, session.context, session=session
            )
        except Exception:
            return self._failure(reply["request_id"])
        return reply if revision is not None else session_error("session_revoked")

    async def _say(self, session: "_Session", request: SayRequest) -> dict:
        """Accept, replay or refuse one input and send its reply; an accepted
        input's job is running, and its message and `busy` queued, before
        that reply is sent, so a failed send never strands the job. Holding
        the sender's send lock throughout keeps the reply ahead of them.

        In a character game the `say` names the actor the client believes it
        has; the input is accepted for the connection's actual actor, and
        only if the two match (character contract section 9)."""
        replay = []
        if self.characters != (request.actor is not None):
            refusal = (
                error_message(
                    "invalid_request",
                    "In this game a say names its actor.",
                    request_id=request.request_id,
                )
                if self.characters
                else _no_characters(request.request_id)
            )
            return await session.send_read(lambda: self._recheck(session, refusal))

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
                        _submit,
                        session.context,
                        request,
                        self._host_run,
                        session.character_id if self.characters else _NO_ACTOR,
                        session=session,
                    )
                except Exception:
                    return self._failure(request.request_id)
                kind = outcome[0]
                if kind == "lost":  # the selected character is no longer theirs
                    session.character_id = None
                    return _unavailable(request.request_id)
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
                    replay.extend(
                        _wire(m, names, self.characters) for m in submission.messages
                    )
                return accepted_message(
                    request.request_id, submission.job.job_id, said["seq"]
                )

        reply = await session.send_read(decide)
        for message in replay:
            await session.send(message)
        return reply

    async def _character(self, session: "_Session", request: CharacterRequest) -> dict:
        """A character message in a character game (character contract
        section 9). Selection changes only this connection and runs no
        module code; lifecycle operations are the store's, and tell any other
        connection of the same participant whose character they changed."""
        context = session.context
        if request.kind == "characters":
            try:
                found = await self._call(
                    _characters_read, context, session.character_id, session=session
                )
            except Exception:
                return self._failure(None)
            if found is None:
                return session_error("session_revoked")
            listed, view = found
            if view is None:  # its selection is no longer the caller's
                session.character_id = None
                view = (SETUP, None, None)
            return characters_message(actor_object(view[0].mode, view[1]), listed)
        if request.kind == "character_select":
            if request.character_id is not None:
                try:
                    found = await self._call(
                        _actor_view, context, request.character_id, session=session
                    )
                except Exception:
                    return self._failure(None)
                if found is None:
                    if session.character_id == request.character_id:
                        session.character_id = None
                    return await self._recheck(session, _unavailable(None))
            session.character_id = request.character_id
            return await self._sync(session)
        operation = {
            "character_create": (GameStore.create_character, (request.name,)),
            "character_complete": (
                GameStore.complete_character,
                (request.character_id, request.draft_revision),
            ),
            "character_archive": (GameStore.archive_character, (request.character_id,)),
        }[request.kind]
        method, args = operation
        try:
            reply, committed = await self._call(
                method, context, request.request_id, *args, session=session
            )
        except Exception:
            return self._failure(request.request_id)
        if reply["type"] != "result":
            if (
                reply["type"] == "error"
                and reply["code"] == "character_unavailable"
                and session.character_id == request.character_id
            ):
                session.character_id = None
            return reply
        character = reply["result"]["character"]
        if committed:
            self._publish(reply["revision"])
            if request.kind != "character_create":
                self._tell_others(session, character, request.kind)
        try:
            # The actor after the operation: an archived selection is back in
            # setup here, and a completed one is in play.
            found = await self._actor(session, keep_lost=False)
        except Exception:
            return self._failure(request.request_id)
        return character_message(
            request.request_id,
            reply["revision"],
            character,
            actor_object(found[0].mode, found[1]),
        )

    async def _host(self, session: "_Session", request: HostRequest) -> dict:
        """A campaign host message (governance contract sections 4 and 7):
        checked and read inside the registry guard, refused to anyone but
        the campaign host. Host data is only ever this reply."""
        if request.kind == "campaign_host_transfer":
            return await self._transfer(session, request)
        try:
            found = await self._call(
                _host_read,
                session.context,
                request.kind,
                request.after_seq,
                self.characters,
                session=session,
                role=True,
            )
        except Exception:
            return self._failure(None)
        if found is None:
            return session_error("session_revoked")
        if found is _NOT_HOST:
            return _not_host(None)
        return found

    async def _transfer(self, session: "_Session", request: HostRequest) -> dict:
        if self._host_transfer is None or session.guard is None:
            return await self._recheck(session, _not_host(request.request_id))
        reply, transferred = await self._host_transfer(session, request)
        if reply["type"] == "role":
            session.role = reply["role"]
            # Every connection of this campaign learns its role now, after
            # any reply it is being sent; this one too if the reply was a
            # replay and it is not a guest now.
            for other in list(self._sessions):
                self.check_role(other)
            if transferred:
                logger.info("Campaign %s has a new campaign host", self.game_id)
        return reply

    def check_role(self, session: "_Session"):
        """Tell `session` its role if it has changed since it was last told,
        unsolicited and decided when sent (governance contract section 4)."""
        if session.role is _UNTOLD or not session.active:
            return
        session.role_dirty = True
        if session.role_task is None:
            task = asyncio.create_task(self._role_notices(session))
            session.role_task = task
            self._notices.add(task)
            task.add_done_callback(self._notices.discard)

    async def _role_notices(self, session: "_Session"):
        ended = None

        async def decide():
            nonlocal ended
            try:
                found = await self._call(
                    _session_role, session.context, session=session, role=True
                )
            except Exception:
                logger.exception("Could not check a session for a role notice")
                ended = session.unreachable
                return None
            if found is _REVOKED:  # its access has gone: nothing about roles
                ended = session.revoked
                return None
            if found is None or found == session.role:
                return None
            session.role = found
            return role_message(found)

        try:
            while session.role_dirty and session.active and ended is None:
                session.role_dirty = False
                await session.send_decided(decide)
            if ended is not None:
                await ended()
        except (ConnectionClosed, _SlowConsumer):
            pass
        finally:
            session.role_task = None

    def _saw_role(self, session: "_Session", role):
        """A guarded call saw `session`'s role; if it is not what the
        connection was told, a role notice follows."""
        if role is not _UNSEEN and session.role is not _UNTOLD and role != session.role:
            self.check_role(session)

    def _tell_others(self, session: "_Session", character: dict, kind: str):
        """Every other connection of this participant with this character
        selected learns that it was completed (now in play) or archived (back
        in setup), unsolicited, after any reply it is being sent. Each notice
        is decided when it is sent (`_actor_notice`), never here."""
        for other in list(self._sessions):
            if (
                other is not session
                and other.context.participant_id == session.context.participant_id
                and other.character_id == character["character_id"]
            ):
                task = asyncio.create_task(
                    self._actor_notice(other, character["character_id"])
                )
                self._notices.add(task)
                task.add_done_callback(self._notices.discard)

    async def _actor_notice(self, session: "_Session", character_id: str):
        """Tell `session` what became of its character, under its send lock:
        only if that is still its selection, only while its access holds,
        and only what is true now (character contract section 9)."""
        ended = None

        async def decide():
            nonlocal ended
            if session.character_id != character_id:
                return None  # it has chosen something else since
            try:
                found = await self._call(
                    _notice_view, session.context, character_id, session=session
                )
            except Exception:
                logger.exception("Could not check a session for an actor notice")
                ended = session.unreachable
                return None
            if found is None:  # its access has gone: nothing about the character
                ended = session.revoked
                return None
            actor, summary, _ = found
            if actor.mode == "creation":
                return None  # still a draft: nothing to tell
            if actor.mode == "setup":
                session.character_id = None
            reason = "completed" if actor.mode == "play" else "archived"
            return actor_notice(reason, actor_object(actor.mode, summary))

        try:
            await session.send_decided(decide)
            if ended is not None:
                await ended()
        except (ConnectionClosed, _SlowConsumer):
            pass

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

    async def _referee_read(self, session: "_Session", request) -> dict:
        """`referee_status` or `messages`, for the caller only."""
        context = session.context
        try:
            if isinstance(request, RefereeStatusRequest):
                actor = None
                if self.characters:
                    actor = (await self._actor(session, keep_lost=False))[0]
                projection = await self._call(
                    _referee_projection, context, actor, session=session
                )
                if projection is not None:
                    return referee_status_message(projection)
            else:
                visible = await self._call(
                    _visible_messages,
                    context,
                    request.after_seq,
                    self.characters,
                    session=session,
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
                    _wire(m, names, self.characters)
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
                    await session.unreachable()
                    return
                if revision is None:  # a revoked credential gets nothing
                    await session.revoked()
                    return
            for message in messages:
                await session.send(message)
        except (ConnectionClosed, _SlowConsumer):
            pass

    async def _rejection(self, session: "_Session", error: ProtocolError):
        """An invalid message's error, carrying the revision only if still joined."""
        try:
            revision = await self._call(
                GameStore.session_revision, session.context, session=session
            )
        except Exception:
            return self._failure(error.request_id)
        if revision is None:
            return session_error("session_revoked")
        return error.to_message(revision=revision)

    async def _sync(self, session: "_Session") -> dict:
        try:
            snapshot = await self._snapshot(session)
        except Exception:
            return self._failure(None)
        return snapshot or session_error("session_revoked")

    async def _execute(
        self,
        context: ParticipantContext,
        raw: str,
        session: "_Session | None" = None,
        actor: Actor | None = None,
    ) -> dict:
        try:
            reply, committed = await self._call(
                _execute_as, context, raw, actor, session=session
            )
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

    async def _call(
        self, method, *args, session: "_Session | None" = None, role: bool = False
    ):
        """`method(store, *args)` on a worker thread, with a fresh store.

        A lobby session's call runs inside its registry guard (lobby contract
        section 6). If the guard refuses, the session's context is replaced by
        one no check accepts, so `method` answers as it would for a revoked
        session, and the session records why. With `role`, `method` is also
        given `role=`: the session's campaign role read by that guard ("host",
        "guest", or None with no campaign host, as on a single-game host).
        """
        guard = None if session is None else session.guard
        extra = {"role": None} if role else {}

        def work():
            if guard is None:
                return self._in_store(method, args, extra=extra), _UNSEEN
            checked = guard()
            with checked as allowed:
                if not allowed:
                    denied = replace(session.context, link_id=_NO_LINK)
                    args_ = tuple(denied if a is session.context else a for a in args)
                    return self._in_store(method, args_, extra=extra), _UNSEEN
                if role:
                    extra["role"] = checked.role
                return self._in_store(method, args, session, extra), checked.role

        loop = asyncio.get_running_loop()
        result, seen = await loop.run_in_executor(self._executor, work)
        if session is not None:
            self._saw_role(session, seen)
        return result

    def _in_store(self, method, args, session=None, extra=None):
        try:
            store = self._open()
        except StoreError as error:
            if session is not None and not isinstance(error, StoreBusy):
                session.lost = "campaign_unavailable"  # missing or not a game
            raise
        with store:
            if store.game_id != self.game_id:
                if session is not None:
                    session.lost = "campaign_unavailable"
                raise StoreError("The database now holds a different game.")
            return method(store, *args, **(extra or {}))

    def _open(self) -> GameStore:
        if self._existing:
            return GameStore.open_existing(
                self.database, randint=self._randint, runtime=self._runtime
            )
        return GameStore(self.database, randint=self._randint, runtime=self._runtime)


class GameHost(CampaignService):
    """Serves one game store to any number of local connections, on its own
    listener, in development or authenticated mode (docs/protocol.md)."""

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
        """See CampaignService for the game's options.

        `browser_assets` (tesserae.web.load_assets) also serves the browser
        page from this port: plain loopback, authenticated and in referee mode
        only (docs/designs/browser_contract.md sections 2 and 3). A browser
        game is an existing referee game: one is never created, even to
        refuse it."""
        self.host = host
        self._ssl = ssl_context
        self._join_timeout = join_timeout
        self._browser_assets = browser_assets
        super().__init__(
            database,
            authenticated=authenticated,
            randint=randint,
            write_timeout=write_timeout,
            runtime=runtime,
            referee_executor=referee_executor,
            referee_dir=referee_dir,
            referee_timeout=referee_timeout,
            existing=browser_assets is not None,
        )
        self._requests = RequestPolicy(browser_assets)

    def _check_serving(self):
        if self.characters:
            raise HostConfigError(
                "this game uses characters; serve it from a lobby host "
                "(serve --registry)"
            )
        if self._browser_assets is None:
            return
        if not self.authenticated:
            raise HostConfigError("--browser requires --authenticated")
        if not self.referee_mode:
            raise HostConfigError(
                "--browser needs a game in referee mode (tesserae referee enable)"
            )
        if self._ssl is not None or self.host != HOST:
            raise HostConfigError(
                f"--browser serves this computer only, on {HOST} without TLS"
            )

    async def run(self, port: int, stop: asyncio.Future, on_ready=None):
        """Serve until `stop` completes, then close connections and the store."""
        try:
            await self.start()
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
                await self.end_jobs()
        finally:
            await self.close()

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
            reply = await session.send_read(lambda: self._join_snapshot(session))
        except BaseException:
            self._sessions.discard(session)  # it never became a joined session
            raise
        if reply["type"] != "snapshot":
            self._sessions.discard(session)
            await connection.close(SESSION_CLOSE_CODE, reply["code"])
            return None
        return session

    async def _join_snapshot(self, session: "_Session") -> dict:
        try:
            snapshot = await self._snapshot(session)
        except Exception:
            logger.exception("Could not check a join")
            return _JOIN_FAILED
        return snapshot or session_error("authentication_failed")

    async def _refuse(self, connection: ServerConnection, reply: dict) -> None:
        await connection.send(encode_message(reply))
        await connection.close(SESSION_CLOSE_CODE, reply["code"])
        return None

    async def _welcome(self) -> dict:
        try:
            return welcome_message(await self._call(GameStore.revision))
        except Exception:
            logger.exception("Could not read the game revision")
            return error_message("internal_error", "The host could not read the game.")


_NO_ACTOR = object()  # a game without characters: inputs have no actor


def _submit(store: GameStore, context, request: SayRequest, host_run: str,
            selection=_NO_ACTOR) -> tuple:  # fmt: skip
    referee = RefereeStore(store)
    actor = None
    if selection is not _NO_ACTOR:
        # The connection's actor now; the store checks it again inside the
        # acceptance transaction, so a completion racing this cannot slip by.
        found = _actor_view(store, context, selection)
        if found is None:
            return ("lost",)
        actor = found[0]
        if (actor.character_id, actor.mode) != request.actor:
            return (
                "error",
                "character_changed",
                "Your character has changed; check your characters and try again.",
            )
    try:
        submission = referee.submit(
            context, request.request_id, request.text, request.private, host_run,
            actor=actor,
        )  # fmt: skip
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


def _referee_projection(store: GameStore, context, actor=None) -> dict | None:
    if store.session_revision(context) is None:
        return None
    return RefereeStore(store).projection(context.participant_id, actor)


def _visible_messages(store: GameStore, context, after_seq: int,
                      characters: bool = False) -> list | None:  # fmt: skip
    if store.session_revision(context) is None:
        return None
    names = _names(store)
    return [
        _wire(m, names, characters)
        for m in RefereeStore(store).messages_for(context.participant_id, after_seq)
    ]


def _names(store: GameStore) -> dict:
    """Participant and character IDs to their names (the two never clash:
    p_ and c_)."""
    names = {p.participant_id: p.name for p in store.participants()}
    names.update((c.character_id, c.name) for c in store.inspect_characters())
    return names


def _wire(message: dict, names: dict, characters: bool = False) -> dict:
    """A stored message as its recipients see it. In a character game a
    player's message names the character they spoke as, and a creation
    exchange is marked (character contract section 9)."""
    sender = message["sender"]
    extra = {}
    character_id = message.get("character_id")
    if characters and sender != REFEREE:
        extra["character"] = None if character_id is None else names.get(character_id)
    if message.get("creation"):
        extra["creation"] = {
            "character_id": character_id,
            "name": names.get(character_id),
        }
    return conversation_message(
        message["seq"],
        None if sender == REFEREE else names.get(sender, sender),
        "table" if message["audience"] == "table" else "private",
        message["text"],
        **extra,
    )


def _actor_view(store: GameStore, context, character_id):
    """(Actor, summary, sheet) for a connection's selection, or None if it is
    not the caller's unarchived character (or the caller's authority has
    ended). No selection is setup."""
    if character_id is None:
        return SETUP, None, None
    found = store.character_sheet(context, character_id)
    if found is None:
        return None
    record, sheet = found
    mode = "creation" if record.phase == "draft" else "play"
    return Actor(character_id, mode), record.summary(), sheet


def _characters_read(store: GameStore, context, character_id):
    """The caller's characters and their actor view, or None if revoked."""
    listed = store.characters(context)
    if listed is None:
        return None
    return [c.summary() for c in listed], _actor_view(store, context, character_id)


def _execute_as(store: GameStore, context, raw, actor):
    return store.execute_command(context, raw, actor=actor)


def _no_characters(request_id) -> dict:
    return error_message(
        "characters_unsupported", "This game has no characters.", request_id=request_id
    )


def _unavailable(request_id) -> dict:
    return error_message(
        "character_unavailable",
        "That character is not available to you.",
        request_id=request_id,
    )


def _snapshot_parts(store: GameStore, context, character_id, characters: bool,
                    referee_mode: bool, *, role=None):  # fmt: skip
    """Everything a snapshot holds, from one committed state: the view, the
    actor view (setup if the selection is no longer the caller's, with
    `lost` set), the referee projection and the guard's `role`. None if
    revoked."""
    with store.snapshot():
        current = store.participant_view(context)
        if current is None:
            return None
        view, lost = None, False
        if characters:
            view = _actor_view(store, context, character_id)
            if view is None:
                view, lost = (SETUP, None, None), True
        referee = None
        if referee_mode:
            actor = None if view is None else view[0]
            referee = _referee_projection(store, context, actor)
            if referee is None:
                return None
        return current, view, referee, lost, role


def _session_role(store: GameStore, context, *, role):
    """For a role notice: the guard's role, or _REVOKED if access has gone."""
    return _REVOKED if store.session_revision(context) is None else role


def _host_read(store: GameStore, context, kind: str, after_seq, characters: bool,
               *, role):  # fmt: skip
    """`host_view` or a `host_messages` page, from one committed state;
    None if revoked, _NOT_HOST unless `role` is the campaign host's."""
    with store.snapshot():
        if store.session_revision(context) is None:
            return None
        if role != "host":
            return _NOT_HOST
        referee = RefereeStore(store)
        names = _names(store)
        if kind == "host_messages":
            page, next_after = referee.transcript(after_seq)
            return host_messages_message(
                [_host_wire(m, names, characters) for m in page], next_after
            )
        runtime, state = store.runtime(), store.inspect_state()
        inspect = runtime.referee_context(state)
        participants = store.participants()
        if inspect is None:  # as the referee sees it: each participant's view
            inspect = {
                "views": {
                    p.participant_id: runtime.view(state, p.participant_id)
                    for p in participants
                }
            }
        listed = []
        for character in store.inspect_characters():
            entry = character.summary()
            entry["participant_id"] = character.participant_id
            entry["introduced"] = character.introduced_at is not None
            entry["sheet"] = (
                None
                if character.phase == "archived"
                else runtime.character_sheet(state, character.character_id)
            )
            listed.append(entry)
        return host_view_message(
            {
                "revision": store.revision(),
                "inspect": inspect,
                "characters": listed,
                "participants": [
                    {"participant_id": p.participant_id, "name": p.name}
                    for p in participants
                ],
                "pending_choices": referee.all_choices(every=True),
                "rulings": referee.rulings(),
                "policy": None,  # the settings document (GOV-3)
            }
        )


def _host_wire(message: dict, names: dict, characters: bool) -> dict:
    """A stored message as the campaign host reads it: as its recipients
    see it, and to whom a private one went."""
    wired = _wire(message, names, characters)
    if message["audience"] != "table":
        wired["to"] = [
            {"participant_id": p, "name": names.get(p, p)} for p in message["audience"]
        ]
    return wired


def _not_host(request_id) -> dict:
    return error_message(
        "not_campaign_host",
        "Only the campaign host can do that.",
        request_id=request_id,
    )


def _notice_view(store: GameStore, context, character_id):
    """For an actor notice: the character's actor view now, setup if it is no
    longer the caller's, or None if the caller's access has ended. The sheet
    read checks access in the same read as the character."""
    found = _actor_view(store, context, character_id)
    if found is not None:
        return found
    if store.session_revision(context) is None:
        return None
    return SETUP, None, None


_UNCHECKED = object()
_UNSEEN = object()  # a call that read no role: no guard, or access refused
_UNTOLD = object()  # a session not yet sent its role in a snapshot
_REVOKED = object()
_NOT_HOST = object()
_NO_LINK = -1  # a host link ID that never exists, so no check accepts it
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

    def __init__(
        self,
        connection,
        context: ParticipantContext,
        write_timeout: float,
        *,
        lock: asyncio.Lock | None = None,
        guard=None,
        campaign: dict | None = None,
    ):
        """A lobby host's session shares its connection's `lock` with the
        lobby, and has a registry `guard` and the selected `campaign`."""
        self.connection = connection
        self.context = context
        self.guard = guard
        self.campaign = campaign
        # The connection's own character selection in a character game: an
        # ID, or None for setup. Never persisted (character contract section 9).
        self.character_id = None
        self.active = True  # False once the connection has left this game
        self.lost = None  # why a guarded check refused, set by a worker
        # The campaign role this connection was last told (governance
        # contract section 4), and its pending role notice.
        self.role = _UNTOLD
        self.role_dirty = False
        self.role_task = None
        self._write_timeout = write_timeout
        self._lock = lock or asyncio.Lock()
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

    async def send_decided(self, decide):
        """Under the send lock, await `decide()` and send what it returns, if
        anything: for an unsolicited message that must hold when it is sent."""
        async with self._lock:
            message = await decide()
            if message is not None:
                await self._send(message)

    async def send_read(self, read) -> dict:
        """Await `read()` and send the message it returns, holding back
        notifications from before the read until after the send."""
        async with self._lock:
            message = await read()
            await self._send(message)
            return message

    async def revoked(self):
        """A check outside any request found the session revoked."""
        await self.connection.close(SESSION_CLOSE_CODE, "session_revoked")

    async def unreachable(self):
        """A check outside any request could not complete."""
        await self.connection.close(FAILURE_CLOSE_CODE, "internal error")

    def _outgoing(self, message: dict) -> dict | None:
        """The message to send instead, or None to send nothing."""
        return message

    async def _send(self, message: dict):
        if not self.active:
            return  # this connection has left the game: nothing more from it
        message = self._outgoing(message)
        if message is None:
            return
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
