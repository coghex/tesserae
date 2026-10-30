"""Noninteractive CLI adapter; game mechanics do not live here."""

import argparse
import json
import sys

from .dice import DiceExpressionError, RollResult, roll_expression


def format_roll(result: RollResult) -> str:
    lines = [f"Expression: {result.expression}"]
    for item in result.terms:
        sign = "+" if item.term.sign > 0 else "-"
        if item.term.count:
            lines.append(
                f"{sign} {item.term.count}d{item.term.sides}: {list(item.rolls)} = {item.subtotal}"
            )
        else:
            lines.append(f"{sign} {item.term.constant} = {item.subtotal}")
    lines.append(f"Total: {result.total}")
    return "\n".join(lines)


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        port = -1
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(
            f"must be an integer from 0 to 65535: {text!r}"
        )
    return port


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tesserae", description="Tools for modular tabletop play."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    roll = commands.add_parser(
        "roll", help="Standalone dice arithmetic; not saved to a campaign."
    )
    roll.add_argument(
        "expression", help="NdS terms and integers joined by + or -, e.g. '2d6+3'."
    )
    roll.add_argument("--json", action="store_true", help="Emit a structured result.")
    serve = commands.add_parser(
        "serve",
        help="Run a loopback-only game host in the foreground.",
        description="Serve one game over WebSocket on 127.0.0.1 until Ctrl-C. "
        "By default every connection acts as one fixed development participant; "
        "with --authenticated, each must join with a personal token.",
    )
    serve.add_argument(
        "--database", required=True, help="SQLite game store; created if missing."
    )
    serve.add_argument(
        "--port", required=True, type=_port, help="TCP port; 0 picks a free one."
    )
    serve.add_argument(
        "--authenticated",
        action="store_true",
        help="Require each connection to join with a token from "
        "'tesserae participant'. There is no fallback to development mode.",
    )
    participant = commands.add_parser(
        "participant",
        help="Manage this game's participants and their join tokens (local only).",
        description="Operator commands, run on the host against its database. "
        "New tokens are written only to the --token-file you name, which must "
        "not exist yet, with user-only permissions.",
    )
    actions = participant.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add", help="Add a participant and issue their token.")
    add.add_argument("--name", required=True, help="Display label; not an identity.")
    actions.add_parser("list", help="List participants and whether they can join.")
    rotate = actions.add_parser(
        "rotate", help="Issue a new token; the old one stops working."
    )
    revoke = actions.add_parser(
        "revoke", help="Revoke a token; the participant and history stay."
    )
    for action in (rotate, revoke):
        action.add_argument("participant_id")
    for action in (add, rotate):
        action.add_argument(
            "--token-file", required=True, help="New file to write the token to."
        )
    for action in actions.choices.values():
        action.add_argument(
            "--database", required=True, help="The game's SQLite store."
        )
    args = parser.parse_args(argv)
    if args.command == "participant":
        from .admin import run

        return run(args)
    if args.command == "serve":
        from .server import run_foreground

        return run_foreground(
            args.database, args.port, authenticated=args.authenticated
        )
    try:
        result = roll_expression(args.expression)
    except DiceExpressionError as error:
        if args.json:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "error": {"code": "invalid_expression", "message": str(error)},
                    }
                ),
                file=sys.stderr,
            )
        else:
            print(f"tesserae: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict()) if args.json else format_roll(result))
    return 0
