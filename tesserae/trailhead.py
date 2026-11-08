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
scene and write preparation only the referee sees; nothing it does rolls.
A draft's stamina roll, maximum stamina modifier and starting gear can be
overridden by negotiation (docs/designs/creation_overrides_contract.md),
in two categories, endurance and supplies; an accepted override keeps the
original values and never rolls. The theme and numbers are this module's
own; it exists to exercise the host's character capability
(docs/designs/character_contract.md). See docs/trailhead.md.
"""

import re

from .modules import (
    ActionRejected,
    Administration,
    ArgumentError,
    CampaignSettings,
    Category,
    CharacterRules,
    Command,
    CreationOverrides,
    IntentError,
    Module,
    Outcome,
    ProviderChoice,
    SeedError,
    UnsupportedOverride,
    check_intent,
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
    "trailhead.pick": (
        'Pick one item from the outfitter\'s list: {"item": "..."}. Only with '
        "gear picks an accepted override allowed; unused picks expire at approval."
    ),
}
_WALKER_FIELDS = {"trait", "rolled", "stamina", "max_stamina", "ready", "in_danger"}
# Recorded at completion only when a starting bonus applied (absent reads 0),
# by the host's corrections only once made (absent reads empty), and by an
# applied creation override (absent reads "no overrides").
_OPTIONAL_WALKER_FIELDS = {"bonus", "gear", "description", "facts", "overrides"}
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
    # Creation-only authority ends here; applied effects and their
    # provenance stay (creation overrides contract section 3, duty 4).
    overrides = walker.get("overrides")
    if overrides is not None:
        overrides.pop("gear_picks", None)
        if not overrides:
            del walker["overrides"]
    bonus = policy["settings"][BONUS]
    if bonus:
        walker["bonus"] = bonus
    walker["max_stamina"] = _max_stamina(walker)
    walker["stamina"] = walker["max_stamina"]
    walker["ready"] = True
    return state


def _max_stamina(walker: dict) -> int:
    """The effective stamina roll, plus one for a keen walker, any starting
    bonus and any overridden modifier: the one calculation creation,
    completion, the sheet and corrections share."""
    keen = 1 if walker["trait"] == "keen" else 0
    return _roll(walker) + keen + walker.get("bonus", 0) + _modifier(walker)


def _roll(walker: dict) -> int | None:
    """The effective stamina roll: an applied replacement's, else the roll."""
    replaced = walker.get("overrides", {}).get("stamina_roll")
    return walker["rolled"] if replaced is None else replaced["value"]


def _modifier(walker: dict) -> int:
    found = walker.get("overrides", {}).get("max_stamina_modifier")
    return 0 if found is None else found["amount"]


def _sheet(state: dict, character_id: str) -> dict:
    walker = state["walkers"][character_id]
    sheet = {
        "trait": walker["trait"],
        "stamina": walker["stamina"] if walker["ready"] else _roll(walker),
        "max_stamina": walker["max_stamina"],
        "in_danger": walker["in_danger"],
    }
    for optional in ("gear", "description", "facts"):  # only once set
        if optional in walker:
            sheet[optional] = walker[optional]
    if "overrides" in walker:  # what changed, by whose authority, and originals
        shown = dict(walker["overrides"])
        if "stamina_roll" in shown:
            shown["stamina_roll"] = {
                **shown["stamina_roll"],
                "original": walker["rolled"],
            }
        sheet["overrides"] = shown
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
    picks = _picks(walker)
    if picks:
        actions.append(
            {
                "command": "trailhead.pick",
                "args": {"item": "one of the outfitter's items"},
                "description": CREATION_RULES["trailhead.pick"],
                "stakes": f"None; {picks} pick{'s' if picks > 1 else ''} left, "
                "nothing is rolled.",
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
        if "overrides" in walker and _overrides_problem(walker["overrides"]):
            raise SeedError(f"Walker {character_id} has invalid overrides.")
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
    """Replace a walker's gear. The provenance of overridden gear is kept
    where it still holds: a granted or picked item still carried, a removed
    one still absent."""
    state = action.state
    walker = _walker(state, action.args["character_id"])
    walker["gear"] = list(action.args["gear"])
    record = walker.get("overrides", {}).get("gear")
    if record is not None:
        for item, entry in list(record.items()):
            if (entry["change"] == "removed") == (item in walker["gear"]):
                del record[item]
        _tidy(walker)
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


def _operation_target(command: str, args: dict) -> dict:
    """What each host operation changes: a walker, an NPC, or the scene."""
    if "character_id" in args:
        return {"kind": "character", "id": args["character_id"]}
    if command == "trailhead.npc_set":
        return {"kind": "npc", "id": args["npc_id"]}
    return {"kind": "scene", "id": SCENE["id"]}  # scene_set and prep_set


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


# --- negotiated creation overrides (creation overrides contract) ---------------
# Two original families: endurance (a replaced roll and a modifier, both
# feeding the one maximum-stamina calculation) and supplies (granted or
# removed gear, and picks: an allowance spent by an ordinary step later).
# Every preview is computed from the facts it lists in `dependencies` and
# the intent alone, so an unrelated creation step leaves it unchanged.

ENDURANCE = "endurance"
SUPPLIES = "supplies"
OUTFITTER = (
    "bivouac sack", "camp stove", "climbing rope", "compass", "first-aid kit",
    "headlamp", "rain shell", "spare socks", "trail map", "trail mix",
    "trekking poles", "water filter",
)  # fmt: skip
MAX_PICKS = 3
MODIFIER = (-1, 2)
MAX_GEAR_RECORD = 16
OPTION_PAGE = 5
QUALITY_PAGE = 50
_PROVENANCE_KINDS = ("accepted_offer", "direct")


def _integer(minimum: int, maximum: int) -> dict:
    return {"type": "integer", "minimum": minimum, "maximum": maximum, "required": True}


QUALITIES = {
    "stamina_roll": {
        "category_id": ENDURANCE, "label": "Stamina roll",
        "description": f"The walker's {STAMINA_DIE} stamina roll. A replacement "
        "keeps the original roll; the walker must have rolled first.",
        "actions": [{"action_id": "replace", "label": "Replace the roll",
                     "parameters": {"value": _integer(2, 4)}, "later_random": False}],
    },
    "max_stamina_modifier": {
        "category_id": ENDURANCE, "label": "Maximum stamina modifier",
        "description": "Added to the maximum stamina at approval, after the trait "
        "and any starting bonus. Normally 0.",
        "actions": [{"action_id": "set", "label": "Set the modifier",
                     "parameters": {"amount": _integer(*MODIFIER)},
                     "later_random": False}],
    },
    "starting_gear": {
        "category_id": SUPPLIES, "label": "Starting gear",
        "description": f"What the walker carries, up to {MAX_GEAR} items.",
        "actions": [
            {"action_id": "grant", "label": "Grant an item",
             "parameters": {"item": {"type": "option", "required": True}},
             "later_random": False},
            {"action_id": "remove", "label": "Remove an item",
             "parameters": {"carried": {"type": "option", "required": True}},
             "later_random": False},
            {"action_id": "allow", "label": "Allow picks",
             "parameters": {"count": _integer(1, MAX_PICKS)}, "later_random": False},
        ],
    },
}  # fmt: skip


def _overrides_problem(overrides, draft: bool = False) -> str | None:
    """The shape of a walker's `overrides`; picks only on a draft."""
    allowed = {"stamina_roll", "max_stamina_modifier", "gear"}
    if draft:
        allowed.add("gear_picks")
    if not isinstance(overrides, dict) or not overrides or set(overrides) - allowed:
        return "Overrides hold stamina_roll, max_stamina_modifier and gear."

    def provenance(entry, *fields) -> bool:
        by = entry.get("by") if isinstance(entry, dict) else None
        return (
            isinstance(entry, dict)
            and set(entry) == {*fields, "by"}
            and isinstance(by, dict)
            and set(by) == {"kind", "record_id"}
            and by["kind"] in _PROVENANCE_KINDS
            and isinstance(by["record_id"], str)
        )

    replaced = overrides.get("stamina_roll", {"value": 2, "by": None})
    modifier = overrides.get("max_stamina_modifier", {"amount": 0, "by": None})
    picks = overrides.get("gear_picks")
    record = overrides.get("gear", {})
    if (
        ("stamina_roll" in overrides and not provenance(replaced, "value"))
        or type(replaced["value"]) is not int
        or not 2 <= replaced["value"] <= 4
        or ("max_stamina_modifier" in overrides and not provenance(modifier, "amount"))
        or type(modifier["amount"]) is not int
        or not MODIFIER[0] <= modifier["amount"] <= MODIFIER[1]
        or (picks is not None and not (
            provenance(picks, "remaining") and type(picks["remaining"]) is int
            and 1 <= picks["remaining"] <= MAX_PICKS))
        or not isinstance(record, dict)
        or ("gear" in overrides and not record)
        or len(record) > MAX_GEAR_RECORD
        or not all(_text(item, MAX_ITEM) and item and provenance(entry, "change")
                   and entry["change"] in ("granted", "picked", "removed")
                   for item, entry in record.items())
    ):  # fmt: skip
        return "Overrides are not valid."
    return None


def _picks(walker: dict) -> int:
    found = walker.get("overrides", {}).get("gear_picks")
    return 0 if found is None else found["remaining"]


def _tidy(walker: dict):
    """Drop empty override records, so "no overrides" reads as before."""
    overrides = walker.get("overrides")
    if overrides is None:
        return
    if not overrides.get("gear", True):
        del overrides["gear"]
    if not overrides:
        del walker["overrides"]


def _facts(walker: dict) -> dict:
    """Everything a preview may read of a draft. A preview lists in its
    `dependencies` exactly those it read."""
    overrides = walker.get("overrides", {})
    return {
        "rolled": walker["rolled"],
        "stamina_roll": overrides.get("stamina_roll", {}).get("value"),
        "max_stamina_modifier": _modifier(walker),
        "gear": list(walker.get("gear", [])),
        "gear_record": len(overrides.get("gear", {})),
        "gear_picks": _picks(walker),
    }


def _draft(state: dict, character_id: str) -> dict:
    walker = state["walkers"].get(character_id)
    if walker is None:
        raise IntentError(["There is no such walker."])
    if walker["ready"]:
        raise IntentError(["This walker is approved; overrides change drafts only."])
    return walker


def _discover(state: dict, character_id: str, query: dict) -> dict:
    walker = _draft(state, character_id)
    quality_id = query.get("quality_id")
    if quality_id is not None and quality_id not in QUALITIES:
        raise UnsupportedOverride("Trailhead has no such quality.")
    facts = _facts(walker)
    if "parameter" in query:
        options = {
            ("starting_gear", "item"): [i for i in OUTFITTER if i not in facts["gear"]],
            ("starting_gear", "carried"): facts["gear"],
        }.get((quality_id, query["parameter"]))
        if options is None:
            raise UnsupportedOverride(
                "That quality has no such parameter with options."
            )
        match = query.get("match", "").casefold()
        found = [{"value": o, "label": o} for o in options if match in o.casefold()]
        return _page(found, query.get("after"), OPTION_PAGE)
    current = {
        "stamina_roll": _roll(walker),
        "max_stamina_modifier": facts["max_stamina_modifier"],
        "starting_gear": facts["gear"],
    }
    found = [
        {"quality_id": q, "category_id": d["category_id"], "label": d["label"],
         "description": d["description"], "current": current[q],
         "actions": d["actions"]}
        for q, d in QUALITIES.items()
        if quality_id in (None, q)
        and query.get("category_id") in (None, d["category_id"])
    ]  # fmt: skip
    return _page(found, query.get("after"), QUALITY_PAGE)


def _page(items: list, after, size: int) -> dict:
    start = 0
    if after is not None:
        if not after.isdigit() or not 0 < int(after) <= len(items):
            raise ArgumentError("'after' is a cursor from an earlier page.")
        start = int(after)
    end = start + size
    return {"items": items[start:end], "next": str(end) if end < len(items) else None}


def _preview(state: dict, character_id: str, intent: dict, policy: dict) -> dict:
    """Exactly what `intent` would do to this draft now. Reads the draft
    only through `_facts`, and lists what it read as `dependencies`."""
    facts = _facts(_draft(state, character_id))
    read = set()

    def fact(name):
        read.add(name)
        return facts[name]

    work = {}  # values this intent has already changed

    def value(name):
        return work[name] if name in work else fact(name)

    effects, consequences, later_steps, seen = [], [], [], set()
    for change in intent["changes"]:
        quality_id, action_id = change["quality_id"], change["action_id"]
        described = QUALITIES.get(quality_id)
        if described is None:
            raise UnsupportedOverride("Trailhead has no such quality.")
        actions = {a["action_id"]: a for a in described["actions"]}
        if action_id not in actions:
            raise UnsupportedOverride("That quality has no such action.")
        parameters = change["parameters"]
        names = sorted(actions[action_id]["parameters"])
        if sorted(parameters) != names:
            raise IntentError([f"{action_id} takes exactly {', '.join(names)}."])
        if quality_id != "starting_gear":
            if quality_id in seen:
                raise IntentError(["Change each endurance quality once per request."])
            seen.add(quality_id)
        effect, consequence = _change(
            quality_id, action_id, parameters, fact, value, work
        )
        effects.append(effect)
        if consequence not in consequences:
            consequences.append(consequence)
    if work.get("gear_picks", facts["gear_picks"]) != facts["gear_picks"]:
        later_steps.append({
            "command": "trailhead.pick",
            "summary": "Pick an item from the outfitter's list, one ordinary "
            "creation step per pick.",
            "stakes": "None; nothing is rolled. Unused picks expire at approval.",
        })  # fmt: skip
    affected = [
        {"quality_id": q, "category_id": QUALITIES[q]["category_id"]}
        for q in QUALITIES
        if any(e["quality_id"] == q for e in effects)
    ]
    if len(effects) == 1:
        headline = effects[0]["summary"]
    else:
        labels = ", ".join(
            QUALITIES[a["quality_id"]]["label"].lower() for a in affected
        )
        headline = f"{len(effects)} changes to {labels}."
    allowances = []
    if later_steps:
        picks = work["gear_picks"]
        allowances.append({
            "allowance_id": "gear_picks", "quality_id": "starting_gear",
            "summary": f"{picks} pick{'s' if picks > 1 else ''} from the outfitter's "
            "list at no cost, until approval.",
            "creation_only": True,
        })  # fmt: skip
    return {
        "plan": {"changes": [dict(c) for c in intent["changes"]]},
        "headline": headline,
        "affected": affected,
        "effects": effects,
        "waivers": [],
        "allowances": allowances,
        "later_steps": later_steps,
        "consequences": consequences,
        "limitations": [],
        "dependencies": {name: facts[name] for name in sorted(read)},
    }


def _change(quality_id, action_id, parameters, fact, value, work):
    """One change's effect on the working values: (effect, consequence)."""
    if quality_id == "stamina_roll":
        new = parameters["value"]
        if type(new) is not int or not 2 <= new <= 4:
            raise IntentError(
                ["A replacement stamina roll is a whole number from 2 to 4."]
            )
        rolled = fact("rolled")
        if rolled is None:
            raise IntentError(
                ["Roll stamina first: a replacement keeps the original roll."]
            )
        before = fact("stamina_roll")
        before = rolled if before is None else before
        if new == before:
            raise IntentError(
                [f"The stamina roll is already {new}; nothing would change."]
            )
        return (
            {"quality_id": quality_id, "before": before, "after": new,
             "summary": f"Replaces the stamina roll of {before} with {new}; the "
             f"original roll of {rolled} is kept."},
            "Approval computes maximum stamina from the replacement, not the roll.",
        )  # fmt: skip
    if quality_id == "max_stamina_modifier":
        new = parameters["amount"]
        if type(new) is not int or not MODIFIER[0] <= new <= MODIFIER[1]:
            raise IntentError([
                f"The modifier is a whole number from {MODIFIER[0]} to {MODIFIER[1]}."
            ])  # fmt: skip
        before = fact("max_stamina_modifier")
        if new == before:
            raise IntentError(
                [f"The modifier is already {new:+d}; nothing would change."]
            )
        return (
            {"quality_id": quality_id, "before": before, "after": new,
             "summary": f"Sets the maximum stamina modifier from {before:+d} to {new:+d}."},
            "Approval adds the modifier to the maximum stamina, after the trait and "
            "any starting bonus.",
        )  # fmt: skip
    if action_id == "allow":
        count = parameters["count"]
        if type(count) is not int or not 1 <= count <= MAX_PICKS:
            raise IntentError([f"'count' is a whole number from 1 to {MAX_PICKS}."])
        before = value("gear_picks")
        if before + count > MAX_PICKS:
            raise IntentError([f"A walker has at most {MAX_PICKS} gear picks at once."])
        work["gear_picks"] = before + count
        return (
            {"quality_id": quality_id, "before": before, "after": before + count,
             "summary": f"Allows {count} more pick{'s' if count > 1 else ''} from "
             "the outfitter's list."},
            "Unused picks expire when the walker is approved.",
        )  # fmt: skip
    gear = list(value("gear"))
    # At most one more recorded change each (an undo records one fewer).
    recorded = value("gear_record") + 1
    if recorded > MAX_GEAR_RECORD:
        raise IntentError(["This walker's gear has too many recorded changes."])
    work["gear_record"] = recorded
    if action_id == "grant":
        item = parameters["item"]
        if not isinstance(item, str) or item not in OUTFITTER:
            raise IntentError(["That item is not on the outfitter's list."])
        if item in gear:
            raise IntentError(["The walker already carries that item."])
        if len(gear) >= MAX_GEAR:
            raise IntentError([f"A walker carries at most {MAX_GEAR} items."])
        work["gear"] = gear + [item]
        effect = {"before": None, "after": item,
                  "summary": f"Adds {item} to the starting gear."}  # fmt: skip
    else:  # remove
        item = parameters["carried"]
        if not isinstance(item, str) or item not in gear:
            raise IntentError(["The walker does not carry that item."])
        work["gear"] = [g for g in gear if g != item]
        effect = {"before": item, "after": None,
                  "summary": f"Removes {item} from the starting gear."}  # fmt: skip
    # The item alone, not the whole list: the list is in `dependencies`.
    return (
        {"quality_id": quality_id, **effect},
        "The change is recorded on the sheet with who authorized it.",
    )


def _apply_override(action) -> Outcome:
    """Perform exactly a plan this module previews now, recording who
    authorized it; nothing is drawn. Refuses a plan it would not preview."""
    try:
        check_intent(action.plan)
    except IntentError:
        raise IntentError(["That is not a plan Trailhead made."]) from None
    preview = _preview(action.state, action.character_id, action.plan, action.policy)
    if preview["plan"] != action.plan:
        raise IntentError(["That is not a plan Trailhead made."])
    state = action.state
    walker = state["walkers"][action.character_id]
    by = {"kind": action.authorization["kind"],
          "record_id": action.authorization["record_id"]}  # fmt: skip
    overrides = walker.setdefault("overrides", {})
    receipt = {}
    for change in action.plan["changes"]:
        quality_id, parameters = change["quality_id"], change["parameters"]
        if quality_id == "stamina_roll":
            overrides["stamina_roll"] = {"value": parameters["value"], "by": by}
            receipt["stamina_roll"] = {"original": walker["rolled"],
                                       "value": parameters["value"]}  # fmt: skip
        elif quality_id == "max_stamina_modifier":
            overrides["max_stamina_modifier"] = {
                "amount": parameters["amount"],
                "by": by,
            }
            receipt["max_stamina_modifier"] = parameters["amount"]
        elif change["action_id"] == "allow":
            picks = _picks(walker) + parameters["count"]
            overrides["gear_picks"] = {"remaining": picks, "by": by}
            receipt["gear_picks"] = picks
        else:
            granted = change["action_id"] == "grant"
            item = parameters["item" if granted else "carried"]
            gear = walker.setdefault("gear", [])
            if granted:
                gear.append(item)
            else:
                gear.remove(item)
            _record_gear(overrides, item, "granted" if granted else "removed", by)
            moved = receipt.setdefault("gear", {"added": [], "removed": []})
            moved["added" if granted else "removed"].append(item)
    _tidy(walker)
    return Outcome(state, receipt)


def _record_gear(overrides: dict, item: str, change: str, by: dict):
    """Record one gear change against the original list: undoing an earlier
    change drops its record, so the original is always recoverable."""
    record = overrides.setdefault("gear", {})
    earlier = record.get(item, {}).get("change")
    if earlier is not None and (earlier == "removed") != (change == "removed"):
        del record[item]
    else:
        record[item] = {"change": change, "by": by}


def _allowances(state: dict, character_id: str) -> list:
    picks = _picks(state["walkers"][character_id])
    if not picks:
        return []
    return [{
        "allowance_id": "gear_picks", "quality_id": "starting_gear",
        "summary": f"{picks} pick{'s' if picks > 1 else ''} from the outfitter's list.",
        "remaining": picks,
    }]  # fmt: skip


def _validate_pick(args: dict):
    exact_fields(args, {"item"}, "Pick args")
    if args["item"] not in OUTFITTER:
        raise ArgumentError("'item' is one of the outfitter's items.")


def _pick(action) -> Outcome:
    """Spend one gear pick an accepted override allowed. Without one, or
    beyond what it allowed, it is refused: picks never come from here."""
    state = action.state
    walker = _walker(state, action.character_id)
    picks = walker.get("overrides", {}).get("gear_picks")
    if picks is None:
        raise ActionRejected("This walker has no gear picks; an override allows them.")
    gear = walker.setdefault("gear", [])
    item = action.args["item"]
    if item in gear:
        raise ActionRejected("This walker already carries that item.")
    if len(gear) >= MAX_GEAR:
        raise ActionRejected(f"A walker carries at most {MAX_GEAR} items.")
    if len(walker["overrides"].get("gear", {})) >= MAX_GEAR_RECORD:
        raise ActionRejected("This walker's gear has too many recorded changes.")
    gear.append(item)
    _record_gear(walker["overrides"], item, "picked", picks["by"])
    picks["remaining"] -= 1
    if not picks["remaining"]:
        del walker["overrides"]["gear_picks"]
    _tidy(walker)
    return Outcome(state, {"item": item, "picks_left": picks["remaining"]})


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
            Command("trailhead.pick", _validate_pick, _pick),
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
        target=_operation_target,
    ),
    settings=CampaignSettings(
        version=1,
        defaults=_defaults,
        validate=_validate_settings,
        requirements=_requirements,
    ),
    creation_overrides=CreationOverrides(
        version=1,
        categories=(
            Category(
                ENDURANCE,
                "Endurance",
                "How much a walker can take: its stamina roll and the "
                "modifier to its maximum stamina.",
            ),
            Category(
                SUPPLIES,
                "Supplies",
                "What a walker carries from the start, and picks from the "
                "outfitter's list.",
            ),
        ),
        discover=_discover,
        preview=_preview,
        apply=_apply_override,
        allowances=_allowances,
    ),
)
