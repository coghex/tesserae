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
| Module | `trailhead`, version `1`, state version 1, host API 1, character rules version 2, campaign settings version 1 |
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
it is deterministic: a keen walker gets one more maximum stamina, so does
every walker by its starting stamina bonus ([below](#campaign-settings)), and
every walker starts at its maximum. Nothing is rolled at completion, so the player
approves every roll.

**Play** (as a ready walker):

| Command | `args` | Effect |
|---|---|---|
| `trailhead.push` | Exactly `{}` | Spends 1 stamina and rolls `1d6` (+1 if quick) against 4. Less leaves the walker in danger. Refused in danger or with no stamina. |
| `trailhead.recover` | Exactly `{}` | Rolls `1d6` (+1 if steady) against 3; success ends the danger. Costs nothing. Refused when not in danger. |

A walker in danger cannot be archived. Switching characters changes nothing:
stamina and danger stay with each walker.

## Campaign settings

A campaign's settings document may set one thing:

| Setting | Values | Default | Effect |
|---|---|---|---|
| `starting_stamina_bonus` | 0, 1 or 2 | 0 | Added to a walker's maximum stamina when it is approved |

A campaign-wide value goes in the document's `settings`; one walker's
exception (`{"exception_id", "character_id", "settings", "reason"}`) raises
or lowers it for that walker alone. With no document, every walker is made
exactly as before settings existed. A change of settings never touches a
walker already approved: its stamina is not recalculated, refreshed or
rerolled. A draft whose bonus changes gets a new draft revision, so an
approval shown before the change is refused as stale.

Its requirements, the one description the referee and players see:

| `id` | Kind | |
|---|---|---|
| `character.name` | mechanical | The host's: a name (any text) |
| `trait` | mechanical | Choose steady, quick or keen |
| `stamina` | mechanical | Roll `1d3+1` once; says the bonus approval will add |
| `background` | narrative | Who the walker is, agreed with the referee; changes no numbers |
| `home` | optional | Where the walker comes from; not recorded by the module yet (`support`) |

There are no classes, levels or roles.

## Host administration

The campaign host (it needs no walker) can run these; nothing rolls, and each
changes only its target:

| Operation | `args` | Effect |
|---|---|---|
| `trailhead.set_stamina` | `{"character_id", "stamina"}` | A ready walker's stamina, 0 to its maximum |
| `trailhead.set_trait` | `{"character_id", "trait"}` | A ready walker's trait; its maximum is recomputed (keen +1, the starting bonus kept) and its stamina kept, lowered only to fit |
| `trailhead.set_gear` | `{"character_id", "gear"}` | A walker's gear: up to 8 items of 1 to 60 characters |
| `trailhead.describe` | `{"character_id", "description"}` | A walker's description, up to 500 characters |
| `trailhead.npc_set` | `{"npc_id", "name", "note", "visible"}` | Adds or updates an NPC (up to 50); players see it only when visible |
| `trailhead.scene_set` | `{"description"}` | The scene's description, which everyone sees |
| `trailhead.prep_set` | `{"note"}` | The referee's preparation, up to 2,000 characters; no player sees it |

A draft's stamina and trait come from creation, so those corrections are
refused on a draft; gear and description may be set on either, and change a
draft's revision. The host's inspection shows every walker (drafts too),
every NPC, the scene and the preparation. Targets for digests are a
character, an NPC and the scene (`trailhead`).

## State

```json
{"walkers": {"c_…": {"trait": "keen", "rolled": 3, "stamina": 4,
                      "max_stamina": 4, "ready": true, "in_danger": false}}}
```

`rolled` is the stamina roll; `stamina` and `max_stamina` are set at
completion, with `bonus` (1 or 2) recorded only when a starting bonus
applied, and `gear` and `description` only once the host set them. The
state gains `npcs`, `scene` (`{"description"}`) and `prep` only once the host
sets them; absent, the game is as before. The owner's sheet shows `trait`, `stamina` (the roll, while a
draft), `max_stamina` and `in_danger`, and `gear` and `description` when set.
Each player's view is the shared scene and rules, and the visible NPCs;
walkers reach their owners as sheets. The referee's context lists ready
walkers only, never drafts, and every NPC (with its visibility) and the
preparation.

A version 2 seed declares ready walkers and their owners; its state must hold
exactly the declared walkers, each ready with a valid trait and stamina, and
an optional `bonus` of 1 or 2 counted in its maximum, `gear` and
`description`; and may hold `npcs`, `scene` and `prep` (see
[seeded creation](game_store.md#seeded-creation)).
