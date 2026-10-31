"""Loading an explicit external module package (tesserae/external.py).

The Beacon example in tests/fixtures/modules/beacon is original; variants are
copies with a rewritten manifest or extra files in a temporary directory.
"""

import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tesserae.builtin import CATALOG
from tesserae.dice import DiceExpressionError
from tesserae.external import load_module_dir
from tesserae.modules import ModuleError

BEACON = Path(__file__).resolve().parent / "fixtures" / "modules" / "beacon"
BUILTIN_DICE = (
    '{ operation = "dice.roll.v1", provider = "builtin.dice", version = "1" }'
)
STEADY_DICE = (
    '{ operation = "dice.roll.v1", provider = "beacon.steady", version = "1" }'
)


def manifest(providers="", entry="rules:bundle", extra=""):
    return (
        f'schema_version = 1\nentry_point = "{entry}"\n{extra}'
        f"providers = [{providers}]\n"
    )


def loaded_packages():
    return {name for name in sys.modules if name.startswith("_tesserae_external_")}


class ExternalCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.count = 0

    def beacon(self, toml=None, **files):
        """A copy of Beacon, with `module.toml` and other files replaced."""
        self.count += 1
        target = self.root / f"beacon{self.count}"
        shutil.copytree(BEACON, target, ignore=shutil.ignore_patterns("__pycache__"))
        if toml is not None:
            files["module.toml"] = toml
        for name, text in files.items():
            (target / name).write_text(textwrap.dedent(text))
        return target

    def package(self, rules, toml=None):
        """A one-file package whose rules.py is `rules`."""
        return self.beacon(
            toml if toml is not None else manifest(), **{"rules.py": rules}
        )

    def assertRefused(self, directory, message):
        before = loaded_packages()
        with self.assertRaisesRegex(ModuleError, message) as caught:
            load_module_dir(directory)
        self.assertEqual(loaded_packages(), before)  # nothing left imported
        return caught.exception


class LoadingTests(ExternalCase):
    def test_package_loads_with_relative_helper_and_data(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        before = (os.getcwd(), list(sys.path))
        os.chdir(elsewhere)  # package data must not depend on the working directory
        self.addCleanup(os.chdir, before[0])
        runtime = load_module_dir(BEACON)
        os.chdir(before[0])

        self.assertEqual((os.getcwd(), sys.path), before)
        self.assertEqual(
            (runtime.module.id, runtime.module.version, runtime.selection),
            ("beacon", "1", CATALOG.resolve("dice").selection),
        )
        outcome = runtime.execute(
            runtime.initial_state(), "p1", "beacon.signal", {},
            randint=lambda low, high: 2,
        )  # fmt: skip
        self.assertEqual(outcome.result["colour"], "green")  # signals.json, entry 2
        self.assertEqual(
            runtime.view(outcome.state, "p1"), {"last": "green", "yours": 1}
        )
        self.assertEqual(
            runtime.view(outcome.state, "p2"), {"last": "green", "yours": 0}
        )

    def test_manifest_selects_an_external_provider(self):
        low = {"randint": lambda low, high: low}
        bundled = load_module_dir(self.beacon(manifest(BUILTIN_DICE)))
        steady = load_module_dir(self.beacon(manifest(STEADY_DICE)))
        self.assertEqual(steady.selection[0].provider, "beacon.steady")
        self.assertEqual(
            [
                runtime.execute(
                    runtime.initial_state(), "p1", "beacon.signal", {}, **low
                ).result["colour"]
                for runtime in (bundled, steady)
            ],
            ["red", "white"],  # the same draws; the selected provider decides
        )

    def test_each_load_is_separate_and_cannot_shadow_installed_modules(self):
        import tesserae.dice

        # A package file named like an installed module stays inside the
        # package: the package's own `from tesserae.dice import` and the
        # host's import both get the real one.
        directory = self.beacon(**{"tesserae.py": "raise RuntimeError('shadowed')\n"})
        first, second = load_module_dir(directory), load_module_dir(directory)
        self.assertIs(sys.modules["tesserae.dice"], tesserae.dice)
        self.assertIsNot(first.module, second.module)

    def test_a_module_without_requirements_has_no_dice(self):
        rules = """\
            from tesserae.external import Bundle
            from tesserae.modules import Module

            QUIET = Module(
                id="quiet", version="1", state_version=1, commands=(),
                initial_state=dict, view=lambda state, participant_id: None,
                tags=frozenset({"dice"}),
            )


            def bundle():
                return Bundle(QUIET)
            """
        runtime = load_module_dir(self.package(rules))
        self.assertEqual((dict(runtime.providers), runtime.command_names), ({}, ()))
        self.assertRefused(
            self.package(rules, manifest(BUILTIN_DICE)), "does not use 'dice.roll.v1'"
        )


class SelectionTests(ExternalCase):
    def test_manifest_alone_selects_providers(self):
        other_version = BUILTIN_DICE.replace('version = "1"', 'version = "2"')
        cases = [
            # Beacon's Python defaults are complete; they never fill a gap.
            ("partial manifest", "", "no provider selected for 'dice.roll.v1'"),
            ("two choices", f"{BUILTIN_DICE}, {STEADY_DICE}", "More than one provider"),
            ("unknown provider", BUILTIN_DICE.replace("builtin.dice", "x"), "No provider 'x'"),
            ("other version", other_version, "is version 1, not 2"),
            (
                "unused operation",
                f'{BUILTIN_DICE}, {{ operation = "cards.draw.v1", provider = "beacon.steady", version = "1" }}',
                "does not use 'cards.draw.v1'",
            ),
        ]  # fmt: skip
        for name, providers, message in cases:
            with self.subTest(name):
                self.assertRefused(self.beacon(manifest(providers)), message)

    def test_ids_never_replace_bundled_ones(self):
        cases = {
            "module id": ('id="beacon"', 'id="relay"', "Duplicate module 'relay'"),
            "provider id": (
                'Provider("beacon.steady"', 'Provider("builtin.dice"',
                "Duplicate provider 'builtin.dice'",
            ),
        }  # fmt: skip
        rules = (BEACON / "rules.py").read_text()
        for name, (old, new, message) in cases.items():
            with self.subTest(name):
                self.assertIn(old, rules)
                directory = self.beacon(**{"rules.py": rules.replace(old, new)})
                self.assertRefused(directory, message)

    def test_an_unsupported_host_api_is_refused(self):
        rules = (BEACON / "rules.py").read_text()
        rules = rules.replace("    providers=(", "    api_version=2,\n    providers=(")
        self.assertRefused(self.beacon(**{"rules.py": rules}), "needs host API 2")


class ManifestTests(ExternalCase):
    def test_bad_manifests_are_refused(self):
        cases = [
            ("not TOML", "schema_version = ", "Cannot read"),
            ("schema 2", manifest().replace("= 1", "= 2"), "manifest schema 2"),
            ("schema true", manifest().replace("= 1", "= true"), "manifest schema True"),
            ("schema 1.0", manifest().replace("= 1", "= 1.0"), "manifest schema 1.0"),
            ("missing", 'schema_version = 1\nentry_point = "rules:bundle"\n', "missing providers"),
            ("extra", manifest(extra='name = "beacon"\n'), "unexpected name"),
            ("providers table", manifest().replace("providers = []", "[providers]"), "array of tables"),
            ("choice fields", manifest('{ operation = "dice.roll.v1", provider = "builtin.dice" }'), "choice 1 needs"),
            ("choice type", manifest(BUILTIN_DICE.replace('"1"', "1")), "choice 1 needs"),
        ]  # fmt: skip
        for entry in (
            "rules",
            "rules:",
            ":bundle",
            "../rules:bundle",
            ".rules:bundle",
            "rules:bundle()",
        ):
            cases.append(
                (f"entry {entry}", manifest(BUILTIN_DICE, entry), "module:function")
            )
        for name, toml, message in cases:
            with self.subTest(name):
                self.assertRefused(self.beacon(toml), message)

    def test_a_manifest_that_is_not_utf8_is_refused(self):
        directory = self.beacon()
        (directory / "module.toml").write_bytes(
            b'schema_version = 1\nentry_point = "\xff"\n'
        )
        self.assertRefused(directory, "Cannot read")

    def test_missing_manifest_or_package(self):
        directory = self.beacon()
        (directory / "module.toml").unlink()
        self.assertRefused(directory, "No module.toml")
        (directory / "__init__.py").unlink()
        self.assertRefused(directory, "not a Python package")
        self.assertRefused(self.root / "absent", "not a Python package")


class EntryPointTests(ExternalCase):
    def test_bad_entry_points_are_refused(self):
        cases = [
            ("no module", manifest(BUILTIN_DICE, "missing:bundle"), {}, "ModuleNotFoundError"),
            ("no function", manifest(BUILTIN_DICE, "rules:absent"), {}, "rules:absent .* is not a function"),
            ("not callable", manifest(BUILTIN_DICE, "rules:DICE"), {}, "rules:DICE .* is not a function"),
        ]  # fmt: skip
        for name, toml, files, message in cases:
            with self.subTest(name):
                self.assertRefused(self.beacon(toml, **files), message)

    def test_failing_package_code_keeps_its_cause(self):
        rules = (BEACON / "rules.py").read_text() + "\nroll_expression('1d0')\n"
        error = self.assertRefused(self.beacon(**{"rules.py": rules}), "failed to load")
        self.assertIsInstance(error.__cause__, DiceExpressionError)

    def test_entry_points_must_return_a_bundle(self):
        rules = (BEACON / "rules.py").read_text()
        cases = {
            "not a bundle": ("return Bundle(module=BEACON, providers=(STEADY,))", "return BEACON", "returned Module, not a tesserae.external.Bundle"),
            "no module": ("Bundle(module=BEACON,", "Bundle(module=STEADY,", "has no Module"),
            "bad provider": ("providers=(STEADY,))", "providers=(STEADY, DICE))", "has a non-Provider"),
            "bad commands": ("commands=(Command(", "commands=(DICE, Command(", "failed to load: AttributeError"),
        }  # fmt: skip
        for name, (old, new, message) in cases.items():
            with self.subTest(name):
                self.assertIn(old, rules)
                directory = self.beacon(**{"rules.py": rules.replace(old, new)})
                self.assertRefused(directory, message)


if __name__ == "__main__":
    unittest.main()
