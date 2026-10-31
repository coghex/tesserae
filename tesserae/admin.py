"""Local operator commands: creating games, and participants with their tokens.

Run on the host machine against the game's database file; there is no network
administration. A new token goes only to the token file the operator names.
It is never printed, logged, passed as an argument, or stored.
"""

import os
import sqlite3
import sys
from pathlib import Path

from .builtin import CATALOG
from .credentials import new_token, write_token_file
from .external import load_module_dir
from .modules import ModuleError, SeedError
from .seed import read_seed
from .store import GameStore, StoreError


def run_game(args) -> int:
    """`tesserae game create`: a new game bound to one bundled module, or to
    the module in an explicit package directory (`--module-dir`), optionally
    starting from a seed file (`--seed`)."""
    try:
        runtime = _runtime(args.module, args.module_dir)  # before any file exists
        seed = None if args.seed is None else read_seed(args.seed)
        with GameStore.create(args.database, runtime, seed=seed) as store:
            game_id = store.game_id
    except (ModuleError, SeedError, StoreError, sqlite3.Error, OSError) as error:
        print(f"tesserae game create: {error}", file=sys.stderr)
        return 1
    module = runtime.module
    print(f"Created game {game_id} ({module.id} {module.version}) at {args.database}.")
    if seed is None:
        print("Add its participants with 'tesserae participant add'.")
        return 0
    print(
        f"Started from the seed, with {len(seed.participants)} participants and no tokens:"
    )
    for participant in seed.participants:
        print(f"{participant.participant_id}\t{participant.name}")
    print("Issue each a token with 'tesserae participant rotate'.")
    return 0


def _runtime(module_id: str, module_dir):
    if module_dir is None:
        return CATALOG.resolve(module_id)
    runtime = load_module_dir(module_dir)
    if runtime.module.id != module_id:
        raise ModuleError(
            f"The package in {module_dir} is module {runtime.module.id!r}, "
            f"not {module_id!r}."
        )
    return runtime


def run_referee(args) -> int:
    """`tesserae referee enable|setup|check`."""
    if args.action in ("setup", "check"):
        return _referee_folder(args)
    from .referee.store import RefereeStore

    try:
        with _existing(args.database) as store:
            RefereeStore(store).enable()
    except (StoreError, sqlite3.Error, OSError) as error:
        print(f"tesserae referee {args.action}: {error}", file=sys.stderr)
        return 1
    print(f"Referee mode is on for {args.database}.")
    return 0


def _referee_folder(args) -> int:
    from .referee import runner

    root = (
        Path(args.referee_dir).expanduser() if args.referee_dir else runner.DEFAULT_DIR
    )
    if args.action == "setup":
        folder = runner.setup(root)
        print(f"Referee folder ready at {folder.root}.")
        print(f"Log Codex in for the referee once, with: {folder.login_command()}")
        return 0
    report = runner.check(root)
    print(f"Referee folder: {report['folder']}")
    print(f"Codex: {report['codex_version'] or 'not found'}")
    print(
        f"Config current: {'yes' if report['config_current'] else 'no (run tesserae referee setup)'}"
    )
    print(
        f"Logged in: {'yes' if report['logged_in'] else 'no (' + runner.RefereeDir(root).login_command() + ')'}"
    )
    return (
        0
        if report["codex_version"] and report["config_current"] and report["logged_in"]
        else 1
    )


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
        if record.active:
            status = "active"
        else:
            status = "revoked" if record.ever_issued else "no token"
        print(f"{record.participant_id}\t{status}\t{record.created_at}\t{record.name}")
    return 0


def _rotate(args) -> int:
    with _existing(args.database) as store, _issued_token(args.token_file) as token:
        replaced = store.rotate_credential(args.participant_id, token)
    if replaced:
        print(f"Rotated {args.participant_id}: the old token no longer works.")
    else:
        print(f"Issued {args.participant_id} a token; they had no active one.")
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
