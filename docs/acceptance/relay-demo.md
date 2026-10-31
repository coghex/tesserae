# M1-4 Relay demo acceptance

Tracker [#37](https://github.com/coghex/tesserae/issues/37): the combined demo
passed. The automated checks and one live walkthrough with the installed CLI
are recorded below. The walkthrough ran both players on one Mac over loopback.
The Ubuntu VM from M1-3 was not used for this record (see
[Not covered](#not-covered)). This completes M1-4's mechanical demo only: no
AI referee, private adaptation or browser client is claimed.

| Child | Delivered by |
|---|---|
| #38: module contract and `dice` adapter | PR #44 |
| #39: persisted binding/state, atomic execution, `game create` | PR #45 |
| #40: the original Relay module | PR #46 |
| #41: host runtime loading and per-player snapshot views | PR #47 |
| #42: combined demo, [runbook](../relay-demo.md) and this record | PR #48 |

## Automated checks

Run on 2026-10-31 on macOS with Python 3.14.8, websockets 17.4 and Ruff 0.17.3,
from the #42 branch: 133 tests, Ruff check and format clean. The PRs' GitHub
Actions runs repeat them on Python 3.11 on Ubuntu.

| #37 acceptance | Evidence |
|---|---|
| Two real CLI participants with their own workers, the declared check and shared updates | `test_relay_demo` creates the game with `game create`, issues both tokens with `participant add`, and runs real `serve --authenticated` and two `connect` processes with live dice. Each player enlists once; the check's die, total, target, success and effort agree with each other; the idle player gets only the four-field notification and syncs for a fresh view. |
| Only permitted state | Neither player's output ever has the other's worker name or participant ID, the worker map or the referee note. `test_module_host` checks the same for joins, syncs, results, errors and notifications against a live host, and `test_relay` checks the module's views directly. |
| Exact recovery after reconnect and restart | After SIGINT and restart, Alice's original enlist returns its exact result although a fresh enlist is `action_rejected`, the winning repair replays exactly, and the losing one stays `stale_revision`. The store then holds revision 3, three commands, efforts 2 and 3, and the relay state the result reported. |
| Stale-action and resource guards; no duplicate dice or spend | Simultaneous repairs from the same revision give one result and one `stale_revision` (`test_relay_demo`). Fixed draws cover success, failure and every refusal below the network, with no draw on refusal (`test_relay`, `test_module_state`). |
| Views keep their revision | `test_module_host` forces a commit between reading and sending both a join and a sync snapshot. Every snapshot's view matches its revision, and revisions never go backwards. |
| No fallback | Unknown or mismatched module/provider bindings fail before listening and change nothing; Relay in development mode exits 2 (`test_module_host`, `test_relay_demo`). |
| Existing behavior | The dice, credential, TLS, streaming and session tests pass unchanged, apart from one race test that now patches the read joins use. `dice` snapshots stay metadata only. |

## Live walkthrough

Run on 2026-10-31 at application code `dc30da6` (the #41 change; #42 adds
only tests and documentation).

| | |
|---|---|
| Platform | macOS 26.7.1, Apple Silicon |
| Python | 3.14.8, websockets 17.4, OpenSSL 3.6.5 |
| Installation | A fresh virtual environment outside the checkout, `pip install` of the branch (not editable); the `tesserae` entry point |
| Network | Loopback only, `ws://127.0.0.1`, port 0. No TLS, no non-loopback listener, no firewall or router changes |
| Players | Alice and Bob, each a separate `connect` process with its own token file, driven line by line over stdin |

The database, tokens and raw log stayed in a temporary directory outside the
repository. The observations below are sanitized.

| Step | Observed |
|---|---|
| Setup | `game create --module relay` created the game at revision 0 (`relay 1`); two `participant add` calls wrote 0600 token files. `serve` named `module: relay 1`. |
| Join | Both snapshots: same game, different participant IDs, revision 0, `character: null`, `commands: ["relay.enlist"]`, relay offline. |
| Enlist | Alice's worker Wren (repair 1, effort 3) committed revision 1. Bob received only `{"version", "type", "game_id", "revision": 1}`; his sync showed `character: null`. Bob's worker Odo committed revision 2. |
| Repair | Alice's live roll was 5: total 6 against 5, success, effort 3 → 2, relay restored at revision 3. Bob was notified, and his sync showed the relay restored, his own effort 3 and no commands left. |
| Simultaneous repairs | Both players then sent a repair at revision 3. Because the relay was already restored, both got `action_rejected`, with nothing rolled or spent. The live result was accepted; nothing was rerolled. |
| Restart and replay | Both clients exited 0; the host exited 0 on SIGINT and restarted. Alice rejoined at revision 3. Her original enlist and repair lines, resent unchanged, returned identical results. A fresh enlist was `action_rejected`; a wrong `expected_revision` was `stale_revision`. |
| Final state | Alice's last view, the database and the results agree: revision 3, three commands (`a1`, `b1`, `a2`), Wren effort 2, Odo effort 3, relay restored. |
| Privacy | No token appeared in any output or log. Neither client's output had the other's worker or participant ID, the worker map or the referee note. No tracebacks. |
| Configuration errors | `serve` without `--authenticated` exited 2 with `requires --authenticated`, starting nothing. |
| Cleanup | Both tokens revoked with `participant revoke`; the temporary directory was removed. |

## Not covered

- **Second operating system.** The tracker prefers one player on the Ubuntu VM
  used for M1-3. That needs a non-loopback test listener, which the owner
  starts or approves for the run; it was not done here. A VM run can be added
  to this record. M1-3's deferred physical-device Wi-Fi check is not a gate for
  M1-4.
- **A live failed check.** Live dice gave a success on the first repair. The
  failure branch and an exhausted worker are covered by fixed draws in the
  automated checks.
