"""Exercise local operator commands through the real CLI, with token files."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tesserae.builtin import DICE_RUNTIME
from tesserae.credentials import read_token_file
from tesserae.registry import HostRegistry
from tesserae.store import GameStore


class HostAdminTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "host.sqlite3"

    def cli(self, *args, ok=True):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tesserae",
                "host",
                *map(str, args),
                "--registry",
                str(self.path),
            ],
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(
            result.returncode, 0 if ok else 1, result.stdout + result.stderr
        )
        return result

    def test_local_bootstrap_registration_and_access_lifecycle(self):
        self.cli("init")
        token_file = self.root / "alice.token"
        self.cli("player", "add", "--name", "Alice", "--token-file", token_file)
        [player] = json.loads(self.cli("player", "list").stdout)
        player_id = player["player_id"]
        token = read_token_file(token_file)
        self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
        games = []
        for name in ("Harbor", "Bridge"):
            game_path = self.root / f"{name}.sqlite3"
            with GameStore.create(game_path, DICE_RUNTIME) as store:
                games.append(store.game_id)
            self.cli("campaign", "add", "--database", game_path, "--name", name)
        self.assertEqual(len(json.loads(self.cli("campaign", "list").stdout)), 2)
        self.cli("campaign", "activate", games[0])
        self.cli("campaign", "grant", games[0], player_id)
        self.assertEqual(
            json.loads(self.cli("campaign", "members", games[0]).stdout)[0][
                "player_id"
            ],
            player_id,
        )
        with HostRegistry(self.path) as registry:
            context = registry.authenticate(token)
            self.assertEqual(
                registry.visible_campaigns(context),
                [{"campaign_id": games[0], "name": "Harbor"}],
            )
        self.cli("campaign", "revoke", games[0], player_id)
        self.cli("campaign", "deactivate", games[0])
        replacement = self.root / "new.token"
        result = self.cli("player", "rotate", player_id, "--token-file", replacement)
        self.assertNotIn(token, result.stdout + result.stderr)
        with HostRegistry(self.path) as registry:
            self.assertIsNone(registry.authenticate(token))
            self.assertIsNotNone(registry.authenticate(read_token_file(replacement)))
        self.cli("player", "revoke", player_id)
        self.assertFalse(json.loads(self.cli("player", "list").stdout)[0]["active"])

    def test_failures_do_not_create_registry_or_destroy_existing_credentials(self):
        token_file = self.root / "alice.token"
        self.cli(
            "player", "add", "--name", "Alice", "--token-file", token_file, ok=False
        )
        self.assertFalse(self.path.exists())
        self.assertFalse(token_file.exists())
        self.cli("init")
        self.cli("player", "add", "--name", "Alice", "--token-file", token_file)
        token = read_token_file(token_file)
        with HostRegistry(self.path) as registry:
            player_id = registry.authenticate(token).player_id
        self.cli("init", ok=False)
        self.cli("player", "rotate", player_id, "--token-file", token_file, ok=False)
        failed = self.root / "failed.token"
        self.cli("player", "rotate", "missing", "--token-file", failed, ok=False)
        self.assertFalse(failed.exists())
        self.cli("player", "add", "--name", " ", "--token-file", failed, ok=False)
        self.assertFalse(failed.exists())
        self.assertEqual(read_token_file(token_file), token)
        with HostRegistry(self.path) as registry:
            self.assertIsNotNone(registry.authenticate(token))
            self.assertEqual(len(registry.players()), 1)
        missing = self.root / "mistyped.sqlite3"
        self.cli(
            "campaign", "add", "--name", "Missing", "--database", missing, ok=False
        )
        self.assertFalse(missing.exists())
