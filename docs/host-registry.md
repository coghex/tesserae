# Host registry: local operator bootstrap

HOST-1 / [#96](https://github.com/coghex/tesserae/issues/96) adds a separate
SQLite registry for stable host players, credentials, campaign locations and
access grants. The registry is local operator data, not a campaign database.
Keep it private along with the token files and your game databases.

This is the first part of [M2-2](designs/host_lobby_design.md), tracker
[#95](https://github.com/coghex/tesserae/issues/95). It does **not** yet serve a
campaign lobby. Existing `serve --database`, CLI connections and browser joins
continue using game-specific participant tokens. A new host token will not join
those existing servers. Network admission and client selection follow in
#97–#100; character lifecycle follows that tracker.

## Create players and register a campaign

After the usual Python 3.11+ installation (`pip install -e .`), run these on the
host computer, in a private directory outside the public checkout:

```sh
tesserae host init --registry host.sqlite3
tesserae host player add --registry host.sqlite3 --name Alice --token-file alice.token
tesserae host player list --registry host.sqlite3

tesserae game create --database harbor.sqlite3 --module dice
tesserae host campaign add --registry host.sqlite3 --database harbor.sqlite3 --name Harbor
tesserae host campaign list --registry host.sqlite3
```

`init` exclusively creates a new registry, with private file permissions. All
other registry commands require an existing valid registry. Mistyped paths,
empty files, game databases, unrelated SQLite databases, newer schemas and
damaged registries are refused. No registry migration is implemented yet.

Player listing emits JSON with a stable `u_…` player ID, display name,
creation time and active-token status. Names are labels and may coincide.
Tokens are written only to the named file, never printed or stored in raw form
in SQLite. The writer refuses existing files; failed issuance removes its new
file and leaves the registry unchanged. Use a new filename for rotation.

Campaign listing emits operator records with IDs, canonical absolute paths,
titles, availability and creation times. Registration requires an existing
Tesserae game database; its campaign ID is its persisted game ID. Inspection
reads the identity/schema without upgrading it, executing its module or changing
its journal mode. Duplicate IDs and paths are refused, including copied games
and symlink aliases. A read-only inspection can read committed WAL data;
SQLite may use WAL sidecars of a concurrently open game. Registration does not
prove that its rules module is installed or that the file will remain there.
The future host must reopen and revalidate the identity/runtime before admission.

New campaigns are inactive. Substitute the IDs printed above:

```sh
tesserae host campaign activate --registry host.sqlite3 CAMPAIGN_ID
tesserae host campaign grant --registry host.sqlite3 CAMPAIGN_ID PLAYER_ID
tesserae host campaign members --registry host.sqlite3 CAMPAIGN_ID
```

Only an active campaign with an explicit unrevoked grant appears in that
player's catalog. Activation means operator availability, not that a server is
running. Grant/revoke are idempotent; granting again restores revoked access.
Member inspection retains revoked grants for the operator. These local commands
never create a campaign participant, change game state or execute gameplay.
No existing game participant is automatically matched by name.

## Rotate, revoke and deactivate

```sh
tesserae host player rotate --registry host.sqlite3 PLAYER_ID --token-file alice-new.token
tesserae host player revoke --registry host.sqlite3 PLAYER_ID
tesserae host campaign revoke --registry host.sqlite3 CAMPAIGN_ID PLAYER_ID
tesserae host campaign deactivate --registry host.sqlite3 CAMPAIGN_ID
```

Rotation replaces the active credential; old tokens and their authenticated
contexts stop working. Revoking a token preserves the player and campaign
grants. Revoking a grant affects that campaign; deactivation hides the campaign
from everyone. Issuing another token with `rotate` restores a player's login,
but does not restore revoked grants or activate campaigns. No player/campaign
hard-delete or location-update command is included in this foundation.

Only distribute a player's token file to that player. Keep the registry and
campaign files on the host. Back up the registry with SQLite's backup API (or
while all registry users are closed), separately from each campaign. It contains
credentials and access grants but no character sheets or referee history.
Campaign files remain authoritative for those records. Keeping the registry
path stable is separate from the canonical registered campaign paths.

## Python boundary for the next host slice

`HostRegistry(path)` opens an existing registry; `HostRegistry.create(path)`
creates one explicitly. `authenticate(token)` returns a frozen
`HostPlayerContext` or `None`. Only the host creates those contexts from actual
credentials; never build them from client-asserted IDs. Contexts include the
host ID and exact credential ID, and are distinct from `ParticipantContext`.

`visible_campaigns(context)` rechecks the credential and returns only campaign
IDs/titles for active grants. Invalid or stale credentials raise `RegistryError`,
which is distinct from a valid player with an empty catalog. Operator methods
such as `campaigns()` and `members()` are **not** player projections.

`authorize_campaign(context, campaign_id)` is a short context manager that
rechecks credential, availability and grant, then yields an internal record
including the database path. It grants no game participant authority. Hold it
only for the protected local admission/read/commit operation, never over model
execution, human input or network waits. Use one registry connection per thread;
transactions/guards are not reentrant.

The registry requires DELETE rollback journaling. Its read lock prevents access
revocation from committing until the guarded operation ends; a writer can time
out and fail without changing access. Switching it to WAL breaks that ordering
and is refused on open. Do not change journal mode with external tools while
registry users are open. This is not an atomic transaction across the registry
and game databases. HOST-2 must establish/test lock ordering, participant links,
bridge rechecks and frozen request identities before using this API for actual
network gameplay. The foundation alone cannot enforce revocation on legacy
servers, which do not read the registry.
