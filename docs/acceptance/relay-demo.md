# M1-4 Relay demo acceptance

Tracker [#37](https://github.com/coghex/tesserae/issues/37): the combined demo
passed. The automated checks and two live walkthroughs with the installed CLI
are recorded below: one with both players on one Mac over loopback, and one
with Alice on the Mac and Bob on the Ubuntu VM from M1-3 over authenticated
TLS. This completes M1-4's mechanical demo only: no AI referee, private
adaptation or browser client is claimed.

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

## Live walkthroughs

Both ran on 2026-10-31 at application code `dc30da6` (the #41 change; #42
adds only tests and documentation). Live dice were kept as rolled.

### Loopback

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

### Mac and Ubuntu VM

Alice played on the Mac and Bob on the Ubuntu VM, each through a real
`connect` over authenticated TLS. The owner approved a non-loopback test
listener for this run. The guest's commands ran over SSH with the VM-specific
management key and pinned host key from M1-3.

| | Mac host and Alice | Ubuntu guest and Bob |
|---|---|---|
| OS | macOS 26.7.1, Apple Silicon | Ubuntu Server 26.10, aarch64 |
| Python | 3.14.8, websockets 17.4, OpenSSL 3.6.5 | 3.14.8, websockets 17.4, OpenSSL 4.0.3 |
| Installation | A fresh venv outside the checkout, non-editable install of `dc30da6` | A fresh public clone of the #41 branch at `dc30da6`, its own venv, non-editable install |
| Network | `serve --authenticated` with a test certificate, bound to the UTM Shared Network bridge address only (not the Mac's Wi-Fi address), port 0; Alice joined at the same address with `--ca-file` | UTM Shared Network (NAT); connects to the bridge address with `--ca-file` |
| Network changes | None: no firewall, router or port-forwarding changes | None |
| Files transferred | The public certificate and Bob's token, by SCP. Their SHA-256 matched on the guest, the token was mode 0600, and the Mac's copy of it was then deleted | Kept outside the checkout |

| Step | Observed |
|---|---|
| Join | Same game ID, different participant IDs, revision 0, the Relay module and `character: null` on both sides. |
| Enlist | Alice's Wren committed revision 1. Bob, on the VM, received only the four-field notification; his sync showed `character: null`. Bob's Odo committed revision 2. |
| Simultaneous repairs | Both sent a repair at revision 2 back to back. Alice's committed: a live 5, total 6 against 5, success, effort 3 → 2, relay restored at revision 3. Bob's got `stale_revision` with nothing rolled or spent. Both syncs showed revision 3, the relay restored, and only their own worker (efforts 2 and 3). |
| Restart and replay | Both clients exited 0; the host exited 0 on SIGINT, restarted, and both rejoined at revision 3. Alice's original enlist and repair returned identical results, and a fresh enlist was `action_rejected`. From the VM, Bob's losing repair stayed `stale_revision` and his original enlist returned its identical result. |
| Final state | The database held revision 3, three commands, efforts 2 and 3, and the relay restored, as the players saw. |
| Privacy | No token, referee note or traceback in any host, client or SSH output. Neither player's output had the other's worker, participant ID or the worker map. |
| Cleanup | Both tokens revoked. The guest's token, certificate copy, clone and venv were removed, and so was the Mac's temporary directory. The VM stays running. |

## Not covered

- **Physical second computer.** As in M1-3, the Ubuntu VM stands in for a
  friend's computer. Physical-device Wi-Fi testing stays deferred and is not a
  gate for M1-4.
- **A live failed check.** Both walkthroughs rolled a 5 on the first repair,
  a success. The failure branch and an exhausted worker are covered by fixed
  draws in the automated checks.
