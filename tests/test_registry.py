"""Host identity, access boundaries and read-only campaign registration."""

import shutil
import sqlite3
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from tesserae.builtin import DICE_RUNTIME
from tesserae.credentials import new_token
from tesserae.registry import (
    _SCHEMA,
    CampaignUnavailable,
    CredentialRevoked,
    HostRefused,
    HostRegistry,
    RegistryError,
)
from tesserae.registry import APPLICATION_ID as REGISTRY_ID
from tesserae.store import (
    APPLICATION_ID,
    GameStore,
    ParticipantContext,
    StoreError,
    inspect_game_id,
)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "host.sqlite3"
        self.registry = self.enterContext(HostRegistry.create(self.path))
        self.token = new_token()
        self.player = self.registry.add_player("Alice", self.token)
        self.context = self.registry.authenticate(self.token)
        self.game = self.root / "game.sqlite3"
        with GameStore.create(self.game, DICE_RUNTIME) as store:
            self.game_id = store.game_id
        self.campaign = self.registry.register_campaign(self.game, "Harbor")

    def test_persistent_identity_and_digest_only_credentials(self):
        self.assertRegex(self.player, r"^u_[0-9a-f]{16}$")
        self.assertEqual(self.campaign, self.game_id)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.token.encode(), self.path.read_bytes())
        with HostRegistry(self.path) as reopened:
            self.assertEqual(reopened.host_id, self.registry.host_id)
            self.assertEqual(reopened.authenticate(self.token), self.context)
            self.assertEqual(reopened.players()[0]["name"], "Alice")
            self.assertEqual(reopened.campaigns()[0]["path"], str(self.game.resolve()))

    def test_catalog_requires_both_active_campaign_and_explicit_grant(self):
        bob = self.registry.add_player("Bob", new_token())
        self.assertEqual(self.registry.visible_campaigns(self.context), [])
        self.registry.grant(self.campaign, self.player)
        self.assertEqual(self.registry.visible_campaigns(self.context), [])
        self.registry.set_campaign_active(self.campaign, True)
        self.assertEqual(
            self.registry.visible_campaigns(self.context),
            [{"campaign_id": self.campaign, "name": "Harbor"}],
        )
        self.registry.revoke_access(self.campaign, self.player)
        self.assertEqual(self.registry.visible_campaigns(self.context), [])
        self.registry.grant(self.campaign, self.player)
        self.registry.grant(self.campaign, self.player)
        self.assertEqual(len(self.registry.members(self.campaign)), 1)
        with self.registry.authorize_campaign(self.context, self.campaign) as record:
            self.assertEqual(record["path"], str(self.game.resolve()))
        self.assertEqual(
            self.registry.members(self.campaign)[0]["player_id"], self.player
        )
        self.assertNotEqual(bob, self.player)
        self.registry.set_campaign_active(self.campaign, False)
        with self.assertRaises(RegistryError):
            with self.registry.authorize_campaign(self.context, self.campaign):
                self.fail("Inactive campaign admitted")

    def test_two_players_have_distinct_catalogs_without_path_disclosure(self):
        token = new_token()
        bob = self.registry.add_player("Alice", token)  # same label is not identity
        bob_context = self.registry.authenticate(token)
        path = self.root / "bridge.sqlite3"
        with GameStore.create(path, DICE_RUNTIME):
            pass
        bridge = self.registry.register_campaign(path, "Bridge")
        for campaign in (self.campaign, bridge):
            self.registry.set_campaign_active(campaign, True)
        self.registry.grant(self.campaign, self.player)
        self.registry.grant(bridge, bob)
        self.assertEqual(
            self.registry.visible_campaigns(self.context),
            [{"campaign_id": self.campaign, "name": "Harbor"}],
        )
        self.assertEqual(
            self.registry.visible_campaigns(bob_context),
            [{"campaign_id": bridge, "name": "Bridge"}],
        )
        for campaign in (self.campaign, "missing"):
            with self.assertRaises(RegistryError):
                with self.registry.authorize_campaign(bob_context, campaign):
                    self.fail("Inaccessible campaign admitted")
        self.registry.grant(bridge, self.player)
        self.assertEqual(len(self.registry.visible_campaigns(self.context)), 2)

    def test_rotation_and_revocation_preserve_player_and_grants(self):
        self.registry.grant(self.campaign, self.player)
        self.registry.set_campaign_active(self.campaign, True)
        token = new_token()
        self.registry.rotate_credential(self.player, token)
        self.assertIsNone(self.registry.authenticate(self.token))
        self.assertFalse(self.registry.credential_active(self.context))
        with self.assertRaises(RegistryError):
            self.registry.visible_campaigns(self.context)
        context = self.registry.authenticate(token)
        self.assertEqual(context.player_id, self.player)
        self.assertNotEqual(context.credential_id, self.context.credential_id)
        self.assertEqual(len(self.registry.visible_campaigns(context)), 1)
        self.assertTrue(self.registry.revoke_credential(self.player))
        self.assertFalse(self.registry.revoke_credential(self.player))
        self.assertFalse(self.registry.credential_active(context))
        self.registry.rotate_credential(self.player, new_token())
        self.assertEqual(len(self.registry.players()), 1)
        self.assertEqual(len(self.registry.members(self.campaign)), 1)

    def test_unauthorized_contexts_and_malformed_tokens(self):
        self.registry.grant(self.campaign, self.player)
        self.registry.set_campaign_active(self.campaign, True)
        for token in (None, 0, "", "x ", "x" * 513, "\ud800", new_token()):
            self.assertIsNone(self.registry.authenticate(token))
        for context in (
            None,
            ParticipantContext(self.player),
            replace(self.context, host_id=str(uuid.uuid4())),
            replace(self.context, credential_id=self.context.credential_id + 1),
            replace(self.context, player_id="u_0000000000000000"),
        ):
            with self.subTest(context=context), self.assertRaises(RegistryError):
                self.registry.visible_campaigns(context)
        with HostRegistry.create(self.root / "other.sqlite3") as other:
            self.assertFalse(other.credential_active(self.context))

    def test_failed_credentials_are_atomic(self):
        before = self.registry.players()
        with self.assertRaises(sqlite3.IntegrityError):
            self.registry.add_player("Duplicate credential", self.token)
        self.assertEqual(self.registry.players(), before)
        bob_token = new_token()
        bob = self.registry.add_player("Bob", bob_token)
        with self.assertRaises(sqlite3.IntegrityError):
            self.registry.rotate_credential(bob, self.token)
        self.assertEqual(self.registry.authenticate(bob_token).player_id, bob)
        for name in ("", " ", "x\n", "a" * 101):
            with self.assertRaises(ValueError):
                self.registry.add_player(name, new_token())
        with self.assertRaises(RegistryError):
            self.registry.rotate_credential("missing", new_token())
        with self.assertRaises(RegistryError):
            self.registry.grant(self.campaign, "missing")
        with self.assertRaises(RegistryError):
            self.registry.set_campaign_active("missing", True)
        self.assertEqual(self.registry.members(self.campaign), [])

    def test_canonical_paths_and_copied_identities_cannot_duplicate_campaigns(self):
        alias = self.root / "alias.sqlite3"
        alias.symlink_to(self.game)
        clone = self.root / "copy.sqlite3"
        shutil.copyfile(self.game, clone)
        for path in (self.game, alias, clone):
            with self.subTest(path=path), self.assertRaises(RegistryError):
                self.registry.register_campaign(path, "Duplicate")
        self.assertEqual(len(self.registry.campaigns()), 1)

    def test_open_refuses_missing_unrelated_damaged_future_and_wal_registries(self):
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(RegistryError):
            HostRegistry(missing)
        self.assertFalse(missing.exists())
        for kind in ("unrelated", "future", "damaged", "wal"):
            path = self.root / f"{kind}.sqlite3"
            shutil.copyfile(self.path, path)
            with sqlite3.connect(path) as connection:
                if kind == "unrelated":
                    connection.execute("PRAGMA application_id = 0")
                elif kind == "future":
                    connection.execute("PRAGMA user_version = 99")
                elif kind == "damaged":
                    connection.execute("DROP TABLE memberships")
                else:
                    connection.execute("PRAGMA journal_mode = WAL")
            before = path.read_bytes()
            with self.subTest(kind=kind), self.assertRaises(RegistryError):
                HostRegistry(path)
            self.assertEqual(path.read_bytes(), before)
        before = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            HostRegistry.create(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(RegistryError):
            HostRegistry(self.game)

    def test_registration_does_not_migrate_old_game_or_load_module(self):
        legacy = self.root / "old.sqlite3"
        legacy_id = str(uuid.uuid4())
        with sqlite3.connect(legacy) as connection:
            connection.executescript(f"""
                PRAGMA application_id = {APPLICATION_ID};
                PRAGMA user_version = 1;
                CREATE TABLE game (singleton INTEGER, game_id TEXT, revision INTEGER);
                INSERT INTO game VALUES (1, '{legacy_id}', 0);
                CREATE TABLE commands (id INTEGER, revision INTEGER, participant_id TEXT,
                    request_id TEXT, content TEXT, result TEXT);
            """)
        before = legacy.read_bytes()
        self.assertEqual(self.registry.register_campaign(legacy, "Old"), legacy_id)
        self.assertEqual(legacy.read_bytes(), before)
        with sqlite3.connect(self.game) as connection:
            connection.execute("UPDATE module SET module_id = 'not-installed'")
        before = self.game.read_bytes()
        self.assertEqual(inspect_game_id(self.game), self.campaign)
        self.assertEqual(self.game.read_bytes(), before)

    def test_inspection_reads_committed_wal_and_refuses_bad_games(self):
        with GameStore.open_existing(self.game) as store:
            changed_id = str(uuid.uuid4())
            store.query("UPDATE game SET game_id = ?", (changed_id,))
            self.assertEqual(inspect_game_id(self.game), changed_id)
        absent = self.root / "absent.sqlite3"
        with self.assertRaises(FileNotFoundError):
            self.registry.register_campaign(absent, "Missing")
        self.assertFalse(absent.exists())
        empty = self.root / "empty.sqlite3"
        empty.touch()
        with self.assertRaises(StoreError):
            self.registry.register_campaign(empty, "Empty")
        self.assertEqual(empty.read_bytes(), b"")
        with sqlite3.connect(self.game) as connection:
            connection.execute("UPDATE game SET game_id = 'invalid'")
        with self.assertRaises(StoreError):
            inspect_game_id(self.game)

    def test_authorization_guard_orders_revocation_commit_after_protected_operation(
        self,
    ):
        self.registry.grant(self.campaign, self.player)
        self.registry.set_campaign_active(self.campaign, True)
        # A real second SQLite connection attempts COMMIT while the reader is
        # inside its protected operation. Test all three access revocation paths.
        for operation in ("credential", "membership", "campaign"):
            with self.subTest(operation=operation):
                started, commit_attempted, done = (threading.Event() for _ in range(3))
                errors = []

                def revoke():
                    try:
                        with HostRegistry(self.path) as registry:
                            registry._connection.set_trace_callback(
                                lambda sql: (
                                    commit_attempted.set() if sql == "COMMIT" else None
                                )
                            )
                            started.set()
                            if operation == "credential":
                                registry.revoke_credential(self.player)
                            elif operation == "membership":
                                registry.revoke_access(self.campaign, self.player)
                            else:
                                registry.set_campaign_active(self.campaign, False)
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        done.set()

                with self.registry.authorize_campaign(self.context, self.campaign):
                    thread = threading.Thread(target=revoke)
                    thread.start()
                    self.assertTrue(started.wait(2))
                    self.assertTrue(commit_attempted.wait(2))
                    self.assertFalse(
                        done.wait(0.05), "Revocation committed during guard"
                    )
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(done.is_set())
                with self.assertRaises(RegistryError):
                    with self.registry.authorize_campaign(self.context, self.campaign):
                        self.fail("Revoked access admitted")
                token = new_token()
                self.registry.rotate_credential(self.player, token)
                self.context = self.registry.authenticate(token)
                self.registry.grant(self.campaign, self.player)
                self.registry.set_campaign_active(self.campaign, True)

    def test_failed_protected_operation_releases_guard(self):
        self.registry.grant(self.campaign, self.player)
        self.registry.set_campaign_active(self.campaign, True)
        with self.assertRaisesRegex(RuntimeError, "operation failed"):
            with self.registry.authorize_campaign(self.context, self.campaign):
                raise RuntimeError("operation failed")
        self.assertTrue(self.registry.revoke_access(self.campaign, self.player))


class CampaignHostTests(unittest.TestCase):
    """The campaign host designation (governance contract section 3)."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "host.sqlite3"
        self.registry = self.enterContext(HostRegistry.create(self.path))
        self.tokens = {name: new_token() for name in ("hana", "gil", "dora")}
        self.ids = {
            name: self.registry.add_player(name.title(), token)
            for name, token in self.tokens.items()
        }
        self.game = self.root / "game.sqlite3"
        with GameStore.create(self.game, DICE_RUNTIME):
            pass
        self.campaign = self.registry.register_campaign(
            self.game, "Harbor", self.ids["hana"]
        )
        self.registry.grant(self.campaign, self.ids["gil"])
        self.registry.set_campaign_active(self.campaign, True)

    def context(self, name):
        return self.registry.authenticate(self.tokens[name])

    def host(self):
        return self.registry.campaign(self.campaign)["host_player_id"]

    def transfer(self, sender, to, request_id="t-1"):
        return self.registry.transfer_host(
            self.context(sender), self.campaign, self.ids.get(to, to), request_id
        )

    def test_schema_1_registry_is_upgraded_in_place_and_kept(self):
        old = self.root / "old.sqlite3"
        with sqlite3.connect(old) as connection:
            connection.executescript(_SCHEMA)
            connection.execute("INSERT INTO host VALUES (1, ?)", (str(uuid.uuid4()),))
            connection.execute(
                "INSERT INTO players VALUES ('u_0123456789abcdef', 'Ann', 'then')"
            )
            connection.execute(
                "INSERT INTO campaigns VALUES (?, '/old/path', 'Old', 1, 'then')",
                (str(uuid.uuid4()),),
            )
            connection.execute(f"PRAGMA application_id = {REGISTRY_ID}")
            connection.execute("PRAGMA user_version = 1")
        connection.close()
        damaged = self.root / "damaged.sqlite3"
        shutil.copyfile(old, damaged)
        with sqlite3.connect(damaged) as connection:
            connection.execute("DROP TABLE memberships")
        connection.close()
        before = damaged.read_bytes()
        with self.assertRaises(RegistryError):
            HostRegistry(damaged)
        self.assertEqual(damaged.read_bytes(), before)  # never half-upgraded
        with HostRegistry(old) as upgraded:
            [campaign] = upgraded.campaigns()
            self.assertEqual(
                (campaign["name"], campaign["host_player_id"]), ("Old", None)
            )
            self.assertEqual(upgraded.players()[0]["name"], "Ann")
            self.assertEqual(upgraded.host_changes(campaign["campaign_id"]), [])
        with sqlite3.connect(old) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone(), (2,))
        connection.close()
        HostRegistry(old).close()  # opening it again changes nothing

    def test_registration_with_a_host_grants_and_designates_it_at_once(self):
        self.assertEqual(self.host(), self.ids["hana"])
        [change] = self.registry.host_changes(self.campaign)
        self.assertEqual(
            (change["from_player_id"], change["to_player_id"], change["by"]),
            (None, self.ids["hana"], "operator"),
        )
        members = {
            m["player_id"]: m["host"] for m in self.registry.members(self.campaign)
        }
        self.assertEqual(members, {self.ids["hana"]: 1, self.ids["gil"]: 0})
        other = self.root / "other.sqlite3"
        with GameStore.create(other, DICE_RUNTIME):
            pass
        with self.assertRaises(RegistryError):
            self.registry.register_campaign(other, "Other", "u_0000000000000000")
        self.assertEqual(len(self.registry.campaigns()), 1)  # nothing registered
        legacy = self.registry.register_campaign(other, "Other")
        self.assertIsNone(self.registry.campaign(legacy)["host_player_id"])

    def test_the_host_grant_stays_until_another_player_is_host(self):
        with self.assertRaisesRegex(RegistryError, "transfer the campaign host first"):
            self.registry.revoke_access(self.campaign, self.ids["hana"])
        # A revoked or rotated credential keeps the designation; nobody is
        # promoted in its place.
        self.registry.revoke_credential(self.ids["hana"])
        self.assertEqual(self.host(), self.ids["hana"])
        self.tokens["hana"] = new_token()
        self.registry.rotate_credential(self.ids["hana"], self.tokens["hana"])
        self.assertEqual(self.host(), self.ids["hana"])
        context = self.context("hana")
        with self.registry.authorize_campaign(context, self.campaign) as campaign:
            self.assertEqual(campaign["host_player_id"], self.ids["hana"])
        self.registry.set_campaign_active(self.campaign, False)  # allowed
        self.assertEqual(self.host(), self.ids["hana"])

    def test_operator_adopts_and_recovers_with_set_host(self):
        other = self.root / "legacy.sqlite3"
        with GameStore.create(other, DICE_RUNTIME):
            pass
        legacy = self.registry.register_campaign(other, "Legacy")
        self.assertTrue(self.registry.set_host(legacy, self.ids["dora"]))  # adopted
        self.assertEqual(
            self.registry.campaign(legacy)["host_player_id"], self.ids["dora"]
        )
        self.assertEqual(
            [m["player_id"] for m in self.registry.members(legacy)], [self.ids["dora"]]
        )
        self.registry.revoke_credential(self.ids["hana"])  # a lost host token
        self.assertTrue(self.registry.set_host(self.campaign, self.ids["gil"]))
        self.assertFalse(self.registry.set_host(self.campaign, self.ids["gil"]))
        self.assertTrue(self.registry.revoke_access(self.campaign, self.ids["hana"]))
        with self.assertRaises(RegistryError):
            self.registry.set_host(self.campaign, "u_0000000000000000")
        self.assertEqual(
            [(c["from_player_id"], c["to_player_id"], c["by"])
             for c in self.registry.host_changes(self.campaign)],
            [(None, self.ids["hana"], "operator"),
             (self.ids["hana"], self.ids["gil"], "operator")],
        )  # fmt: skip

    def test_transfer_replays_exactly_and_refuses_everything_else(self):
        for sender, to, code in (
            ("gil", "hana", "not_campaign_host"),  # a guest
            ("hana", "hana", "invalid_request"),  # already the host
            ("hana", "dora", "invalid_request"),  # no grant
            ("hana", "u_0000000000000000", "invalid_request"),  # no such player
        ):
            with self.subTest(sender=sender, to=to):
                with self.assertRaises(HostRefused) as refused:
                    self.transfer(sender, to)
                self.assertEqual(refused.exception.code, code)
        self.registry.grant(self.campaign, self.ids["dora"])
        self.registry.revoke_credential(self.ids["dora"])
        with self.assertRaises(HostRefused):  # no active credential
            self.transfer("hana", "dora")
        self.assertEqual(self.host(), self.ids["hana"])
        self.assertEqual(len(self.registry.host_changes(self.campaign)), 1)

        self.assertTrue(self.transfer("hana", "gil"))
        self.assertEqual(self.host(), self.ids["gil"])
        # The former host's exact retry is its recorded reply, even as a guest.
        self.assertFalse(self.transfer("hana", "gil"))
        with self.assertRaises(HostRefused) as conflict:
            self.transfer("hana", "dora")
        self.assertEqual(conflict.exception.code, "request_conflict")
        with self.assertRaises(HostRefused) as guest:
            self.transfer("hana", "gil", "t-2")
        self.assertEqual(guest.exception.code, "not_campaign_host")
        self.assertTrue(self.transfer("gil", "hana"))  # its own namespace
        self.assertEqual(
            [(c["by"], c["request_id"])
             for c in self.registry.host_changes(self.campaign)],
            [("operator", None), (self.ids["hana"], "t-1"), (self.ids["gil"], "t-1")],
        )  # fmt: skip

    def test_transfer_needs_the_senders_access(self):
        context = self.context("hana")
        self.registry.set_campaign_active(self.campaign, False)
        with self.assertRaises(CampaignUnavailable):
            self.registry.transfer_host(context, self.campaign, self.ids["gil"], "t")
        self.registry.set_campaign_active(self.campaign, True)
        self.registry.revoke_credential(self.ids["hana"])
        with self.assertRaises(CredentialRevoked):
            self.registry.transfer_host(context, self.campaign, self.ids["gil"], "t")
        self.assertEqual(self.host(), self.ids["hana"])

    def test_guard_orders_a_transfer_wholly_after_the_protected_operation(self):
        started, done = threading.Event(), threading.Event()
        errors = []

        def transfer():
            try:
                with HostRegistry(self.path) as registry:
                    context = registry.authenticate(self.tokens["hana"])
                    started.set()
                    registry.transfer_host(
                        context, self.campaign, self.ids["gil"], "t-1"
                    )
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()

        context = self.context("hana")
        with self.registry.authorize_campaign(context, self.campaign) as campaign:
            thread = threading.Thread(target=transfer)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(done.wait(0.2), "A transfer committed during the guard")
            self.assertEqual(campaign["host_player_id"], self.ids["hana"])
        thread.join(6)
        self.assertEqual(errors, [])
        with self.registry.authorize_campaign(context, self.campaign) as campaign:
            self.assertEqual(campaign["host_player_id"], self.ids["gil"])
