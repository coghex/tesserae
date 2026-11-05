# Characters: operator and player runbook

In a campaign whose module has characters, each player owns their own
characters there, picks one per connection, and creates new ones with the
referee, privately, before approving them for play. This runbook adds
characters to a running [lobby](lobby-runbook.md): set that up first. The
exact rules are in the [character contract](designs/character_contract.md); a
recorded run is in the [acceptance record](acceptance/characters.md). Paths,
names and IDs below are placeholders.

Characters are served by the lobby host only (`serve --registry`), on this
computer, with the browser page or `tesserae connect --lobby`. A single-game
host (`serve --database`) refuses a game with characters and serves every
other game as before.

## 1. Modules with characters

A game has characters exactly when its module declares them. The bundled
original [Trailhead](trailhead.md) module does; Relay and dice do not, and
their games are unchanged. An external package with characters is loaded with
`--module-dir`, as any other ([modules guide](modules.md#characters)).

A game's module is fixed when the game is created, so an existing game
without characters never gains them: start a new game, from a seed if it
should begin with characters (section 3).

## 2. A new campaign with characters

```sh
tesserae game create --database trail.sqlite3 --module trailhead
tesserae referee enable --database trail.sqlite3
tesserae host campaign add --registry host.sqlite3 --database trail.sqlite3 --name Trail --host HOST_PLAYER_ID
tesserae host campaign activate --registry host.sqlite3 CAMPAIGN_ID
tesserae host campaign grant --registry host.sqlite3 CAMPAIGN_ID PLAYER_ID
```

Players then create their characters themselves, with the referee
(section 5). Nobody starts with one.

## 3. Starting with existing characters (seeds and adoption)

To begin with characters already in play, for example ones carried over
from earlier sessions, create the game from a version 2 seed. It declares
each character, ready, with its owning participant; the module checks the
state holds exactly those characters
([seeded creation](game_store.md#seeded-creation)):

```json
{"seed_version": 2,
 "module": {"id": "trailhead", "version": "1", "state_version": 1,
            "providers": [{"operation": "dice.roll.v1", "provider": "builtin.dice", "version": "1"}]},
 "participants": [{"participant_id": "p_a1a1a1a1a1a1a1a1", "name": "Alice"}],
 "characters": [{"character_id": "c_0123456789abcdef", "participant_id": "p_a1a1a1a1a1a1a1a1", "name": "Mara"}],
 "state": {"walkers": {"c_0123456789abcdef": {"trait": "quick", "rolled": 3, "stamina": 3,
                                              "max_stamina": 3, "ready": true, "in_danger": false}}},
 "provenance": {"source": "..."}}
```

```sh
tesserae game create --database trail.sqlite3 --module trailhead --seed trail-seed.json
tesserae participant list --database trail.sqlite3
tesserae host campaign add --registry host.sqlite3 --database trail.sqlite3 --name Trail --host HOST_PLAYER_ID
tesserae host campaign link --registry host.sqlite3 CAMPAIGN_ID PLAYER_ID p_a1a1a1a1a1a1a1a1
tesserae host campaign activate --registry host.sqlite3 CAMPAIGN_ID
```

Link each host player to their seeded participant **before** you activate
the campaign, as for any existing participant
([registry guide](host-registry.md#link-existing-participants)): the
characters belong to that participant, and the link is how the player
reaches them. Seeded characters are ready and already in the scene; drafts
cannot be seeded. Nothing is inferred from names or module state. Keep seeds
of private material out of any public repository.

**Relinking moves the player, never the characters.** Characters belong to
their campaign participant for good. If you link a player to a different
participant, they see that participant's characters, and their old ones stay
with the old participant (link them back to reach them again). A player who
selects the campaign while unlinked is admitted as a new participant, with no
characters.

## 4. Choosing a character

On entering a campaign with characters a connection has none selected: it may
ask setup and rules questions, which change nothing in the game. Each
connection chooses its own; one player can play two characters at once, from
two connections. Choosing changes nothing in the game.

**CLI** (`tesserae connect --lobby`): the client lists your characters on
entering. `/characters` lists them again; `/character N` plays a ready one or
resumes a draft (by number, name or ID); `/character none` returns to setup
questions. `--character ID` selects one right after `--campaign ID`. The
status line says who you are: `[character] playing as Mara`.

**Browser:** after choosing the campaign, **Play** a ready character,
**Resume** a draft, or **Create character**. The status line names the
character; Settings shows it, with **Change character**.

## 5. Creating and approving

Creation is a conversation with the referee, private to you, one answer at a
time; the referee is free for other players between your answers. In a
campaign with a [campaign host](host-registry.md#the-campaign-host), the host
can also read it (and every sheet); the clients say so while you create.

- **Start:** `/create [NAME]` (CLI) or **Create character** (browser), the
  name optional. The new draft is selected.
- **Answer:** type as usual. Everything you say while creating is private and
  marked so (`[private · creating Ash]`, or "Private · creating Ash"); the
  referee runs the module's creation steps on your draft only and asks what
  next. Your draft never acts in the scene.
- **Review:** `/sheet` (CLI) or the sheet panel (browser) shows the module's
  sheet and its draft revision.
- **Approve:** `/approve` or **Approve NAME (revision N)** completes exactly
  the revision you were shown. If the draft changed since, you are shown the
  new one. If it is not finished, both clients list what is missing. Approving
  rolls nothing: anything rolled was rolled during creation, on the sheet you
  approved.
- **Enter play:** your character is ready; on your first words in play the
  referee brings them into the scene, at the table.

## 6. Recovery

- **A dropped CLI client.** Run it again with `--campaign ID --character ID`.
  `/retry` resends your last input exactly, as the character you sent it as;
  after switching characters it resends that character's own, never
  another's.
- **A dropped browser connection.** **Reconnect** logs in again and returns
  to the same campaign and character. If that character was archived or is no
  longer yours, the page is back at your characters, with why.
- **A refreshed page** keeps nothing: log in again and choose the character.
- **A host restart.** Start the host again on the same port; clients
  reconnect as above.

## 7. Archiving

`/archive N`, then `/archive N confirm` (CLI), or Settings, **Archive
character…**, then the confirmation naming it (browser). An archived character
leaves your list for good; its history stays in the campaign. Archiving is
refused while the referee is answering for that character, and the module may
refuse it (Trailhead does while a walker is in danger). Your other
connections with that character selected are told, and return to setup
questions. There is no un-archiving, transfer or deletion yet.

## 8. Storage and backups

Characters, drafts, their module data and their creation conversations live
in the campaign's own file: back it up as the
[lobby runbook](lobby-runbook.md#9-storage-and-backups) says, with the
registry and the referee folder's `codex-home/sessions/`. Nothing about
characters is kept in the registry or the browser.

## 9. What this is, and what comes next

Shipped (tracker [#110](https://github.com/coghex/tesserae/issues/110)): the
generic character lifecycle for any module that declares it, private
referee-guided creation, approval, per-connection selection in the CLI and
the browser, recovery, archive and seeds. It was accepted with a scripted
referee, under the [model-free testing policy](testing.md); that shows the
application works end to end, not how a model narrates.

Not yet: the private module's character rules, importing or continuing the
private campaign and its two-client playtest (each separate, later work),
character transfer, un-archiving or renaming after approval, browser access
from other computers, TLS for the lobby and remembered logins.
