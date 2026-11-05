"""Trailhead: the original public module with characters.

Walkers set out up a mountain trail. Each player creates walkers with the
referee: a trait (steady, quick or keen) and stamina rolled once as 1d3+1.
Completing a walker makes it playable; a keen walker gains one more stamina
then, and so does every walker by the campaign's starting stamina bonus (a
setting, 0 to 2, default 0, which one walker's exception can raise). A ready
walker can push on (spend 1 stamina, 1d6 against 4, +1 if
quick; failing leaves it in danger) or recover (1d6 against 3, +1 if steady;
no cost). A walker in danger cannot be archived. The campaign host can
correct a walker (stamina, trait, gear, description), keep NPCs, change the
scene and write preparation only the referee sees; nothing it does rolls. The theme and numbers are
this module's own; it exists to exercise the host's character capability
(docs/designs/character_contract.md). See docs/trailhead.md.
"""

import re

from .modules import (
    ActionRejected,
    Administration,
    ArgumentError,
    CampaignSettings,
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
BONUS = "starting_stamina_bonus"
MAX_BONUS = 2

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
# Recorded at completion only when a starting bonus applied (absent reads 0),
# and by the host's corrections only once made (absent reads empty).
_OPTIONAL_WALKER_FIELDS = {"bonus", "gear", "description", "facts"}
# Narrative facts agreed in creation (governance contract section 11), each
# with where it came from; an optional one is simply absent until agreed.
FACTS = ("background", "home")
PROVENANCE = ("chosen", "assumed", "corrected", "unspecified")
MAX_FACT = 300
# Optional state the host's administration adds; absent reads as before.
_OPTIONAL_STATE = {"npcs", "scene", "prep"}
MAX_GEAR = 8
MAX_ITEM = 60
MAX_DESCRIPTION = 500
MAX_NPCS = 50
MAX_NPC_NOTE = 500
MAX_SCENE = 1000
MAX_PREP = 2000
_NPC_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


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


def _problems(state: dict, character_id: str, policy: dict) -> list:
    walker = state["walkers"][character_id]
    problems = []
    if walker["trait"] is None:
        problems.append("Choose a trait.")
    if walker["rolled"] is None:
        problems.append("Roll stamina.")
    return problems


def _complete(state: dict, character_id: str, policy: dict) -> dict:
    walker = state["walkers"][character_id]
    bonus = policy["settings"][BONUS]
    if bonus:
        walker["bonus"] = bonus
    walker["max_stamina"] = _max_stamina(walker)
    walker["stamina"] = walker["max_stamina"]
    walker["ready"] = True
    return state


def _max_stamina(walker: dict) -> int:
    """The stamina roll, plus one for a keen walker and any starting bonus."""
    keen = 1 if walker["trait"] == "keen" else 0
    return walker["rolled"] + keen + walker.get("bonus", 0)


def _sheet(state: dict, character_id: str) -> dict:
    walker = state["walkers"][character_id]
    sheet = {
        "trait": walker["trait"],
        "stamina": walker["stamina"] if walker["ready"] else walker["rolled"],
        "max_stamina": walker["max_stamina"],
        "in_danger": walker["in_danger"],
    }
    for optional in ("gear", "description", "facts"):  # only once set
        if optional in walker:
            sheet[optional] = walker[optional]
    return sheet


def _archive_veto(state: dict, character_id: str) -> str | None:
    if state["walkers"][character_id]["in_danger"]:
        return "Not while this walker is in danger; recover first."
    return None


def _creation_actions(state: dict, character_id: str, policy: dict) -> list:
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


def _actions(state: dict, character_id: str, policy: dict) -> list:
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
    if not {"walkers"} <= set(state) <= {"walkers"} | _OPTIONAL_STATE or not isinstance(
        state["walkers"], dict
    ):
        raise SeedError(
            'Trailhead state is {"walkers": {...}}, with optional "npcs", '
            '"scene" and "prep".'
        )
    problem = _world_problem(state)
    if problem is not None:
        raise SeedError(problem)
    if set(state["walkers"]) != set(owners):
        raise SeedError("The seed's walkers must be exactly its declared characters.")
    for character_id, walker in state["walkers"].items():
        if not isinstance(walker, dict) or not (
            _WALKER_FIELDS <= set(walker) <= _WALKER_FIELDS | _OPTIONAL_WALKER_FIELDS
        ):
            raise SeedError(f"Walker {character_id} has the wrong fields.")
        bonus = walker.get("bonus", 1)
        if type(bonus) is not int or not 1 <= bonus <= MAX_BONUS:
            raise SeedError(f"Walker {character_id} has an invalid starting bonus.")
        if "gear" in walker and _gear_problem(walker["gear"]) is not None:
            raise SeedError(f"Walker {character_id} has invalid gear.")
        if "facts" in walker and _facts_problem(walker["facts"]) is not None:
            raise SeedError(f"Walker {character_id} has invalid facts.")
        if "description" in walker and not _text(
            walker["description"], MAX_DESCRIPTION
        ):
            raise SeedError(f"Walker {character_id} has an invalid description.")
        numbers = [walker["rolled"], walker["stamina"], walker["max_stamina"]]
        if (
            walker["trait"] not in TRAITS
            or walker["ready"] is not True
            or not isinstance(walker["in_danger"], bool)
            or not all(type(n) is int for n in numbers)
            or not 2 <= walker["rolled"] <= 4
            or walker["max_stamina"] != _max_stamina(walker)
            or not 0 <= walker["stamina"] <= walker["max_stamina"]
        ):
            raise SeedError(f"Walker {character_id} is not a valid ready walker.")


# --- campaign settings (docs/designs/governance_contract.md section 6) ---------


def _defaults() -> dict:
    return {BONUS: 0}  # the rules from before there were settings


def _validate_settings(settings: dict, state: dict) -> list:
    bonus = settings[BONUS]
    if type(bonus) is not int or not 0 <= bonus <= MAX_BONUS:
        return [f"The starting stamina bonus is a whole number from 0 to {MAX_BONUS}."]
    return []


def _requirements(policy: dict, state: dict, character_id: str | None) -> list:
    """What a walker needs, the one description of it for the referee and
    players: a trait and a stamina roll (mechanical), a background agreed in
    conversation (narrative) and, optionally, a home town."""
    walker = None if character_id is None else state["walkers"].get(character_id)
    bonus = policy["settings"][BONUS]
    approval = (
        f" Approval adds {bonus} starting stamina (this campaign's setting)."
        if bonus
        else ""
    )

    def met(value):
        return {} if walker is None else {"met": value}

    facts = {} if walker is None else walker.get("facts", {})

    return [
        {"id": "trait", "kind": "mechanical", "method": "choose",
         "values": list(TRAITS),
         "description": CREATION_RULES["trailhead.trait"], **met(
             walker is not None and walker["trait"] is not None)},
        {"id": "stamina", "kind": "mechanical", "method": "roll", "die": STAMINA_DIE,
         "description": CREATION_RULES["trailhead.stamina"] + approval, **met(
             walker is not None and walker["rolled"] is not None)},
        {"id": "background", "kind": "narrative", "method": "trailhead.fact",
         "description": "Who the walker is and why they are climbing; agree it "
         "with the player and record it with trailhead.fact. It changes no numbers "
         "and approval does not need it.", **met("background" in facts)},
        {"id": "home", "kind": "optional", "method": "trailhead.fact",
         "description": "Where the walker comes from, only if the player wants to "
         "say; never invent it.", **met(True if "home" in facts else None)},
    ]  # fmt: skip


def _facts_problem(facts) -> str | None:
    if not isinstance(facts, dict) or not set(facts) <= set(FACTS):
        return f"Facts are {', '.join(FACTS)}."
    for fact in facts.values():
        if (
            not isinstance(fact, dict)
            or set(fact) != {"text", "provenance"}
            or fact["provenance"] not in PROVENANCE
            or not _text(fact["text"], MAX_FACT)
            or (fact["provenance"] == "unspecified") != (fact["text"] == "")
        ):
            return (
                f'A fact is {{"text", "provenance"}}: up to {MAX_FACT} characters, '
                "and provenance chosen, assumed or corrected; unspecified has no text."
            )
    return None


def _validate_fact(args: dict):
    exact_fields(args, {"fact", "text", "provenance"}, "Fact args")
    if args["fact"] not in FACTS:
        raise ArgumentError("'fact' is background or home.")
    problem = _facts_problem({args["fact"]: {"text": args["text"],
                                             "provenance": args["provenance"]}})  # fmt: skip
    if problem is not None:
        raise ArgumentError(problem)


def _fact(action) -> Outcome:
    """Record one narrative fact the player agreed to, with its provenance:
    `chosen` by the player, `assumed` and accepted, a `corrected` one, or
    explicitly `unspecified`. It is the draft's own data, seen by its owner,
    the campaign host and the referee."""
    state = action.state
    walker = _walker(state, action.character_id)
    facts = walker.setdefault("facts", {})
    facts[action.args["fact"]] = {"text": action.args["text"],
                                  "provenance": action.args["provenance"]}  # fmt: skip
    return Outcome(state, {"fact": action.args["fact"], **facts[action.args["fact"]]})


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


def _scene(state: dict) -> dict:
    """The scene, with the host's description if it changed it."""
    return {**SCENE, **state.get("scene", {})}


def view(state: dict, participant_id: str) -> dict:
    """The shared scene and rules, and the NPCs the host has made visible.
    Walkers are shown to their owners as sheets by the host, never in this
    view; the referee's preparation never is."""
    seen = {"scene": _scene(state), "rules": dict(RULES)}
    npcs = {
        npc_id: {"name": npc["name"], "note": npc["note"]}
        for npc_id, npc in state.get("npcs", {}).items()
        if npc["visible"]
    }
    if npcs:
        seen["npcs"] = npcs
    return seen


def referee_context(state: dict) -> dict:
    """The scene, the rules, every ready walker, and the host's NPCs (each
    marked visible or not) and preparation once there are any. Drafts are
    left out: the host gives a creation job its own draft (contract section
    7)."""
    context = {
        "scene": _scene(state),
        "rules": {**RULES, **CREATION_RULES},
        "walkers": {
            character_id: _sheet(state, character_id)
            for character_id, walker in state["walkers"].items()
            if walker["ready"]
        },
    }
    if state.get("npcs"):
        context["npcs"] = state["npcs"]
    if state.get("prep"):
        context["prep"] = state["prep"]
    return context


# --- host administration (docs/designs/governance_contract.md section 6) -------
# Each operation names its target in its own arguments, validates them alone,
# checks the state before changing it (ActionRejected), recomputes derived
# numbers through the module's own rules, and never rolls.


def _text(value, limit: int) -> bool:
    return isinstance(value, str) and len(value) <= limit and value == value.strip()


def _gear_problem(gear) -> str | None:
    if (
        not isinstance(gear, list)
        or len(gear) > MAX_GEAR
        or not all(_text(item, MAX_ITEM) and item for item in gear)
    ):
        return (
            f"'gear' is a list of up to {MAX_GEAR} items, each 1 to {MAX_ITEM} "
            "characters."
        )
    return None


def _world_problem(state: dict) -> str | None:
    npcs = state.get("npcs", {})
    if not isinstance(npcs, dict) or len(npcs) > MAX_NPCS:
        return f"'npcs' is an object of up to {MAX_NPCS} NPCs."
    for npc_id, npc in npcs.items():
        if (
            not _NPC_ID.fullmatch(npc_id)
            or not isinstance(npc, dict)
            or set(npc) != {"name", "note", "visible"}
            or not (_text(npc["name"], 100) and npc["name"])
            or not _text(npc["note"], MAX_NPC_NOTE)
            or not isinstance(npc["visible"], bool)
        ):
            return f"NPC {npc_id!r} is not a valid NPC."
    scene = state.get("scene", {})
    if (
        not isinstance(scene, dict)
        or set(scene) - {"description"}
        or not _text(scene.get("description", ""), MAX_SCENE)
    ):
        return 'The scene is {"description": TEXT}.'
    if not _text(state.get("prep", ""), MAX_PREP):
        return f"'prep' is text of up to {MAX_PREP} characters."
    return None


def _character_arg(args: dict):
    if not isinstance(args["character_id"], str):
        raise ArgumentError("'character_id' is a character ID.")


def _validate_set_stamina(args: dict):
    exact_fields(args, {"character_id", "stamina"}, "set_stamina args")
    _character_arg(args)
    if type(args["stamina"]) is not int or args["stamina"] < 0:
        raise ArgumentError("'stamina' is a whole number, 0 or more.")


def _ready_walker(state: dict, character_id: str) -> dict:
    walker = _walker(state, character_id)
    if not walker["ready"]:
        raise ActionRejected(
            "That walker is still a draft; its numbers come from creation."
        )
    return walker


def _set_stamina(action) -> Outcome:
    state = action.state
    walker = _ready_walker(state, action.args["character_id"])
    stamina = action.args["stamina"]
    if stamina > walker["max_stamina"]:
        raise ActionRejected(
            f"This walker's stamina is at most {walker['max_stamina']}."
        )
    before, walker["stamina"] = walker["stamina"], stamina
    return Outcome(state, {"stamina": {"before": before, "after": stamina}})


def _validate_set_trait(args: dict):
    exact_fields(args, {"character_id", "trait"}, "set_trait args")
    _character_arg(args)
    if args["trait"] not in TRAITS:
        raise ArgumentError("'trait' is steady, quick or keen.")


def _set_trait(action) -> Outcome:
    """A corrected trait: the maximum is recomputed by the module's rule
    (keen adds one), and stamina is kept, lowered only to fit. Nothing is
    rolled again and the starting bonus stays."""
    state = action.state
    walker = _ready_walker(state, action.args["character_id"])
    walker["trait"] = action.args["trait"]
    walker["max_stamina"] = _max_stamina(walker)
    walker["stamina"] = min(walker["stamina"], walker["max_stamina"])
    return Outcome(state, {"trait": walker["trait"],
                           "max_stamina": walker["max_stamina"],
                           "stamina": walker["stamina"]})  # fmt: skip


def _validate_set_gear(args: dict):
    exact_fields(args, {"character_id", "gear"}, "set_gear args")
    _character_arg(args)
    problem = _gear_problem(args["gear"])
    if problem is not None:
        raise ArgumentError(problem)


def _set_gear(action) -> Outcome:
    state = action.state
    walker = _walker(state, action.args["character_id"])
    walker["gear"] = list(action.args["gear"])
    return Outcome(state, {"gear": walker["gear"]})


def _validate_describe(args: dict):
    exact_fields(args, {"character_id", "description"}, "describe args")
    _character_arg(args)
    if not _text(args["description"], MAX_DESCRIPTION):
        raise ArgumentError(
            f"'description' is text of up to {MAX_DESCRIPTION} characters."
        )


def _describe_walker(action) -> Outcome:
    state = action.state
    walker = _walker(state, action.args["character_id"])
    walker["description"] = action.args["description"]
    return Outcome(state, {"description": walker["description"]})


def _validate_npc(args: dict):
    exact_fields(args, {"npc_id", "name", "note", "visible"}, "npc_set args")
    if not isinstance(args["npc_id"], str) or not _NPC_ID.fullmatch(args["npc_id"]):
        raise ArgumentError(
            "'npc_id' is 1 to 32 lowercase letters, digits, '_' or '-'."
        )
    if not (_text(args["name"], 100) and args["name"]):
        raise ArgumentError("'name' is text of 1 to 100 characters.")
    if not _text(args["note"], MAX_NPC_NOTE):
        raise ArgumentError(f"'note' is text of up to {MAX_NPC_NOTE} characters.")
    if not isinstance(args["visible"], bool):
        raise ArgumentError("'visible' is true or false.")


def _npc_set(action) -> Outcome:
    """Add or update one NPC; a hidden one is the referee's alone."""
    state = action.state
    npcs = state.setdefault("npcs", {})
    npc_id = action.args["npc_id"]
    if npc_id not in npcs and len(npcs) >= MAX_NPCS:
        raise ActionRejected(f"A campaign keeps at most {MAX_NPCS} NPCs.")
    added = npc_id not in npcs
    npcs[npc_id] = {k: action.args[k] for k in ("name", "note", "visible")}
    return Outcome(state, {"npc_id": npc_id, "added": added})


def _validate_scene(args: dict):
    exact_fields(args, {"description"}, "scene_set args")
    if not (_text(args["description"], MAX_SCENE) and args["description"]):
        raise ArgumentError(f"'description' is text of 1 to {MAX_SCENE} characters.")


def _scene_set(action) -> Outcome:
    state = action.state
    state["scene"] = {"description": action.args["description"]}
    return Outcome(state, {"scene": _scene(state)})


def _validate_prep(args: dict):
    exact_fields(args, {"note"}, "prep_set args")
    if not _text(args["note"], MAX_PREP):
        raise ArgumentError(f"'note' is text of up to {MAX_PREP} characters.")


def _prep_set(action) -> Outcome:
    """The referee's preparation: in its context only, never a player's."""
    state = action.state
    state["prep"] = action.args["note"]
    return Outcome(state, {"prep": "updated"})


def _inspect(state: dict) -> dict:
    """Everything: every walker's data, drafts included, NPCs hidden or not,
    the scene and the preparation."""
    return {
        "scene": _scene(state),
        "walkers": state["walkers"],
        "npcs": state.get("npcs", {}),
        "prep": state.get("prep", ""),
    }


_CHARACTER = {"name": "character_id", "type": "character", "required": True}


def _host_operations(state: dict) -> list:
    ready = any(w["ready"] for w in state["walkers"].values())
    any_walker = bool(state["walkers"])
    operations = []
    if ready:
        operations += [
            {"command": "trailhead.set_stamina", "target_kinds": ["character"],
             "description": "Correct a ready walker's stamina, from 0 to its maximum.",
             "args": [_CHARACTER, {"name": "stamina", "type": "integer", "min": 0,
                                   "required": True}]},
            {"command": "trailhead.set_trait", "target_kinds": ["character"],
             "description": "Correct a ready walker's trait; its maximum stamina is "
             "recomputed and its stamina kept (lowered only to fit). Nothing is rolled.",
             "args": [_CHARACTER, {"name": "trait", "type": "string",
                                   "values": list(TRAITS), "required": True}]},
        ]  # fmt: skip
    if any_walker:
        operations += [
            {"command": "trailhead.set_gear", "target_kinds": ["character"],
             "description": "Replace a walker's gear list.",
             "args": [_CHARACTER, {"name": "gear", "type": "list of text",
                                   "max_items": MAX_GEAR, "max_length": MAX_ITEM,
                                   "required": True}]},
            {"command": "trailhead.describe", "target_kinds": ["character"],
             "description": "Set a walker's description.",
             "args": [_CHARACTER, {"name": "description", "type": "text",
                                   "max_length": MAX_DESCRIPTION, "required": True}]},
        ]  # fmt: skip
    operations += [
        {"command": "trailhead.npc_set", "target_kinds": ["npc"],
         "description": "Add or update an NPC; players see it only when visible.",
         "args": [{"name": "npc_id", "type": "identifier", "required": True},
                  {"name": "name", "type": "text", "max_length": 100, "required": True},
                  {"name": "note", "type": "text", "max_length": MAX_NPC_NOTE,
                   "required": True},
                  {"name": "visible", "type": "boolean", "required": True}]},
        {"command": "trailhead.scene_set", "target_kinds": ["scene"],
         "description": "Change the scene's description, which every player sees.",
         "args": [{"name": "description", "type": "text", "max_length": MAX_SCENE,
                   "required": True}]},
        {"command": "trailhead.prep_set", "target_kinds": ["scene"],
         "description": "Replace the referee's preparation; no player sees it.",
         "args": [{"name": "note", "type": "text", "max_length": MAX_PREP,
                   "required": True}]},
    ]  # fmt: skip
    return operations


def _target_state(state: dict, target: dict) -> dict:
    kind, target_id = target["kind"], target["id"]
    if kind == "character":
        if target_id not in state["walkers"]:
            raise ArgumentError("There is no such walker.")
        return state["walkers"][target_id]
    if kind == "npc":
        return state.get("npcs", {}).get(target_id, {})  # {} until it exists
    if kind == "scene" and target_id == SCENE["id"]:
        return {"scene": _scene(state), "prep": state.get("prep", "")}
    raise ArgumentError("Trailhead's targets are a character, an NPC or the scene.")


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
        version=2,
        start=_start,
        steps=(
            Command("trailhead.trait", _validate_trait, _trait),
            Command("trailhead.stamina", _validate_no_args, _stamina),
            Command("trailhead.fact", _validate_fact, _fact),
        ),
        problems=_problems,
        complete=_complete,
        sheet=_sheet,
        archive_veto=_archive_veto,
        actions=_actions,
        creation_actions=_creation_actions,
        validate_seed=_validate_seed,
    ),
    administration=Administration(
        version=1,
        inspect=_inspect,
        operations=(
            Command("trailhead.set_stamina", _validate_set_stamina, _set_stamina),
            Command("trailhead.set_trait", _validate_set_trait, _set_trait),
            Command("trailhead.set_gear", _validate_set_gear, _set_gear),
            Command("trailhead.describe", _validate_describe, _describe_walker),
            Command("trailhead.npc_set", _validate_npc, _npc_set),
            Command("trailhead.scene_set", _validate_scene, _scene_set),
            Command("trailhead.prep_set", _validate_prep, _prep_set),
        ),
        describe=_host_operations,
        target_state=_target_state,
    ),
    settings=CampaignSettings(
        version=1,
        defaults=_defaults,
        validate=_validate_settings,
        requirements=_requirements,
    ),
)
