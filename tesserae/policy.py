"""The campaign settings document (docs/designs/governance_contract.md
sections 5 and 6): its shape, its canonical form, its validation against the
game's module and state, and the effective policy it gives each character.

Pure functions: storage and authority are the store's (tesserae.store and
tesserae.campaign_rules). Nothing in a document loads code, changes providers
or installs a module; the binding must equal the game's exactly.
"""

import hashlib
import json
import re

DOCUMENT_VERSION = 1
# Version 2 adds category request policy, only for a module with creation
# overrides (creation overrides contract section 4).
OVERRIDES_DOCUMENT_VERSION = 2
CATEGORY_POLICIES = ("approval_required", "blocked")
MAX_GUIDANCE = 8000
MAX_REASON = 1000
MAX_EXCEPTIONS = 500
MAX_DOCUMENT_BYTES = 256 * 1024
_FIELDS = ("document_version", "module", "settings", "exceptions", "guidance")
_EXCEPTION_FIELDS = ("exception_id", "character_id", "settings", "reason")
_MODULE_FIELDS = ("id", "version", "state_version")
# Client input shown in a problem, and an exception's ID: a lowercase
# identifier, never mixed case, so a pasted token is not echoed.
_SHOWN = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_CHARACTER_ID = re.compile(r"c_[0-9a-f]{16}")
PROPOSAL_ID = re.compile(r"hp-[0-9a-f]{12}")


class DocumentError(ValueError):
    """A document that cannot be stored or activated: player-safe `problems`,
    each naming client input only when it is a plain identifier."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


def canonical(document: dict) -> str:
    """The stored form: sorted keys, no insignificant whitespace."""
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def check_shape(
    document, binding: dict, supported: frozenset, categories: frozenset | None = None
) -> dict:
    """Raise DocumentError unless `document` is a document for exactly this
    `binding` ({"id", "version", "state_version"}) using only `supported`
    setting names; return it unchanged. Version 1 always; version 2, with
    its category policy over `categories`, only for a module with creation
    overrides (`categories` not None). Values are the module's to judge
    (check_against)."""
    if not isinstance(document, dict):
        raise DocumentError(["A settings document is a JSON object."])
    versions = [DOCUMENT_VERSION]
    if categories is not None:
        versions.append(OVERRIDES_DOCUMENT_VERSION)
    version = document.get("document_version")
    if type(version) is not int or version not in versions:
        raise DocumentError([
            "'document_version' must be "
            + " or ".join(str(v) for v in versions)
            + ("." if categories is not None
               else "; version 2 needs a module with creation overrides.")
        ])  # fmt: skip
    fields = _FIELDS if version == DOCUMENT_VERSION else (*_FIELDS, "overrides")
    problems = _fields(document, fields, "The document")
    if problems:
        raise DocumentError(problems)
    if version == OVERRIDES_DOCUMENT_VERSION:
        problems += _overrides(document["overrides"], categories)
    module = document["module"]
    if not isinstance(module, dict) or _fields(module, _MODULE_FIELDS, "module"):
        problems.append('\'module\' is exactly {"id", "version", "state_version"}.')
    elif canonical(module) != canonical(binding):  # as JSON: true is not 1
        problems.append(
            f"This document is for module {_shown(module['id'])} "
            f"{_shown(module['version'])} (state {_shown(module['state_version'])}); "
            f"this game uses {binding['id']} {binding['version']} "
            f"(state {binding['state_version']})."
        )
    problems += _settings(document["settings"], supported, "'settings'", empty=True)
    guidance = document["guidance"]
    if guidance is not None and (
        not isinstance(guidance, str) or len(guidance) > MAX_GUIDANCE
    ):
        problems.append(
            f"'guidance' is null or text of up to {MAX_GUIDANCE} characters."
        )
    exceptions = document["exceptions"]
    if not isinstance(exceptions, list) or len(exceptions) > MAX_EXCEPTIONS:
        problems.append(f"'exceptions' is a list of up to {MAX_EXCEPTIONS}.")
    else:
        problems += _exceptions(exceptions, supported)
    if not problems and len(canonical(document).encode()) > MAX_DOCUMENT_BYTES:
        problems.append(f"A document is at most {MAX_DOCUMENT_BYTES} bytes.")
    if problems:
        raise DocumentError(problems)
    return document


def check_against(document: dict, runtime, state: dict, characters) -> list[str]:
    """Problems with a well-formed document in this game now: each exception
    must name one of `characters` (every character ID of the game), and the
    module must accept the campaign's effective settings and each
    exception's. Empty if it can be activated."""
    problems = []
    if runtime.settings is None:
        return problems  # nothing to judge: the shape allowed only {} and []
    defaults = runtime.settings_defaults()
    campaign = {**defaults, **document["settings"]}
    problems += [
        f"Campaign settings: {p}" for p in runtime.settings_problems(campaign, state)
    ]
    for exception in document["exceptions"]:
        label = f"Exception {exception['exception_id']}"
        if exception["character_id"] not in characters:
            problems.append(f"{label}: there is no such character in this game.")
            continue
        merged = {**campaign, **exception["settings"]}
        problems += [f"{label}: {p}" for p in runtime.settings_problems(merged, state)]
    return problems


def effective(runtime, revision: int, document: dict | None, character_id) -> dict:
    """The effective policy `{"revision", "settings"}` for one character, or
    for the campaign with `character_id` None: the module's defaults, then the
    document's settings, then that character's exception, key by key."""
    settings = runtime.settings_defaults()
    if document is not None:
        settings.update(document["settings"])
        for exception in document["exceptions"]:
            if character_id is not None and exception["character_id"] == character_id:
                settings.update(exception["settings"])
    return {"revision": revision, "settings": settings}


def category_policy(runtime, document: dict | None) -> dict:
    """Every declared override category's request policy, `{category_id:
    "approval_required" | "blocked"}`: a version 2 document's, a category it
    does not list `approval_required`, and with no document or a version 1
    one every category `approval_required`. {} without the capability."""
    listed = {}
    if document is not None and document["document_version"] == 2:
        listed = document["overrides"]["categories"]
    return {
        category["category_id"]: listed.get(
            category["category_id"], "approval_required"
        )
        for category in runtime.override_categories()
    }


def blocked_categories(runtime, policy: dict, intent: dict, preview: dict) -> list:
    """The blocked categories of the qualities a guest's intent names (by the
    preview's `affected`), `[{"category_id", "label"}]` in declared order;
    empty if the request may be stored. Consequential qualities never
    block: the host sees them in the preview."""
    named = {change["quality_id"] for change in intent["changes"]}
    categories = {
        a["category_id"] for a in preview["affected"] if a["quality_id"] in named
    }
    return [
        {"category_id": c["category_id"], "label": c["label"]}
        for c in runtime.override_categories()
        if c["category_id"] in categories and policy[c["category_id"]] == "blocked"
    ]


def own_exception(document: dict | None, character_id: str) -> dict | None:
    """The exception for this character, or None."""
    if document is None:
        return None
    for exception in document["exceptions"]:
        if exception["character_id"] == character_id:
            return exception
    return None


def _exceptions(exceptions: list, supported: frozenset) -> list[str]:
    problems, ids, characters = [], set(), set()
    for index, exception in enumerate(exceptions, 1):
        label = f"Exception {index}"
        if not isinstance(exception, dict):
            problems.append(f"{label} is not an object.")
            continue
        # An approved request's exception names its proposal (section 9).
        shown = {k: v for k, v in exception.items() if k != "proposal_id"}
        missing = _fields(shown, _EXCEPTION_FIELDS, label)
        proposal = exception.get("proposal_id")
        if "proposal_id" in exception and not (
            isinstance(proposal, str) and PROPOSAL_ID.fullmatch(proposal)
        ):
            missing.append(f"{label}: 'proposal_id' is a proposal ID.")
        if missing:
            problems += missing
            continue
        exception_id = exception["exception_id"]
        if not isinstance(exception_id, str) or not _SHOWN.fullmatch(exception_id):
            problems.append(
                f"{label}: 'exception_id' is 1 to 64 lowercase letters, digits, "
                "'_', '.' or '-', starting with a letter or digit."
            )
        elif exception_id in ids:
            problems.append(f"Exception {exception_id} is listed twice.")
        else:
            ids.add(exception_id)
            label = f"Exception {exception_id}"
        character_id = exception["character_id"]
        if not isinstance(character_id, str) or not _CHARACTER_ID.fullmatch(
            character_id
        ):
            problems.append(f"{label}: 'character_id' must be a character ID.")
        elif character_id in characters:
            problems.append(f"{label}: that character already has an exception.")
        else:
            characters.add(character_id)
        problems += _settings(exception["settings"], supported, f"{label}'s settings")
        reason = exception["reason"]
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > MAX_REASON
        ):
            problems.append(
                f"{label}: 'reason' is text of 1 to {MAX_REASON} characters."
            )
    return problems


def _overrides(overrides, categories: frozenset) -> list[str]:
    if not isinstance(overrides, dict) or _fields(overrides, ("categories",), ""):
        return ["'overrides' is exactly {\"categories\": {...}}."]
    listed = overrides["categories"]
    if not isinstance(listed, dict):
        return ["'overrides.categories' must be an object."]
    problems = []
    unknown = sorted(listed.keys() - categories)
    if unknown:
        problems.append(
            f"'overrides.categories': unknown categor{'ies' if len(unknown) > 1 else 'y'} "
            + ", ".join(_shown(key) for key in unknown)
            + f"; this module declares {', '.join(sorted(categories))}."
        )
    if any(value not in CATEGORY_POLICIES for value in listed.values()):
        problems.append(
            "'overrides.categories' maps each category to approval_required or blocked."
        )
    return problems


def _settings(settings, supported: frozenset, label: str, empty=False) -> list[str]:
    if not isinstance(settings, dict):
        return [f"{label} must be an object."]
    if not settings and not empty:
        return [f"{label} must change at least one setting."]
    unknown = sorted(settings.keys() - supported)
    if not unknown:
        return []
    if not supported:
        return [f"{label}: this game's module has no settings."]
    named = [_shown(key) for key in unknown]
    return [
        f"{label}: unknown setting{'s' if len(unknown) > 1 else ''} "
        + ", ".join(named)
        + f"; this module supports {', '.join(sorted(supported))}."
    ]


def _fields(data: dict, fields, subject: str) -> list[str]:
    missing = [name for name in fields if name not in data]
    extra = data.keys() - set(fields)
    problems = []
    if missing:
        problems.append(f"{subject} is missing {', '.join(missing)}.")
    if extra:
        problems.append(
            f"{subject} has {len(extra)} unexpected field{'s' if len(extra) > 1 else ''}."
        )
    return problems


def _shown(value) -> str:
    """Client input in a problem only as a lowercase identifier (never a
    pasted secret or a paragraph)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str) and _SHOWN.fullmatch(value):
        return repr(value)
    return "(not shown)"
