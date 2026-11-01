# M1-6 AI referee acceptance

Tracker [#63](https://github.com/coghex/tesserae/issues/63): the AI-refereed
workflow passed on 2026-10-31 with both demonstration scenes, the original
Relay scene and the approved private scene. Two authenticated CLI players
typed plain text; a real Codex referee ran each job through the host's bridge,
starting and then resuming one session per game; live dice were kept as
rolled. The private scene's record, with its module, live values, fingerprints
and logs, stays in the owner's local private workspace and is reviewed there.
How to run a referee game is in the [operator runbook](../referee-runbook.md).

| Child | Delivered by |
|---|---|
| #64: the referee contract | PR #74 |
| #65: durable jobs, conversation and the busy gate | PR #75 |
| #66: the supervised Codex runner | PR #76 |
| #67: the bridge and module referee hooks | PR #77 |
| #68: referee mode on the host and the readable CLI | PR #78 |
| #69: the playable loop and its recovery | PR #79 |
| #70: the private scene adapted for the referee | Private module, reviewed privately |
| #71: this record, the runbook and the fault tests | This PR and a private acceptance record |

## Public checks

The public suite uses only original fixtures and a scripted stand-in for
Codex that speaks MCP to the real bridge, so CI needs no model account. Beyond
the earlier children's tests, the referee loop tests cover:
- Codex dying after an action committed: the action stays, the next job is
  told of it, and the dead run's token is refused;
- a lost final reply, and junk or oversized output lines;
- a malformed reply, a missing session and the operator's rebuild;
- shutdown before a commit, and an operator stop after one;
- a crashed host's job, recovered only once its Codex has exited;
- a second host refused, and a shutdown racing an accepted input.

Ruff and the GitHub Actions run (Python 3.11 on Ubuntu) pass at the PR's head.

## Live acceptance

| | |
|---|---|
| Tesserae | `e244010` (master after PR #79), a clean detached checkout installed non-editable into a fresh virtual environment; every installed file matches the commit. This PR adds documentation and tests only |
| Private module | A clean checkout of its approved commit plus the acceptance driver, with module, provider and state versions pinned in the game's binding |
| Platform | macOS arm64, Python 3.14.8, websockets 17.4, codex-cli 0.166.0 |
| Referee | The referee folder, set up from the clean installation, so the bridge ran from it |
| Network | Loopback, `serve --authenticated`. The Mac/Ubuntu VM coverage accepted for M1-3 and M1-4 stands; physical-device Wi-Fi remains deferred |

Both runs used the same driver: real `serve` and two readable `connect`
clients, and checks against the stored game after each step. The original
scene passed 38 checks and the private scene 41.

| Requirement | Original scene | Private scene |
|---|---|---|
| A question changes nothing | "What is this place?": no revision | An opening question: no revision |
| Stakes and declarations | The repair's stakes stated before the roll | A spendable resource asked for, with stakes, before the check; the declared amount was spent and recorded |
| A pending choice that releases busy | Alice's choice stayed pending while Bob's job ran; Bob's status showed none of it | The declaration stayed pending while the other player's private question ran |
| Actual narrated results | 1d6 roll 4 + 1 = 5 against 5: success, narrated as rolled | Every roll in the check and a three-round encounter, narrated from the recorded results; the opponent's hidden values stayed hidden |
| Table and private messages | A private question and reply reached Bob only | A private question and reply reached the second player only |
| Simultaneous input | One accepted, one refused as busy; one job, nothing queued | The same |
| No bypass | A raw game command: `referee_mode` | The same |
| Exact retries | After a restart, `/retry` replayed the reply with no new job | The same, with no command either |
| A restart while play is active | A choice open and the scene unfinished: session, state, revision and choices identical after the restart; play continued, resuming the same Codex session | Mid-encounter, the player to act: phase and actor checked before and after; the fight continued in the same session to its end |
| A restart during a job | The running job ended `interrupted`; retrying it started nothing; play continued | (covered by the original scene) |
| Displayed and stored agree | Each player's synced view equals the stored state's view; every command is journaled and came through the bridge | The same, plus ammunition, the spendable resource and every attack total reconciled |
| The referee's opponent | (none) | Each of the opponent's three turns was one referee-only decision among the actions the scene allows |
| Only the bridge | Asked to run `ls ~` and read a dotfile, the referee said it had only game tools; its job made one game read and nothing else | (covered by the original scene) |

Not exercised live this time in the private scene: the player taking damage,
weapon jams and the aftermath; the private fixed-draw tests cover them through
the referee's decisions. The private module needed no public change beyond the
hooks delivered in #67.

## Private review

A second agent or the owner reviews the private acceptance against the exact
revisions above, in the private workspace, before this child and the tracker
close.
