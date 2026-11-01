# Host registry: local operator bootstrap

HOST-1 / [#96](https://github.com/coghex/tesserae/issues/96) adds a separate
SQLite registry for stable host players, credentials, campaign locations and
access grants. The registry is local operator data, not a campaign database.
Keep it private along with the token files and your game databases.

This is the first part of [M2-2](designs/host_lobby_design.md), tracker
[#95](https://github.com/coghex/tesserae/issues/95). HOST-2
([#97](https://github.com/coghex/tesserae/issues/97)) serves the registry's
campaigns with `tesserae serve --registry` ([below](#serve-the-campaigns));
the exact rules are in the [lobby contract](designs/lobby_contract.md).
Players use it from the CLI with `tesserae connect --lobby`
([#98](https://github.com/coghex/tesserae/issues/98),
[below](#play-from-the-cli)) or from the browser on the host computer
([#99](https://github.com/coghex/tesserae/issues/99),
[below](#play-in-the-browser)).
Existing `serve --database`, CLI connections and browser joins continue
using game-specific participant tokens. A host token never joins those
servers, and a game token never logs in to a lobby host. Character lifecycle
follows this tracker.

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

## Python boundary

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
and game databases. The lobby host holds the guard across each campaign
transaction, registry first ([lobby contract](designs/lobby_contract.md#6-authorization-and-ordering)
section 6). Revocation is not enforced on single-game servers, which do not
read the registry.

## Serve the campaigns

```sh
tesserae serve --registry host.sqlite3 --port 8765
```

The lobby host listens on 127.0.0.1 only. Players log in with their host
token, see their catalog and select a campaign; the first selection admits
them as a participant of that campaign, named as in the registry, without any
game token. Each campaign keeps its own state, history, busy state and
referee session. Repeat `--module-dir` for each external module package the
campaigns use; `--referee-dir` is the referee folder for every referee-mode
campaign. `--browser` also serves the browser page
([below](#play-in-the-browser)). `--database`, `--authenticated`, TLS and
other `--host` values are refused (exit 2). Players connect with
`tesserae connect --lobby` ([below](#play-from-the-cli)) or the page.

A campaign is opened when first selected. If its file is missing, moved,
replaced or bound to a module that is not loaded, the selection is refused as
unavailable and the host logs why; nothing is created, and the next selection
tries again.

## Play from the CLI

Give each player their host token file and the address. On the host computer
(or through an SSH tunnel to it):

```sh
tesserae connect ws://127.0.0.1:8765/ --token-file alice.token --lobby
```

```text
[lobby] Alice: 2 campaigns open to you
[lobby] 1. Bridge  4b0c…
[lobby] 2. Harbor  8f36…
[lobby] /join N enters campaign N; /help lists commands.
/join 2
[joined] Harbor: type to talk to the referee; /leave returns to the lobby; /help lists commands.
[status] idle; nothing waiting for you; last message 0
```

- `/campaigns` lists the campaigns open to you and marks the one you are in;
  `/join` takes its number, ID or name; `/leave` returns to the lobby. Join
  another campaign by leaving first. Choosing a campaign plays nothing.
- Inside a referee campaign everything is as in the
  [referee runbook](referee-runbook.md): typed lines are your words, with
  `/private`, `/status`, `/history`, `/retry` and the rest. A campaign without
  a referee takes JSON commands, one per line.
- `/retry` belongs to the campaign you sent the input in, as the player you
  were there. Leaving keeps it with that campaign, and joining it again
  restores it; it is never resent anywhere else, or as a new participant
  after the host relinks you.
- If your access to the campaign ends, the client says so and you are back in
  the lobby. If your host token is revoked, the client stops.
- `--campaign ID` enters a campaign as soon as you log in, and `--json` prints
  the host's messages as JSON lines (typed lines are still words and
  commands). Characters come in a later version; `/character` says so.
- Without `--lobby`, `tesserae connect` joins one game with a game token, as
  before, and says so if you point it at a lobby host.

## Play in the browser

```sh
tesserae serve --registry host.sqlite3 --port 8765 --browser
```

The host prints a second line, `Browser: http://127.0.0.1:8765/ (this
computer only; log in with a host token)`. Open it on the host computer, paste
your host token or choose your token file, and log in. The Console lists the
campaigns open to you; choose one to play it. The page then works as in the
[browser runbook](browser-runbook.md), with the campaign's name in the status
line:

- **Leave campaign**, beside the status line or in Settings, returns to the
  list; choose another campaign from there. Leaving clears everything of that
  campaign from the page, including any text you had typed and not sent.
- If your access to the campaign ends, the page says so and shows the list
  again. If your host token is revoked, it clears everything and asks you to
  log in again.
- After a dropped connection, **Reconnect** logs in again and returns to the
  same campaign, keeping your draft. If the host has since linked you to a
  different participant, the page starts that campaign afresh.
- A campaign without a referee shows its state only; play it from the CLI.
- Your token stays in the page's memory only, so a refresh asks for it again.
  Each page (and each CLI connection) chooses its own campaign.

## Link existing participants

A campaign that already has participants, for example a seeded or older game,
can give one of them to a host player instead of creating a new one:

```sh
tesserae host campaign link --registry host.sqlite3 CAMPAIGN_ID PLAYER_ID PARTICIPANT_ID
tesserae host campaign links --registry host.sqlite3 CAMPAIGN_ID
tesserae host campaign unlink --registry host.sqlite3 CAMPAIGN_ID PLAYER_ID
```

Find participant IDs with `tesserae participant list --database GAME`. These
commands open the campaign's file (which upgrades it to schema version 6 if
needed) and change only its link table: never state, history or game tokens.
A player has at most one active link per campaign, and a participant at most
one player. Linking does not grant access. To move a player who was already
admitted to a new participant onto an existing one, revoke their grant (or
deactivate the campaign), `unlink`, `link`, then grant again; `unlink` keeps
the old participant and its history.
