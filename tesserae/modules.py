"""The game-module contract: trusted, bundled rules behind registered commands.

A module defines its commands, its JSON state and what each participant may
see of it. The host owns transport, identity, persistence, replay and
revisions; a module owns its fields and its rules. Modules come from an
explicit in-process catalog. The only code from a path is one package the
operator names explicitly (tesserae/external.py); nothing is discovered.
See docs/modules.md for the contract.
"""

import copy
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from functools import partial
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

HOST_API_VERSION = 1
# Version 2 judges drafts under the campaign's effective policy
# (docs/designs/governance_contract.md section 6).
CHARACTER_RULES_VERSIONS = (1, 2)
CAMPAIGN_SETTINGS_VERSION = 1
ADMINISTRATION_VERSION = 1
# Negotiated creation overrides (docs/designs/creation_overrides_contract.md).
CREATION_OVERRIDES_VERSION = 1
MAX_CATEGORIES = 16
MAX_CATEGORY_LABEL = 80
MAX_CATEGORY_DESCRIPTION = 500
MAX_CHANGES = 16
INTENT_BYTES = 4096
DISCOVERY_ITEMS = 50
DISCOVERY_BYTES = 16384
PREVIEW_BYTES = 12288
HEADLINE_CHARS = 200
HEADLINE_BYTES = 600
PREVIEW_ENTRIES = 32
PREVIEW_TEXT = 300
RECEIPT_BYTES = 8192
MAX_MATCH = 100
MAX_CURSOR = 200
# Category, quality, action and parameter IDs: never a path or a pattern.
OVERRIDE_ID = re.compile(r"[a-z][a-z0-9_]{0,47}")
PARAMETER_TYPES = ("integer", "string", "enum", "option")
REQUIREMENT_KINDS = ("mechanical", "narrative", "optional")
ACTION_REJECTED = "action_rejected"
# Command names starting with this belong to the host (character.name).
RESERVED_PREFIX = "character."
NAME_STEP = "character.name"
MAX_CHARACTER_NAME = 100

RandInt = Callable[[int, int], int]


class ModuleError(ValueError):
    """A catalog, selection or runtime cannot be used; nothing was run."""


class ArgumentError(ValueError):
    """A command's arguments are invalid. Never echo client input in it.

    `invalid_request` is a structural error, reported before the protocol
    version check; any other code is reported after it, like the dice
    `invalid_expression`.
    """

    def __init__(self, message: str, code: str = "invalid_request"):
        super().__init__(message)
        self.code = code
        self.message = message


class SeedError(ValueError):
    """A seed's module state is not valid for this module. Raised by a
    module's `validate_seed`; its message is shown to the operator."""


class ActionRejected(Exception):
    """A fresh action the rules do not allow now, such as spending a resource
    the caller lacks. Raise it before the first draw. The host reports the
    player-safe message as `action_rejected` and commits nothing.
    """

    code = ACTION_REJECTED

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class IntentError(ValueError):
    """An override intent the module cannot preview or apply as asked
    (creation overrides contract section 3): player-safe `problems`, refused
    as `invalid_intent` with nothing changed."""

    code = "invalid_intent"

    def __init__(self, problems: list[str]):
        problems = list(problems)
        if not problems or not all(isinstance(p, str) and p for p in problems):
            raise TypeError("IntentError needs a list of player-safe problems.")
        super().__init__("; ".join(problems))
        self.problems = problems


class UnsupportedOverride(ValueError):
    """An override names a category, quality, action or parameter the module
    does not have: refused as `override_unsupported`, nothing changed. The
    message is player-safe and never echoes client input."""

    code = "override_unsupported"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Provider:
    """One implementation of one operation.

    `implementation` is called with the execution's random source (a
    `randint(a, b)`, or None for the system's), then the operation's own
    arguments. It must hold no state between calls.
    """

    id: str
    version: str
    operation: str
    implementation: Callable


@dataclass(frozen=True)
class ProviderChoice:
    """An explicit selection: this provider, at this version, for this operation."""

    operation: str
    provider: str
    version: str


@dataclass(frozen=True)
class Action:
    """What a command's executor receives. `state` is a private copy.

    `participant_id` comes from the host's trusted context, never from the
    message. `operations` holds only the module's selected operations, bound
    to this execution's random source. `character_id` is the host's frozen
    actor in a character game (docs/designs/character_contract.md section 6),
    and always None in a game without characters. `policy` is the host's
    effective policy for that character (or the campaign's, with none),
    `{"revision", "settings"}`, in a game whose module has CampaignSettings,
    and None in any other.
    """

    state: dict
    participant_id: str
    args: dict
    operations: Mapping[str, Callable]
    character_id: str | None = None
    policy: dict | None = None


@dataclass(frozen=True)
class Outcome:
    """A committed action's replacement state and the caller's result."""

    state: dict
    result: dict


@dataclass(frozen=True)
class Command:
    """A registered command.

    `validate(args)` is pure: it checks the arguments alone, raises
    ArgumentError, and never reads state or draws. `execute(action)` is
    synchronous; it checks state-dependent rules (raising ActionRejected)
    before its first draw and returns an Outcome.
    """

    name: str
    validate: Callable[[dict], None]
    execute: Callable[[Action], Outcome]


@dataclass(frozen=True)
class CharacterRules:
    """A module's optional character capability (character contract section 3).

    `start(action)` gives a new draft its module data; it may draw, and raises
    ActionRejected before any draw to refuse. `steps` are creation commands,
    run only on a selected draft. `problems(state, character_id)` lists
    player-safe reasons a draft cannot be completed; an empty list means it
    can. `complete(state, character_id)` is pure and gets no random source:
    it returns the state with the draft made playable. `sheet(state,
    character_id)` is the owner's view of one character. `archive_veto(state,
    character_id)` returns a player-safe reason to refuse archiving, or None.
    `actions` and `creation_actions` describe, for the referee, what a ready
    character may do and which creation steps remain. `validate_seed(state,
    participant_ids, owners)` checks seeded state, where `owners` maps each
    declared character ID to its participant; raise SeedError.

    Version 2 (only with CampaignSettings) passes the character's effective
    policy as a third argument to `problems`, `complete`, `actions` and
    `creation_actions` (governance contract section 6).
    """

    version: int
    start: Callable[[Action], Outcome]
    steps: tuple[Command, ...]
    problems: Callable[[dict, str], list]
    complete: Callable[[dict, str], dict]
    sheet: Callable[[dict, str], dict]
    archive_veto: Callable[[dict, str], str | None]
    actions: Callable[[dict, str], list] | None = None
    creation_actions: Callable[[dict, str], list] | None = None
    validate_seed: Callable[[dict, frozenset, Mapping], None] | None = None


@dataclass(frozen=True)
class CampaignSettings:
    """A module's optional campaign settings and character requirements
    (docs/designs/governance_contract.md section 6).

    `defaults()` returns every setting the module supports with its default;
    no other key is accepted, and the defaults must reproduce the module's
    rules from before it had settings. `validate(settings, state)` is pure:
    player-safe problems with one character's or the campaign's effective
    settings, empty if valid. `requirements(policy, state, character_id)` is
    pure: what a character needs, as a list of entries with a stable `id`, a
    `kind` (mechanical, narrative or optional), a `description`, whether it is
    `met` (true, false, or null when the module does not record it; omitted
    with no character), and anything else the module describes (allowed
    `values`, a `method` such as choose or roll, a `support` note). It is the
    one description of a requirement, for the referee and players alike.
    """

    version: int
    defaults: Callable[[], dict]
    validate: Callable[[dict, dict], list]
    requirements: Callable[[dict, dict, str | None], list]


@dataclass(frozen=True)
class Administration:
    """A module's optional host administration
    (docs/designs/governance_contract.md sections 6 and 8).

    `inspect(state)` is pure: everything the campaign host may see of the
    state (every character's data, NPCs, the world, the referee's
    preparation). `operations` are commands only the campaign host runs,
    each naming its target in its own validated arguments (`character_id`,
    `npc_id`, ...); their executors see the host's sender, `host:<player_id>`,
    as `participant_id`, get the effective policy of a `character_id`
    argument's character (else the campaign's), and draw nothing: a
    correction never implies a roll. `describe(state)` is pure: the
    operations available now, each `{"command", "description",
    "target_kinds", "args"}`, guidance for clients and the referee, never a
    validator. `target_state(state, target)` is pure: the part of the state
    an operation or request on `target` (`{"kind", "id"}`) depends on, or
    ArgumentError for an unknown target. `target(command, args)`, if given,
    is pure: the target one of its operations changes, from its validated
    arguments; a request for an operation must name exactly that target, so
    its stale check covers what the operation changes. Without it only an
    operation with a `character_id` argument can be requested.
    """

    version: int
    inspect: Callable[[dict], dict]
    operations: tuple[Command, ...]
    describe: Callable[[dict], list]
    target_state: Callable[[dict, dict], dict]
    target: Callable[[str, dict], dict] | None = None


@dataclass(frozen=True)
class Category:
    """One module-declared group of overridable qualities."""

    id: str  # OVERRIDE_ID
    label: str  # 1 to 80 characters
    description: str  # up to 500 characters


@dataclass(frozen=True)
class OverrideAction:
    """What `CreationOverrides.apply` receives: copies, and an
    `authorization` the host built (never a client, a tool or a model)."""

    state: dict
    character_id: str
    plan: dict  # the preview's plan, exactly
    policy: dict  # the draft's effective policy
    authorization: dict


@dataclass(frozen=True)
class CreationOverrides:
    """A module's optional negotiated creation overrides
    (docs/designs/creation_overrides_contract.md section 3).

    `discover(state, character_id, query)` is pure: one page of what can be
    overridden on that draft now, `{"items", "next"}`. `preview(state,
    character_id, intent, policy)` is pure and deterministic, gets no random
    source, and returns exactly what the intent would do, with the facts it
    was computed from in `dependencies`; it raises IntentError or
    UnsupportedOverride. `apply(OverrideAction)` performs exactly the plan,
    deterministically and with no random source, enforcing every technical
    invariant itself; it returns an Outcome (the receipt as its result) or
    raises ActionRejected, IntentError or UnsupportedOverride with nothing
    changed. `allowances(state, character_id)` is pure: the draft's live
    creation-only allowances. Needs CharacterRules version 2.
    """

    version: int
    categories: tuple[Category, ...]
    discover: Callable[[dict, str, dict], dict]
    preview: Callable[[dict, str, dict, dict], dict]
    apply: Callable[[OverrideAction], Outcome]
    allowances: Callable[[dict, str], list]


@dataclass(frozen=True)
class Module:
    """A trusted module definition. It holds no game state.

    `providers` is the module's explicit choice for every operation in
    `requires`. `initial_state()` returns a fresh JSON object for a new game.
    `view(state, participant_id)` returns what that participant may see, or
    None for a metadata-only snapshot. Tags only describe the module.

    `validate_seed(state, participant_ids)`, if given, lets a game start from
    imported state (`tesserae game create --seed`). It is pure: it checks a
    proposed state, including that every reference to a participant names one
    of `participant_ids`, and raises SeedError if it is not valid. A module
    without it refuses seeds.

    Referee mode (docs/designs/referee_contract.md section 9) adds three
    optional hooks. `referee_context(state)` returns everything the referee
    should know about the bounded scene, hidden preparation included; it is
    never sent to a player. `referee_actions(state, participant_id)` describes
    the commands that participant may use now, with their stakes.
    `referee_commands` run only in the host's `referee` context, which no
    player can authenticate as; their executors see `participant_id ==
    "referee"`. A module without them gets a minimal default and no
    referee-only commands.

    `characters`, if given, makes every game of this module a character game
    (docs/designs/character_contract.md). Such a module checks seeds with
    `characters.validate_seed`, never `validate_seed`.

    `settings`, if given, lets a campaign's settings document configure the
    module (CampaignSettings); without it a document holds no settings.
    `administration`, if given, gives the campaign host its inspection and
    operations (Administration); without it the host inspects the referee
    context and every sheet, and runs nothing. `creation_overrides`, if
    given, describes, previews and applies negotiated changes to drafts
    (CreationOverrides).
    """

    id: str
    version: str
    state_version: int
    commands: tuple[Command, ...]
    initial_state: Callable[[], dict]
    view: Callable[[dict, str], dict | None]
    requires: frozenset[str] = frozenset()
    providers: tuple[ProviderChoice, ...] = ()
    tags: frozenset[str] = frozenset()
    requires_authentication: bool = False
    api_version: int = HOST_API_VERSION
    validate_seed: Callable[[dict, frozenset[str]], None] | None = None
    referee_context: Callable[[dict], dict] | None = None
    referee_actions: Callable[[dict, str], list] | None = None
    referee_commands: tuple[Command, ...] = ()
    characters: CharacterRules | None = None
    settings: CampaignSettings | None = None
    administration: Administration | None = None
    creation_overrides: CreationOverrides | None = None


@dataclass(frozen=True)
class Runtime:
    """A module with its selected providers resolved. Immutable and shareable
    between threads; every call works on copies."""

    module: Module
    providers: Mapping[str, Provider]
    _commands: Mapping[str, Command] = field(repr=False)

    def command(self, name: str) -> Command | None:
        return self._commands.get(name)

    @property
    def characters(self) -> CharacterRules | None:
        """The module's character capability, or None: games of this module
        then have no characters."""
        return self.module.characters

    def is_step(self, name: str) -> bool:
        """Whether `name` is a creation step (the module's, or the host's
        `character.name`) rather than a gameplay command."""
        rules = self.module.characters
        return rules is not None and (
            name == NAME_STEP or any(step.name == name for step in rules.steps)
        )

    def for_referee(self) -> "Runtime":
        """The same module and providers with only its referee-only commands,
        for the host's `referee` context. Player runtimes never include them."""
        return Runtime(
            module=self.module,
            providers=self.providers,
            _commands=MappingProxyType(
                {command.name: command for command in self.module.referee_commands}
            ),
        )

    def for_host(self) -> "Runtime":
        """The same module and providers with only its host operations, for a
        campaign host's context; player and referee runtimes never include
        them. Their actions get no random source: host operations draw
        nothing."""
        operations = () if self.module.administration is None else (
            self.module.administration.operations
        )  # fmt: skip
        return Runtime(
            module=self.module,
            providers=MappingProxyType({}),
            _commands=MappingProxyType({c.name: c for c in operations}),
        )

    # Host administration (governance contract section 6).

    @property
    def administration(self) -> Administration | None:
        return self.module.administration

    def host_inspect(self, state: dict) -> dict | None:
        """The module's host view of its state, or None without the hook."""
        if self.module.administration is None:
            return None
        return _json_copy(
            self.module.administration.inspect(_json_copy(state, "state")),
            "host inspection",
        )

    def host_operations(self, state: dict) -> list:
        """The host operations available now; [] without the capability."""
        if self.module.administration is None:
            return []
        described = self.module.administration.describe(_json_copy(state, "state"))
        described = _json_copy({"operations": described}, "host operations")
        operations = described["operations"]
        if not isinstance(operations, list) or not all(
            isinstance(o, dict) and isinstance(o.get("command"), str)
            for o in operations
        ):
            raise TypeError("Host operations are a list of objects with a command.")
        return operations

    def operation_target(self, command: str, args: dict) -> dict | None:
        """The target a host operation changes, by the module's own rule, or
        a `character_id` argument's character; None if neither says."""
        administration = self.module.administration
        if administration is not None and administration.target is not None:
            found = administration.target(command, copy.deepcopy(args))
            if (
                not isinstance(found, dict)
                or set(found) != {"kind", "id"}
                or not all(isinstance(v, str) for v in found.values())
            ):
                raise TypeError('An operation\'s target is {"kind", "id"} strings.')
            return dict(found)
        if isinstance(args.get("character_id"), str):
            return {"kind": "character", "id": args["character_id"]}
        return None

    def target_state(self, state: dict, target: dict) -> dict:
        """What `target` depends on (Administration.target_state). Without
        the capability only a character is a target: its sheet. Raises
        ArgumentError for an unknown target."""
        if not isinstance(target, dict) or set(target) != {"kind", "id"}:
            raise ArgumentError('A target is exactly {"kind", "id"}.')
        administration = self.module.administration
        if administration is not None:
            return _json_copy(
                administration.target_state(_json_copy(state, "state"), dict(target)),
                "target state",
            )
        if target["kind"] != "character" or self.module.characters is None:
            raise ArgumentError("This module has no such kind of target.")
        # The host checks that the character exists before asking.
        return self.character_sheet(state, target["id"])

    def referee_context(self, state: dict) -> dict | None:
        """The module's referee context, or None if it has no hook."""
        hook = self.module.referee_context
        if hook is None:
            return None
        return _json_copy(hook(_json_copy(state, "state")), "referee context")

    def referee_actions(self, state: dict, participant_id: str) -> list | None:
        """What `participant_id` may do now, or None if the module has no hook."""
        hook = self.module.referee_actions
        if hook is None:
            return None
        actions = hook(_json_copy(state, "state"), participant_id)
        return _json_copy({"actions": actions}, "referee actions")["actions"]

    @property
    def command_names(self) -> tuple[str, ...]:
        return tuple(self._commands)

    @property
    def selection(self) -> tuple[ProviderChoice, ...]:
        """The resolved choices, sorted, as a store would persist them."""
        return tuple(
            ProviderChoice(operation, provider.id, provider.version)
            for operation, provider in sorted(self.providers.items())
        )

    def initial_state(self) -> dict:
        return _json_copy(self.module.initial_state(), "initial state")

    def execute(
        self,
        state: dict,
        participant_id: str,
        command: str,
        args: dict,
        *,
        randint: RandInt | None = None,
        character_id: str | None = None,
        policy: dict | None = None,
    ) -> Outcome:
        """Run one validated command. Raises ActionRejected, or anything the
        module raises unexpectedly; the caller then commits nothing."""
        return self._run(
            self._commands[command].execute,
            state,
            participant_id,
            args,
            randint,
            character_id,
            f"Command {command!r}",
            policy,
        )

    # Campaign settings (governance contract section 6). The host merges and
    # stores them; the module only supplies defaults and judges values.

    @property
    def settings(self) -> CampaignSettings | None:
        return self.module.settings

    def settings_defaults(self) -> dict:
        """Every supported setting with its default; {} without the capability."""
        if self.module.settings is None:
            return {}
        return _json_copy(self.module.settings.defaults(), "settings defaults")

    def settings_problems(self, settings: dict, state: dict) -> list[str]:
        """The module's player-safe problems with these effective settings."""
        problems = self.module.settings.validate(
            _json_copy(settings, "settings"), _json_copy(state, "state")
        )
        _check_problems(problems, "Settings problems")
        return list(problems)

    def requirements(
        self, policy: dict, state: dict, character_id: str | None
    ) -> list | None:
        """What a character (or, with None, any new character) needs, or None
        without the capability. Entries are checked to carry what clients and
        the referee rely on."""
        if self.module.settings is None:
            return None
        entries = self.module.settings.requirements(
            _json_copy(policy, "policy"), _json_copy(state, "state"), character_id
        )
        entries = _json_copy({"entries": entries}, "requirements")["entries"]
        if not isinstance(entries, list):
            raise TypeError("Requirements must be a list.")
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("id"), str)
                or entry.get("kind") not in REQUIREMENT_KINDS
                or not isinstance(entry.get("description"), str)
                or not any(entry.get("met") is value for value in (True, False, None))
            ):
                raise TypeError(
                    "A requirement has a string id and description, a kind of "
                    "mechanical, narrative or optional, and met true, false or null."
                )
        return entries

    # The character capability (docs/designs/character_contract.md section 3).
    # Each hook sees copies; every result is checked to be JSON.

    def start_character(
        self,
        state: dict,
        participant_id: str,
        character_id: str,
        *,
        randint: RandInt | None = None,
        policy: dict | None = None,
    ) -> Outcome:
        """A new draft's module data. Raises ActionRejected to refuse."""
        return self._run(
            self._rules().start,
            state,
            participant_id,
            {},
            randint,
            character_id,
            "Character start",
            policy,
        )

    def character_problems(
        self, state: dict, character_id: str, policy: dict | None = None
    ) -> list[str]:
        problems = self._judge(self._rules().problems, state, character_id, policy)
        _check_problems(problems, "Character problems")
        return list(problems)

    def complete_character(
        self, state: dict, character_id: str, policy: dict | None = None
    ) -> dict:
        """The state with the draft made playable; deterministic, no draws."""
        return _json_copy(
            self._judge(self._rules().complete, state, character_id, policy),
            "state",
        )

    def character_sheet(self, state: dict, character_id: str) -> dict:
        return _json_copy(
            self._rules().sheet(_json_copy(state, "state"), character_id),
            "character sheet",
        )

    def archive_veto(self, state: dict, character_id: str) -> str | None:
        reason = self._rules().archive_veto(_json_copy(state, "state"), character_id)
        if reason is not None and (not isinstance(reason, str) or not reason):
            raise TypeError("An archive veto is a non-empty string or None.")
        return reason

    def character_actions(
        self, state: dict, character_id: str, policy: dict | None = None
    ) -> list | None:
        """What a ready character may do now, or None without the hook."""
        return self._described(self._rules().actions, state, character_id, policy)

    def creation_actions(
        self, state: dict, character_id: str, policy: dict | None = None
    ) -> list | None:
        """The creation steps a draft has left, or None without the hook."""
        return self._described(
            self._rules().creation_actions, state, character_id, policy
        )

    def validate_character_seed(
        self, state: dict, participant_ids: frozenset[str], owners: Mapping[str, str]
    ):
        """Check seeded state for a character module; raise SeedError."""
        validate = self._rules().validate_seed
        if validate is None:
            raise SeedError(f"Module {self.module.id!r} does not accept seeds.")
        try:
            validate(
                _json_copy(state, "seed state"),
                frozenset(participant_ids),
                MappingProxyType(dict(owners)),
            )
        except SeedError:
            raise
        except Exception as error:  # a broken validator, not a verdict
            raise SeedError(
                f"Module {self.module.id!r} could not check the seed: "
                f"{type(error).__name__}: {error}"
            ) from error

    def _rules(self) -> CharacterRules:
        rules = self.module.characters
        if rules is None:
            raise ModuleError(f"Module {self.module.id!r} has no characters.")
        return rules

    def _judge(self, hook, state: dict, character_id: str, policy: dict | None):
        """A draft-judging hook, called as its rules version expects: version 2
        also gets the effective policy."""
        if self._rules().version == 1:
            return hook(_json_copy(state, "state"), character_id)
        if policy is None:
            raise ValueError("Version 2 character rules are judged under a policy.")
        return hook(
            _json_copy(state, "state"), character_id, _json_copy(policy, "policy")
        )

    def _described(self, hook, state, character_id, policy) -> list | None:
        if hook is None:
            return None
        actions = self._judge(hook, state, character_id, policy)
        return _json_copy({"actions": actions}, "actions")["actions"]

    def _run(
        self, execute, state, participant_id, args, randint, character_id, what,
        policy=None,
    ):  # fmt: skip
        operations = {
            operation: partial(provider.implementation, randint)
            for operation, provider in self.providers.items()
        }
        outcome = execute(
            Action(
                state=_json_copy(state, "state"),
                participant_id=participant_id,
                args=copy.deepcopy(args),
                operations=operations,
                character_id=character_id,
                policy=None if policy is None else _json_copy(policy, "policy"),
            )
        )
        if not isinstance(outcome, Outcome):
            raise TypeError(f"{what} did not return an Outcome.")
        return Outcome(
            state=_json_copy(outcome.state, "state"),
            result=_json_copy(outcome.result, "result"),
        )

    def validate_seed(self, state: dict, participant_ids: frozenset[str]):
        """Check imported state with the module's validator; raise SeedError.

        A module without one refuses every seed. The validator sees a copy.
        """
        validate = self.module.validate_seed
        if validate is None:
            raise SeedError(f"Module {self.module.id!r} does not accept seeds.")
        try:
            validate(_json_copy(state, "seed state"), frozenset(participant_ids))
        except SeedError:
            raise
        except Exception as error:  # a broken validator, not a verdict
            raise SeedError(
                f"Module {self.module.id!r} could not check the seed: "
                f"{type(error).__name__}: {error}"
            ) from error

    def view(self, state: dict, participant_id: str) -> dict | None:
        view = self.module.view(_json_copy(state, "state"), participant_id)
        return None if view is None else _json_copy(view, "view")

    # Negotiated creation overrides (creation overrides contract section 3).
    # The host checks authority, the target's phase and category policy
    # before calling these; each hook sees copies, and every answer is
    # checked against the contract's shapes and result-byte bounds. A
    # malformed or oversized answer is a module failure (TypeError).

    @property
    def creation_overrides(self) -> CreationOverrides | None:
        return self.module.creation_overrides

    def override_categories(self) -> list[dict]:
        """The declared categories, `{"category_id", "label", "description"}`,
        in the module's order; [] without the capability."""
        overrides = self.module.creation_overrides
        if overrides is None:
            return []
        return [
            {"category_id": c.id, "label": c.label, "description": c.description}
            for c in overrides.categories
        ]

    def override_discover(self, state: dict, character_id: str, query: dict) -> dict:
        """One page of discovery for a draft, `{"items", "next"}`. Raises
        ArgumentError for a malformed query, UnsupportedOverride for an
        unknown category, quality or parameter."""
        overrides = self._overrides()
        check_query(query)
        if "category_id" in query and query["category_id"] not in self._category_ids():
            raise UnsupportedOverride("This module has no such category.")
        page = overrides.discover(
            _json_copy(state, "state"), character_id, copy.deepcopy(query)
        )
        page = _json_copy(page, "discovery")
        if set(page) != {"items", "next"} or not isinstance(page["items"], list):
            raise TypeError('Discovery is exactly {"items", "next"}.')
        if not (
            page["next"] is None
            or (isinstance(page["next"], str) and 0 < len(page["next"]) <= MAX_CURSOR)
        ):
            raise TypeError(
                f"Discovery's next is null or 1 to {MAX_CURSOR} characters."
            )
        if len(page["items"]) > DISCOVERY_ITEMS:
            raise TypeError(f"A discovery page has at most {DISCOVERY_ITEMS} items.")
        options = "parameter" in query
        for item in page["items"]:
            if options:
                _check_option(item)
            else:
                _check_quality(item, self._category_ids())
        _check_bytes(page, DISCOVERY_BYTES, "A discovery page")
        return page

    def override_preview(
        self, state: dict, character_id: str, intent: dict, policy: dict
    ) -> dict:
        """The module's preview of `intent` on a draft now. Raises
        IntentError (the intent's shape, the module's problems, or too big a
        preview) or UnsupportedOverride; nothing changes."""
        overrides = self._overrides()
        check_intent(intent)
        preview = overrides.preview(
            _json_copy(state, "state"),
            character_id,
            _json_copy(intent, "intent"),
            _json_copy(policy, "policy"),
        )
        preview = _json_copy(preview, "preview")
        _check_preview(preview, intent, self._category_ids())
        if result_bytes(preview) > PREVIEW_BYTES:
            raise IntentError(["Too many changes at once; split the request."])
        return preview

    def override_apply(
        self,
        state: dict,
        character_id: str,
        plan: dict,
        policy: dict,
        authorization: dict,
    ) -> Outcome:
        """Perform a previewed plan, with no random source. `authorization`
        is the host's own (check_authorization). Raises ActionRejected,
        IntentError or UnsupportedOverride with nothing changed."""
        overrides = self._overrides()
        check_authorization(authorization)
        outcome = overrides.apply(
            OverrideAction(
                state=_json_copy(state, "state"),
                character_id=character_id,
                plan=_json_copy(plan, "plan"),
                policy=_json_copy(policy, "policy"),
                authorization=_json_copy(authorization, "authorization"),
            )
        )
        if not isinstance(outcome, Outcome):
            raise TypeError("Override apply did not return an Outcome.")
        outcome = Outcome(
            state=_json_copy(outcome.state, "state"),
            result=_json_copy(outcome.result, "receipt"),
        )
        _check_bytes(outcome.result, RECEIPT_BYTES, "A receipt")
        return outcome

    def override_allowances(self, state: dict, character_id: str) -> list:
        """The draft's live allowances, each `{"allowance_id", "quality_id",
        "summary", "remaining"}`; [] without the capability."""
        overrides = self.module.creation_overrides
        if overrides is None:
            return []
        found = overrides.allowances(_json_copy(state, "state"), character_id)
        found = _json_copy({"allowances": found}, "allowances")["allowances"]
        if not isinstance(found, list) or not all(
            isinstance(a, dict)
            and set(a) == {"allowance_id", "quality_id", "summary", "remaining"}
            and _is_id(a["allowance_id"])
            and _is_id(a["quality_id"])
            and _is_text(a["summary"], PREVIEW_TEXT)
            for a in found
        ):
            raise TypeError(
                'Allowances are a list of {"allowance_id", "quality_id", '
                '"summary", "remaining"}.'
            )
        return found

    def _overrides(self) -> CreationOverrides:
        overrides = self.module.creation_overrides
        if overrides is None:
            raise UnsupportedOverride("This game's module has no creation overrides.")
        return overrides

    def _category_ids(self) -> frozenset:
        return frozenset(c.id for c in self._overrides().categories)


class Catalog:
    """An explicit set of modules and providers. Registration order never
    decides anything: every required operation needs an explicit choice."""

    def __init__(self, modules: Iterable[Module], providers: Iterable[Provider]):
        self._modules = _unique(modules, lambda m: m.id, "module")
        self._providers = _unique(providers, lambda p: p.id, "provider")
        for module in self._modules.values():
            steps = () if module.characters is None else module.characters.steps
            operations = (
                ()
                if module.administration is None
                else module.administration.operations
            )
            commands = (*module.commands, *module.referee_commands, *steps, *operations)
            _unique(commands, lambda c: c.name, f"{module.id} command")
            for command in commands:
                if command.name.startswith(RESERVED_PREFIX):
                    raise ModuleError(
                        f"Command {command.name!r} of {module.id} uses the host's "
                        f"reserved {RESERVED_PREFIX!r} prefix."
                    )
            if module.characters is not None and module.validate_seed is not None:
                raise ModuleError(
                    f"Module {module.id} has characters, so it checks seeds with "
                    "characters.validate_seed, not validate_seed."
                )
            if module.creation_overrides is not None:
                _check_categories(module)

    def module_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._modules))

    def extended(
        self, modules: Iterable[Module] = (), providers: Iterable[Provider] = ()
    ) -> "Catalog":
        """A new catalog with these added. An ID already here is a duplicate,
        never a replacement."""
        return Catalog(
            [*self._modules.values(), *modules],
            [*self._providers.values(), *providers],
        )

    def resolve(
        self,
        module_id: str,
        *,
        version: str | None = None,
        providers: Iterable[ProviderChoice] | None = None,
    ) -> Runtime:
        """The runtime for one module, checked before anything runs.

        `version` and `providers` pin a binding, such as one a game was
        created with; they default to the catalog module's own. Raises
        ModuleError for an unknown or mismatched module, an unsupported API
        version, or a missing, unknown, mismatched or competing provider.
        """
        module = self._modules.get(module_id)
        if module is None:
            raise ModuleError(f"No bundled module {module_id!r}.")
        if version is not None and version != module.version:
            raise ModuleError(
                f"Module {module_id!r} is version {module.version}, not {version}."
            )
        if module.api_version != HOST_API_VERSION:
            raise ModuleError(
                f"Module {module_id!r} needs host API {module.api_version}; "
                f"this host supports {HOST_API_VERSION}."
            )
        if (
            module.characters is not None
            and module.characters.version not in CHARACTER_RULES_VERSIONS
        ):
            raise ModuleError(
                f"Module {module_id!r} has character rules version "
                f"{module.characters.version}; this host supports "
                + " and ".join(str(v) for v in CHARACTER_RULES_VERSIONS)
                + "."
            )
        if (
            module.settings is not None
            and module.settings.version != CAMPAIGN_SETTINGS_VERSION
        ):
            raise ModuleError(
                f"Module {module_id!r} has campaign settings version "
                f"{module.settings.version}; this host supports "
                f"{CAMPAIGN_SETTINGS_VERSION}."
            )
        if (
            module.administration is not None
            and module.administration.version != ADMINISTRATION_VERSION
        ):
            raise ModuleError(
                f"Module {module_id!r} has administration version "
                f"{module.administration.version}; this host supports "
                f"{ADMINISTRATION_VERSION}."
            )
        if (
            module.characters is not None
            and module.characters.version == 2
            and module.settings is None
        ):
            raise ModuleError(
                f"Module {module_id!r} uses character rules version 2, which "
                "needs campaign settings."
            )
        overrides = module.creation_overrides
        if overrides is not None and overrides.version != CREATION_OVERRIDES_VERSION:
            raise ModuleError(
                f"Module {module_id!r} has creation overrides version "
                f"{overrides.version}; this host supports "
                f"{CREATION_OVERRIDES_VERSION}."
            )
        if overrides is not None and (
            module.characters is None or module.characters.version != 2
        ):
            raise ModuleError(
                f"Module {module_id!r} has creation overrides, which need "
                "character rules version 2."
            )
        choices = tuple(module.providers if providers is None else providers)
        selected = {}
        for choice in choices:
            if choice.operation not in module.requires:
                raise ModuleError(
                    f"Module {module_id!r} does not use {choice.operation!r}."
                )
            if choice.operation in selected:
                raise ModuleError(
                    f"More than one provider is selected for {choice.operation!r}."
                )
            provider = self._providers.get(choice.provider)
            if provider is None:
                raise ModuleError(f"No provider {choice.provider!r}.")
            if provider.operation != choice.operation:
                raise ModuleError(
                    f"Provider {choice.provider!r} supplies {provider.operation!r}, "
                    f"not {choice.operation!r}."
                )
            if provider.version != choice.version:
                raise ModuleError(
                    f"Provider {choice.provider!r} is version {provider.version}, "
                    f"not {choice.version}."
                )
            selected[choice.operation] = provider
        missing = sorted(module.requires - selected.keys())
        if missing:
            raise ModuleError(
                f"Module {module_id!r} has no provider selected for "
                + ", ".join(repr(operation) for operation in missing)
                + "."
            )
        # A character game's players may name its creation steps too; the
        # store decides which ones the caller's mode allows.
        commands = module.commands
        if module.characters is not None:
            commands = (*commands, *module.characters.steps, NAME_COMMAND)
        return Runtime(
            module=module,
            providers=MappingProxyType(dict(sorted(selected.items()))),
            _commands=MappingProxyType({command.name: command for command in commands}),
        )


def check_character_name(name):
    """Raise ArgumentError unless `name` is a valid character label: the same
    rules as a participant name."""
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > MAX_CHARACTER_NAME
        or any(unicodedata.category(character) == "Cc" for character in name)
    ):
        raise ArgumentError(
            f"A name is 1 to {MAX_CHARACTER_NAME} characters, not blank, "
            "with no control characters."
        )


def _validate_name_step(args: dict):
    exact_fields(args, {"name"}, "Name args")
    check_character_name(args["name"])


def _name_step(action) -> Outcome:
    # The store records the label in the character's metadata; the module's
    # state is unchanged.
    return Outcome(action.state, {"name": action.args["name"]})


# The host's creation step every character game has (contract section 3).
NAME_COMMAND = Command(NAME_STEP, _validate_name_step, _name_step)


def exact_fields(args: dict, names: Iterable[str], subject: str):
    """Raise ArgumentError unless `args` has exactly these field names."""
    names = frozenset(names)
    missing = sorted(names - args.keys())
    extra = sorted(args.keys() - names)
    if missing or extra:
        raise ArgumentError(describe_fields(missing, extra, subject))


def describe_fields(missing: list[str], extra: list[str], subject: str) -> str:
    # Missing names come from the contract; extra names are client input (a
    # token pasted as a key, for example), so they are counted, never echoed.
    parts = []
    if missing:
        parts.append("missing " + ", ".join(missing))
    if extra:
        parts.append(f"{len(extra)} unexpected field{'s' if len(extra) > 1 else ''}")
    return f"{subject} has {'; '.join(parts)}."


def result_bytes(value) -> int:
    """A value's size as the referee bridge sends a tool result:
    `json.dumps` with Python's defaults, so every non-ASCII character is
    escaped (creation overrides contract section 10)."""
    return len(json.dumps(value))


def preview_digest(preview: dict) -> str:
    """SHA-256 of a preview's canonical JSON: what "the same effects" means."""
    canonical = json.dumps(
        preview, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def check_intent(intent):
    """Raise IntentError unless `intent` is exactly `{"changes": [...]}`: 1
    to 16 changes, each exactly `{"quality_id", "action_id", "parameters"}`
    with identifier IDs and an object of parameters, in at most 4,096
    result bytes. The module judges what the changes mean."""
    if not isinstance(intent, dict) or set(intent) != {"changes"}:
        raise IntentError(['An intent is exactly {"changes": [...]}.'])
    changes = intent["changes"]
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_CHANGES:
        raise IntentError([f"An intent has 1 to {MAX_CHANGES} changes."])
    for change in changes:
        if (
            not isinstance(change, dict)
            or set(change) != {"quality_id", "action_id", "parameters"}
            or not _is_id(change["quality_id"])
            or not _is_id(change["action_id"])
            or not isinstance(change["parameters"], dict)
            or not all(_is_id(name) for name in change["parameters"])
        ):
            raise IntentError([
                'A change is exactly {"quality_id", "action_id", "parameters"}: '
                "two identifiers and an object of named parameters."
            ])  # fmt: skip
    try:
        size = result_bytes(intent)
        json.dumps(intent, allow_nan=False)
    except (TypeError, ValueError):
        raise IntentError(["An intent is JSON."]) from None
    if size > INTENT_BYTES:
        raise IntentError([f"An intent is at most {INTENT_BYTES} bytes."])


def check_query(query):
    """Raise ArgumentError unless `query` holds only discovery's optional
    string fields (creation overrides contract section 3)."""
    allowed = {"category_id", "quality_id", "parameter", "match", "after"}
    if not isinstance(query, dict) or set(query) - allowed:
        raise ArgumentError(
            "A query has only category_id, quality_id, parameter, match and after."
        )
    for name in ("category_id", "quality_id", "parameter"):
        if name in query and not _is_id(query[name]):
            raise ArgumentError(f"'{name}' is an identifier.")
    if "match" in query and not _is_text(query["match"], MAX_MATCH):
        raise ArgumentError(f"'match' is text of 1 to {MAX_MATCH} characters.")
    if "after" in query and not _is_text(query["after"], MAX_CURSOR):
        raise ArgumentError("'after' is a cursor from an earlier page.")
    if "parameter" in query and "quality_id" not in query:
        raise ArgumentError("A query for a parameter's options names its quality.")


AUTHORIZATION_KINDS = ("accepted_offer", "direct")
_AUTHORIZATION_FIELDS = frozenset({
    "kind", "record_id", "host_player_id", "proposal_id", "offer_id",
    "requester_participant_id", "reason",
})  # fmt: skip


def check_authorization(authorization):
    """Raise ValueError unless `authorization` is the host's own record of
    who authorized an application (creation overrides contract section 3).
    It is built by the host, so a bad one is a host bug, not a refusal."""
    if (
        not isinstance(authorization, dict)
        or set(authorization) != _AUTHORIZATION_FIELDS
        or authorization["kind"] not in AUTHORIZATION_KINDS
        or not all(
            isinstance(authorization[k], str) and authorization[k]
            for k in ("record_id", "host_player_id", "reason")
        )
        or not all(
            authorization[k] is None or isinstance(authorization[k], str)
            for k in ("proposal_id", "offer_id", "requester_participant_id")
        )
        or (authorization["kind"] == "direct") != (authorization["proposal_id"] is None)
    ):
        raise ValueError("Not a host-built override authorization.")


def _check_categories(module: Module):
    categories = module.creation_overrides.categories
    if not isinstance(categories, tuple) or not 1 <= len(categories) <= MAX_CATEGORIES:
        raise ModuleError(
            f"Module {module.id} declares 1 to {MAX_CATEGORIES} override categories."
        )
    seen = set()
    for category in categories:
        if (
            not isinstance(category, Category)
            or not _is_id(category.id)
            or not _is_text(category.label, MAX_CATEGORY_LABEL)
            or not isinstance(category.description, str)
            or len(category.description) > MAX_CATEGORY_DESCRIPTION
        ):
            raise ModuleError(
                f"Module {module.id} has a malformed override category: an ID of "
                f"lowercase letters, digits and '_', a label of 1 to "
                f"{MAX_CATEGORY_LABEL} characters and a description of up to "
                f"{MAX_CATEGORY_DESCRIPTION}."
            )
        if category.id in seen:
            raise ModuleError(
                f"Duplicate {module.id} override category {category.id!r}."
            )
        seen.add(category.id)


def _check_quality(item, categories: frozenset):
    if (
        not isinstance(item, dict)
        or set(item) != {"quality_id", "category_id", "label", "description",
                         "current", "actions"}
        or not _is_id(item["quality_id"])
        or item["category_id"] not in categories
        or not _is_text(item["label"], MAX_CATEGORY_LABEL)
        or not isinstance(item["description"], str)
        or len(item["description"]) > MAX_CATEGORY_DESCRIPTION
        or not isinstance(item["actions"], list)
    ):  # fmt: skip
        raise TypeError(
            'A discovered quality is {"quality_id", "category_id", "label", '
            '"description", "current", "actions"} in a declared category.'
        )
    for action in item["actions"]:
        if (
            not isinstance(action, dict)
            or set(action) != {"action_id", "label", "parameters", "later_random"}
            or not _is_id(action["action_id"])
            or not _is_text(action["label"], MAX_CATEGORY_LABEL)
            or not isinstance(action["later_random"], bool)
            or not isinstance(action["parameters"], dict)
        ):
            raise TypeError(
                'An action is {"action_id", "label", "parameters", "later_random"}.'
            )
        for name, described in action["parameters"].items():
            if (
                not _is_id(name)
                or not isinstance(described, dict)
                or described.get("type") not in PARAMETER_TYPES
                or not isinstance(described.get("required"), bool)
            ):
                raise TypeError(
                    "A parameter is described by a type (integer, string, enum "
                    "or option) and whether it is required."
                )


def _check_option(item):
    if (
        not isinstance(item, dict)
        or set(item) != {"value", "label"}
        or not _is_text(item["label"], MAX_CATEGORY_LABEL)
    ):
        raise TypeError('A parameter option is {"value", "label"}.')


_PREVIEW_FIELDS = frozenset({
    "plan", "headline", "affected", "effects", "waivers", "allowances",
    "later_steps", "consequences", "limitations", "dependencies",
})  # fmt: skip
_ENTRY_FIELDS = {
    "effects": frozenset({"quality_id", "summary", "before", "after"}),
    "waivers": frozenset({"quality_id", "summary", "creation_only"}),
    "allowances": frozenset({"allowance_id", "quality_id", "summary", "creation_only"}),
    "later_steps": frozenset({"command", "summary", "stakes"}),
}


def _check_preview(preview: dict, intent: dict, categories: frozenset):
    """TypeError unless `preview` has the contract's shape, every quality the
    intent names among `affected`, and every affected quality in a declared
    category."""
    if set(preview) != _PREVIEW_FIELDS:
        raise TypeError("A preview has exactly the contract's fields.")
    if not isinstance(preview["plan"], dict) or not isinstance(
        preview["dependencies"], dict
    ):
        raise TypeError("A preview's plan and dependencies are objects.")
    headline = preview["headline"]
    if (
        not _is_text(headline, HEADLINE_CHARS)
        or "\n" in headline
        or result_bytes(headline) > HEADLINE_BYTES
    ):
        raise TypeError(
            f"A headline is one line of 1 to {HEADLINE_CHARS} characters and at "
            f"most {HEADLINE_BYTES} bytes."
        )
    affected = preview["affected"]
    if not isinstance(affected, list) or not all(
        isinstance(a, dict)
        and set(a) == {"quality_id", "category_id"}
        and _is_id(a["quality_id"])
        and a["category_id"] in categories
        for a in affected
    ):
        raise TypeError(
            'Affected qualities are {"quality_id", "category_id"} in declared '
            "categories."
        )
    named = {change["quality_id"] for change in intent["changes"]}
    if not named <= {a["quality_id"] for a in affected}:
        raise TypeError("A preview lists every quality its intent names as affected.")
    for name in ("consequences", "limitations"):
        entries = preview[name]
        if (
            not isinstance(entries, list)
            or len(entries) > PREVIEW_ENTRIES
            or not all(_is_text(text, PREVIEW_TEXT) for text in entries)
        ):
            raise TypeError(
                f"A preview's {name} are up to {PREVIEW_ENTRIES} texts of 1 to "
                f"{PREVIEW_TEXT} characters."
            )
    for name, fields in _ENTRY_FIELDS.items():
        entries = preview[name]
        if not isinstance(entries, list) or len(entries) > PREVIEW_ENTRIES:
            raise TypeError(f"A preview has up to {PREVIEW_ENTRIES} {name}.")
        for entry in entries:
            texts = [entry.get(k) for k in ("summary", "stakes") if k in fields]
            if (
                not isinstance(entry, dict)
                or set(entry) != fields
                or not all(_is_text(text, PREVIEW_TEXT) for text in texts)
                or ("quality_id" in fields and not _is_id(entry["quality_id"]))
                or ("creation_only" in fields
                    and not isinstance(entry["creation_only"], bool))
                or (name == "allowances" and (entry["creation_only"] is not True
                                              or not _is_id(entry["allowance_id"])))
                or (name == "later_steps" and not _is_text(entry["command"], 100))
            ):  # fmt: skip
                raise TypeError(f"A preview's {name} do not have the contract's shape.")


def _check_bytes(value, limit: int, what: str):
    if result_bytes(value) > limit:
        raise TypeError(f"{what} is over {limit} result bytes.")


def _is_id(value) -> bool:
    return isinstance(value, str) and OVERRIDE_ID.fullmatch(value) is not None


def _is_text(value, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _check_problems(problems, what: str):
    if not isinstance(problems, list) or not all(
        isinstance(problem, str) and problem for problem in problems
    ):
        raise TypeError(f"{what} must be a list of strings.")


def _json_copy(value, what: str) -> dict:
    """A detached copy of a JSON object, or TypeError if it is not one."""
    if not isinstance(value, dict):
        raise TypeError(f"Module {what} must be a JSON object.")
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise TypeError(f"Module {what} is not JSON: {error}") from None


def _unique(items, key, kind: str) -> dict:
    found = {}
    for item in items:
        if key(item) in found:
            raise ModuleError(f"Duplicate {kind} {key(item)!r}.")
        found[key(item)] = item
    return found
