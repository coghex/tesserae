"""Seeds: a new game's imported starting point (`tesserae game create --seed`).

A seed is a version-1 JSON file naming the module binding it was made for,
the game's participants (without credentials), the module state and opaque
import provenance. The host checks the envelope, the participants and the
binding; the module checks its own state (`Module.validate_seed`). A seed is
a baseline at revision 0, never a replay of earlier commands. See
docs/game_store.md#seeded-creation.
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path

from .modules import ProviderChoice, Runtime, SeedError
from .store import SEED_VERSION, check_name, is_participant_id


@dataclass(frozen=True)
class SeedParticipant:
    participant_id: str
    name: str


@dataclass(frozen=True)
class Seed:
    module_id: str
    module_version: str
    state_version: int
    providers: tuple[ProviderChoice, ...]
    participants: tuple[SeedParticipant, ...]
    state: dict
    provenance: dict

    def check(self, runtime: Runtime):
        """Raise SeedError unless this seed fits `runtime` exactly and the
        module accepts its state for these participants."""
        made_for = (
            self.module_id,
            self.module_version,
            self.state_version,
            tuple(sorted(self.providers, key=lambda c: c.operation)),
        )
        module = runtime.module
        loaded = (module.id, module.version, module.state_version, runtime.selection)
        if made_for[:3] != loaded[:3]:
            raise SeedError(
                f"The seed is for module {self.module_id} {self.module_version} "
                f"(state {self.state_version}), not {module.id} {module.version} "
                f"(state {module.state_version})."
            )
        if made_for[3] != loaded[3]:
            raise SeedError("The seed's provider choices differ from the module's.")
        runtime.validate_seed(
            self.state, frozenset(p.participant_id for p in self.participants)
        )


def read_seed(path) -> Seed:
    """Read and check a seed file's envelope; raise SeedError or OSError."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise SeedError(f"Seed {path} is not UTF-8: {error}") from None
    return parse_seed(text)


def parse_seed(text: str) -> Seed:
    try:
        data = json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_no_constant,
            parse_float=_finite_float,
        )
    except (ValueError, RecursionError) as error:
        raise SeedError(f"The seed is not valid JSON: {error}") from None
    _exact(
        data,
        {"seed_version", "module", "participants", "state", "provenance"},
        "The seed",
    )
    if type(data["seed_version"]) is not int or data["seed_version"] != SEED_VERSION:
        raise SeedError(
            f"The seed is version {data['seed_version']!r}; "
            f"this host reads version {SEED_VERSION}."
        )

    module = data["module"]
    _exact(module, {"id", "version", "state_version", "providers"}, "The seed's module")
    if not isinstance(module["id"], str) or not isinstance(module["version"], str):
        raise SeedError("The seed's module id and version must be strings.")
    state_version = module["state_version"]
    if type(state_version) is not int or state_version < 0:
        raise SeedError("The seed's state_version must be a non-negative integer.")
    if not isinstance(module["providers"], list):
        raise SeedError("The seed's module providers must be a list.")
    providers = []
    for number, choice in enumerate(module["providers"], 1):
        _exact(
            choice, {"operation", "provider", "version"}, f"Provider choice {number}"
        )
        if not all(isinstance(value, str) for value in choice.values()):
            raise SeedError(f"Provider choice {number} must hold strings.")
        providers.append(
            ProviderChoice(choice["operation"], choice["provider"], choice["version"])
        )

    if not isinstance(data["participants"], list):
        raise SeedError("The seed's participants must be a list.")
    participants, seen = [], set()
    for number, entry in enumerate(data["participants"], 1):
        _exact(entry, {"participant_id", "name"}, f"Participant {number}")
        participant_id = entry["participant_id"]
        if not is_participant_id(participant_id):
            raise SeedError(
                f"Participant {number}'s ID is not p_ followed by 16 lowercase "
                "hexadecimal digits."
            )
        if participant_id in seen:
            raise SeedError(f"Participant {number} repeats an earlier ID.")
        seen.add(participant_id)
        try:
            check_name(entry["name"])
        except ValueError as error:
            raise SeedError(f"Participant {number}: {error}") from None
        participants.append(SeedParticipant(participant_id, entry["name"]))

    for key in ("state", "provenance"):
        if not isinstance(data[key], dict):
            raise SeedError(f"The seed's {key} must be a JSON object.")
    return Seed(
        module["id"],
        module["version"],
        state_version,
        tuple(providers),
        tuple(participants),
        data["state"],
        data["provenance"],
    )


def _exact(value, names: set[str], subject: str):
    if not isinstance(value, dict):
        raise SeedError(f"{subject} must be a JSON object.")
    missing, extra = sorted(names - value.keys()), sorted(value.keys() - names)
    if missing or extra:
        problems = [f"missing {', '.join(missing)}"] if missing else []
        problems += [f"unexpected {', '.join(extra)}"] if extra else []
        raise SeedError(f"{subject} has {'; '.join(problems)}.")


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _no_constant(name):
    raise ValueError(f"non-finite number {name}")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"non-finite number {text}")
    return value
