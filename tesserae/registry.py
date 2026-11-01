"""Local host identities and campaign access, separate from game state.

One registry/connection per thread. Short authorization guards use rollback
journal read locks to order protected operations before access revocation.
Never hold one over model execution or a human decision; this is not a
transaction spanning the registry and a campaign database.
"""

import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .credentials import token_digest
from .store import check_name, inspect_game_id

APPLICATION_ID = 0x54524547  # TREG, distinct from a game store
SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE host (singleton INTEGER PRIMARY KEY CHECK (singleton = 1), host_id TEXT NOT NULL);
CREATE TABLE players (player_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE credentials (
    id INTEGER PRIMARY KEY, player_id TEXT NOT NULL REFERENCES players(player_id),
    digest TEXT NOT NULL UNIQUE, issued_at TEXT NOT NULL, revoked_at TEXT
);
CREATE UNIQUE INDEX one_active_credential ON credentials(player_id) WHERE revoked_at IS NULL;
CREATE TABLE campaigns (
    campaign_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)), created_at TEXT NOT NULL
);
CREATE TABLE memberships (
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
    player_id TEXT NOT NULL REFERENCES players(player_id),
    granted_at TEXT NOT NULL, revoked_at TEXT,
    PRIMARY KEY (campaign_id, player_id)
);
"""
_COLUMNS = {
    "host": ["singleton", "host_id"],
    "players": ["player_id", "name", "created_at"],
    "credentials": ["id", "player_id", "digest", "issued_at", "revoked_at"],
    "campaigns": ["campaign_id", "path", "name", "active", "created_at"],
    "memberships": ["campaign_id", "player_id", "granted_at", "revoked_at"],
}


class RegistryError(RuntimeError):
    """Invalid registry, operator request or stale/unauthorized host context."""


class CredentialRevoked(RegistryError):
    """The host context's credential is no longer active."""


class CampaignUnavailable(RegistryError):
    """The campaign is unknown, inactive or not granted to this player."""


@dataclass(frozen=True)
class HostPlayerContext:
    """Host-created authority, never constructed from client-asserted IDs."""

    host_id: str
    player_id: str
    credential_id: int


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(token):
    if (
        not isinstance(token, str)
        or not 1 <= len(token) <= 512
        or any(character.isspace() for character in token)
    ):
        raise ValueError("Invalid token.")
    try:
        return token_digest(token)
    except UnicodeError:
        raise ValueError("Invalid token.") from None


class HostRegistry:
    def __init__(self, path):
        """Open and validate an existing registry; never create or migrate one."""
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise RegistryError("No host registry at that path; use 'host init' first.")
        self._connection = sqlite3.connect(
            self.path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._validate()
            self._connection.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            self._connection.close()
            raise

    @classmethod
    def create(cls, path):
        path = Path(path).expanduser().absolute()
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        connection = None
        try:
            connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)
            connection.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
            connection.execute("INSERT INTO host VALUES (1, ?)", (str(uuid.uuid4()),))
            connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()
            connection.close()
            connection = None
            return cls(path)
        except BaseException:
            if connection is not None:
                connection.close()
            path.unlink(missing_ok=True)
            Path(str(path) + "-journal").unlink(missing_ok=True)
            raise

    def _validate(self):
        connection = self._connection
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise RegistryError("Not a Tesserae host registry; refusing to modify it.")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise RegistryError(f"Unsupported host registry schema version {version}.")
        for table, expected in _COLUMNS.items():
            actual = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM pragma_table_info(?)", (table,)
                )
            ]
            if actual != expected:
                raise RegistryError(f"Host registry table {table!r} is damaged.")
        rows = connection.execute("SELECT singleton, host_id FROM host").fetchall()
        if len(rows) != 1 or rows[0][0] != 1:
            raise RegistryError("Host registry must contain exactly one host.")
        self.host_id = rows[0][1]
        try:
            if str(uuid.UUID(self.host_id)) != self.host_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise RegistryError("Host registry has an invalid host ID.") from None
        if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise RegistryError("Host registry requires DELETE rollback journal mode.")

    def close(self):
        self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _transaction(self, *, write=False):
        self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    def _records(self, sql, parameters=()):
        return [dict(row) for row in self._connection.execute(sql, parameters)]

    def add_player(self, name, token):
        check_name(name)
        digest = _digest(token)
        player_id = "u_" + secrets.token_hex(8)
        with self._transaction(write=True):
            self._connection.execute(
                "INSERT INTO players VALUES (?, ?, ?)", (player_id, name, _now())
            )
            self._issue(player_id, digest)
        return player_id

    def require_player(self, player_id):
        """Raise RegistryError unless the host player exists."""
        self._require_player(player_id)

    def campaign(self, campaign_id):
        """One operator record, including its private path."""
        return self._campaign(campaign_id)

    def _require_player(self, player_id):
        if not self._connection.execute(
            "SELECT 1 FROM players WHERE player_id = ?", (player_id,)
        ).fetchone():
            raise RegistryError("Unknown host player.")

    def _issue(self, player_id, digest):
        self._connection.execute(
            "INSERT INTO credentials (player_id, digest, issued_at) VALUES (?, ?, ?)",
            (player_id, digest, _now()),
        )

    def players(self):
        return self._records(
            "SELECT player_id, name, created_at, EXISTS (SELECT 1 FROM credentials c "
            "WHERE c.player_id = players.player_id AND c.revoked_at IS NULL) AS active "
            "FROM players ORDER BY created_at, player_id"
        )

    def rotate_credential(self, player_id, token):
        digest = _digest(token)
        with self._transaction(write=True):
            self._require_player(player_id)
            self._revoke(player_id)
            self._issue(player_id, digest)

    def _revoke(self, player_id):
        return (
            self._connection.execute(
                "UPDATE credentials SET revoked_at = ? "
                "WHERE player_id = ? AND revoked_at IS NULL",
                (_now(), player_id),
            ).rowcount
            > 0
        )

    def revoke_credential(self, player_id):
        with self._transaction(write=True):
            self._require_player(player_id)
            return self._revoke(player_id)

    def authenticate(self, token):
        try:
            digest = _digest(token)
        except ValueError:
            return None
        row = self._connection.execute(
            "SELECT player_id, id FROM credentials WHERE digest = ? AND revoked_at IS NULL",
            (digest,),
        ).fetchone()
        return HostPlayerContext(self.host_id, row[0], row[1]) if row else None

    def credential_active(self, context):
        return (
            isinstance(context, HostPlayerContext)
            and context.host_id == self.host_id
            and self._connection.execute(
                "SELECT 1 FROM credentials WHERE id = ? AND player_id = ? AND revoked_at IS NULL",
                (context.credential_id, context.player_id),
            ).fetchone()
            is not None
        )

    def register_campaign(self, path, name):
        check_name(name)
        path = Path(path).expanduser().resolve(strict=True)
        campaign_id = inspect_game_id(path)
        with self._transaction(write=True):
            if self._connection.execute(
                "SELECT 1 FROM campaigns WHERE campaign_id = ? OR path = ?",
                (campaign_id, str(path)),
            ).fetchone():
                raise RegistryError("Campaign identity or path is already registered.")
            self._connection.execute(
                "INSERT INTO campaigns VALUES (?, ?, ?, 0, ?)",
                (campaign_id, str(path), name, _now()),
            )
        return campaign_id

    def campaigns(self):
        """Operator records including private paths; never a player payload."""
        return self._records("SELECT * FROM campaigns ORDER BY name, campaign_id")

    def _campaign(self, campaign_id):
        row = self._connection.execute(
            "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()
        if row is None:
            raise RegistryError("Unknown campaign.")
        return dict(row)

    def set_campaign_active(self, campaign_id, active):
        if not isinstance(active, bool):
            raise ValueError("Campaign active must be a boolean.")
        with self._transaction(write=True):
            self._campaign(campaign_id)
            self._connection.execute(
                "UPDATE campaigns SET active = ? WHERE campaign_id = ?",
                (active, campaign_id),
            )

    def grant(self, campaign_id, player_id):
        with self._transaction(write=True):
            self._campaign(campaign_id)
            self._require_player(player_id)
            self._connection.execute(
                "INSERT INTO memberships VALUES (?, ?, ?, NULL) "
                "ON CONFLICT (campaign_id, player_id) DO UPDATE SET "
                "granted_at = CASE WHEN revoked_at IS NULL THEN granted_at ELSE excluded.granted_at END, "
                "revoked_at = NULL",
                (campaign_id, player_id, _now()),
            )

    def revoke_access(self, campaign_id, player_id):
        with self._transaction(write=True):
            self._campaign(campaign_id)
            self._require_player(player_id)
            return (
                self._connection.execute(
                    "UPDATE memberships SET revoked_at = ? "
                    "WHERE campaign_id = ? AND player_id = ? AND revoked_at IS NULL",
                    (_now(), campaign_id, player_id),
                ).rowcount
                > 0
            )

    def members(self, campaign_id):
        self._campaign(campaign_id)
        return self._records(
            "SELECT m.player_id, p.name, m.granted_at, m.revoked_at "
            "FROM memberships m JOIN players p ON p.player_id = m.player_id "
            "WHERE m.campaign_id = ? ORDER BY m.player_id",
            (campaign_id,),
        )

    def _require_context(self, context):
        if not self.credential_active(context):
            raise CredentialRevoked("Host authorization is no longer valid.")

    def player_name(self, context):
        """The context's player's display name, rechecking its credential."""
        with self._transaction():
            self._require_context(context)
            return self._connection.execute(
                "SELECT name FROM players WHERE player_id = ?", (context.player_id,)
            ).fetchone()[0]

    def visible_campaigns(self, context):
        with self._transaction():
            self._require_context(context)
            return self._records(
                "SELECT c.campaign_id, c.name FROM campaigns c JOIN memberships m "
                "ON c.campaign_id = m.campaign_id WHERE m.player_id = ? "
                "AND m.revoked_at IS NULL AND c.active = 1 ORDER BY c.name, c.campaign_id",
                (context.player_id,),
            )

    @contextmanager
    def authorize_campaign(self, context, campaign_id):
        """Yield an operator-only record for a short protected operation.

        In DELETE journal mode the read lock blocks revocation COMMIT until
        this exits. Not reentrant; use a separate registry per worker thread.
        This neither opens a game nor grants game participant authority.
        """
        with self._transaction():
            self._require_context(context)
            row = self._connection.execute(
                "SELECT c.* FROM campaigns c JOIN memberships m ON c.campaign_id = m.campaign_id "
                "WHERE c.campaign_id = ? AND c.active = 1 AND m.player_id = ? AND m.revoked_at IS NULL",
                (campaign_id, context.player_id),
            ).fetchone()
            if row is None:
                raise CampaignUnavailable("Campaign is not available to this player.")
            yield dict(row)
