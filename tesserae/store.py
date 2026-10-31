"""Durable command execution for one game in one SQLite store.

Transport-independent: no sockets, terminal formatting, or model calls. One
GameStore owns one SQLite connection and must be used only by the thread that
created it. Each game is bound to one module, whose state it keeps. See
docs/game_store.md for the execution boundary.
"""

import json
import os
import re
import secrets
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .builtin import CATALOG, DICE_RUNTIME
from .credentials import token_digest
from .modules import ActionRejected, ModuleError, ProviderChoice, Runtime
from .protocol import (
    ProtocolError,
    error_message,
    parse_request,
    result_message,
    session_error,
)

APPLICATION_ID = 0x54455353  # "TESS" in SQLite's header application_id field
SCHEMA_VERSION = 4
SEED_VERSION = 1  # the seed file format (tesserae/seed.py)
DEFAULT_BUSY_TIMEOUT = 5.0
PARTICIPANT_ID_PREFIX = "p_"
_PARTICIPANT_ID = re.compile(r"p_[0-9a-f]{16}")
MAX_NAME_LENGTH = 100
_V1_COLUMNS = {
    "game": ["singleton", "game_id", "revision"],
    "commands": [
        "id",
        "revision",
        "participant_id",
        "request_id",
        "content",
        "result",
    ],
}
_V2_COLUMNS = {
    **_V1_COLUMNS,
    "participants": ["participant_id", "name", "created_at"],
    "credentials": ["id", "participant_id", "digest", "issued_at", "revoked_at"],
}
_BINDING_COLUMNS = [
    "module_id",
    "module_version",
    "state_version",
    "providers",
    "state",
]
_COLUMNS = {
    1: _V1_COLUMNS,
    2: _V2_COLUMNS,
    3: {**_V2_COLUMNS, "module": ["singleton", *_BINDING_COLUMNS]},
}
_COLUMNS[4] = {
    **_COLUMNS[3],
    "seed": ["singleton", "seed_version", "provenance", "created_at"],
}

# Version 1 tables. A new store creates these, then applies every migration.
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


def _binding_row(runtime: Runtime, state: dict) -> tuple:
    return (
        runtime.module.id,
        runtime.module.version,
        runtime.module.state_version,
        json.dumps([[c.operation, c.provider, c.version] for c in runtime.selection]),
        json.dumps(state, sort_keys=True),
    )


# Statements, or (statement, parameters), that upgrade a store to each version.
# Existing data is untouched.
_MIGRATIONS = {
    2: [
        """CREATE TABLE participants (
            participant_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE credentials (
            id INTEGER PRIMARY KEY,
            participant_id TEXT NOT NULL REFERENCES participants (participant_id),
            digest TEXT NOT NULL UNIQUE,
            issued_at TEXT NOT NULL,
            revoked_at TEXT
        )""",
        """CREATE UNIQUE INDEX one_active_credential
            ON credentials (participant_id) WHERE revoked_at IS NULL""",
    ],
    3: [
        """CREATE TABLE module (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            module_id TEXT NOT NULL,
            module_version TEXT NOT NULL,
            state_version INTEGER NOT NULL,
            providers TEXT NOT NULL,
            state TEXT NOT NULL
        )""",
        # Every older game is a `dice` game, and a new store starts as one.
        (
            "INSERT INTO module (singleton, " + ", ".join(_BINDING_COLUMNS) + ") "
            "VALUES (1, ?, ?, ?, ?, ?)",
            _binding_row(DICE_RUNTIME, DICE_RUNTIME.initial_state()),
        ),
    ],
    # The provenance of a game created from a seed; no row for any other game.
    4: [
        """CREATE TABLE seed (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            seed_version INTEGER NOT NULL,
            provenance TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
    ],
}


class StoreError(RuntimeError):
    """The store cannot be used as requested; nothing was changed."""


class StoreBusy(StoreError):
    """The write guard was not acquired within the bounded wait."""


@dataclass(frozen=True)
class ParticipantContext:
    """Trusted, host-established identity; never taken from client input.

    An authenticated context also names the credential it was verified with,
    so its current validity can be rechecked. Development contexts have none.
    """

    participant_id: str
    credential_id: int | None = None

    def __post_init__(self):
        if not isinstance(self.participant_id, str) or not self.participant_id:
            raise ValueError("participant_id must be a non-empty string.")
        if self.credential_id is not None and (
            not isinstance(self.credential_id, int)
            or isinstance(self.credential_id, bool)
        ):
            raise ValueError("credential_id must be an integer or None.")


@dataclass(frozen=True)
class ParticipantRecord:
    """Operator view of a participant; never includes credential material."""

    participant_id: str
    name: str
    created_at: str
    active: bool
    ever_issued: bool = True  # False until a first token, e.g. after a seed


@dataclass(frozen=True)
class ModuleBinding:
    """The module a game was created with. It never changes."""

    module_id: str
    version: str
    state_version: int
    providers: tuple[ProviderChoice, ...]


@dataclass(frozen=True)
class ParticipantView:
    """One participant's view, read with the revision it belongs to.

    `view` is the module's projection for that participant, or None when the
    module shows only metadata (as `dice` does).
    """

    revision: int
    view: dict | None


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
    """Opens an existing game, or creates a `dice` game in a missing file
    (`open_existing` never creates one; `create` makes a bound game).

    `runtime`, if given, is the already-loaded runtime to execute with; it must
    match the game's binding exactly. Without one, the binding is resolved
    from the bundled catalog the first time a command or view needs it, so
    participant administration and operator reads never need the module;
    `load_runtime=True` resolves it at once instead. A given or loaded runtime
    is checked before any upgrade commits or setting changes, so an open
    refused for its module leaves the file unchanged.
    """

    def __init__(
        self,
        path,
        *,
        randint: Callable[[int, int], int] | None = None,
        busy_timeout: float = DEFAULT_BUSY_TIMEOUT,
        runtime: Runtime | None = None,
        load_runtime: bool = False,
        _new_game: Runtime | None = None,
        _seed=None,
        _existing: bool = False,
    ):
        self._randint = randint
        self._runtime = None
        self._wanted_runtime = runtime
        self._load_runtime = load_runtime
        self._new_game = _new_game
        self._seed = _seed
        self._existing = _existing
        if _existing:
            # mode=rw: SQLite opens the file but will never create it.
            path = Path(path).resolve().as_uri() + "?mode=rw"
        self._connection = sqlite3.connect(
            path, timeout=busy_timeout, isolation_level=None, uri=_existing
        )
        try:
            self._initialize()
            # Persistent settings change only once the file is known to be
            # ours, and its runtime (if wanted now) is known to fit.
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self.game_id = self._connection.execute(
                "SELECT game_id FROM game"
            ).fetchone()[0]
        except BaseException:
            self._connection.close()
            raise

    @classmethod
    def create(cls, path, runtime: Runtime, *, seed=None, **options) -> "GameStore":
        """Create a new game bound to `runtime`, at revision 0, in a new file.

        Refuses any existing path. The binding and the module's initial state
        are written in the same transaction as the rest of the new store.
        With a `tesserae.seed.Seed`, checked first (SeedError), the game starts
        from the seed's state and participants instead, without credentials,
        with its provenance recorded and an empty command history.
        """
        if seed is not None:
            seed.check(runtime)  # before any file exists
        path = Path(path)
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        except FileExistsError:
            raise StoreError(f"Refusing to replace existing file {path}.") from None
        try:
            return cls(path, runtime=runtime, _new_game=runtime, _seed=seed, **options)
        except BaseException:
            for name in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                name.unlink(missing_ok=True)
            raise

    @classmethod
    def open_existing(cls, path, **options) -> "GameStore":
        """Open a game that already exists, never creating one.

        A missing path, a directory or an empty file is a StoreError, and no
        file is created or initialized; a file that is not a game store is
        refused unchanged, as always.
        """
        if not Path(path).is_file():
            raise StoreError(f"No game store at {path}.")
        return cls(path, _existing=True, **options)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self):
        self._connection.close()

    def execute(self, context: ParticipantContext, raw: str | bytes) -> dict:
        """Validate and execute one request; return a result or error message.

        An authenticated context's credential is checked inside the write
        transaction, before replay or the revision check, so a fresh action and
        a revocation are strictly ordered. A revoked context gets
        session_revoked and nothing else.
        """
        return self.execute_command(context, raw)[0]

    def execute_command(
        self, context: ParticipantContext, raw: str | bytes
    ) -> tuple[dict, bool]:
        """Like execute, plus whether this call committed a fresh command.

        Only a True outcome advanced the revision. Replays, conflicts, stale
        requests, invalid input, and revoked sessions are all False.
        """
        _check_context(context)
        runtime = self._loaded_runtime()
        try:
            request = parse_request(raw, runtime)
        except ProtocolError as error:
            revision = self.session_revision(context)
            if revision is None:
                return session_error("session_revoked"), False
            return error.to_message(revision=revision), False
        content = request.content()
        with self._write():
            if context.credential_id is not None and not self._credential_is_active(
                context
            ):
                return session_error("session_revoked"), False
            record = self._find(context.participant_id, request.request_id)
            current = self._current_revision()
            if record is not None:
                if record.content == content:
                    return record.to_message(), False
                return error_message(
                    "request_conflict",
                    "This request_id was already used for a different command.",
                    request_id=request.request_id,
                    revision=current,
                ), False
            if request.expected_revision != current:
                return error_message(
                    "stale_revision",
                    f"Expected revision {request.expected_revision}, "
                    f"but the game is at revision {current}.",
                    request_id=request.request_id,
                    revision=current,
                ), False
            # Only now, with replay and staleness settled, do the rules run.
            try:
                outcome = runtime.execute(
                    self._state(),
                    context.participant_id,
                    request.command,
                    request.args,
                    randint=self._randint,
                )
            except ActionRejected as rejection:
                return error_message(
                    "action_rejected",
                    rejection.message,
                    request_id=request.request_id,
                    revision=current,
                ), False
            record = CommandRecord(
                revision=current + 1,
                participant_id=context.participant_id,
                request_id=request.request_id,
                content=content,
                result=outcome.result,
            )
            self._write_state(outcome.state)
            self._insert_record(record)
            self._advance_revision(record.revision)
        return record.to_message(), True

    def revision(self) -> int:
        return self._current_revision()

    def session_revision(self, context: ParticipantContext) -> int | None:
        """The revision, as a participant-facing read: None if revoked.

        Development contexts have no credential and always read. One statement,
        so the check and the read see the same committed state.
        """
        _check_context(context)
        if context.credential_id is None:
            return self._current_revision()
        row = self._connection.execute(
            "SELECT revision FROM game WHERE EXISTS ("
            "    SELECT 1 FROM credentials"
            "    WHERE id = ? AND participant_id = ? AND revoked_at IS NULL)",
            (context.credential_id, context.participant_id),
        ).fetchone()
        return None if row is None else row[0]

    def participant_view(self, context: ParticipantContext) -> ParticipantView | None:
        """The caller's view and its revision, or None if revoked.

        The credential check, revision and state come from one statement, so
        they describe the same committed state. Never draws or writes.
        """
        _check_context(context)
        runtime = self._loaded_runtime()
        query = "SELECT revision, state FROM game, module"
        parameters = ()
        if context.credential_id is not None:
            query += (
                " WHERE EXISTS (SELECT 1 FROM credentials"
                " WHERE id = ? AND participant_id = ? AND revoked_at IS NULL)"
            )
            parameters = (context.credential_id, context.participant_id)
        row = self._connection.execute(query, parameters).fetchone()
        if row is None:
            return None
        return ParticipantView(
            row[0], runtime.view(json.loads(row[1]), context.participant_id)
        )

    def runtime(self) -> Runtime:
        """The game's runtime: the one given when the store was opened, or
        else resolved from the bundled catalog, and always checked against its
        binding. Raises StoreError if it cannot be loaded exactly."""
        return self._loaded_runtime()

    def binding(self) -> ModuleBinding:
        """The game's module binding; needs no module code."""
        module_id, version, state_version, providers = self._connection.execute(
            "SELECT module_id, module_version, state_version, providers FROM module"
        ).fetchone()
        return ModuleBinding(
            module_id,
            version,
            state_version,
            tuple(ProviderChoice(*choice) for choice in json.loads(providers)),
        )

    def seed_provenance(self) -> dict | None:
        """The provenance recorded when this game was created from a seed, or
        None for any other game. Operator inspection; needs no module code."""
        row = self._connection.execute("SELECT provenance FROM seed").fetchone()
        return None if row is None else json.loads(row[0])

    def inspect_state(self) -> dict:
        """Operator/test inspection of the complete module state."""
        return self._state()

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

    # Participants and credentials. None of these change the mechanical
    # revision or command history.

    def add_participant(self, name: str, token: str) -> str:
        """Create a participant with an opaque ID and one active credential."""
        _check_name(name)
        participant_id = PARTICIPANT_ID_PREFIX + secrets.token_hex(8)
        now = _now()
        with self._write():
            self._connection.execute(
                "INSERT INTO participants (participant_id, name, created_at) "
                "VALUES (?, ?, ?)",
                (participant_id, name, now),
            )
            self._issue_credential(participant_id, token, now)
        return participant_id

    def rotate_credential(self, participant_id: str, token: str) -> bool:
        """Replace any credential with a new one; the identity is unchanged.
        Returns whether an active one was replaced (False for a first token)."""
        now = _now()
        with self._write():
            self._require_participant(participant_id)
            replaced = self._revoke_active(participant_id, now) > 0
            self._issue_credential(participant_id, token, now)
        return replaced

    def revoke_credential(self, participant_id: str) -> bool:
        """Revoke the active credential; False if there was none."""
        with self._write():
            self._require_participant(participant_id)
            return self._revoke_active(participant_id, _now()) > 0

    def participants(self) -> list[ParticipantRecord]:
        rows = self._connection.execute(
            "SELECT participant_id, name, created_at, EXISTS ("
            "    SELECT 1 FROM credentials AS c"
            "    WHERE c.participant_id = p.participant_id AND c.revoked_at IS NULL"
            "), EXISTS ("
            "    SELECT 1 FROM credentials AS c WHERE c.participant_id = p.participant_id"
            ") FROM participants AS p ORDER BY created_at, participant_id"
        )
        return [ParticipantRecord(i, n, c, bool(a), bool(e)) for i, n, c, a, e in rows]

    def authenticate(self, token) -> ParticipantContext | None:
        """The trusted context for an active token, or None. Never raises on input."""
        if not isinstance(token, str):
            return None
        try:
            digest = token_digest(token)
        except UnicodeEncodeError:  # lone surrogates can never match a token
            return None
        row = self._connection.execute(
            "SELECT id, participant_id FROM credentials "
            "WHERE digest = ? AND revoked_at IS NULL",
            (digest,),
        ).fetchone()
        return None if row is None else ParticipantContext(row[1], credential_id=row[0])

    def credential_active(self, context: ParticipantContext) -> bool:
        """Whether an authenticated context's credential is still valid.

        Development contexts carry no credential and are never active here.
        """
        _check_context(context)
        if context.credential_id is None:
            return False
        return self._credential_is_active(context)

    def _credential_is_active(self, context: ParticipantContext) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM credentials "
                "WHERE id = ? AND participant_id = ? AND revoked_at IS NULL",
                (context.credential_id, context.participant_id),
            ).fetchone()
            is not None
        )

    def _require_participant(self, participant_id: str):
        row = self._connection.execute(
            "SELECT 1 FROM participants WHERE participant_id = ?", (participant_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"No participant {participant_id!r}.")

    def _issue_credential(self, participant_id: str, token: str, now: str):
        self._connection.execute(
            "INSERT INTO credentials (participant_id, digest, issued_at) "
            "VALUES (?, ?, ?)",
            (participant_id, token_digest(token), now),
        )

    def _revoke_active(self, participant_id: str, now: str) -> int:
        return self._connection.execute(
            "UPDATE credentials SET revoked_at = ? "
            "WHERE participant_id = ? AND revoked_at IS NULL",
            (now, participant_id),
        ).rowcount

    def _loaded_runtime(self) -> Runtime:
        if self._runtime is None:
            binding = self.binding()
            try:
                runtime = CATALOG.resolve(
                    binding.module_id,
                    version=binding.version,
                    providers=binding.providers,
                )
            except ModuleError as error:
                if binding.module_id not in CATALOG.module_ids():
                    raise StoreError(
                        f"This game uses module {binding.module_id!r}, which is not "
                        "bundled; load its package explicitly (--module-dir)."
                    ) from None
                raise StoreError(f"Cannot run this game: {error}") from None
            self._runtime = self._checked(runtime)
        return self._runtime

    def _checked(self, runtime: Runtime) -> Runtime:
        """`runtime`, if it is exactly the game's binding; never a substitute."""
        binding = self.binding()
        loaded = ModuleBinding(
            runtime.module.id,
            runtime.module.version,
            runtime.module.state_version,
            runtime.selection,
        )
        if loaded != binding:
            raise StoreError(
                f"This game uses module {binding.module_id} {binding.version} "
                f"(state {binding.state_version}, providers "
                f"{_describe(binding.providers)}), not {loaded.module_id} "
                f"{loaded.version} (state {loaded.state_version}, providers "
                f"{_describe(loaded.providers)})."
            )
        return runtime

    def _state(self) -> dict:
        return json.loads(
            self._connection.execute("SELECT state FROM module").fetchone()[0]
        )

    def _write_state(self, state: dict):
        self._connection.execute(
            "UPDATE module SET state = ?", (json.dumps(state, sort_keys=True),)
        )

    def _initialize(self):
        """Validate an existing store, or create one in an empty database.

        Validation only reads, so a rejected file is left byte-for-byte unchanged.
        An older store is upgraded in the same single transaction as its final
        validation, so an upgrade either completes or leaves nothing behind.
        """
        try:
            if self._existing and self._is_empty():
                raise StoreError("This file is empty, not a game store.")
            if (
                self._new_game is None
                and not self._is_empty()
                and self._validate() == SCHEMA_VERSION
            ):
                self._settle_runtime()
                return
        except sqlite3.DatabaseError as error:
            raise StoreError(f"Not a Tesserae game store: {error}") from error
        with self._write():
            if self._is_empty():
                self._create()
                version = 1
            elif self._new_game is not None:
                raise StoreError("A new game needs an empty file.")
            else:
                version = self._validate()
            if version == SCHEMA_VERSION:
                self._settle_runtime()
                return
            for target in range(version + 1, SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS[target]:
                    if isinstance(statement, str):
                        statement = (statement,)
                    self._connection.execute(*statement)
            if self._new_game is not None:
                runtime = self._new_game
                seed = self._seed
                state = runtime.initial_state() if seed is None else seed.state
                self._connection.execute(
                    "UPDATE module SET "
                    + ", ".join(f"{column} = ?" for column in _BINDING_COLUMNS),
                    _binding_row(runtime, state),
                )
                if seed is not None:
                    self._write_seed(seed)
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            # Before the upgrade commits: a refusal rolls it back.
            self._settle_runtime()

    def _write_seed(self, seed):
        now = _now()
        self._connection.executemany(
            "INSERT INTO participants (participant_id, name, created_at) "
            "VALUES (?, ?, ?)",
            [(p.participant_id, p.name, now) for p in seed.participants],
        )
        self._connection.execute(
            "INSERT INTO seed (singleton, seed_version, provenance, created_at) "
            "VALUES (1, ?, ?, ?)",
            (SEED_VERSION, json.dumps(seed.provenance, sort_keys=True), now),
        )

    def _settle_runtime(self):
        """Check the wanted runtime against the binding, or with `load_runtime`
        resolve it from the bundled catalog: before any migration commits or
        persistent setting changes, so a refused open leaves the file as it was."""
        if self._wanted_runtime is not None:
            self._runtime = self._checked(self._wanted_runtime)
        elif self._load_runtime:
            self._loaded_runtime()

    def _create(self):
        # Not executescript(): it would commit the open transaction.
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                self._connection.execute(statement)
        self._connection.execute(
            "INSERT INTO game (singleton, game_id, revision) VALUES (1, ?, 0)",
            (str(uuid.uuid4()),),
        )
        self._connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")

    def _pragma(self, name: str) -> int:
        return self._connection.execute(f"PRAGMA {name}").fetchone()[0]

    def _is_empty(self) -> bool:
        objects = self._connection.execute(
            "SELECT count(*) FROM sqlite_schema"
        ).fetchone()[0]
        return (
            not objects
            and not self._pragma("application_id")
            and not (self._pragma("user_version"))
        )

    def _validate(self) -> int:
        """Check identity and schema with reads only; return the schema version."""
        if self._pragma("application_id") != APPLICATION_ID:
            raise StoreError("Not a Tesserae game store; refusing to modify it.")
        version = self._pragma("user_version")
        if version > SCHEMA_VERSION:
            raise StoreError(
                f"This game store uses schema version {version}, which is newer "
                f"than this Tesserae supports ({SCHEMA_VERSION}). Upgrade Tesserae."
            )
        if version not in _COLUMNS:
            raise StoreError(f"Unsupported Tesserae store schema version {version}.")
        for table, expected in _COLUMNS[version].items():
            columns = [
                row[0]
                for row in self._connection.execute(
                    "SELECT name FROM pragma_table_info(?)", (table,)
                )
            ]
            if columns != expected:
                raise StoreError(f"Tesserae store table {table!r} is damaged.")
        if self._connection.execute("SELECT count(*) FROM game").fetchone()[0] != 1:
            raise StoreError("Tesserae store must hold exactly one game.")
        if (
            version >= 3
            and self._connection.execute("SELECT count(*) FROM module").fetchone()[0]
            != 1
        ):
            raise StoreError("Tesserae store must hold exactly one module binding.")
        return version

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


def _describe(providers) -> str:
    return (
        ", ".join(f"{c.operation}={c.provider} {c.version}" for c in providers)
        or "none"
    )


def is_participant_id(value) -> bool:
    """Whether `value` has the form every participant ID has: p_ and 16
    lowercase hexadecimal digits."""
    return isinstance(value, str) and _PARTICIPANT_ID.fullmatch(value) is not None


def check_name(name):
    """Raise ValueError unless `name` is a valid participant display name."""
    _check_name(name)


def _check_name(name):
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > MAX_NAME_LENGTH
        or any(unicodedata.category(character) == "Cc" for character in name)
    ):
        raise ValueError(
            f"A name is 1 to {MAX_NAME_LENGTH} characters, not blank, "
            "with no control characters."
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_context(context):
    if not isinstance(context, ParticipantContext):
        raise TypeError("A trusted ParticipantContext is required.")


def _is_busy(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return code is not None and code & 0xFF in (
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    )
