# Command protocol, version 1

This is the host/client message contract from
[issue #3](https://github.com/coghex/tesserae/issues/3) (HOST-2 in the
[server delivery design](designs/foreground_server_design.md)). The
implementation is `tesserae/protocol.py`. It validates requests and formats
messages; it does not execute commands, store state, or open connections. The
durable execution (HOST-3) and WebSocket server (HOST-4) children consume this
contract rather than defining their own.

Each message is one JSON object. Field order is insignificant.

## Messages

Request, client to host:

```json
{"version": 1, "type": "command", "request_id": "r1", "expected_revision": 0,
 "command": "roll", "args": {"expression": "2d6+3"}}
```

Result, host to client. `result` is the dice evaluator's structured result
unchanged, the same object `tesserae roll --json` prints:

```json
{"version": 1, "type": "result", "request_id": "r1", "revision": 1,
 "result": {"schema_version": 1, "expression": "2d6+3",
            "terms": [{"kind": "dice", "count": 2, "sides": 6, "rolls": [4, 3],
                       "sign": 1, "subtotal": 7},
                      {"kind": "constant", "value": 3, "sign": 1, "subtotal": 3}],
            "total": 10}}
```

Error, host to client:

```json
{"version": 1, "type": "error", "request_id": "r1", "revision": null,
 "code": "invalid_request", "message": "Human-readable explanation"}
```

Welcome, host to client on connection:

```json
{"version": 1, "type": "welcome", "revision": 0}
```

## Request rules

- A request has exactly the six fields shown above; missing or extra fields are
  rejected.
- `version` is an integer. Only `1` is supported.
- `type` is the string `"command"`.
- `request_id` is a string of 1 to 128 characters containing no whitespace
  anywhere. It is preserved exactly; nothing is trimmed or normalized.
- `expected_revision` is an integer from 0 through 9223372036854775807
  (2^63 − 1).
- `command` is a string and `args` is a JSON object.
- JSON booleans are never accepted as integers.
- The only command is `roll`. Its `args` has exactly one field, `expression`, a
  string validated with the existing dice rules and limits (at most 4096
  characters, 1000 dice, and 1,000,000 sides). Validation never rolls dice.
- Malformed JSON, duplicate object keys at any depth, non-finite numbers
  (`NaN`, `Infinity`, `-Infinity`, or overflowing literals such as `1e999`), and
  nesting too deep to parse are all rejected.

## Errors

Error codes are stable and clients may branch on them. Messages are readable
diagnostics only and may change; do not parse them.

| Code | Meaning | Defined by |
|---|---|---|
| `invalid_request` | Malformed JSON, wrong structure, missing/extra field, or wrong type or range | HOST-2 |
| `unsupported_version` | A well-formed request with a version other than 1 | HOST-2 |
| `unsupported_command` | A well-formed request naming an unknown command | HOST-2 |
| `invalid_expression` | A well-formed `roll` whose expression is invalid | HOST-2 |
| `stale_revision` | `expected_revision` does not match the current state | HOST-3 |
| `request_conflict` | The sender reused a request ID for different content | HOST-3 |
| `internal_error` | Reserved: the host failed unexpectedly or its store was busy | HOST-4 |

Checks run in this order, and the first failure decides the code:

1. JSON decoding and top-level object shape.
2. Field presence and types of every top-level field, then, when `command` is
   `"roll"`, the shape and type of its `args`.
3. Version.
4. Command.
5. Expression.

So any structural error is `invalid_request` even if the version or command
is also unsupported. Note that step 2 checks `roll` argument shape before the
version check.

An error's `request_id` is the request's own ID when the JSON parsed to an
object whose `request_id` is valid, even if other fields are wrong. Otherwise
it is `null`. An error's `revision` is the host's current revision when known,
otherwise `null`. Result and welcome revisions are integers in the same range
as `expected_revision`.

## Replay identity

The host uses this to return the original result for a retried request instead
of executing it twice. See [game_store.md](game_store.md) for the execution
order and failure behavior.

- The lookup key is `request_id` within the sender's server-side namespace:
  (game, participant, `request_id`). The host establishes the participant from
  its own trusted context, never from the message, so the wire format has no
  identity fields. Two participants may both use `r1` without conflict, and
  neither can see or replay the other's record.
- The request's content is its parsed `version`, `type`, `command`, and `args`
  (`CommandRequest.content()`). Content compares by parsed JSON value, so object
  key order does not matter.
- `expected_revision` is not part of the content, so an otherwise identical
  retry still retrieves the original result after the revision has moved on.
- Expressions compare as exact strings. `"2d6+3"` and `"2d6 + 3"` are different
  content; equivalent arithmetic and whitespace are not normalized.
- The same ID with the same content is a replay. The same ID with different
  content is a `request_conflict`.
