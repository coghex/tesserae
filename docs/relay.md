# Relay repair: the original demo module

`relay` is a tiny, original public module (`tesserae/relay.py`) for the
M1-4 demo ([#40](https://github.com/coghex/tesserae/issues/40), DEMO-3 in the
[M1-4 design](designs/demo_modules_design.md)). It exercises a character
owned by one participant, a shared scene, a numeric resource and an uncertain
check, through the [module contract](modules.md). Its theme and numbers
belong to this module alone. The host has no worker, effort, repair or check
rules.

The module is registered in the bundled catalog. `game create --database
NEW.sqlite3 --module relay` creates a bound Relay game (#39), and `serve
--authenticated` hosts it, sending each player their view on join and sync
(#41). The [demo runbook](relay-demo.md) walks two players through a game.

## Binding

| | |
|---|---|
| Module | `relay`, version `1`, state version 1, host API 1 |
| Requires | `dice.roll.v1`, selected explicitly as `builtin.dice` version `1` |
| Tags | `demo`, `original`, `dice` (descriptive only) |
| Participants | Authenticated participants only: `serve` refuses development mode for a Relay game |

It has no raw `roll` command; its dice are drawn only by `relay.repair`.

## Rules

A storm-damaged relay is offline. Each participant may enlist one worker,
with repair 1 and effort 3. There is no fixed party size.

| Command | `args` | Effect |
|---|---|---|
| `relay.enlist` | Exactly `{"name": NAME}`: 1 to 40 characters, not blank, with no control characters | Creates the caller's worker. No dice. |
| `relay.repair` | Exactly `{}` | Rolls `1d6`; total = die + repair; 5 or more succeeds. Spends 1 effort either way. Success restores the shared relay. |

Extra fields are `invalid_request`. That includes any attempt to choose an
owner, a rating, the difficulty, the expression or the outcome. Players
never pick their participant; the host supplies it.

These fresh actions are rejected with `action_rejected` before any dice are
drawn, and change nothing:
- enlisting a second time (effort is never reset);
- repairing with no worker;
- repairing with no effort left;
- repairing once the relay is restored.

A failed check is a normal, committed outcome: the effort is spent. If every
worker runs out of effort with the relay still offline, that is the honest
ending; nothing resets. There are no criticals or fumbles. Replay of an
earlier request is the host's job, and still works after a new repair would
be refused.

## State

The module state is a JSON object, stored by the host from #39:

```json
{
  "relay": {"restored": false},
  "workers": {"PARTICIPANT_ID": {"name": "Ada", "repair": 1, "effort": 3}},
  "referee": {"maintenance_note": "..."}
}
```

The referee note is a demonstration field that no player payload may contain.
It tests the view boundary; this public module is not a vault for secrets.

## What players see

A participant's view has the public scene, the rules, the caller's own worker
(or `null`), and the commands the caller can use right now:

```json
{
  "scene": {"id": "relay_station", "title": "The storm-damaged relay",
            "description": "...", "restored": false},
  "rules": {"relay.enlist": "...", "relay.repair": "..."},
  "character": {"name": "Ada", "repair": 1, "effort": 3},
  "commands": ["relay.repair"]
}
```

It never includes the worker map, another worker's fields, participant IDs or
the referee note.

Results go only to the caller. An enlist returns the caller's new worker:

```json
{"worker": {"name": "Ada", "repair": 1, "effort": 3}, "relay": {"restored": false}}
```

A repair returns the check, with the dice result from `dice.roll.v1`
unchanged, plus the effort spent and the relay's state:

```json
{
  "check": {"roll": {"schema_version": 1, "expression": "1d6", "terms": [...], "total": 4},
            "repair": 1, "total": 5, "target": 5, "success": true},
  "effort": {"before": 3, "after": 2},
  "relay": {"restored": true}
}
```
