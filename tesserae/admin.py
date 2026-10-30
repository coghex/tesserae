"""Local operator commands for participants and their join tokens.

Run on the host machine against the game's database file; there is no network
administration. A new token goes only to the token file the operator names.
It is never printed, logged, passed as an argument, or stored.
"""

import os
import sqlite3
import sys
from pathlib import Path

from .credentials import new_token, write_token_file
from .store import GameStore, StoreError


def run(args) -> int:
    try:
        return _COMMANDS[args.action](args)
    except (StoreError, sqlite3.Error, OSError, ValueError) as error:
        print(f"tesserae participant {args.action}: {error}", file=sys.stderr)
        return 1


def _add(args) -> int:
    created = not Path(args.database).exists()
    with _issued_token(args.token_file) as token, GameStore(args.database) as store:
        participant_id = store.add_participant(args.name, token)
    if created:
        print(f"Created a new game store at {args.database}.", file=sys.stderr)
    print(f"Added participant {participant_id} ({args.name}).")
    print(f"Wrote their token to {args.token_file}. Give it only to them.")
    return 0


def _list(args) -> int:
    with _existing(args.database) as store:
        records = store.participants()
    print("participant_id\tstatus\tcreated_at\tname")
    for record in records:
        status = "active" if record.active else "revoked"
        print(f"{record.participant_id}\t{status}\t{record.created_at}\t{record.name}")
    return 0


def _rotate(args) -> int:
    with _existing(args.database) as store, _issued_token(args.token_file) as token:
        store.rotate_credential(args.participant_id, token)
    print(f"Rotated {args.participant_id}: the old token no longer works.")
    print(f"Wrote the new token to {args.token_file}. Give it only to them.")
    return 0


def _revoke(args) -> int:
    with _existing(args.database) as store:
        revoked = store.revoke_credential(args.participant_id)
    if revoked:
        print(f"Revoked {args.participant_id}'s token. Their history is kept.")
    else:
        print(f"{args.participant_id} had no active token.")
    return 0


_COMMANDS = {"add": _add, "list": _list, "rotate": _rotate, "revoke": _revoke}


def _existing(database) -> GameStore:
    if not Path(database).is_file():
        raise StoreError(f"No game store at {database}.")
    return GameStore(database)


class _issued_token:
    """Write a new token file first; remove it again if the store change fails.

    Writing first means an existing file or bad path fails before anything is
    committed, and a committed credential always has its token on disk.
    """

    def __init__(self, path):
        self.path = path

    def __enter__(self) -> str:
        token = new_token()
        try:
            write_token_file(self.path, token)
        except FileExistsError:
            raise OSError(f"Refusing to overwrite existing file {self.path}.") from None
        return token

    def __exit__(self, kind, error, traceback):
        if kind is not None:
            os.remove(self.path)
