"""Durable command execution for one game in one SQLite store.

Transport-independent: no sockets, terminal formatting, or model calls. One
GameStore owns one SQLite connection and must be used only by the thread that
created it. See docs/game_store.md for the execution boundary.
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable

from .dice import roll_expression
from .protocol import ProtocolError, error_message, parse_request, result_message

SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT = 5.0

_SCHEMA = """
CREATE TABLE game (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    game_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 0)
);
CREATE TABLE commands (
    id INTEGER PRIMARY KEY,
    revision INTEGER NOT NULL UNIQUE,
    participant_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    content TEXT NOT NULL,
    result TEXT NOT NULL,
    UNIQUE (participant_id, request_id)
);
"""


class StoreError(RuntimeError):
    """The store cannot be used as requested; nothing was changed."""


class StoreBusy(StoreError):
    """The write guard was not acquired within the bounded wait."""


@dataclass(frozen=True)
class ParticipantContext:
    """Trusted, host-established identity; never taken from client input."""

    participant_id: str

    def __post_init__(self):
        if not isinstance(self.participant_id, str) or not self.participant_id:
            raise ValueError("participant_id must be a non-empty string.")


@dataclass(frozen=True)
class CommandRecord:
    revision: int
    participant_id: str
    request_id: str
    content: dict
    result: dict

    def to_message(self) -> dict:
        return result_message(self.request_id, self.revision, self.result)


class GameStore:
    def __init__(
        self,
        path,
        *,
        randint: Callable[[int, int], int] | None = None,
        busy_timeout: float = DEFAULT_BUSY_TIMEOUT,
    ):
        self._randint = randint
        self._connection = sqlite3.connect(
            path, timeout=busy_timeout, isolation_level=None
        )
        try:
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._initialize()
            self.game_id = self._connection.execute(
                "SELECT game_id FROM game"
            ).fetchone()[0]
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self):
        self._connection.close()

    def execute(self, context: ParticipantContext, raw: str | bytes) -> dict:
        """Validate and execute one request; return a result or error message."""
        _check_context(context)
        try:
            request = parse_request(raw)
        except ProtocolError as error:
            return error.to_message(revision=self.revision())
        content = request.content()
        with self._write():
            record = self._find(context.participant_id, request.request_id)
            current = self._current_revision()
            if record is not None:
                if record.content == content:
                    return record.to_message()
                return error_message(
                    "request_conflict",
                    "This request_id was already used for a different command.",
                    request_id=request.request_id,
                    revision=current,
                )
            if request.expected_revision != current:
                return error_message(
                    "stale_revision",
                    f"Expected revision {request.expected_revision}, "
                    f"but the game is at revision {current}.",
                    request_id=request.request_id,
                    revision=current,
                )
            result = roll_expression(
                request.args["expression"], randint=self._randint
            ).as_dict()
            record = CommandRecord(
                revision=current + 1,
                participant_id=context.participant_id,
                request_id=request.request_id,
                content=content,
                result=result,
            )
            self._insert_record(record)
            self._advance_revision(record.revision)
        return record.to_message()

    def revision(self) -> int:
        return self._current_revision()

    def lookup(self, context: ParticipantContext, request_id: str):
        """The caller's own recorded command, or None."""
        _check_context(context)
        return self._find(context.participant_id, request_id)

    def history(self, context: ParticipantContext) -> list[CommandRecord]:
        """The caller's own recorded commands, oldest first."""
        _check_context(context)
        return self._select("WHERE participant_id = ?", (context.participant_id,))

    def inspect_history(self) -> list[CommandRecord]:
        """Operator/test inspection of every participant's commands."""
        return self._select("", ())

    def _initialize(self):
        if self._schema_version() == SCHEMA_VERSION:
            return
        with self._write():
            version = self._schema_version()
            if version == SCHEMA_VERSION:
                return
            tables = self._connection.execute(
                "SELECT count(*) FROM sqlite_schema"
            ).fetchone()[0]
            if version != 0 or tables:
                raise StoreError(
                    f"Not a Tesserae game store (schema version {version}); "
                    "refusing to modify it."
                )
            # Not executescript(): it would commit the open transaction.
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    self._connection.execute(statement)
            self._connection.execute(
                "INSERT INTO game (singleton, game_id, revision) VALUES (1, ?, 0)",
                (str(uuid.uuid4()),),
            )
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _schema_version(self) -> int:
        return self._connection.execute("PRAGMA user_version").fetchone()[0]

    @contextmanager
    def _write(self):
        """Serialize writers; every decision inside sees the latest state."""
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if _is_busy(error):
                raise StoreBusy(
                    "The game store is busy; nothing was changed."
                ) from error
            raise
        try:
            yield
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def _current_revision(self) -> int:
        return self._connection.execute("SELECT revision FROM game").fetchone()[0]

    def _find(self, participant_id: str, request_id: str):
        records = self._select(
            "WHERE participant_id = ? AND request_id = ?", (participant_id, request_id)
        )
        return records[0] if records else None

    def _select(self, where: str, parameters: tuple) -> list[CommandRecord]:
        rows = self._connection.execute(
            "SELECT revision, participant_id, request_id, content, result "
            f"FROM commands {where} ORDER BY revision",
            parameters,
        )
        return [
            CommandRecord(
                revision, participant, request, json.loads(content), json.loads(result)
            )
            for revision, participant, request, content, result in rows
        ]

    def _insert_record(self, record: CommandRecord):
        self._connection.execute(
            "INSERT INTO commands "
            "(revision, participant_id, request_id, content, result) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                record.revision,
                record.participant_id,
                record.request_id,
                json.dumps(record.content, sort_keys=True),
                json.dumps(record.result),
            ),
        )

    def _advance_revision(self, revision: int):
        self._connection.execute("UPDATE game SET revision = ?", (revision,))


def _check_context(context):
    if not isinstance(context, ParticipantContext):
        raise TypeError("A trusted ParticipantContext is required.")


def _is_busy(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return code is not None and code & 0xFF in (
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    )
