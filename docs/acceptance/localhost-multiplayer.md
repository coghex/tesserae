# Localhost multiplayer acceptance

Tracker [#12](https://github.com/coghex/tesserae/issues/12): complete.
Verified on 2026-10-30 against master at
[`a5f0a80`](https://github.com/coghex/tesserae/commit/a5f0a8018807bf8098143254ce8c89cbde9eb95e),
after all five children merged. No runtime changes were needed for acceptance.

| Child | Delivered by |
|---|---|
| #13: session contract | PR #18 |
| #14: participants and credentials | PR #19 |
| #15: authenticated host | PR #20 |
| #16: snapshots and notifications | PR #22 |
| #17: streaming CLI | PR #23 |

## Checks

A new virtual environment installed the checkout with
`python3 -m pip install -e '.[dev]'`. Local verification used Python 3.14.8,
websockets 17.4 and Ruff 0.17.3. These commands passed:

```sh
python3 -m ruff check .
python3 -m ruff format --check .
python3 -m unittest discover -s tests -v
```

All 77 tests passed. The installed offline CLI was also exercised from outside
the checkout. The same source commit's
[GitHub Actions run](https://github.com/coghex/tesserae/actions/runs/41142673336)
passed on Python 3.11.

## Combined acceptance

| Requirement | Verified evidence |
|---|---|
| Durable participants, rotation and revocation | Local operator commands provisioned two participants. Restart retained their IDs. Rotation invalidated the old token, kept the participant ID and replay outcome, and ended the old session. Revocation rejected the replacement token. Participant/store tests cover transactional failure and restart. |
| Version-1 migration preserves history | A copy of `tests/fixtures/store_v1.sqlite3` upgraded to schema 2. Every value in every original `game` and `commands` row was compared before and after and was unchanged. The original development request replayed without drawing dice. Migration tests also cover rollback and newer/foreign-store refusal. |
| Authentication precedes disclosure | Joining tests reject absent, malformed, unknown and revoked credentials without exposing a game view or drawing dice. The combined CLI check also rejected a rotated token without game metadata. |
| Two clients, private replies and passive updates | Two real `tesserae connect` processes used distinct IDs against one real foreground host. Each received its own result, while the other received only revision metadata. Captured output contained neither the other player's expression nor any token. |
| Concurrency and recovery | Both clients submitted fresh requests at the same revision: one committed and one received `stale_revision`. Both independently used `r1`. After host SIGINT/restart, an unchanged retry returned the exact original result and left three committed records at revision 3. |
| Non-mutating operations | Synchronization, joining, replay, rotation and revocation left that revision and action history unchanged. Store and notification tests cover invalid requests, rejected mutations, read-only access and no spurious notifications. |
| Bounded CLI lifecycle and JSON output | Both clients completed EOF cleanly. Client process tests cover idle Ctrl-C, host closure, failed joining, malformed replies and bounded drain timeout with uncertain-request reporting. Stdout is parsed as JSON lines; diagnostics and secret-free output are checked. |
| Existing behavior and localhost scope | Offline dice and development-host tests pass, including the original welcome/reply procedure and absence of development notifications. The host still binds only `127.0.0.1`; there is no LAN listener option. |
| Focused CI and reproducibility | Clean installation, local Ruff/unittest checks and merged-head CI pass. The runnable two-client procedure is linked below. |

The independent combined CLI check used only installed commands and temporary
external database/token paths. Its final store had exactly three actions at
revision 3 after synchronization, restart/replay and credential administration.
Only processes started for verification were stopped.

## Reproduce the operator flow

Follow [Two players on one computer](../../README.md#two-players-on-one-computer):
provision two personal token files, start `serve --authenticated`, and run one
`connect` process per participant. Use the same request ID independently for
each participant; observe the other client's revision update. Restart the host
and resend an original line unchanged to confirm replay. The same README covers
rotation/revocation, file input, synchronization and exit statuses. Run the
unittest command above for controlled contention, failure and lifecycle checks.

This acceptance completes only tracker #12's localhost scope. LAN access and a
friend connecting from another computer remain unimplemented. The original demo,
rules modules, browser and AI referee are later milestone work.
