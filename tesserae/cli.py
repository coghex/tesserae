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
            lines.append(f"{sign} {item.term.count}d{item.term.sides}: {list(item.rolls)} = {item.subtotal}")
        else:
            lines.append(f"{sign} {item.term.constant} = {item.subtotal}")
    lines.append(f"Total: {result.total}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tesserae", description="Tools for modular tabletop play.")
    commands = parser.add_subparsers(dest="command", required=True)
    roll = commands.add_parser("roll", help="Standalone dice arithmetic; not saved to a campaign.")
    roll.add_argument("expression", help="NdS terms and integers joined by + or -, e.g. '2d6+3'.")
    roll.add_argument("--json", action="store_true", help="Emit a structured result.")
    args = parser.parse_args(argv)
    try:
        result = roll_expression(args.expression)
    except DiceExpressionError as error:
        if args.json:
            print(json.dumps({"schema_version": 1, "error": {"code": "invalid_expression",
                                                           "message": str(error)}}), file=sys.stderr)
        else:
            print(f"tesserae: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict()) if args.json else format_roll(result))
    return 0
