# Running a campaign lobby: operator runbook

A lobby host serves several campaigns from one port. Each player has one
personal host token, logs in from the CLI or the browser, sees only the
campaigns you have opened to them, and picks one. Each campaign keeps its own
game, history, busy state and AI referee session. This runbook takes you from
installation to a running lobby and its upkeep. The exact rules are in the
[lobby contract](designs/lobby_contract.md); the operator commands are
detailed in the [registry guide](host-registry.md), and a recorded run is in
the [acceptance record](acceptance/lobby.md). Paths, names, IDs and ports
below are placeholders.

The lobby is for this computer only (127.0.0.1). A player on another computer
connects with the CLI through an SSH tunnel to the host computer. Browser
access over the network, TLS for the lobby and remembered logins are later
work. Characters are later work too: today a player enters a campaign as their
participant there, with no character to choose or create.

## 1. Install

Python 3.11 or newer, in a virtual environment, on the host computer:

```sh
python3 -m pip install /path/to/tesserae          # or -e for a checkout
```

For campaigns played with the AI referee, set up the referee folder and log
Codex in once, as in the [referee runbook](referee-runbook.md#2-the-referee-folder-and-its-one-time-login):

```sh
tesserae referee setup
CODEX_HOME=~/.local/share/tesserae/referee/codex-home codex login
tesserae referee check
```

Rerun `tesserae referee setup` whenever you reinstall Tesserae or move its
virtual environment.

Keep everything below (the registry, campaign files and token files) in a
private directory outside any public checkout.

## 2. Create the registry and the players

```sh
tesserae host init --registry host.sqlite3
tesserae host player add --registry host.sqlite3 --name Alice --token-file alice.token
tesserae host player add --registry host.sqlite3 --name Bob --token-file bob.token
tesserae host player list --registry host.sqlite3
```

`host init` is the only command that creates a registry. Each `player add`
prints the player's `u_…` ID and writes their token to the named file only;
the token is never printed or stored in readable form. Names are labels: two
players may share one, and a name never selects an identity.

## 3. Create and open campaigns

A campaign is an ordinary Tesserae game. Create it as usual, then register it,
activate it and grant players access:

```sh
tesserae game create --database harbor.sqlite3 --module relay
tesserae referee enable --database harbor.sqlite3      # for the AI referee
tesserae host campaign add --registry host.sqlite3 --database harbor.sqlite3 --name Harbor
tesserae host campaign activate --registry host.sqlite3 CAMPAIGN_ID
tesserae host campaign grant --registry host.sqlite3 CAMPAIGN_ID PLAYER_ID
tesserae host campaign list --registry host.sqlite3
```

The campaign ID is the game's own ID. Registration only reads the file; a new
campaign is inactive until you activate it. A player sees a campaign only when
it is active and they have an unrevoked grant. Keep the file where you
registered it: there is no command to change a campaign's location yet.

**An existing game with participants.** By default a player's first
selection creates a new participant for them in that campaign, named as in the
registry, with no game token. To give a player an existing participant instead
(for example their character's participant in an older game), link them before
they first select it:

```sh
tesserae participant list --database harbor.sqlite3
tesserae host campaign link --registry host.sqlite3 CAMPAIGN_ID PLAYER_ID PARTICIPANT_ID
tesserae host campaign links --registry host.sqlite3 CAMPAIGN_ID
```

Linking changes only the campaign's link table, never its state or history.
To move a player who was already admitted onto an existing participant, see
the [registry guide](host-registry.md#link-existing-participants).

## 4. Give players their tokens

Give each player their own token file privately, for example in person or
over an encrypted message, never in a shared channel or a repository. A token
is a password: anyone holding it is that player. The same token works from the
CLI and the browser, and in every campaign granted to that player. Game
tokens from `tesserae participant add` are a different kind: they work only on
a single-game host (`serve --database`), never at the lobby.

## 5. Launch

```sh
tesserae serve --registry host.sqlite3 --port 8765 --browser
```

```text
Tesserae lobby host listening on ws://127.0.0.1:8765/ (registry: /path/host.sqlite3,
host: 8c14…, log in with a host token; no encryption, this computer only). Press Ctrl-C to stop.
Browser: http://127.0.0.1:8765/ (this computer only; log in with a host token)
```

Add `--module-dir DIR` once per external module package your campaigns use,
and `--referee-dir DIR` if your referee folder is not the default. Leave out
`--browser` to serve the CLI only. Ctrl-C stops the host: a referee answer in
progress ends as interrupted, and players reconnect after you start it again.
A campaign is opened when first selected; if its file is missing, moved or
needs a module you did not load, players are told it cannot be opened now and
the host's log says why.

## 6. Players choose a campaign

**CLI** (on the host computer, or through an SSH tunnel to it):

```sh
tesserae connect ws://127.0.0.1:8765/ --token-file alice.token --lobby
```

`/campaigns` lists the campaigns, `/join N` enters one (by number, ID or
name), `/leave` returns to the list, and `--campaign ID` enters one right
after logging in. Inside a referee campaign, typed lines are words to the
referee, as in the [referee runbook](referee-runbook.md).

**Browser** (on the host computer): open the `Browser:` address, paste the
token or choose the token file, and log in. Choose a campaign from the list;
**Leave campaign**, beside the status line or in Settings, returns to the
list. A campaign without a referee shows its state only in the browser; play
it from the CLI.

Each connection chooses its own campaign: one player can be in Harbor from the
CLI and in another campaign from the browser at the same time. While one
campaign's referee is answering, the others are not busy.

## 7. Recovery

- **A dropped CLI client.** Run the same command again (with `--campaign` to go
  straight back). The client shows your last input and its state; `/retry`
  resends it exactly, and if the referee already had it you get the same
  answer again, never a second action. A retry is only ever resent in the
  campaign, and as the participant, it was first sent in.
- **A dropped browser connection.** The page keeps what it showed and your
  draft and offers **Reconnect**, which logs in again and returns to the same
  campaign. An input the host never confirmed is shown as unconfirmed, with
  **Resend exactly**.
- **A refreshed page** forgets the token: log in again and choose the campaign.
  Your last accepted input is shown; text never sent is gone.
- **A host restart.** Start the host again on the same port; players reconnect
  as above. Each campaign resumes its own referee session.

## 8. Rotate, revoke and close

```sh
tesserae host player rotate --registry host.sqlite3 PLAYER_ID --token-file alice-new.token
tesserae host player revoke --registry host.sqlite3 PLAYER_ID
tesserae host campaign revoke --registry host.sqlite3 CAMPAIGN_ID PLAYER_ID
tesserae host campaign deactivate --registry host.sqlite3 CAMPAIGN_ID
```

These take effect on connected players at their next action: a rotated or
revoked token ends the session (the CLI stops; the page clears and asks for a
login); a revoked grant or a deactivated campaign returns the player to the
list with the reason, leaving their other campaigns alone. An answer the
referee had already started finishes and is kept; the revoked player gets
nothing more of it. Rotating issues a new token file (use a new name) and keeps
the player's grants. Nothing here deletes a player, a campaign or any game
history.

## 9. Storage and backups

| File | Holds | Back up |
|---|---|---|
| The registry (`host.sqlite3`) | Players, hashed tokens, campaign locations and grants; no game data | With SQLite's backup API (`sqlite3 host.sqlite3 ".backup host-backup.sqlite3"`) or while no host or command is using it |
| Each campaign file | Its game state, history, participants, links and referee conversation | The same way, separately; it is authoritative for everything in its game |
| The referee folder | Each campaign's Codex sessions and job logs, and the Codex login | Its `games/` folder with the campaign files if you want sessions back; never share `codex-home/auth.json` |
| Token files | One player's credential each | Not needed: rotate a lost token instead |

Restoring a registry restores its tokens and grants as they were at backup
time: rotate any token issued or revoked since. A restored campaign file must
go back to its registered path.

## 10. What is shipped and what comes next

Shipped (tracker [#95](https://github.com/coghex/tesserae/issues/95)): the
registry and operator commands, the lobby host, CLI and browser login and
campaign selection, per-connection selection with independent campaigns,
linking existing participants, and rotation and revocation while connected.
Single-game hosts (`serve --database`) and game tokens work as before.

Not yet: choosing or creating characters (a campaign is entered as the
player's participant), joining an ongoing scenario as a new character, the
private campaign continuation, browser access from other computers, TLS for
the lobby, remembered logins, and registry migration or location updates.
