# Running a game from an external module package

This runbook is for a host operator with a game module that lives outside
Tesserae: a trusted Python package of their own, kept in their own (possibly
private) directory. It uses the original public example, Beacon
(`tests/fixtures/modules/beacon/`); a private module works the same way. For
the contracts behind each step, see [modules.md](modules.md#external-packages)
and [game_store.md](game_store.md#seeded-creation). The
[acceptance record](acceptance/private-module.md) summarizes a run of these
steps with a private module.

Paths, IDs and tokens below are placeholders. Keep the module package, the
game directory and any seed outside the Tesserae repository.

## Who holds what

| File | Host computer | A player's computer |
|---|---|---|
| The module package (`module.toml`, its Python and data) | Yes: it runs as the host user | **Never**: players need only Tesserae |
| A seed file and anything it was converted from | Yes, privately | **Never** |
| The game database | Yes | **Never** |
| TLS key (only for another computer, see the [LAN runbook](lan-runbook.md)) | Yes | **Never** |
| TLS certificate | Yes | A copy, as `--ca-file` |
| A player's token file | Briefly, until handed over | Their own, mode 0600 |

## 1. Install Tesserae

Python 3.11 or newer, on the host and on every player's computer:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .        # from a Tesserae checkout
```

Note the Tesserae version you installed. A game is bound to its module's
exact version, so upgrade Tesserae or the module only between games.

## 2. Check the package

A package is a directory with `__init__.py` and a `module.toml`:

```toml
schema_version = 1
entry_point = "rules:bundle"     # module:function, relative to the package

[[providers]]                    # one choice per operation the module requires
operation = "dice.roll.v1"
provider = "builtin.dice"
version = "1"
```

Loading runs the package's code as you: only use a package you trust. The
manifest alone chooses providers. Tesserae refuses a package whose manifest
is partial, whose module or provider IDs clash with built-in ones, or whose
code fails to load, and creates or serves nothing.

## 3. Create the game

Create the game first: `serve --module-dir` never creates one.

```sh
mkdir -m 700 ~/beacon-game
tesserae game create --database ~/beacon-game/game.sqlite3 \
  --module beacon --module-dir path/to/beacon
```

`--module` must name the package's module. To start from imported state
instead of the module's initial state, add `--seed FILE` (step 4). Otherwise,
add players with `tesserae participant add --database ... --name NAME
--token-file FILE`.

## 4. Or start from a seed

A seed is a recorded starting point: the module state and the participants,
checked by Tesserae and by the module, then written at revision 0 with no
command history. It never carries tokens.

```json
{"seed_version": 1,
 "module": {"id": "beacon", "version": "1", "state_version": 1,
            "providers": [{"operation": "dice.roll.v1", "provider": "builtin.dice", "version": "1"}]},
 "participants": [{"participant_id": "p_a1a1a1a1a1a1a1a1", "name": "Alice"},
                  {"participant_id": "p_b2b2b2b2b2b2b2b2", "name": "Bob"}],
 "state": {"shown": ["red"], "by": {"p_a1a1a1a1a1a1a1a1": 1}},
 "provenance": {"source": "an example"}}
```

```sh
tesserae game create --database ~/beacon-game/game.sqlite3 \
  --module beacon --module-dir path/to/beacon --seed ~/beacon-game/seed.json
```

- **Participant mapping.** The seed names each participant with a new ID of the
  form `p_` and 16 lowercase hexadecimal digits. The module's own state refers
  to those IDs (here, Beacon's `by` counts), and its validator refuses a
  reference to anyone not in the seed. Whatever converts your old records into
  a seed decides which person gets which character; keep that converter and its
  inputs private, and work from copies.
- **Refusals.** A seed for another module, version, state version or provider
  choice, a malformed participant, or state the module rejects creates nothing:
  no database file at all. An existing destination is never replaced.
- `tesserae game create` lists the seeded participants. `participant list`
  shows them as `no token`.

## 5. Issue tokens

Each seeded participant gets a first token:

```sh
tesserae participant rotate p_a1a1a1a1a1a1a1a1 \
  --database ~/beacon-game/game.sqlite3 --token-file ~/beacon-game/alice.token
```

It says the participant had no active token. Give each token file only to its
player, privately, never in chat, an argument or a URL. Participant commands
never load the module package.

## 6. Serve

```sh
tesserae serve --authenticated --database ~/beacon-game/game.sqlite3 \
  --port 8790 --module-dir path/to/beacon
```

The first line names the module and version, for example `module: beacon 1`.
`serve` exits before listening, and leaves the game unchanged, if:

- the database is missing or empty (`serve --module-dir` never creates one);
- the package fails to load, or is missing;
- the package's module, version, state version or provider choices differ
  from the game's (a game is bound to exactly what it was created with); or
- `--module-dir` is left out for a game whose module is not built in.

To change module code, give it a new version and create a new game. For a
player on another computer, add TLS and `--host` as in the
[LAN runbook](lan-runbook.md).

## 7. Play

```sh
tesserae connect ws://127.0.0.1:8790/ --token-file ~/beacon-game/alice.token
```

The first line is the snapshot with the player's own view. Commands are one
JSON line each, with a `request_id` and the `expected_revision` last seen:

```json
{"version": 1, "type": "command", "request_id": "a1", "expected_revision": 0, "command": "beacon.signal", "args": {}}
```

The module decides what each player sees; other players get only a revision
notification and can `{"version": 1, "type": "sync"}` for their own fresh
view.

## 8. Retry, restart and reopen

- If a connection drops or you are unsure whether a command went through, run
  `connect` again and resend the same line unchanged. A committed command
  returns its original result, with nothing drawn or spent again.
- To restart the host, stop it with Ctrl-C and run the same `serve` command,
  with the same `--module-dir`. The game continues where it was.
- Two players sending commands from the same revision cannot both change the
  game: one gets `stale_revision` and nothing happens for it.

## 9. Clean up

```sh
tesserae participant list --database ~/beacon-game/game.sqlite3
tesserae participant revoke --database ~/beacon-game/game.sqlite3 PARTICIPANT_ID
```

Ask players to delete their token files. Remove the game directory when the
game is over. Keep private packages, seeds, databases and logs out of any
public repository.
