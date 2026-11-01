"""Local operator bootstrap for a host registry, with no network side effects."""

import json
import sqlite3
import sys

from .credentials import IssuedToken
from .registry import HostRegistry, RegistryError
from .store import StoreError


def add_parser(commands):
    host = commands.add_parser(
        "host", help="Manage host players and registered campaigns locally."
    )
    groups = host.add_subparsers(dest="host_group", required=True)
    init = groups.add_parser("init", help="Create a new, empty host registry.")
    init.add_argument("--registry", required=True)
    player = groups.add_parser(
        "player", help="Manage stable host players and their tokens."
    )
    actions = player.add_subparsers(dest="host_action", required=True)
    for action in ("add", "list", "rotate", "revoke"):
        parser = actions.add_parser(action)
        parser.add_argument("--registry", required=True)
        if action == "add":
            parser.add_argument("--name", required=True)
        if action in ("rotate", "revoke"):
            parser.add_argument("player_id")
        if action in ("add", "rotate"):
            parser.add_argument("--token-file", required=True)
    campaign = groups.add_parser(
        "campaign", help="Manage campaign locations and player grants."
    )
    actions = campaign.add_subparsers(dest="host_action", required=True)
    for action in (
        "add",
        "list",
        "activate",
        "deactivate",
        "grant",
        "revoke",
        "members",
    ):
        parser = actions.add_parser(action)
        parser.add_argument("--registry", required=True)
        if action == "add":
            parser.add_argument("--database", required=True)
            parser.add_argument("--name", required=True)
        elif action != "list":
            parser.add_argument("campaign_id")
        if action in ("grant", "revoke"):
            parser.add_argument("player_id")


def run(args):
    try:
        if args.host_group == "init":
            with HostRegistry.create(args.registry) as registry:
                print(f"Created host {registry.host_id} at {registry.path}.")
            return 0
        with HostRegistry(args.registry) as registry:
            if args.host_group == "player":
                _player(registry, args)
            else:
                _campaign(registry, args)
        return 0
    except (RegistryError, StoreError, sqlite3.Error, OSError, ValueError) as error:
        print(f"tesserae host: {error}", file=sys.stderr)
        return 1


def _player(registry, args):
    action = args.host_action
    if action == "list":
        print(json.dumps(registry.players(), indent=2))
    elif action == "revoke":
        revoked = registry.revoke_credential(args.player_id)
        print("Revoked player token." if revoked else "Player had no active token.")
    else:
        with IssuedToken(args.token_file) as token:
            if action == "add":
                player_id = registry.add_player(args.name, token)
            else:
                player_id = args.player_id
                registry.rotate_credential(player_id, token)
        print(
            f"{'Added' if action == 'add' else 'Rotated token for'} player {player_id}."
        )
        print(f"Wrote token to {args.token_file}. Give it only to that player.")


def _campaign(registry, args):
    action = args.host_action
    if action == "list":
        print(json.dumps(registry.campaigns(), indent=2))
    elif action == "add":
        campaign_id = registry.register_campaign(args.database, args.name)
        print(f"Registered campaign {campaign_id} (inactive).")
    elif action == "members":
        print(json.dumps(registry.members(args.campaign_id), indent=2))
    elif action in ("activate", "deactivate"):
        registry.set_campaign_active(args.campaign_id, action == "activate")
        print(
            f"Campaign {args.campaign_id} is now {'active' if action == 'activate' else 'inactive'}."
        )
    else:
        if action == "grant":
            registry.grant(args.campaign_id, args.player_id)
        else:
            registry.revoke_access(args.campaign_id, args.player_id)
        print(
            f"{'Granted' if action == 'grant' else 'Revoked'} campaign access for {args.player_id}."
        )
