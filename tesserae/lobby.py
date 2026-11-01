"""Registry-backed foreground host: one loopback listener, many campaigns.

A connection logs in with a host token, sees only the campaigns its player
may enter, and selects one at a time. Inside a campaign it is exactly a joined
connection of that game (tesserae.server.CampaignService), as the participant
linked to its host player. Every protected operation runs inside the
registry's short authorization guard, so access revocation is ordered against
game commits. See docs/designs/lobby_contract.md.
"""

import asyncio
import json
import logging
import signal
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .browser import RequestPolicy
from .external import load_module_dir
from .modules import ModuleError
from .protocol import (
    JOIN_TIMEOUT_SECONDS,
    LOBBY_TYPES,
    LeaveRequest,
    LoginRequest,
    ProtocolError,
    SelectRequest,
    encode_message,
    error_message,
    is_valid_request_id,
    left_message,
    lobby_error,
    lobby_message,
    parse_lobby_message,
    session_error,
)
from .referee.runner import DEFAULT_DIR as DEFAULT_REFEREE_DIR
from .referee.runner import DEFAULT_TIMEOUT, RunnerError
from .registry import CampaignUnavailable, CredentialRevoked, HostRegistry
from .server import (
    _BINARY_REPLY,
    HOST,
    SESSION_CLOSE_CODE,
    SLOW_CLOSE_CODE,
    WRITE_TIMEOUT_SECONDS,
    CampaignService,
    HostConfigError,
    _Session,
    _SlowConsumer,
)
from .store import GameStore, StoreError

REGISTRY_THREADS = 4
# Messages a campaign sends without being asked; every other one is a reply.
_UNSOLICITED = frozenset({"revision", "message", "referee"})
_CANNOT_OPEN = "That campaign cannot be opened right now; ask the host."
_LOGIN_FAILED = error_message("internal_error", "The host could not check the login.")
_HOST_FAILED = "The host failed; nothing was changed. Try again."

_CAMPAIGN_GONE = error_message(  # a session error: no request ID
    "campaign_unavailable", "That campaign is no longer available to you."
)

logger = logging.getLogger("tesserae.lobby")


class LobbyHost:
    """Serves the registry's campaigns to logged-in host players.

    `runtimes` are externally loaded module runtimes (tesserae.external),
    offered to every campaign bound to their module. Each referee-mode
    campaign gets a CodexReferee in `referee_dir`, or, for tests,
    `referee_executor_for(campaign_id)`'s executor.
    """

    def __init__(
        self,
        registry,
        *,
        runtimes=(),
        referee_dir=None,
        referee_timeout: float = DEFAULT_TIMEOUT,
        referee_executor_for=None,
        randint=None,
        join_timeout: float = JOIN_TIMEOUT_SECONDS,
        write_timeout: float = WRITE_TIMEOUT_SECONDS,
    ):
        self.registry = Path(registry).expanduser().resolve()
        with HostRegistry(self.registry) as opened:  # RegistryError if not one
            self.host_id = opened.host_id
        self._runtimes = {}
        for runtime in runtimes:
            if runtime.module.id in self._runtimes:
                raise HostConfigError(
                    f"two module packages provide module {runtime.module.id}"
                )
            self._runtimes[runtime.module.id] = runtime
        self._referee_dir = referee_dir
        self._referee_timeout = referee_timeout
        self._executor_for = referee_executor_for
        self._randint = randint
        self._join_timeout = join_timeout
        self._write_timeout = write_timeout
        self._services: dict[str, CampaignService] = {}
        self._starting: dict[str, asyncio.Lock] = {}
        self._closing = False
        self._requests = RequestPolicy(None)  # no page: any Origin gets 403
        self._executor = ThreadPoolExecutor(
            REGISTRY_THREADS, thread_name_prefix="tesserae-registry"
        )

    async def run(self, port: int, stop: asyncio.Future, on_ready=None):
        """Serve until `stop` completes, then end every campaign's jobs and
        close them."""
        try:
            async with serve(
                self._handle, HOST, port, process_request=self._requests.process_request
            ) as server:
                address, bound_port = server.sockets[0].getsockname()[:2]
                self._requests.bind(bound_port)
                if on_ready is not None:
                    on_ready(f"ws://{address}:{bound_port}/")
                await stop
                self._closing = True
                for service in list(self._services.values()):
                    await service.end_jobs()
        finally:
            self._closing = True
            for service in list(self._services.values()):
                await service.close()
            self._executor.shutdown(wait=True)

    # --- One connection -------------------------------------------------------

    async def _handle(self, connection: ServerConnection):
        try:
            client = await self._login(connection)
            if client is None:
                return
            try:
                async for message in connection:
                    if not await self._answer(client, message):
                        return
            finally:
                client.end_selection()
        except (ConnectionClosed, _SlowConsumer):
            pass

    async def _login(self, connection: ServerConnection) -> "_Client | None":
        """Admit a connection by its host token, or send one error and close.
        Nothing describing the host or a campaign is sent before that."""
        try:
            first = await asyncio.wait_for(connection.recv(), self._join_timeout)
        except TimeoutError:
            return await _refuse(connection, lobby_error("join_timeout"))
        if isinstance(first, bytes):
            return await _refuse(connection, _BINARY_REPLY)
        try:
            request = parse_lobby_message(first)
        except ProtocolError as error:
            return await _refuse(connection, error.to_message())
        if not isinstance(request, LoginRequest):
            return await _refuse(connection, lobby_error("login_required"))
        try:
            found = await self._registry_call(_authenticate, request.token)
        except Exception:
            logger.exception("Could not check a login")  # the token is never logged
            return await _refuse(connection, _LOGIN_FAILED)
        if found is None:
            return await _refuse(connection, lobby_error("authentication_failed"))
        context, name, campaigns = found
        client = _Client(connection, context, name, self._write_timeout)
        await client.send(lobby_message(context.player_id, name, campaigns, None))
        return client

    async def _answer(self, client: "_Client", raw) -> bool:
        """Send exactly one reply to one message; False once the connection
        has been closed."""
        if isinstance(raw, bytes):
            if client.selection is None:
                return await self._refusal(client, _BINARY_REPLY)
            return await self._in_campaign(client, raw)
        kind = _type_of(raw)
        if kind not in LOBBY_TYPES and kind != "join" and client.selection is not None:
            return await self._in_campaign(client, raw)
        try:
            request = parse_lobby_message(raw)
        except ProtocolError as error:
            return await self._refusal(client, error.to_message())
        if request is None:
            if kind == "join":
                return await self._refusal(client, lobby_error("already_logged_in"))
            return await self._refusal(client, _no_campaign(raw))  # in the lobby
        if isinstance(request, LoginRequest):
            return await self._refusal(client, lobby_error("already_logged_in"))
        if isinstance(request, SelectRequest):
            if client.selection is not None:
                return await self._refusal(client, lobby_error("campaign_selected"))
            return await self._select(client, request.campaign_id)
        if isinstance(request, LeaveRequest):
            if client.selection is None:
                return await self._refusal(client, lobby_error("no_campaign"))
            async with client.lock:
                client.end_selection()  # nothing from it after this point
        return await self._send_lobby(client)

    async def _refusal(self, client: "_Client", reply: dict) -> bool:
        """A lobby error, unless the host credential has gone: then
        session_revoked and close, as for any request (contract section 3)."""
        try:
            active = await self._registry_call(_credential_active, client.context)
        except Exception:
            logger.exception("Could not recheck a host credential")
            await client.send(error_message("internal_error", _HOST_FAILED))
            return True
        if not active:
            return await client.revoke()
        await client.send(reply)
        return True

    async def _send_lobby(self, client: "_Client") -> bool:
        """The reply to `lobby` and `leave`: the catalog and the selection."""
        try:
            _, campaigns = await self._registry_call(_catalog, client.context)
        except CredentialRevoked:
            return await client.revoke()
        except Exception:
            logger.exception("Could not read a player's campaigns")
            await client.send(error_message("internal_error", _HOST_FAILED))
            return True
        selected = None if client.selection is None else client.selection.campaign
        await client.send(
            lobby_message(client.context.player_id, client.name, campaigns, selected)
        )
        return True

    async def _select(self, client: "_Client", campaign_id: str) -> bool:
        try:
            record = await self._registry_call(_authorized, client.context, campaign_id)
        except CredentialRevoked:
            return await client.revoke()
        except CampaignUnavailable:
            await client.send(lobby_error("campaign_unavailable"))
            return True
        except Exception:
            logger.exception("Could not check a campaign selection")
            await client.send(error_message("internal_error", _HOST_FAILED))
            return True
        service = await self._service(campaign_id, record["path"])
        if service is None:
            await client.send(lobby_error("campaign_unavailable", _CANNOT_OPEN))
            return True
        guard = partial(_Guard, self.registry, client.context, campaign_id)
        try:
            context = await asyncio.get_running_loop().run_in_executor(
                self._executor,
                partial(self._admit, guard, client, campaign_id, service),
            )
        except CredentialRevoked:
            return await client.revoke()
        except CampaignUnavailable:
            await client.send(lobby_error("campaign_unavailable"))
            return True
        except StoreError as error:
            logger.warning("Campaign %s cannot admit players: %s", campaign_id, error)
            await client.send(lobby_error("campaign_unavailable", _CANNOT_OPEN))
            return True
        except Exception:
            logger.exception("Could not admit a player to a campaign")
            await client.send(error_message("internal_error", _HOST_FAILED))
            return True
        campaign = {"campaign_id": campaign_id, "name": record["name"]}
        session = _CampaignSession(client, service, context, guard, campaign)
        service.subscribe(session)  # before the snapshot is read: no gap
        client.selection = session
        reply = await session.send_read(lambda: _first_snapshot(service, session))
        if session.closing:
            return await client.close_revoked()
        if reply["type"] != "snapshot" or not session.active:
            session.detach()  # still in the lobby (or already back in it)
            return True
        client.notifier = asyncio.create_task(service.deliver_notifications(session))
        return True

    async def _in_campaign(self, client: "_Client", raw) -> bool:
        session = client.selection
        await session.service.answer(session, raw)
        if session.closing:
            return await client.close_revoked()
        return True

    # --- Campaign runtimes -----------------------------------------------------

    async def _service(self, campaign_id: str, path: str) -> CampaignService | None:
        """The campaign's running service, starting it on first use; None if
        it cannot be opened now (logged, never retried automatically)."""
        lock = self._starting.setdefault(campaign_id, asyncio.Lock())
        async with lock:
            service = self._services.get(campaign_id)
            if service is not None or self._closing:
                return service
            loop = asyncio.get_running_loop()
            try:
                service = await loop.run_in_executor(
                    self._executor, self._build, campaign_id, path
                )
            except Exception as error:
                logger.warning("Campaign %s cannot be opened: %s", campaign_id, error)
                return None
            try:
                await service.start()
            except Exception as error:
                logger.warning("Campaign %s cannot be served: %s", campaign_id, error)
                await service.close()
                return None
            self._services[campaign_id] = service
            return service

    def _build(self, campaign_id: str, path: str) -> CampaignService:
        with GameStore.open_existing(path) as store:
            if store.game_id != campaign_id:
                raise StoreError("The registered file now holds a different game.")
            module_id = store.binding().module_id
        executor = (
            None if self._executor_for is None else self._executor_for(campaign_id)
        )
        return CampaignService(
            path,
            authenticated=True,
            randint=self._randint,
            write_timeout=self._write_timeout,
            runtime=self._runtimes.get(module_id),
            referee_executor=executor,
            referee_dir=self._referee_dir,
            referee_timeout=self._referee_timeout,
            existing=True,
            game_id=campaign_id,
        )

    def _admit(self, guard, client: "_Client", campaign_id, service):
        """On a worker thread: the player's participant context in the
        campaign, found or created inside the registry guard."""
        checked = guard()
        with checked as allowed:
            if not allowed:
                raise checked.lost_error
            with service.open_store() as store:
                if store.game_id != campaign_id:
                    raise StoreError("The registered file now holds a different game.")
                return store.admit_host_player(
                    self.host_id, client.context.player_id, client.name
                )

    async def _registry_call(self, function, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, partial(function, self.registry, *args)
        )


# --- Worker-thread registry reads ---------------------------------------------


def _authenticate(path, token):
    with HostRegistry(path) as registry:
        context = registry.authenticate(token)
        if context is None:
            return None
        try:
            name, campaigns = _catalog(path, context, registry)
        except CredentialRevoked:  # rotated in between
            return None
        return context, name, campaigns


def _catalog(path, context, registry=None):
    if registry is None:
        with HostRegistry(path) as opened:
            return _catalog(path, context, opened)
    return registry.player_name(context), registry.visible_campaigns(context)


def _credential_active(path, context) -> bool:
    with HostRegistry(path) as registry:
        return registry.credential_active(context)


def _authorized(path, context, campaign_id) -> dict:
    with HostRegistry(path) as registry:
        with registry.authorize_campaign(context, campaign_id) as record:
            return record


class _Guard:
    """The registry's authorization guard for one campaign operation, on a
    worker thread: entering yields whether access holds, and records why not
    on the class instance for the caller."""

    def __init__(self, path, context, campaign_id, session=None):
        self._path, self._context, self._campaign = path, context, campaign_id
        self._session = session
        self._registry = None
        self._authorized = None
        self.lost_error = None

    def __enter__(self) -> bool:
        self._registry = HostRegistry(self._path)
        try:
            self._authorized = self._registry.authorize_campaign(
                self._context, self._campaign
            )
            self._authorized.__enter__()
            return True
        except (CredentialRevoked, CampaignUnavailable) as error:
            self._authorized = None
            self.lost_error = error
            if self._session is not None:
                self._session.lost = (
                    "session_revoked"
                    if isinstance(error, CredentialRevoked)
                    else "campaign_unavailable"
                )
            return False
        except BaseException:
            self._registry.close()
            raise

    def __exit__(self, *exc_info):
        try:
            if self._authorized is not None:
                return self._authorized.__exit__(*exc_info)
            return False
        finally:
            self._registry.close()


# --- Connections and their selection ------------------------------------------


class _Client:
    """One logged-in connection: its host context and at most one selection.
    Lobby replies and the selection's messages share one send lock."""

    def __init__(self, connection, context, name: str, write_timeout: float):
        self.connection = connection
        self.context = context
        self.name = name
        self.lock = asyncio.Lock()
        self.selection: _CampaignSession | None = None
        self.notifier: asyncio.Task | None = None
        self._write_timeout = write_timeout

    async def send(self, message: dict):
        async with self.lock:
            await self.send_locked(message)

    async def send_locked(self, message: dict):
        try:
            await asyncio.wait_for(
                self.connection.send(encode_message(message)), self._write_timeout
            )
        except TimeoutError:
            logger.warning("Closing a connection that stopped reading")
            try:
                await asyncio.wait_for(
                    self.connection.close(SLOW_CLOSE_CODE, "slow consumer"), 1
                )
            except TimeoutError:
                self.connection.transport.abort()
            raise _SlowConsumer from None

    def end_selection(self):
        if self.selection is not None:
            self.selection.detach()

    async def revoke(self) -> bool:
        """The host credential is gone: the final reply, then close."""
        await self.send(lobby_error("session_revoked"))
        return await self.close_revoked()

    async def close_revoked(self) -> bool:
        self.end_selection()
        await self.connection.close(SESSION_CLOSE_CODE, "session_revoked")
        return False


class _CampaignSession(_Session):
    """A client's session in its selected campaign. Lost access turns the
    campaign's revoked-session answers into the lobby's (contract section 6)."""

    def __init__(self, client: _Client, service, context, guard, campaign: dict):
        super().__init__(
            client.connection,
            context,
            client._write_timeout,
            lock=client.lock,
            guard=lambda: guard(session=self),
            campaign=campaign,
        )
        self.client = client
        self.service = service
        self.closing = False  # the host credential is gone: close after this reply

    def detach(self):
        """Leave the campaign: nothing more from it reaches this connection."""
        self.active = False
        self.service.unsubscribe(self)
        if self.client.selection is self:
            self.client.selection = None
            notifier, self.client.notifier = self.client.notifier, None
            if notifier is not None and notifier is not asyncio.current_task():
                notifier.cancel()

    def _outgoing(self, message: dict) -> dict | None:
        if message["type"] != "error":
            return message
        code = message["code"]
        if code == "session_revoked" and self.lost == "session_revoked":
            self.closing = True
            return lobby_error("session_revoked")
        if code == "session_revoked" or (
            code == "internal_error" and self.lost == "campaign_unavailable"
        ):
            # The grant, the campaign, its file or the participant link is
            # gone; the host credential is not.
            self.detach()
            return _CAMPAIGN_GONE
        return message

    async def _send(self, message: dict):
        if not self.active and message["type"] not in _UNSOLICITED:
            # A request still being answered when the connection left the
            # campaign: it still gets exactly one reply, the lobby's, and
            # nothing of the campaign's.
            await self.client.send_locked(_CAMPAIGN_GONE)
            return
        await super()._send(message)

    async def revoked(self):
        if self.lost == "session_revoked":
            self.detach()
            await self.connection.close(SESSION_CLOSE_CODE, "session_revoked")
        else:
            await self._left()

    async def unreachable(self):
        if self.lost == "campaign_unavailable":
            await self._left()
        else:
            await super().unreachable()

    async def _left(self):
        """Back to the lobby, told with one unsolicited `left`."""
        async with self._lock:
            if not self.active:
                return
            self.detach()
            await self.client.send_locked(
                left_message(self.campaign["campaign_id"], "campaign_unavailable")
            )


async def _first_snapshot(service: CampaignService, session: _CampaignSession) -> dict:
    try:
        snapshot = await service.snapshot(session)
    except Exception:
        logger.exception("Could not read a campaign snapshot")
        if session.lost == "campaign_unavailable":
            return session_error("session_revoked")  # becomes campaign_unavailable
        return error_message("internal_error", _HOST_FAILED)
    return snapshot or session_error("session_revoked")


async def _refuse(connection: ServerConnection, reply: dict) -> None:
    await connection.send(encode_message(reply))
    await connection.close(SESSION_CLOSE_CODE, reply["code"])
    return None


def _type_of(raw: str):
    """The message's type, only to route it; parsing happens where it goes."""
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    kind = data.get("type") if isinstance(data, dict) else None
    return kind if isinstance(kind, str) else None


def _no_campaign(raw: str) -> dict:
    try:
        request_id = json.loads(raw).get("request_id")
    except (ValueError, RecursionError, AttributeError):
        request_id = None
    return error_message(
        "no_campaign",
        "Select a campaign first.",
        request_id=request_id if is_valid_request_id(request_id) else None,
    )


# --- The `tesserae serve --registry` command -----------------------------------


def run_foreground(
    registry: str,
    port: int,
    *,
    module_dirs=(),
    referee_dir=None,
    referee_timeout: float = DEFAULT_TIMEOUT,
) -> int:
    """Serve the registry's campaigns until Ctrl-C, then exit cleanly."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    runtimes = []
    for module_dir in module_dirs:
        try:
            runtimes.append(load_module_dir(module_dir))
        except ModuleError as error:
            return _fail(f"cannot load the module package {module_dir}: {error}")
    try:
        host = LobbyHost(
            registry,
            runtimes=runtimes,
            referee_dir=referee_dir or DEFAULT_REFEREE_DIR,
            referee_timeout=referee_timeout,
        )
    except HostConfigError as error:
        return _fail(f"{error}; nothing was started", code=2)
    except (StoreError, sqlite3.Error, OSError, RuntimeError) as error:
        return _fail(f"cannot open the host registry {registry}: {error}")

    def ready(uri):
        print(
            f"Tesserae lobby host listening on {uri} (registry: {host.registry}, "
            f"host: {host.host_id}, log in with a host token; no encryption, "
            "this computer only). Press Ctrl-C to stop.",
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
    except RunnerError as error:
        return _fail(str(error))
    except OSError as error:
        return _fail(f"cannot listen on {HOST}:{port}: {error.strerror or error}")
    print("Tesserae lobby host stopped.", flush=True)
    return 0


def _fail(message: str, code: int = 1) -> int:
    print(f"tesserae serve: {message}", file=sys.stderr)
    return code
