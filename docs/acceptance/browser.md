# M2-1 browser acceptance

Tracker [#82](https://github.com/coghex/tesserae/issues/82): the localhost
browser page passed on 2026-11-01 with both demonstration scenes, the original
Relay scene and the approved private scene. Two players used two independent
browser contexts on the host computer, typing plain text to a real Codex
referee; live dice were kept as rolled. The private scene's record, with its
module, live values, fingerprints and logs, stays in the owner's local private
workspace and is reviewed there. How to run it is in the
[browser runbook](../browser-runbook.md).

| Child | Delivered by |
|---|---|
| #83: the browser contract | PR #89 |
| #84: serving the Console and Settings shell | PR #90 |
| #85: joining and conversation | PR #91 |
| #86: state and pending questions | PR #92 |
| #87: recovery and exact retries | PR #93 |
| #88: this record, the runbook and the live acceptance | This PR and a private acceptance record |

## Public checks

The public suite runs the page in Chromium with Playwright in CI's `browser`
job, against a scripted stand-in for Codex, so CI needs no model account. It
covers the serving rules (Host, Origin, methods, assets, headers), the shell,
joining and refusals, table and private delivery, busy refusal, identity
change, the state panel and pending questions, and every recovery path:
a lost input resent exactly once, an acceptance confirmed after reconnecting,
a newer input from elsewhere, refresh, the resend target after a busy refusal,
and a host restart. Ruff and both CI jobs pass at the PR's head.

## Live acceptance

| | |
|---|---|
| Tesserae | `efb8528` (master after PR #93), a clean detached checkout installed non-editable with its browser test extra into a fresh virtual environment; every installed file matches the commit, and every page asset served matches both. This PR adds documentation only |
| Private module | A clean checkout of its approved commit plus the browser acceptance driver, with module, provider and state versions pinned in the game's binding |
| Platform | macOS arm64, Python 3.14.8, websockets 17.4, codex-cli 0.166.0, Playwright 1.64.1, Chromium 156 (headless) |
| Referee | The referee folder, set up from the clean installation, so the bridge ran from it |
| Network | Loopback, `serve --authenticated --browser`, restarted on the same port |

Both runs used the same driver: the real host, two Chromium contexts typing
into the page and reading it, and checks against the stored game after each
step. The original scene passed 69 checks and the private scene 91.

| Requirement | Original scene | Private scene |
|---|---|---|
| The page | Installed assets served; Console by default; exactly two items; keyboard collapse; a reload opens Console; a narrow window starts collapsed and fits | The same; the module's view shown as a read-only tree |
| Joining | One player by token file, one by paste; no token left in either page | The same |
| A question changes nothing | "What is this place?": no revision | An opening question: no revision |
| Stakes and a pending choice | The repair's stakes, then a question shown only in its player's panel, with no revision | A spendable resource asked for before the check, shown only to its player; the declared amount spent and recorded |
| Pending input releases busy | The other player's input ran while the choice waited; meanwhile the first player drafted, opened Settings, collapsed the column and came back: one connection, the draft kept, nothing sent | The other player's private question ran while the declaration waited |
| Table and private messages | A private question and reply reached that player only, marked Private; the other page received neither | The same |
| Simultaneous input | One accepted, one refused as busy with its text kept; one job, nothing queued | The same |
| No bypass | A raw game command: `referee_mode` | The same |
| A browser and a CLI player | A third player on `tesserae connect` saw the browser players' words, and theirs reached the browsers | (covered by the original scene) |
| Refresh | After a reload and a new join, the accepted input was shown and the lost draft explained; Resend exactly started no job and no command | The same, mid-encounter |
| A restart while play is active | A choice open and the scene unfinished: both pages kept the conversation, reconnected after the restart, and session, state, revision and choices were identical | Mid-encounter, the player to act: phase and actor checked before and after; the fight continued in the same session to its end |
| A connection lost around an acceptance | The answer's acknowledgment was lost and the connection cut; the job completed meanwhile; on reconnect the host confirmed it: one roll, one spend, no second job | (covered by the original scene) |
| Displayed and stored agree | Each page's view equals the stored state's view after each change; every command journaled and came through the bridge | The same, plus ammunition, the spendable resource and every attack total reconciled |
| Revocation | A revoked player's next input was refused, and the page cleared and said so | (covered by the original scene) |

**Cleanup.** After each run, on success or failure alike, the driver closes
the browser, reaps every process it started, revokes every player credential
and removes the token files, then records that none is left. Both runs'
records say so, and a private test drives a failing scene with the real host,
a joined page and a CLI player and checks the same.

Not exercised live this time in the private scene: the player taking damage,
weapon jams, reloading and the aftermath; the private fixed-draw tests cover
them. The private module needed no change for the browser.

## Private review

A second agent or the owner reviews the private acceptance against the exact
revisions above, in the private workspace, before this child and the tracker
close.
