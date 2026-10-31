"""The game-module contract: trusted, bundled rules behind registered commands.

A module defines its commands, its JSON state and what each participant may
see of it. The host owns transport, identity, persistence, replay and
revisions; a module owns its fields and its rules. Modules come from an
explicit in-process catalog; nothing is discovered or imported from a path.
See docs/modules.md for the contract.
"""

import copy
import json
from dataclasses import dataclass, field
from functools import partial
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

HOST_API_VERSION = 1
ACTION_REJECTED = "action_rejected"

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


class ActionRejected(Exception):
    """A fresh action the rules do not allow now, such as spending a resource
    the caller lacks. Raise it before the first draw. The host reports the
    player-safe message as `action_rejected` and commits nothing.
    """

    code = ACTION_REJECTED

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
    to this execution's random source.
    """

    state: dict
    participant_id: str
    args: dict
    operations: Mapping[str, Callable]


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
class Module:
    """A trusted module definition. It holds no game state.

    `providers` is the module's explicit choice for every operation in
    `requires`. `initial_state()` returns a fresh JSON object for a new game.
    `view(state, participant_id)` returns what that participant may see, or
    None for a metadata-only snapshot. Tags only describe the module.
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
    ) -> Outcome:
        """Run one validated command. Raises ActionRejected, or anything the
        module raises unexpectedly; the caller then commits nothing."""
        definition = self._commands[command]
        operations = {
            operation: partial(provider.implementation, randint)
            for operation, provider in self.providers.items()
        }
        outcome = definition.execute(
            Action(
                state=_json_copy(state, "state"),
                participant_id=participant_id,
                args=copy.deepcopy(args),
                operations=operations,
            )
        )
        if not isinstance(outcome, Outcome):
            raise TypeError(f"Command {command!r} did not return an Outcome.")
        return Outcome(
            state=_json_copy(outcome.state, "state"),
            result=_json_copy(outcome.result, "result"),
        )

    def view(self, state: dict, participant_id: str) -> dict | None:
        view = self.module.view(_json_copy(state, "state"), participant_id)
        return None if view is None else _json_copy(view, "view")


class Catalog:
    """An explicit set of modules and providers. Registration order never
    decides anything: every required operation needs an explicit choice."""

    def __init__(self, modules: Iterable[Module], providers: Iterable[Provider]):
        self._modules = _unique(modules, lambda m: m.id, "module")
        self._providers = _unique(providers, lambda p: p.id, "provider")
        for module in self._modules.values():
            _unique(module.commands, lambda c: c.name, f"{module.id} command")

    def module_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._modules))

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
        return Runtime(
            module=module,
            providers=MappingProxyType(dict(sorted(selected.items()))),
            _commands=MappingProxyType(
                {command.name: command for command in module.commands}
            ),
        )


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
