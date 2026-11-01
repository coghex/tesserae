"""Relay repair: the original public demo module.

A storm-damaged relay is offline. Each participant may enlist one worker
(repair 1, effort 3), then attempt 1d6 + repair against 5, spending one
effort win or lose. Success restores the shared relay. The theme and numbers
are this module's own; the host gains no character, resource or check rules.
See docs/relay.md.
"""

import unicodedata

from .modules import (
    ActionRejected,
    ArgumentError,
    Command,
    Module,
    Outcome,
    ProviderChoice,
    exact_fields,
)

DICE_OPERATION = "dice.roll.v1"
MAX_WORKER_NAME = 40
STARTING_REPAIR = 1
STARTING_EFFORT = 3
REPAIR_DIE = "1d6"
REPAIR_TARGET = 5

SCENE = {
    "id": "relay_station",
    "title": "The storm-damaged relay",
    "description": (
        "Last night's storm knocked the hilltop relay offline. Workers have "
        "come up to get it running again."
    ),
}
RULES = {
    "relay.enlist": (
        'Enlist your worker once: {"name": "..."}. Workers start with repair 1 '
        "and effort 3."
    ),
    "relay.repair": (
        f"Attempt a repair: {{}}. Roll {REPAIR_DIE} + repair; {REPAIR_TARGET} or "
        "more restores the relay. Each attempt spends 1 effort, success or not."
    ),
}


def initial_state() -> dict:
    return {
        "relay": {"restored": False},
        "workers": {},  # keyed by trusted participant ID
        "referee": {
            # Never sent to a player: this exercises the view boundary.
            "maintenance_note": "The backup fuse in the east cabinet is spent.",
        },
    }


def _validate_enlist(args: dict):
    exact_fields(args, {"name"}, "Enlist args")
    name = args["name"]
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > MAX_WORKER_NAME
        or any(unicodedata.category(character) == "Cc" for character in name)
    ):
        raise ArgumentError(
            f"'name' is 1 to {MAX_WORKER_NAME} characters, not blank, "
            "with no control characters."
        )


def _enlist(action) -> Outcome:
    state = action.state
    if action.participant_id in state["workers"]:
        raise ActionRejected("You have already enlisted a worker.")
    worker = {
        "name": action.args["name"],
        "repair": STARTING_REPAIR,
        "effort": STARTING_EFFORT,
    }
    state["workers"][action.participant_id] = worker
    return Outcome(state, {"worker": dict(worker), "relay": dict(state["relay"])})


def _validate_repair(args: dict):
    exact_fields(args, (), "Repair args")


def _repair(action) -> Outcome:
    state = action.state
    worker = state["workers"].get(action.participant_id)
    if worker is None:
        raise ActionRejected("Enlist a worker before attempting a repair.")
    if state["relay"]["restored"]:
        raise ActionRejected("The relay is already restored.")
    if worker["effort"] <= 0:
        raise ActionRejected("Your worker has no effort left.")

    roll = action.operations[DICE_OPERATION](REPAIR_DIE)
    total = roll["total"] + worker["repair"]
    success = total >= REPAIR_TARGET
    before = worker["effort"]
    worker["effort"] = before - 1
    if success:
        state["relay"]["restored"] = True
    return Outcome(
        state,
        {
            "check": {
                "roll": roll,
                "repair": worker["repair"],
                "total": total,
                "target": REPAIR_TARGET,
                "success": success,
            },
            "effort": {"before": before, "after": worker["effort"]},
            "relay": dict(state["relay"]),
        },
    )


def view(state: dict, participant_id: str) -> dict:
    """The public scene, the rules, and only the caller's own worker."""
    worker = state["workers"].get(participant_id)
    restored = state["relay"]["restored"]
    if worker is None:
        available = ["relay.enlist"]
    elif worker["effort"] > 0 and not restored:
        available = ["relay.repair"]
    else:
        available = []
    return {
        "scene": {**SCENE, "restored": restored},
        "rules": dict(RULES),
        "character": None if worker is None else dict(worker),
        "commands": available,
    }


def referee_context(state: dict) -> dict:
    """Everything the referee needs: the scene, the rules, every worker, and
    the hidden maintenance note, which no player view includes."""
    return {
        "scene": {**SCENE, "restored": state["relay"]["restored"]},
        "rules": dict(RULES),
        "workers": {pid: dict(worker) for pid, worker in state["workers"].items()},
        "referee_only": dict(state["referee"]),
    }


def referee_actions(state: dict, participant_id: str) -> list:
    """The commands `participant_id` may use now, with what each costs."""
    worker = state["workers"].get(participant_id)
    if worker is None:
        return [
            {
                "command": "relay.enlist",
                "args": {"name": "the worker's name, 1 to 40 characters"},
                "description": RULES["relay.enlist"],
                "stakes": "None: no dice, nothing spent. Only once per player.",
            }
        ]
    if worker["effort"] > 0 and not state["relay"]["restored"]:
        return [
            {
                "command": "relay.repair",
                "args": {},
                "description": RULES["relay.repair"],
                "stakes": (
                    f"Roll {REPAIR_DIE} + repair {worker['repair']} against "
                    f"{REPAIR_TARGET}. Spends 1 of {worker['effort']} effort, "
                    "success or not; success restores the relay."
                ),
            }
        ]
    return []


RELAY_MODULE = Module(
    id="relay",
    version="1",
    state_version=1,
    commands=(
        Command("relay.enlist", _validate_enlist, _enlist),
        Command("relay.repair", _validate_repair, _repair),
    ),
    initial_state=initial_state,
    view=view,
    requires=frozenset({DICE_OPERATION}),
    providers=(ProviderChoice(DICE_OPERATION, "builtin.dice", "1"),),
    tags=frozenset({"demo", "original", "dice"}),
    requires_authentication=True,
    referee_context=referee_context,
    referee_actions=referee_actions,
)
