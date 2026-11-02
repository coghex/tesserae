"""Trailhead: the original public module with characters.

Walkers set out up a mountain trail. Each player creates walkers with the
referee: a trait (steady, quick or keen) and stamina rolled once as 1d3+1.
Completing a walker makes it playable; a keen walker gains one more stamina
then. A ready walker can push on (spend 1 stamina, 1d6 against 4, +1 if
quick; failing leaves it in danger) or recover (1d6 against 3, +1 if steady;
no cost). A walker in danger cannot be archived. The theme and numbers are
this module's own; it exists to exercise the host's character capability
(docs/designs/character_contract.md). See docs/trailhead.md.
"""

from .modules import (
    ActionRejected,
    ArgumentError,
    CharacterRules,
    Command,
    Module,
    Outcome,
    ProviderChoice,
    SeedError,
    exact_fields,
)

DICE_OPERATION = "dice.roll.v1"
TRAITS = ("steady", "quick", "keen")
STAMINA_DIE = "1d3+1"
PUSH_DIE = "1d6"
PUSH_TARGET = 4
RECOVER_DIE = "1d6"
RECOVER_TARGET = 3

SCENE = {
    "id": "trailhead",
    "title": "The trailhead",
    "description": (
        "A gravel lot at the foot of the ridge trail. Clouds are coming in "
        "over the pass, and the walkers mean to reach the hut before dark."
    ),
}
RULES = {
    "trailhead.push": (
        f"Push on: {{}}. Spend 1 stamina and roll {PUSH_DIE} (+1 if quick); "
        f"{PUSH_TARGET} or more makes progress, less leaves the walker in danger."
    ),
    "trailhead.recover": (
        f"Recover from danger: {{}}. Roll {RECOVER_DIE} (+1 if steady); "
        f"{RECOVER_TARGET} or more is out of danger. Costs nothing."
    ),
}
CREATION_RULES = {
    "trailhead.trait": (
        'Choose a trait: {"trait": "steady"}, "quick" or "keen". Steady '
        "recovers better, quick pushes better, keen gets 1 more stamina."
    ),
    "trailhead.stamina": (
        f"Roll stamina once: {{}}. {STAMINA_DIE}; it cannot be rerolled."
    ),
    "character.name": 'Name the walker: {"name": "..."}.',
}
_WALKER_FIELDS = {"trait", "rolled", "stamina", "max_stamina", "ready", "in_danger"}


def initial_state() -> dict:
    return {"walkers": {}}  # keyed by the host's character ID


def _walker(state: dict, character_id: str) -> dict:
    walker = state["walkers"].get(character_id)
    if walker is None:  # the host only ever names its own characters
        raise ActionRejected("There is no such walker.")
    return walker


# --- the character capability ------------------------------------------------


def _start(action) -> Outcome:
    state = action.state
    state["walkers"][action.character_id] = {
        "trait": None,
        "rolled": None,
        "stamina": None,
        "max_stamina": None,
        "ready": False,
        "in_danger": False,
    }
    return Outcome(state, {})


def _validate_trait(args: dict):
    exact_fields(args, {"trait"}, "Trait args")
    if args["trait"] not in TRAITS:
        raise ArgumentError("'trait' is steady, quick or keen.")


def _trait(action) -> Outcome:
    state = action.state
    walker = _walker(state, action.character_id)
    walker["trait"] = action.args["trait"]
    return Outcome(state, {"trait": walker["trait"]})


def _validate_no_args(args: dict):
    exact_fields(args, (), "Args")


def _stamina(action) -> Outcome:
    state = action.state
    walker = _walker(state, action.character_id)
    if walker["rolled"] is not None:
        raise ActionRejected("This walker's stamina is already rolled.")
    roll = action.operations[DICE_OPERATION](STAMINA_DIE)
    walker["rolled"] = roll["total"]
    return Outcome(state, {"roll": roll, "stamina": walker["rolled"]})


def _problems(state: dict, character_id: str) -> list:
    walker = state["walkers"][character_id]
    problems = []
    if walker["trait"] is None:
        problems.append("Choose a trait.")
    if walker["rolled"] is None:
        problems.append("Roll stamina.")
    return problems


def _complete(state: dict, character_id: str) -> dict:
    walker = state["walkers"][character_id]
    walker["max_stamina"] = walker["rolled"] + (1 if walker["trait"] == "keen" else 0)
    walker["stamina"] = walker["max_stamina"]
    walker["ready"] = True
    return state


def _sheet(state: dict, character_id: str) -> dict:
    walker = state["walkers"][character_id]
    return {
        "trait": walker["trait"],
        "stamina": walker["stamina"] if walker["ready"] else walker["rolled"],
        "max_stamina": walker["max_stamina"],
        "in_danger": walker["in_danger"],
    }


def _archive_veto(state: dict, character_id: str) -> str | None:
    if state["walkers"][character_id]["in_danger"]:
        return "Not while this walker is in danger; recover first."
    return None


def _creation_actions(state: dict, character_id: str) -> list:
    walker = state["walkers"][character_id]
    actions = [
        {
            "command": "character.name",
            "args": {"name": "..."},
            "description": CREATION_RULES["character.name"],
            "stakes": "None.",
        }
    ]
    actions.append(
        {
            "command": "trailhead.trait",
            "args": {"trait": "steady, quick or keen"},
            "description": CREATION_RULES["trailhead.trait"],
            "stakes": "None; it can be changed until the walker is approved.",
        }
    )
    if walker["rolled"] is None:
        actions.append(
            {
                "command": "trailhead.stamina",
                "args": {},
                "description": CREATION_RULES["trailhead.stamina"],
                "stakes": f"Rolls {STAMINA_DIE} once; the result stands.",
            }
        )
    return actions  # fmt: skip


def _actions(state: dict, character_id: str) -> list:
    walker = state["walkers"][character_id]
    if walker["in_danger"]:
        bonus = 1 if walker["trait"] == "steady" else 0
        return [{
            "command": "trailhead.recover", "args": {},
            "description": RULES["trailhead.recover"],
            "stakes": f"Roll {RECOVER_DIE} + {bonus} against {RECOVER_TARGET}; nothing spent.",
        }]  # fmt: skip
    if walker["stamina"] > 0:
        bonus = 1 if walker["trait"] == "quick" else 0
        return [{
            "command": "trailhead.push", "args": {},
            "description": RULES["trailhead.push"],
            "stakes": (
                f"Roll {PUSH_DIE} + {bonus} against {PUSH_TARGET}. Spends 1 of "
                f"{walker['stamina']} stamina; failing leaves the walker in danger."
            ),
        }]  # fmt: skip
    return []


def _validate_seed(state: dict, participant_ids: frozenset, owners) -> None:
    if set(state) != {"walkers"} or not isinstance(state["walkers"], dict):
        raise SeedError('Trailhead state is exactly {"walkers": {...}}.')
    if set(state["walkers"]) != set(owners):
        raise SeedError("The seed's walkers must be exactly its declared characters.")
    for character_id, walker in state["walkers"].items():
        if not isinstance(walker, dict) or set(walker) != _WALKER_FIELDS:
            raise SeedError(f"Walker {character_id} has the wrong fields.")
        numbers = [walker["rolled"], walker["stamina"], walker["max_stamina"]]
        if (
            walker["trait"] not in TRAITS
            or walker["ready"] is not True
            or not isinstance(walker["in_danger"], bool)
            or not all(type(n) is int for n in numbers)
            or not 2 <= walker["rolled"] <= 4
            or not 0 <= walker["stamina"] <= walker["max_stamina"]
        ):
            raise SeedError(f"Walker {character_id} is not a valid ready walker.")


# --- play ----------------------------------------------------------------------


def _push(action) -> Outcome:
    state = action.state
    walker = _walker(state, action.character_id)
    if walker["in_danger"]:
        raise ActionRejected("This walker is in danger; recover first.")
    if walker["stamina"] <= 0:
        raise ActionRejected("This walker has no stamina left.")
    roll = action.operations[DICE_OPERATION](PUSH_DIE)
    bonus = 1 if walker["trait"] == "quick" else 0
    total = roll["total"] + bonus
    success = total >= PUSH_TARGET
    before = walker["stamina"]
    walker["stamina"] = before - 1
    walker["in_danger"] = not success
    return Outcome(
        state,
        {
            "check": {"roll": roll, "bonus": bonus, "total": total,
                      "target": PUSH_TARGET, "success": success},
            "stamina": {"before": before, "after": walker["stamina"]},
            "in_danger": walker["in_danger"],
        },
    )  # fmt: skip


def _recover(action) -> Outcome:
    state = action.state
    walker = _walker(state, action.character_id)
    if not walker["in_danger"]:
        raise ActionRejected("This walker is not in danger.")
    roll = action.operations[DICE_OPERATION](RECOVER_DIE)
    bonus = 1 if walker["trait"] == "steady" else 0
    total = roll["total"] + bonus
    success = total >= RECOVER_TARGET
    walker["in_danger"] = not success
    return Outcome(
        state,
        {
            "check": {"roll": roll, "bonus": bonus, "total": total,
                      "target": RECOVER_TARGET, "success": success},
            "in_danger": walker["in_danger"],
        },
    )  # fmt: skip


def view(state: dict, participant_id: str) -> dict:
    """The shared scene and rules. Walkers are shown to their owners as sheets
    by the host, never in this view."""
    return {"scene": dict(SCENE), "rules": dict(RULES)}


def referee_context(state: dict) -> dict:
    """The scene, the rules and every ready walker. Drafts are left out: the
    host gives a creation job its own draft (contract section 7)."""
    return {
        "scene": dict(SCENE),
        "rules": {**RULES, **CREATION_RULES},
        "walkers": {
            character_id: _sheet(state, character_id)
            for character_id, walker in state["walkers"].items()
            if walker["ready"]
        },
    }


TRAILHEAD_MODULE = Module(
    id="trailhead",
    version="1",
    state_version=1,
    commands=(
        Command("trailhead.push", _validate_no_args, _push),
        Command("trailhead.recover", _validate_no_args, _recover),
    ),
    initial_state=initial_state,
    view=view,
    requires=frozenset({DICE_OPERATION}),
    providers=(ProviderChoice(DICE_OPERATION, "builtin.dice", "1"),),
    tags=frozenset({"demo", "original", "dice", "characters"}),
    requires_authentication=True,
    referee_context=referee_context,
    characters=CharacterRules(
        version=1,
        start=_start,
        steps=(
            Command("trailhead.trait", _validate_trait, _trait),
            Command("trailhead.stamina", _validate_no_args, _stamina),
        ),
        problems=_problems,
        complete=_complete,
        sheet=_sheet,
        archive_veto=_archive_veto,
        actions=_actions,
        creation_actions=_creation_actions,
        validate_seed=_validate_seed,
    ),
)
