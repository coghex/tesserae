"""Local operator commands: creating games, and participants with their tokens.

Run on the host machine against the game's database file; there is no network
administration. A new token goes only to the token file the operator names.
It is never printed, logged, passed as an argument, or stored.
"""

import sqlite3
import sys
from pathlib import Path

from .builtin import CATALOG
from .credentials import IssuedToken
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
    """`tesserae referee enable|stop|rebuild|setup|check`."""
    if args.action in ("setup", "check"):
        return _referee_folder(args)
    if args.action in ("stop", "rebuild"):
        from .referee.runner import RunnerError
        from .referee.store import RefereeError

        try:
            print(_REFEREE_GAME_ACTIONS[args.action](args))
        except (RunnerError, RefereeError, StoreError, sqlite3.Error, OSError) as error:
            print(f"tesserae referee {args.action}: {error}", file=sys.stderr)
            return 1
        return 0
    from .referee.store import RefereeStore

    try:
        with _existing(args.database) as store:
            RefereeStore(store).enable()
    except (StoreError, sqlite3.Error, OSError) as error:
        print(f"tesserae referee {args.action}: {error}", file=sys.stderr)
        return 1
    print(f"Referee mode is on for {args.database}.")
    return 0


def _referee_game(args):
    """The referee folder and this game's ID."""
    from .referee import runner

    root = args.referee_dir and Path(args.referee_dir).expanduser()
    with _existing(args.database) as store:
        game_id = store.game_id
    return runner.RefereeDir(root or runner.DEFAULT_DIR), game_id


def _ask_host(folder, game_id: str):
    """The running host's answer to a stop request, or None with no host."""
    import json
    import socket

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10)
            connection.connect(str(folder.control_path(game_id)))
            connection.sendall(b'{"stop": true}\n')
            with connection.makefile("rb") as stream:
                return json.loads(stream.readline())
    except (FileNotFoundError, ConnectionRefusedError):
        return None


def _referee_stop(args) -> str:
    from .referee import runner

    folder, game_id = _referee_game(args)
    lock_path = folder.lock_path(game_id)
    if args.clear_stuck:
        return runner.clear_stuck(lock_path)
    answer = _ask_host(folder, game_id)
    if answer is None:  # no host: only a leftover Codex that is provably the job's
        return runner.stop_orphan(lock_path)
    if not answer.get("ok"):
        raise runner.RunnerError(answer["error"]["message"])
    if answer["result"]["stopping"]:
        return "Asked the host to stop the running job; it ends interrupted."
    return "The host has no referee job running."


def _referee_rebuild(args) -> str:
    from .referee import runner
    from .referee.store import RefereeStore

    folder, game_id = _referee_game(args)
    if folder.control_path(game_id).exists() and _ask_alive(folder, game_id):
        raise runner.RunnerError("The game's host is running; stop it first.")
    with runner.JobLock(folder.lock_path(game_id)), _existing(args.database) as store:
        RefereeStore(store).rebuild()
    return (
        "Ended the game's referee session. The next job starts a new one from the "
        "stored conversation and rulings; the game itself is unchanged."
    )


def _ask_alive(folder, game_id: str) -> bool:
    import socket

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(folder.control_path(game_id)))
        return True
    except (FileNotFoundError, ConnectionRefusedError):
        return False


_REFEREE_GAME_ACTIONS = {"stop": _referee_stop, "rebuild": _referee_rebuild}


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
    with IssuedToken(args.token_file) as token, GameStore(args.database) as store:
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
    with _existing(args.database) as store, IssuedToken(args.token_file) as token:
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
