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
| Module | `trailhead`, version `1`, state version 1, host API 1, character rules version 2, campaign settings version 1, creation overrides version 1 |
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
| `trailhead.fact` | Exactly `{"fact": "background"` or `"home", "text", "provenance"}` | Records one agreed narrative fact (up to 300 characters) with where it came from: `chosen` by the player, `assumed` and accepted, `corrected`, or explicitly `unspecified` (with empty text). It changes no numbers; the sheet shows it. |
| `trailhead.pick` | Exactly `{"item": ITEM}`, one of the outfitter's items | Spends one gear pick an applied override allowed ([below](#creation-overrides)). Refused without a pick, for an item already carried, or with 8 items. Nothing is rolled. |

A draft can be completed once it has a name, a trait and stamina. Completing
it is deterministic: a keen walker gets one more maximum stamina, so does
every walker by its starting stamina bonus ([below](#campaign-settings)) and
by any overridden modifier, and every walker starts at its maximum. Unused
gear picks expire then. Nothing is rolled at completion, so the player
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
| `background` | narrative | Who the walker is, agreed with the player and recorded with `trailhead.fact`; changes no numbers, and approval does not need it |
| `home` | optional | Where the walker comes from, only if the player says; never invented |

There are no classes, levels or roles.

## Creation overrides

A draft's starting qualities can be changed by negotiation
([creation overrides contract](designs/creation_overrides_contract.md)), in
two original families. There are no classes, levels or roles here either.

| Category | Quality | Actions | Effect |
|---|---|---|---|
| `endurance` | `stamina_roll` | `replace {"value": 2–4}` | Replaces the stamina roll; the walker must have rolled, and the roll stays in `rolled` as the original |
| `endurance` | `max_stamina_modifier` | `set {"amount": -1–2}` | Added to the maximum stamina at approval, after the trait and any starting bonus |
| `supplies` | `starting_gear` | `grant {"item"}`, `remove {"carried"}`, `allow {"count": 1–3}` | Adds one of the outfitter's items, removes a carried item, or allows picks (at most 3 at once) that the owner spends later with `trailhead.pick` |

The outfitter's items are a bivouac sack, camp stove, climbing rope,
compass, first-aid kit, headlamp, rain shell, spare socks, trail map, trail
mix, trekking poles and water filter. Discovery lists the qualities with
their current values, and pages the options of `item` (items not carried)
and `carried` (items carried), five at a time, filtered by `match`.

A preview depends only on what it lists in `dependencies`:
- a replacement on `rolled` and any earlier replacement;
- the modifier on the current modifier;
- a grant or removal on the gear and how many gear changes are recorded;
- picks on the picks left.

So naming the walker, choosing its trait or recording a fact never changes
a preview, while a host's gear correction or another applied override can.
Applying a plan re-previews it and refuses one it would not make now
(`invalid_intent`), and draws nothing.

What the overrides keep:
- **Originals.** `rolled` is never changed; a grant, pick or removal is
  recorded against the original gear, and undoing one (a new intent) drops
  its record.
- **One calculation.** The effective roll and the modifier feed the same
  maximum-stamina rule that completion, the sheet and the host's trait
  correction use.
- **Authority.** A pick beyond what was allowed is refused, and the stamina
  step stays refused once rolled. The host's gear correction keeps the
  records that still hold.
- **Completion.** Unused picks expire at approval; the items used and every
  record stay, and a ready walker gets no picks.

The sheet shows `overrides` once there are any: the replacement with its
`original` roll, the modifier, each recorded gear change, and picks left,
each with who authorized it (`accepted_offer` or `direct`, and the record).

## Host administration

The campaign host (it needs no walker) can run these; nothing rolls, and each
changes only its target:

| Operation | `args` | Effect |
|---|---|---|
| `trailhead.set_stamina` | `{"character_id", "stamina"}` | A ready walker's stamina, 0 to its maximum |
| `trailhead.set_trait` | `{"character_id", "trait"}` | A ready walker's trait; its maximum is recomputed (keen +1, the starting bonus kept) and its stamina kept, lowered only to fit |
| `trailhead.set_gear` | `{"character_id", "gear"}` | A walker's gear: up to 8 items of 1 to 60 characters. Records of overridden gear are kept where they still hold |
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
applied, `gear` and `description` only once the host set them, and
`overrides` only once an override was applied (`stamina_roll`,
`max_stamina_modifier`, `gear` and, on a draft, `gear_picks`, each with
its provenance). The
state gains `npcs`, `scene` (`{"description"}`) and `prep` only once the host
sets them; absent, the game is as before. The owner's sheet shows `trait`, `stamina` (the roll, while a
draft), `max_stamina` and `in_danger`, and `gear` and `description` when set.
Each player's view is the shared scene and rules, and the visible NPCs;
walkers reach their owners as sheets. The referee's context lists ready
walkers only, never drafts, and every NPC (with its visibility) and the
preparation.

A version 2 seed declares ready walkers and their owners; its state must hold
exactly the declared walkers, each ready with a valid trait and stamina, and
an optional `bonus` of 1 or 2 counted in its maximum, `gear`, `description`
and `overrides` (without picks); and may hold `npcs`, `scene` and `prep` (see
[seeded creation](game_store.md#seeded-creation)).
