# Trailhead: the original module with characters

`trailhead` is a tiny, original public module (`tesserae/trailhead.py`) for
M2-3 ([#112](https://github.com/coghex/tesserae/issues/112), under tracker
[#110](https://github.com/coghex/tesserae/issues/110)). It exercises the
[character contract](designs/character_contract.md): several characters per
player, creation in steps with a roll the player approves, a deterministic
completion, a resource and an uncertain check in play, and a module veto on
archiving. Its theme and numbers belong to this module alone. The host has no
trait, stamina or danger rules.

The module is in the bundled catalog: `game create --database NEW.sqlite3
--module trailhead` makes a Trailhead game. Players reach characters through
the lobby host once M2-3's host and client slices land; until then the game
store runs every operation below (see the
[game store](game_store.md#characters)).

## Binding

| | |
|---|---|
| Module | `trailhead`, version `1`, state version 1, host API 1, character rules version 1 |
| Requires | `dice.roll.v1`, selected explicitly as `builtin.dice` version `1` |
| Tags | `demo`, `original`, `dice`, `characters` (descriptive only) |
| Participants | Authenticated participants only |

## Walkers

Walkers set out up a mountain trail. A player may create any number of
walkers; each is one host character, and its data is keyed by the
character's ID.

**Creation** (in creation mode, on the player's own draft):

| Step | `args` | Effect |
|---|---|---|
| `character.name` | Exactly `{"name": NAME}` | The host's step: the walker's label. |
| `trailhead.trait` | Exactly `{"trait": "steady"}`, `"quick"` or `"keen"` | Sets the trait; can be changed until approval. |
| `trailhead.stamina` | Exactly `{}` | Rolls `1d3+1` once. A second roll is refused: the result stands. |

A draft can be completed once it has a name, a trait and stamina. Completing
it is deterministic: a keen walker gets one more maximum stamina, and every
walker starts at its maximum. Nothing is rolled at completion, so the player
approves every roll.

**Play** (as a ready walker):

| Command | `args` | Effect |
|---|---|---|
| `trailhead.push` | Exactly `{}` | Spends 1 stamina and rolls `1d6` (+1 if quick) against 4. Less leaves the walker in danger. Refused in danger or with no stamina. |
| `trailhead.recover` | Exactly `{}` | Rolls `1d6` (+1 if steady) against 3; success ends the danger. Costs nothing. Refused when not in danger. |

A walker in danger cannot be archived. Switching characters changes nothing:
stamina and danger stay with each walker.

## State

```json
{"walkers": {"c_…": {"trait": "keen", "rolled": 3, "stamina": 4,
                      "max_stamina": 4, "ready": true, "in_danger": false}}}
```

`rolled` is the stamina roll; `stamina` and `max_stamina` are set at
completion. The owner's sheet shows `trait`, `stamina` (the roll, while a
draft), `max_stamina` and `in_danger`. Each player's view is the shared scene
and rules only; walkers reach their owners as sheets. The referee's context
lists ready walkers only, never drafts.

A version 2 seed declares ready walkers and their owners; its state must hold
exactly the declared walkers, each ready with a valid trait and stamina (see
[seeded creation](game_store.md#seeded-creation)).
