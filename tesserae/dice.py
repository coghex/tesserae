"""Dice arithmetic, without game-specific success or critical rules."""

import re
from dataclasses import dataclass
from random import SystemRandom
from typing import Callable

MAX_EXPRESSION_LENGTH = 4096
MAX_DICE = 1000
MAX_SIDES = 1_000_000
_TERM = re.compile(r"(?:(?P<count>[0-9]*)[dD](?P<sides>[0-9]+)|(?P<constant>[0-9]+))")


class DiceExpressionError(ValueError):
    """An expression is invalid or exceeds this utility's resource limits."""


@dataclass(frozen=True)
class Term:
    sign: int
    count: int = 0
    sides: int = 0
    constant: int = 0


@dataclass(frozen=True)
class TermResult:
    term: Term
    rolls: tuple[int, ...]

    @property
    def subtotal(self) -> int:
        return self.term.sign * (
            sum(self.rolls) if self.term.count else self.term.constant
        )

    def as_dict(self) -> dict:
        common = {"sign": self.term.sign, "subtotal": self.subtotal}
        if self.term.count:
            return {
                "kind": "dice",
                "count": self.term.count,
                "sides": self.term.sides,
                "rolls": list(self.rolls),
                **common,
            }
        return {"kind": "constant", "value": self.term.constant, **common}


@dataclass(frozen=True)
class RollResult:
    expression: str
    terms: tuple[TermResult, ...]

    @property
    def total(self) -> int:
        return sum(term.subtotal for term in self.terms)

    def as_dict(self) -> dict:
        return {
            "schema_version": 1,
            "expression": self.expression,
            "terms": [term.as_dict() for term in self.terms],
            "total": self.total,
        }


def parse_expression(expression: str) -> tuple[Term, ...]:
    """Parse signed NdS terms and integers joined by + or -, before rolling."""
    if not isinstance(expression, str) or not expression.strip():
        raise DiceExpressionError("Provide a dice expression, for example '2d6+3'.")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise DiceExpressionError(
            f"Expression exceeds {MAX_EXPRESSION_LENGTH} characters."
        )
    text = expression.strip()
    position = 0
    terms = []
    dice_count = 0
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        sign = 1
        if position < len(text) and text[position] in "+-":
            sign = -1 if text[position] == "-" else 1
            position += 1
        elif terms:
            raise DiceExpressionError("Separate terms with '+' or '-'.")
        while position < len(text) and text[position].isspace():
            position += 1
        match = _TERM.match(text, position)
        if match is None:
            raise DiceExpressionError(
                "Expected NdS dice or an integer; only '+' and '-' are supported."
            )
        if match.group("sides") is not None:
            count = int(match.group("count") or "1")
            sides = int(match.group("sides"))
            if count < 1 or not 1 <= sides <= MAX_SIDES:
                raise DiceExpressionError(
                    f"Dice need a positive count and 1 to {MAX_SIDES} sides."
                )
            dice_count += count
            if dice_count > MAX_DICE:
                raise DiceExpressionError(f"Expression exceeds {MAX_DICE} dice.")
            terms.append(Term(sign=sign, count=count, sides=sides))
        else:
            terms.append(Term(sign=sign, constant=int(match.group("constant"))))
        position = match.end()
    return tuple(terms)


def roll_expression(
    expression: str, *, randint: Callable[[int, int], int] | None = None
) -> RollResult:
    """Roll a fully validated expression; injectable randomness supports testing."""
    parsed = parse_expression(expression)
    draw = SystemRandom().randint if randint is None else randint
    results = []
    for term in parsed:
        rolls = tuple(draw(1, term.sides) for _ in range(term.count))
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= term.sides
            for value in rolls
        ):
            raise ValueError(
                "Random source returned a value outside the die's integer range."
            )
        results.append(TermResult(term=term, rolls=rolls))
    return RollResult(expression=expression.strip(), terms=tuple(results))
