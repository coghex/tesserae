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
from .modules import (
    NAME_STEP,
    ActionRejected,
    ArgumentError,
    ModuleError,
    ProviderChoice,
    Runtime,
    check_character_name,
)
from .protocol import (
    PROTOCOL_VERSION,
    ProtocolError,
    error_message,
    is_valid_request_id,
    is_valid_revision,
    parse_request,
    result_message,
    session_error,
)

APPLICATION_ID = 0x54455353  # "TESS" in SQLite's header application_id field
SCHEMA_VERSION = 7
DEFAULT_BUSY_TIMEOUT = 5.0
PARTICIPANT_ID_PREFIX = "p_"
_PARTICIPANT_ID = re.compile(r"p_[0-9a-f]{16}")
CHARACTER_ID_PREFIX = "c_"
_CHARACTER_ID = re.compile(r"c_[0-9a-f]{16}")
MODES = ("setup", "creation", "play")
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
_COLUMNS[5] = {
    **_COLUMNS[4],
    "referee": ["singleton", "enabled", "session_id", "state"],
    "referee_sessions": ["session_id", "started_at", "ended_at", "ended_reason"],
    "referee_inputs": [
        "id", "participant_id", "request_id", "text", "private", "received_at",
    ],
    "referee_jobs": [
        "job_id", "input_id", "state", "host_run", "revision_at_start",
        "started_at", "ended_at", "error", "narrated",
    ],
    "referee_steps": [
        "job_id", "step_id", "kind", "context", "command", "args", "request_id",
        "status", "result", "narrated",
    ],
    "referee_choices": ["participant_id", "question", "asked_in_job", "asked_at"],
    "referee_messages": ["seq", "job_id", "sender", "audience", "text", "created_at"],
    "referee_rulings": ["id", "job_id", "text", "scope", "created_at"],
}  # fmt: skip
_COLUMNS[6] = {
    **_COLUMNS[5],
    "host_links": [
        "id", "host_id", "player_id", "participant_id", "linked_at", "unlinked_at",
    ],
}  # fmt: skip
_COLUMNS[7] = {
    **_COLUMNS[6],
    "characters": [
        "character_id", "participant_id", "name", "phase", "draft_revision",
        "created_at", "completed_at", "introduced_at", "archived_at",
    ],
    "referee_inputs": [*_COLUMNS[6]["referee_inputs"], "character_id", "mode"],
    "referee_steps": [*_COLUMNS[6]["referee_steps"], "character_id"],
    "referee_messages": [*_COLUMNS[6]["referee_messages"], "character_id", "creation"],
    "referee_choices": [
        "participant_id", "character_id", "question", "asked_in_job", "asked_at",
    ],
}  # fmt: skip

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
    # The AI referee's conversation, jobs and tool steps (M1-6, #65); see
    # docs/designs/referee_contract.md. Empty until referee mode is enabled.
    5: [
        """CREATE TABLE referee (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            session_id TEXT,
            state TEXT NOT NULL CHECK (state IN ('ok', 'needs_rebuild'))
        )""",
        """CREATE TABLE referee_sessions (
            session_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            ended_reason TEXT
        )""",
        """CREATE TABLE referee_inputs (
            id INTEGER PRIMARY KEY,
            participant_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            text TEXT NOT NULL,
            private INTEGER NOT NULL CHECK (private IN (0, 1)),
            received_at TEXT NOT NULL,
            UNIQUE (participant_id, request_id)
        )""",
        """CREATE TABLE referee_jobs (
            job_id TEXT PRIMARY KEY,
            input_id INTEGER NOT NULL UNIQUE REFERENCES referee_inputs (id),
            state TEXT NOT NULL
                CHECK (state IN ('running', 'completed', 'failed', 'interrupted')),
            host_run TEXT,
            revision_at_start INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            error TEXT,
            narrated INTEGER NOT NULL DEFAULT 0 CHECK (narrated IN (0, 1))
        )""",
        # The database itself allows only one running job per game.
        """CREATE UNIQUE INDEX one_running_job ON referee_jobs (state)
            WHERE state = 'running'""",
        """CREATE TABLE referee_steps (
            job_id TEXT NOT NULL REFERENCES referee_jobs (job_id),
            step_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('act', 'referee_act')),
            context TEXT NOT NULL,
            command TEXT NOT NULL,
            args TEXT NOT NULL,
            request_id TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ('intended', 'committed', 'refused', 'not_committed')),
            result TEXT,
            narrated INTEGER NOT NULL DEFAULT 0 CHECK (narrated IN (0, 1)),
            PRIMARY KEY (job_id, step_id)
        )""",
        """CREATE TABLE referee_choices (
            participant_id TEXT PRIMARY KEY,
            question TEXT NOT NULL,
            asked_in_job TEXT NOT NULL,
            asked_at TEXT NOT NULL
        )""",
        """CREATE TABLE referee_messages (
            seq INTEGER PRIMARY KEY,
            job_id TEXT,
            sender TEXT NOT NULL,
            audience TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE referee_rulings (
            id INTEGER PRIMARY KEY,
            job_id TEXT NOT NULL,
            text TEXT NOT NULL,
            scope TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
    ],
    # Host players admitted to this game by a lobby host (HOST-2, #97); see
    # docs/designs/lobby_contract.md section 5. Empty until one is.
    6: [
        """CREATE TABLE host_links (
            id INTEGER PRIMARY KEY,
            host_id TEXT NOT NULL,
            player_id TEXT NOT NULL,
            participant_id TEXT NOT NULL REFERENCES participants (participant_id),
            linked_at TEXT NOT NULL,
            unlinked_at TEXT
        )""",
        """CREATE UNIQUE INDEX one_link_per_player ON host_links (host_id, player_id)
            WHERE unlinked_at IS NULL""",
        """CREATE UNIQUE INDEX one_link_per_participant ON host_links (participant_id)
            WHERE unlinked_at IS NULL""",
    ],
    # Characters and the actor scope of referee records (M2-3, #112); see
    # docs/designs/character_contract.md section 12. Empty, or null, in every
    # game without characters.
    7: [
        """CREATE TABLE characters (
            character_id TEXT PRIMARY KEY,
            participant_id TEXT NOT NULL REFERENCES participants (participant_id),
            name TEXT,
            phase TEXT NOT NULL CHECK (phase IN ('draft', 'ready', 'archived')),
            draft_revision INTEGER NOT NULL CHECK (draft_revision >= 1),
            created_at TEXT NOT NULL,
            completed_at TEXT,
            introduced_at TEXT,
            archived_at TEXT
        )""",
        "ALTER TABLE referee_inputs ADD COLUMN character_id TEXT",
        """ALTER TABLE referee_inputs ADD COLUMN mode TEXT
            CHECK (mode IS NULL OR mode IN ('setup', 'creation', 'play'))""",
        "ALTER TABLE referee_steps ADD COLUMN character_id TEXT",
        "ALTER TABLE referee_messages ADD COLUMN character_id TEXT",
        """ALTER TABLE referee_messages ADD COLUMN creation INTEGER NOT NULL
            DEFAULT 0 CHECK (creation IN (0, 1))""",
        # One pending choice per (participant, character); a null character is
        # the participant's setup scope, and every earlier choice becomes one.
        """CREATE TABLE referee_choices_7 (
            participant_id TEXT NOT NULL,
            character_id TEXT,
            question TEXT NOT NULL,
            asked_in_job TEXT NOT NULL,
            asked_at TEXT NOT NULL
        )""",
        """INSERT INTO referee_choices_7 (participant_id, question, asked_in_job, asked_at)
            SELECT participant_id, question, asked_in_job, asked_at FROM referee_choices""",
        "DROP TABLE referee_choices",
        "ALTER TABLE referee_choices_7 RENAME TO referee_choices",
        """CREATE UNIQUE INDEX one_choice_per_scope
            ON referee_choices (participant_id, ifnull(character_id, ''))""",
    ],
}


# The host's context for a module's referee-only commands. It is not a
# participant ID (those are p_ and hex digits), so no token can map to it.
REFEREE_PARTICIPANT = "referee"


class StoreError(RuntimeError):
    """The store cannot be used as requested; nothing was changed."""


class StoreBusy(StoreError):
    """The write guard was not acquired within the bounded wait."""


@dataclass(frozen=True)
class ParticipantContext:
    """Trusted, host-established identity; never taken from client input.

    An authenticated context also names the credential it was verified with,
    or for a lobby host's player the host link it was admitted with, so its
    current validity can be rechecked. Development contexts have neither.
    """

    participant_id: str
    credential_id: int | None = None
    link_id: int | None = None

    def __post_init__(self):
        if not isinstance(self.participant_id, str) or not self.participant_id:
            raise ValueError("participant_id must be a non-empty string.")
        for name in ("credential_id", "link_id"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool)
            ):
                raise ValueError(f"{name} must be an integer or None.")
        if self.credential_id is not None and self.link_id is not None:
            raise ValueError("A context has a credential or a host link, not both.")

    @property
    def authenticated(self) -> bool:
        return self.credential_id is not None or self.link_id is not None


@dataclass(frozen=True)
class Actor:
    """Who acts in a character game, established by the host, never taken
    from client input (docs/designs/character_contract.md sections 4 to 6).

    `mode` is `setup` with no character, `creation` with one of the
    participant's drafts, or `play` with one of their ready characters.
    """

    character_id: str | None
    mode: str

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}.")
        if (self.character_id is None) != (self.mode == "setup"):
            raise ValueError("Only setup has no character.")
        if self.character_id is not None and not is_character_id(self.character_id):
            raise ValueError("character_id must be a character ID.")


SETUP = Actor(None, "setup")


@dataclass(frozen=True)
class CharacterRecord:
    """One character's metadata. `draft_revision` is None once ready."""

    character_id: str
    participant_id: str
    name: str | None
    phase: str
    draft_revision: int | None
    created_at: str
    completed_at: str | None = None
    introduced_at: str | None = None
    archived_at: str | None = None

    def summary(self) -> dict:
        """The form every host message uses (contract section 9)."""
        return {
            "character_id": self.character_id,
            "name": self.name,
            "phase": self.phase,
            "draft_revision": self.draft_revision,
        }


@dataclass(frozen=True)
class ParticipantRecord:
    """Operator view of a participant; never includes credential material."""

    participant_id: str
    name: str
    created_at: str
    active: bool
    ever_issued: bool = True  # False until a first token, e.g. after a seed


@dataclass(frozen=True)
class HostLink:
    """Operator view of one host player's link to a participant."""

    link_id: int
    host_id: str
    player_id: str
    participant_id: str
    linked_at: str
    unlinked_at: str | None


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
    module shows only metadata (as `dice` does). `name` is the participant's
    own display label (None for the development participant).
    """

    revision: int
    view: dict | None
    name: str | None = None


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
        self,
        context: ParticipantContext,
        raw: str | bytes,
        *,
        referee: bool = False,
        precondition: Callable[[], None] | None = None,
        actor: Actor | None = None,
    ) -> tuple[dict, bool]:
        """Like execute, plus whether this call committed a fresh command.

        Only a True outcome advanced the revision. Replays, conflicts, stale
        requests, invalid input, and revoked sessions are all False.

        `referee=True` runs one of the module's referee-only commands, and only
        in the `referee` context; that context runs nothing else. A player
        context never sees a referee-only command (`unsupported_command`).

        `precondition()`, if given, runs first inside the write transaction;
        whatever it raises propagates with nothing changed. The referee bridge
        uses it so a job's authorization holds until the command commits.

        In a character game a player's command runs as `actor` (setup when
        omitted), under the character contract's section 5: setup runs
        nothing, creation runs only creation steps on its own draft, and play
        runs only gameplay commands as its own ready character. The actor's
        character is part of the replay content. A game without characters
        takes no actor.
        """
        _check_context(context)
        if referee != (context.participant_id == REFEREE_PARTICIPANT):
            raise ValueError(
                "Referee-only commands run in the referee context, and only they do."
            )
        runtime = self._loaded_runtime()
        acting = runtime.characters is not None and not referee
        if actor is not None and not acting:
            raise ValueError("Only a character game's players act as an actor.")
        if acting and actor is None:
            actor = SETUP
        if referee:
            runtime = runtime.for_referee()
        try:
            request = parse_request(raw, runtime)
        except ProtocolError as error:
            revision = self.session_revision(context)
            if revision is None:
                return session_error("session_revoked"), False
            return error.to_message(revision=revision), False
        content = request.content()
        if acting:
            content["character_id"] = actor.character_id
        with self._write():
            if precondition is not None:
                precondition()
            if context.authenticated and not self._authority_is_active(context):
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
            if acting:
                refusal = self._actor_refusal(
                    context, actor, runtime.is_step(request.command)
                )
                if refusal is not None:
                    return refusal.message(request.request_id, current), False
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
                    character_id=actor.character_id if acting else None,
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
            if acting and actor.mode == "creation":
                # Every committed step is a new revision of the draft.
                self._connection.execute(
                    "UPDATE characters SET draft_revision = draft_revision + 1 "
                    "WHERE character_id = ?",
                    (actor.character_id,),
                )
                if request.command == NAME_STEP:
                    self._connection.execute(
                        "UPDATE characters SET name = ? WHERE character_id = ?",
                        (request.args["name"], actor.character_id),
                    )
        return record.to_message(), True

    def _actor_refusal(
        self, context: ParticipantContext, actor: Actor, step: bool
    ) -> "Refusal | None":
        """Why section 5 of the character contract refuses this command for
        this actor, or None. Read inside the command's write transaction."""
        if actor.mode == "setup":
            return Refusal(
                "mode_forbidden",
                "Choose a character first; without one you can only ask questions.",
            )
        character = self._owned(context, actor.character_id)
        if character is None:
            return _unavailable()
        if _mode_of(character.phase) != actor.mode:
            return _changed()
        if actor.mode == "creation" and not step:
            return Refusal(
                "mode_forbidden",
                "While creating a character you can only take its creation steps.",
            )
        if actor.mode == "play" and step:
            return Refusal("mode_forbidden", "Creation steps are only for drafts.")
        return None

    def revision(self) -> int:
        return self._current_revision()

    def session_revision(self, context: ParticipantContext) -> int | None:
        """The revision, as a participant-facing read: None if revoked.

        Development contexts have no credential and always read. One statement,
        so the check and the read see the same committed state.
        """
        _check_context(context)
        if not context.authenticated:
            return self._current_revision()
        clause, parameters = _authority(context)
        row = self._connection.execute(
            f"SELECT revision FROM game WHERE {clause}", parameters
        ).fetchone()
        return None if row is None else row[0]

    def participant_view(self, context: ParticipantContext) -> ParticipantView | None:
        """The caller's view and its revision, or None if revoked.

        The credential check, revision and state come from one statement, so
        they describe the same committed state. Never draws or writes.
        """
        _check_context(context)
        runtime = self._loaded_runtime()
        query = (
            "SELECT revision, state, (SELECT name FROM participants"
            " WHERE participant_id = ?) FROM game, module"
        )
        parameters = (context.participant_id,)
        if context.authenticated:
            clause, authority = _authority(context)
            query += f" WHERE {clause}"
            parameters += authority
        row = self._connection.execute(query, parameters).fetchone()
        if row is None:
            return None
        return ParticipantView(
            row[0], runtime.view(json.loads(row[1]), context.participant_id), row[2]
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

    @contextmanager
    def transaction(self):
        """A serialized write transaction on this store's connection, for
        companion stores in this package (tesserae.referee). Yields the
        connection; commits on success and rolls back on any error."""
        with self._write():
            yield self._connection

    @contextmanager
    def snapshot(self):
        """A read transaction: every read inside sees one committed state,
        even if another connection commits meanwhile (WAL). For companion
        stores' multi-read views."""
        self._connection.execute("BEGIN")
        try:
            yield self._connection
        finally:
            self._connection.execute("COMMIT")

    def query(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        """Rows from a read-only query, for companion stores in this package."""
        return self._connection.execute(sql, parameters).fetchall()

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

    # Characters (docs/designs/character_contract.md sections 2, 3 and 9).
    # Each lifecycle operation is one command record in the caller's request
    # namespace and advances the revision; its module and metadata changes
    # commit together, or nothing does. Like execute_command, each returns
    # (message, committed) and never raises for a client's input.

    def create_character(
        self,
        context: ParticipantContext,
        request_id: str,
        name: str | None,
        *,
        precondition: Callable[[], None] | None = None,
    ) -> tuple[dict, bool]:
        """Start a draft owned by the caller, with the module's `start` data.
        An exact retry returns the same draft; it is never selected here."""
        if name is not None:
            try:
                check_character_name(name)
            except ArgumentError as error:
                return _invalid(request_id, error.message), False

        def create(runtime, state, now):
            character_id = CHARACTER_ID_PREFIX + secrets.token_hex(8)
            outcome = runtime.start_character(
                state, context.participant_id, character_id, randint=self._randint
            )
            self._connection.execute(
                "INSERT INTO characters (character_id, participant_id, name, phase, "
                "draft_revision, created_at) VALUES (?, ?, ?, 'draft', 1, ?)",
                (character_id, context.participant_id, name, now),
            )
            return outcome.state, {
                "character": self._character(character_id).summary(),
                "start": outcome.result,
            }

        content = _lifecycle_content("character_create", name=name)
        return self._lifecycle(context, request_id, content, create, precondition)

    def complete_character(
        self,
        context: ParticipantContext,
        request_id: str,
        character_id: str,
        draft_revision: int,
        *,
        precondition: Callable[[], None] | None = None,
    ) -> tuple[dict, bool]:
        """Make the caller's draft ready, if it is still at `draft_revision`,
        named, not acted for by a running job, and complete by the module's
        rules. `complete` is deterministic: nothing is drawn."""
        if not isinstance(character_id, str):
            return _invalid(request_id, "'character_id' must be a string."), False
        if not is_valid_revision(draft_revision) or draft_revision < 1:
            return _invalid(
                request_id, "'draft_revision' must be a whole number, 1 or more."
            ), False

        def complete(runtime, state, now):
            character = self._owned(context, character_id)
            if character is None:
                raise _unavailable()
            if character.phase != "draft" or character.draft_revision != draft_revision:
                raise Refusal(
                    "stale_draft",
                    "That draft has changed since you saw it; review it again.",
                    {"draft_revision": character.draft_revision},
                )
            if self._acting_job(character_id):
                raise _busy()
            problems = [] if character.name else ["Give the character a name."]
            problems += runtime.character_problems(state, character_id)
            if problems:
                raise Refusal(
                    "character_incomplete",
                    "That character is not finished yet.",
                    {"problems": problems},
                )
            state = runtime.complete_character(state, character_id)
            self._connection.execute(
                "UPDATE characters SET phase = 'ready', completed_at = ? "
                "WHERE character_id = ?",
                (now, character_id),
            )
            self._end_creation(character_id)
            return state, {"character": self._character(character_id).summary()}

        content = _lifecycle_content(
            "character_complete",
            character_id=character_id,
            draft_revision=draft_revision,
        )
        return self._lifecycle(context, request_id, content, complete, precondition)

    def archive_character(
        self,
        context: ParticipantContext,
        request_id: str,
        character_id: str,
        *,
        precondition: Callable[[], None] | None = None,
    ) -> tuple[dict, bool]:
        """Archive the caller's draft or ready character, unless a running job
        acts for it or the module vetoes. Its history and data stay."""
        if not isinstance(character_id, str):
            return _invalid(request_id, "'character_id' must be a string."), False

        def archive(runtime, state, now):
            character = self._owned(context, character_id)
            if character is None:
                raise _unavailable()
            if self._acting_job(character_id):
                raise _busy()
            reason = runtime.archive_veto(state, character_id)
            if reason is not None:
                raise Refusal("action_rejected", reason)
            self._connection.execute(
                "UPDATE characters SET phase = 'archived', archived_at = ? "
                "WHERE character_id = ?",
                (now, character_id),
            )
            self._end_creation(character_id)
            return None, {"character": self._character(character_id).summary()}

        content = _lifecycle_content("character_archive", character_id=character_id)
        return self._lifecycle(context, request_id, content, archive, precondition)

    def characters(self, context: ParticipantContext) -> list[CharacterRecord] | None:
        """The caller's own drafts and ready characters, ready first, then
        drafts, each in creation order; None if the caller's authority has
        ended. Archived characters are not listed."""
        _check_context(context)
        with self.snapshot():
            if context.authenticated and not self._authority_is_active(context):
                return None
            return self._characters(
                "WHERE participant_id = ? AND phase != 'archived' "
                "ORDER BY phase = 'draft', created_at, rowid",
                (context.participant_id,),
            )

    def character_sheet(
        self, context: ParticipantContext, character_id: str
    ) -> tuple[CharacterRecord, dict] | None:
        """One of the caller's unarchived characters and the module's sheet
        for it, from one committed state; None if it is not theirs, is
        archived, or the caller's authority has ended."""
        _check_context(context)
        runtime = self._loaded_runtime()
        if runtime.characters is None or not isinstance(character_id, str):
            return None
        with self.snapshot():
            if context.authenticated and not self._authority_is_active(context):
                return None
            character = self._owned(context, character_id)
            if character is None:
                return None
            return character, runtime.character_sheet(self._state(), character_id)

    def inspect_characters(self) -> list[CharacterRecord]:
        """Operator/test inspection of every character, archived ones too."""
        return self._characters("ORDER BY created_at, rowid", ())

    def _lifecycle(self, context, request_id, content, apply, precondition):
        """Run one lifecycle operation: authority, then exact replay, then
        `apply(runtime, state, now)`, which returns (new state or None,
        result) or raises Refusal or ActionRejected, rolling back whatever
        it did."""
        _check_context(context)
        if context.participant_id == REFEREE_PARTICIPANT:
            raise ValueError("The referee context owns no characters.")
        if not is_valid_request_id(request_id):
            return _invalid(
                None, "A request ID is 1 to 128 characters with no whitespace."
            ), False
        runtime = self._loaded_runtime()
        if runtime.characters is None:
            return error_message(
                "characters_unsupported",
                "This game has no characters.",
                request_id=request_id,
                revision=self._current_revision(),
            ), False
        current = None
        try:
            with self._write():
                if precondition is not None:
                    precondition()
                if context.authenticated and not self._authority_is_active(context):
                    return session_error("session_revoked"), False
                current = self._current_revision()
                record = self._find(context.participant_id, request_id)
                if record is not None:
                    if record.content == content:
                        return record.to_message(), False
                    raise Refusal(
                        "request_conflict",
                        "This request_id was already used for a different command.",
                    )
                try:
                    state, result = apply(runtime, self._state(), _now())
                except ActionRejected as rejection:
                    raise Refusal("action_rejected", rejection.message) from None
                record = CommandRecord(
                    revision=current + 1,
                    participant_id=context.participant_id,
                    request_id=request_id,
                    content=content,
                    result=result,
                )
                if state is not None:
                    self._write_state(state)
                self._insert_record(record)
                self._advance_revision(record.revision)
        except Refusal as refusal:
            return refusal.message(request_id, current), False
        return record.to_message(), True

    def _character(self, character_id: str) -> CharacterRecord | None:
        found = self._characters("WHERE character_id = ?", (character_id,))
        return found[0] if found else None

    def _owned(self, context, character_id: str) -> CharacterRecord | None:
        """The caller's unarchived character, or None: unknown, archived and
        another participant's look the same."""
        character = self._character(character_id)
        if (
            character is None
            or character.participant_id != context.participant_id
            or character.phase == "archived"
        ):
            return None
        return character

    def _characters(self, where: str, parameters: tuple) -> list[CharacterRecord]:
        rows = self._connection.execute(
            "SELECT character_id, participant_id, name, phase, draft_revision, "
            "created_at, completed_at, introduced_at, archived_at "
            f"FROM characters {where}",
            parameters,
        )
        return [
            CharacterRecord(
                row[0], row[1], row[2], row[3],
                row[4] if row[3] == "draft" else None, *row[5:],
            )
            for row in rows
        ]  # fmt: skip

    def _acting_job(self, character_id: str) -> bool:
        """Whether a running referee job acts for this character."""
        return (
            self._connection.execute(
                "SELECT 1 FROM referee_jobs AS j JOIN referee_inputs AS i "
                "ON i.id = j.input_id WHERE j.state = 'running' AND i.character_id = ?",
                (character_id,),
            ).fetchone()
            is not None
        )

    def _end_creation(self, character_id: str):
        """Completion or archiving ends the character's pending question and
        its unnarrated creation results (character contract section 8). A
        failed play job's results stay listed until a reply narrates them."""
        self._connection.execute(
            "DELETE FROM referee_choices WHERE character_id = ?", (character_id,)
        )
        self._connection.execute(
            "UPDATE referee_steps SET narrated = 1 WHERE character_id = ? "
            "AND status = 'committed' AND job_id IN (SELECT j.job_id FROM "
            "referee_jobs AS j JOIN referee_inputs AS i ON i.id = j.input_id "
            "WHERE i.mode = 'creation')",
            (character_id,),
        )

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
        """Whether an authenticated context's credential, or host link, is
        still valid.

        Development contexts carry neither and are never active here.
        """
        _check_context(context)
        if not context.authenticated:
            return False
        return self._authority_is_active(context)

    def _authority_is_active(self, context: ParticipantContext) -> bool:
        clause, parameters = _authority(context)
        return (
            self._connection.execute(f"SELECT {clause}", parameters).fetchone()[0] == 1
        )

    # Host links (docs/designs/lobby_contract.md section 5). None of these
    # change the mechanical revision or command history.

    def admit_host_player(
        self, host_id: str, player_id: str, name: str
    ) -> ParticipantContext:
        """The context of this host player's participant, creating both the
        participant and its link if the player has no active link.

        One transaction, so retries and concurrent admissions of the same
        player find one participant. The participant gets no credential.
        """
        _check_host_identity(host_id, player_id)
        _check_name(name)
        with self._write():
            row = self._active_link(host_id, player_id)
            if row is None:
                participant_id = PARTICIPANT_ID_PREFIX + secrets.token_hex(8)
                now = _now()
                self._connection.execute(
                    "INSERT INTO participants (participant_id, name, created_at) "
                    "VALUES (?, ?, ?)",
                    (participant_id, name, now),
                )
                link_id = self._insert_link(host_id, player_id, participant_id, now)
                row = (link_id, participant_id)
        return ParticipantContext(row[1], link_id=row[0])

    def link_host_player(
        self, host_id: str, player_id: str, participant_id: str
    ) -> int:
        """Link an existing participant to a host player; the operator's
        explicit migration. Refused if either is already linked."""
        _check_host_identity(host_id, player_id)
        with self._write():
            self._require_participant(participant_id)
            if self._active_link(host_id, player_id) is not None:
                raise StoreError(
                    "That player is already linked in this game; unlink them first."
                )
            if self._connection.execute(
                "SELECT 1 FROM host_links WHERE participant_id = ? "
                "AND unlinked_at IS NULL",
                (participant_id,),
            ).fetchone():
                raise StoreError(f"Participant {participant_id!r} is already linked.")
            return self._insert_link(host_id, player_id, participant_id, _now())

    def unlink_host_player(self, host_id: str, player_id: str) -> bool:
        """End the player's active link; False if there was none. The
        participant and its history stay."""
        _check_host_identity(host_id, player_id)
        with self._write():
            return (
                self._connection.execute(
                    "UPDATE host_links SET unlinked_at = ? WHERE host_id = ? "
                    "AND player_id = ? AND unlinked_at IS NULL",
                    (_now(), host_id, player_id),
                ).rowcount
                > 0
            )

    def host_links(self) -> list[HostLink]:
        rows = self._connection.execute(
            "SELECT id, host_id, player_id, participant_id, linked_at, unlinked_at "
            "FROM host_links ORDER BY id"
        )
        return [HostLink(*row) for row in rows]

    def _active_link(self, host_id: str, player_id: str):
        return self._connection.execute(
            "SELECT id, participant_id FROM host_links WHERE host_id = ? "
            "AND player_id = ? AND unlinked_at IS NULL",
            (host_id, player_id),
        ).fetchone()

    def _insert_link(self, host_id, player_id, participant_id, now) -> int:
        return self._connection.execute(
            "INSERT INTO host_links (host_id, player_id, participant_id, linked_at) "
            "VALUES (?, ?, ?, ?)",
            (host_id, player_id, participant_id, now),
        ).lastrowid

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
        # Seeded characters are ready and already in the scene (contract
        # section 13).
        self._connection.executemany(
            "INSERT INTO characters (character_id, participant_id, name, phase, "
            "draft_revision, created_at, completed_at, introduced_at) "
            "VALUES (?, ?, ?, 'ready', 1, ?, ?, ?)",
            [
                (c.character_id, c.participant_id, c.name, now, now, now)
                for c in seed.characters
            ],
        )
        self._connection.execute(
            "INSERT INTO seed (singleton, seed_version, provenance, created_at) "
            "VALUES (1, ?, ?, ?)",
            (seed.seed_version, json.dumps(seed.provenance, sort_keys=True), now),
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
        return _validate_store(self._connection)

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


class Refusal(Exception):
    """A character operation refused with nothing changed: a protocol error
    code, its fixed message and any extra fields (character contract
    section 9)."""

    def __init__(self, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.code, self.text, self.extra = code, message, dict(extra or {})

    def message(self, request_id: str, revision: int | None) -> dict:
        return {
            **error_message(
                self.code, self.text, request_id=request_id, revision=revision
            ),
            **self.extra,
        }


# Fresh instances each time: a raised exception keeps its traceback, so a
# shared one would grow with every refusal.
def _unavailable() -> Refusal:
    return Refusal("character_unavailable", "That character is not available to you.")


def _changed() -> Refusal:
    return Refusal(
        "character_changed",
        "Your character has changed; check your characters and try again.",
    )


def _busy() -> Refusal:
    return Refusal(
        "character_busy", "The referee is still answering for that character."
    )


def _mode_of(phase: str) -> str:
    return {"draft": "creation", "ready": "play"}[phase]


def _lifecycle_content(kind: str, **fields) -> dict:
    return {"version": PROTOCOL_VERSION, "type": kind, **fields}


def _invalid(request_id, message: str) -> dict:
    valid = request_id if is_valid_request_id(request_id) else None
    return error_message("invalid_request", message, request_id=valid)


def is_character_id(value) -> bool:
    """Whether `value` has the form every character ID has: c_ and 16
    lowercase hexadecimal digits."""
    return isinstance(value, str) and _CHARACTER_ID.fullmatch(value) is not None


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


def _authority(context: ParticipantContext) -> tuple[str, tuple]:
    """The SQL condition that the context's credential or host link is active."""
    if context.credential_id is not None:
        return (
            "EXISTS (SELECT 1 FROM credentials WHERE id = ? AND participant_id = ?"
            " AND revoked_at IS NULL)",
            (context.credential_id, context.participant_id),
        )
    return (
        "EXISTS (SELECT 1 FROM host_links WHERE id = ? AND participant_id = ?"
        " AND unlinked_at IS NULL)",
        (context.link_id, context.participant_id),
    )


def _check_host_identity(host_id, player_id):
    for value in (host_id, player_id):
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 128
            or any(character.isspace() for character in value)
        ):
            raise ValueError("Host and player IDs are opaque strings.")


def _is_busy(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return code is not None and code & 0xFF in (
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    )


def inspect_game_id(path) -> str:
    """Read an existing game's identity without migrations or module loading.

    Read-only SQLite mode sees committed WAL data too. Unlike open_existing,
    this does not initialize the store or change its journal mode/schema.
    """
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise StoreError("Campaign database must be an existing file.")
    connection = sqlite3.connect(
        path.as_uri() + "?mode=ro", uri=True, isolation_level=None
    )
    try:
        connection.execute("BEGIN")
        _validate_store(connection)
        game_id = connection.execute("SELECT game_id FROM game").fetchone()[0]
        try:
            if str(uuid.UUID(game_id)) != game_id:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise StoreError("The campaign has an invalid game ID.") from None
        return game_id
    finally:
        connection.close()


def _validate_store(connection: sqlite3.Connection) -> int:
    """Check identity and schema with reads only; return the schema version."""

    def pragma(name):
        return connection.execute(f"PRAGMA {name}").fetchone()[0]

    if pragma("application_id") != APPLICATION_ID:
        raise StoreError("Not a Tesserae game store; refusing to modify it.")
    version = pragma("user_version")
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
            for row in connection.execute(
                "SELECT name FROM pragma_table_info(?)", (table,)
            )
        ]
        if columns != expected:
            raise StoreError(f"Tesserae store table {table!r} is damaged.")
    if connection.execute("SELECT count(*) FROM game").fetchone()[0] != 1:
        raise StoreError("Tesserae store must hold exactly one game.")
    if (
        version >= 3
        and connection.execute("SELECT count(*) FROM module").fetchone()[0] != 1
    ):
        raise StoreError("Tesserae store must hold exactly one module binding.")
    return version
