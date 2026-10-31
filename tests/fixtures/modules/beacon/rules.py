from tesserae.dice import roll_expression
from tesserae.external import Bundle
from tesserae.modules import (
    Command,
    Module,
    Outcome,
    Provider,
    ProviderChoice,
    exact_fields,
)

from . import signals

DICE = "dice.roll.v1"


def _steady_roll(randint, expression: str) -> dict:
    """`beacon.steady`: every die shows its highest face."""
    return roll_expression(expression, randint=lambda low, high: high).as_dict()


STEADY = Provider("beacon.steady", "1", DICE, _steady_roll)


def _validate_signal(args: dict):
    exact_fields(args, set(), "Signal args")


def _signal(action) -> Outcome:
    colours = signals.colours()
    roll = action.operations[DICE](f"1d{len(colours)}")
    colour = colours[roll["total"] - 1]
    state = action.state
    state["shown"].append(colour)
    return Outcome(state, {"roll": roll, "colour": colour})


BEACON = Module(
    id="beacon",
    version="1",
    state_version=1,
    commands=(Command("beacon.signal", _validate_signal, _signal),),
    initial_state=lambda: {"shown": []},
    view=lambda state, participant_id: {"last": (state["shown"] or [None])[-1]},
    requires=frozenset({DICE}),
    # Complete defaults, but ignored for an external package: module.toml
    # alone selects providers, and a partial manifest is never filled from here.
    providers=(ProviderChoice(DICE, "builtin.dice", "1"),),
)


def bundle() -> Bundle:
    return Bundle(module=BEACON, providers=(STEADY,))
