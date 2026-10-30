"""Foreground, loopback-only WebSocket adapter for the game store.

A development host: one fixed participant, no authentication, no LAN access.
Dice, replay, revisions, and persistence all live in GameStore; this module only
moves messages between sockets and that service. See docs/game_store.md.
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
    ProtocolError,
    encode_message,
    error_message,
    parse_request,
    welcome_message,
)
from .store import GameStore, ParticipantContext, StoreBusy, StoreError

HOST = "127.0.0.1"
DEVELOPMENT_PARTICIPANT = ParticipantContext("local-development")
WORKER_THREADS = 8

logger = logging.getLogger("tesserae.server")


class DevelopmentHost:
    """Serves one game store to any number of local connections.

    Every connection acts as DEVELOPMENT_PARTICIPANT. Store calls run on worker
    threads, each opening its own short-lived GameStore, so the event loop never
    waits on SQLite and no connection is shared between threads.
    """

    def __init__(self, database, *, randint=None):
        self.database = Path(database).resolve()
        self._randint = randint
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
            await connection.send(encode_message(await self._welcome()))
            async for message in connection:
                if isinstance(message, bytes):
                    reply = error_message(
                        "invalid_request", "Send requests as text JSON frames."
                    )
                else:
                    reply = await self._execute(message)
                await connection.send(encode_message(reply))
        except ConnectionClosed:
            pass

    async def _welcome(self) -> dict:
        try:
            return welcome_message(await self._call(GameStore.revision))
        except Exception:
            logger.exception("Could not read the game revision")
            return error_message("internal_error", "The host could not read the game.")

    async def _execute(self, raw: str) -> dict:
        try:
            return await self._call(GameStore.execute, DEVELOPMENT_PARTICIPANT, raw)
        except StoreBusy:
            logger.warning("Game store busy; request not executed")
            message = "The game is busy; nothing was changed. Retry the same request."
        except Exception:
            logger.exception("Request failed inside the game store")
            message = (
                "The host failed; nothing was acknowledged. Retry the same request "
                "to get its recorded outcome."
            )
        return error_message("internal_error", message, request_id=_request_id(raw))

    async def _call(self, method, *args):
        def work():
            with self._open() as store:
                if store.game_id != self.game_id:
                    raise StoreError("The database now holds a different game.")
                return method(store, *args)

        return await asyncio.get_running_loop().run_in_executor(self._executor, work)

    def _open(self) -> GameStore:
        return GameStore(self.database, randint=self._randint)


def run_foreground(database: str, port: int) -> int:
    """The `tesserae serve` command: run until Ctrl-C, then exit cleanly."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        host = DevelopmentHost(database)
    except (StoreError, sqlite3.Error, OSError) as error:
        return _fail(f"cannot open database {database}: {error}")

    def ready(uri):
        print(
            f"Tesserae development host listening on {uri} "
            f"(database: {host.database}, participant: "
            f"{DEVELOPMENT_PARTICIPANT.participant_id}). Press Ctrl-C to stop.",
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
    print("Tesserae development host stopped.", flush=True)
    return 0


def _fail(message: str) -> int:
    print(f"tesserae serve: {message}", file=sys.stderr)
    return 1


def _request_id(raw):
    try:
        return parse_request(raw).request_id
    except ProtocolError as error:
        return error.request_id
