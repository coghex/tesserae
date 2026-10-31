"""Load one trusted module package from an explicit local directory.

The directory is a Python package (it has `__init__.py`) with a
`module.toml` manifest. Its entry point returns a `Bundle`: one `Module` and
any `Provider`s it adds. The manifest alone selects the providers. This runs
the package's code as the host user; it is not a sandbox. Nothing is
discovered, scanned, installed or reloaded. See docs/modules.md.
"""

import importlib
import importlib.util
import itertools
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .builtin import CATALOG
from .modules import Catalog, Module, ModuleError, Provider, ProviderChoice, Runtime

MANIFEST = "module.toml"
MANIFEST_SCHEMA_VERSION = 1

# Each load gets its own top-level package name, so the package never shadows
# or is shadowed by an installed package, and two loads never share modules.
_PACKAGE_PREFIX = "_tesserae_external_"
_load_numbers = itertools.count(1)
_DOTTED = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Bundle:
    """What an external package's entry point returns.

    `module` is the package's one module. `providers` are the providers it
    adds; selecting them, or bundled ones, is the manifest's job. The
    module's own `providers` field is ignored for an external package.
    """

    module: Module
    providers: tuple[Provider, ...] = ()


@dataclass(frozen=True)
class Manifest:
    """A parsed `module.toml`."""

    entry_module: str  # dotted, relative to the package
    entry_function: str
    providers: tuple[ProviderChoice, ...]


def read_manifest(directory) -> Manifest:
    """Parse and check `module.toml` in `directory`; raise ModuleError."""
    path = Path(directory) / MANIFEST
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError:
        raise ModuleError(f"No {MANIFEST} in {directory}.") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ModuleError(f"Cannot read {path}: {error}") from None

    expected = {"schema_version", "entry_point", "providers"}
    missing = sorted(expected - data.keys())
    extra = sorted(data.keys() - expected)
    if missing or extra:
        problems = [f"missing {', '.join(missing)}"] if missing else []
        problems += [f"unexpected {', '.join(extra)}"] if extra else []
        raise ModuleError(f"{path} has {'; '.join(problems)}.")
    version = data["schema_version"]
    if type(version) is not int or version != MANIFEST_SCHEMA_VERSION:
        raise ModuleError(
            f"{path} is manifest schema {version!r}; "
            f"this host reads schema {MANIFEST_SCHEMA_VERSION}."
        )

    entry = data["entry_point"]
    module_name, _, function = (
        entry.partition(":") if isinstance(entry, str) else ("", "", "")
    )
    if not _DOTTED.fullmatch(module_name) or not _NAME.fullmatch(function):
        raise ModuleError(
            f"{path}: entry_point must be 'module:function', relative to the package."
        )

    choices = data["providers"]
    if not isinstance(choices, list):
        raise ModuleError(f"{path}: providers must be an array of tables.")
    selection = []
    for number, choice in enumerate(choices, 1):
        fields = ("operation", "provider", "version")
        if (
            not isinstance(choice, dict)
            or set(choice) != set(fields)
            or not all(isinstance(choice[name], str) for name in fields)
        ):
            raise ModuleError(
                f"{path}: provider choice {number} needs exactly the strings "
                "operation, provider and version."
            )
        selection.append(ProviderChoice(*(choice[name] for name in fields)))
    return Manifest(module_name, function, tuple(selection))


def load_module_dir(directory, *, catalog: Catalog = CATALOG) -> Runtime:
    """Load the package in `directory` and resolve its module.

    The module and its providers join `catalog` (the bundled one by default);
    an ID already there is refused, never replaced. The manifest's choices
    must cover every operation the module requires, and nothing else. Raises
    ModuleError, with the package's own exception as the cause if its code
    fails; nothing is left imported after a failure.
    """
    directory = Path(directory).resolve()
    if not (directory / "__init__.py").is_file():
        raise ModuleError(f"{directory} is not a Python package (no __init__.py).")
    manifest = read_manifest(directory)
    package = f"{_PACKAGE_PREFIX}{next(_load_numbers)}"
    try:
        bundle = _call_entry_point(directory, manifest, package)
        if not isinstance(bundle, Bundle):
            raise ModuleError(
                f"The entry point in {directory} returned {type(bundle).__name__}, "
                "not a tesserae.external.Bundle."
            )
        if not isinstance(bundle.module, Module):
            raise ModuleError(f"The bundle from {directory} has no Module.")
        providers = tuple(bundle.providers)
        if not all(isinstance(provider, Provider) for provider in providers):
            raise ModuleError(f"The bundle from {directory} has a non-Provider.")
        combined = catalog.extended([bundle.module], providers)
        return combined.resolve(bundle.module.id, providers=manifest.providers)
    except ModuleError:
        _forget(package)
        raise
    except Exception as error:  # the package's code, or a malformed definition
        _forget(package)
        raise ModuleError(
            f"The module package in {directory} failed to load: "
            f"{type(error).__name__}: {error}"
        ) from error
    except BaseException:
        _forget(package)
        raise


def _call_entry_point(directory: Path, manifest: Manifest, package: str):
    """Import the package as `package` and call its entry point."""
    spec = importlib.util.spec_from_file_location(
        package, directory / "__init__.py", submodule_search_locations=[str(directory)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module  # needed for the package's relative imports
    spec.loader.exec_module(module)
    entry = importlib.import_module(f"{package}.{manifest.entry_module}")
    function = getattr(entry, manifest.entry_function, None)
    if not callable(function):
        raise ModuleError(
            f"{manifest.entry_module}:{manifest.entry_function} in {directory} "
            "is not a function."
        )
    return function()


def _forget(package: str):
    for name in [n for n in sys.modules if n == package or n.startswith(package + ".")]:
        del sys.modules[name]
