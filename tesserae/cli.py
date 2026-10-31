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
        description="Serve one game over WebSocket until Ctrl-C, on 127.0.0.1 "
        "unless --host says otherwise (which requires authenticated TLS). "
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
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="One IPv4 address of this computer to listen on (default 127.0.0.1). "
        "Any other address needs --authenticated, --tls-cert, and --tls-key.",
    )
    serve.add_argument(
        "--tls-cert", help="Certificate to present, for wss://. See docs/tls.md."
    )
    serve.add_argument(
        "--tls-key", help="Its private key, unencrypted and readable only by you."
    )
    connect = commands.add_parser(
        "connect",
        help="Join an authenticated host and stream JSON lines over stdin/stdout.",
        description="Join the game at URI with the token in --token-file, then "
        "send each nonblank stdin line as one message and print every message "
        "from the host as one JSON line. Diagnostics go to stderr. Exit status: "
        "0 when stdin ended and every request was answered, 1 when the client "
        "could not start or the host ended the session, 3 when some requests "
        "got no reply (resend them unchanged), 130 on Ctrl-C.",
    )
    connect.add_argument(
        "uri",
        help="The host's address: wss://HOST:PORT/, or ws://127.0.0.1:PORT/ "
        "on this computer only.",
    )
    connect.add_argument(
        "--token-file", required=True, help="File holding your personal join token."
    )
    connect.add_argument(
        "--game-id", help="Refuse to send anything unless the host serves this game."
    )
    connect.add_argument(
        "--ca-file",
        help="The host's certificate to trust, for wss:// (see docs/tls.md). "
        "Without it, wss:// uses the system's trusted authorities.",
    )
    connect.add_argument(
        "--drain-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for outstanding replies after stdin ends (default 10).",
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
    if args.command == "connect":
        from .client import run

        return run(
            args.uri, args.token_file, args.game_id, args.drain_timeout, args.ca_file
        )
    if args.command == "participant":
        from .admin import run

        return run(args)
    if args.command == "serve":
        from .server import run_foreground

        return run_foreground(
            args.database,
            args.port,
            authenticated=args.authenticated,
            host=args.host,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
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
