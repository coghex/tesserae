"""The bundled catalog: the `builtin.dice` provider and the `dice` module.

`dice` is the compatibility module for games without one: its `roll`
command behaves exactly as the original bootstrap command did.
"""

from .dice import DiceExpressionError, parse_expression, roll_expression
from .modules import (
    ArgumentError,
    Catalog,
    Command,
    Module,
    Outcome,
    Provider,
    ProviderChoice,
    Runtime,
    exact_fields,
)

DICE_OPERATION = "dice.roll.v1"


def _roll(randint, expression: str) -> dict:
    """`dice.roll.v1`: roll a validated expression; returns its result object."""
    return roll_expression(expression, randint=randint).as_dict()


DICE_PROVIDER = Provider("builtin.dice", "1", DICE_OPERATION, _roll)


def _validate_roll(args: dict):
    exact_fields(args, {"expression"}, "Roll args")
    if not isinstance(args["expression"], str):
        raise ArgumentError("'expression' must be a string.")
    try:
        parse_expression(args["expression"])
    except DiceExpressionError as error:
        raise ArgumentError(str(error), code="invalid_expression") from None


def _execute_roll(action) -> Outcome:
    return Outcome(
        state=action.state,
        result=action.operations[DICE_OPERATION](action.args["expression"]),
    )


DICE_MODULE = Module(
    id="dice",
    version="1",
    state_version=1,
    commands=(Command("roll", _validate_roll, _execute_roll),),
    initial_state=dict,
    view=lambda state, participant_id: None,  # snapshots stay metadata-only
    requires=frozenset({DICE_OPERATION}),
    providers=(
        ProviderChoice(DICE_OPERATION, DICE_PROVIDER.id, DICE_PROVIDER.version),
    ),
    tags=frozenset({"dice"}),
)

CATALOG = Catalog(modules=[DICE_MODULE], providers=[DICE_PROVIDER])
DICE_RUNTIME: Runtime = CATALOG.resolve("dice")
