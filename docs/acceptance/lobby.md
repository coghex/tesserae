# M2-2 campaign lobby acceptance

Tracker [#95](https://github.com/coghex/tesserae/issues/95): host identity
and the campaign lobby passed on 2026-11-01. One lobby host served two
original Relay campaigns to CLI and browser players at once, with a real Codex
referee for each campaign; a single-game host with a game token was checked
alongside. Only original public material was used. The full record, with
transcript, databases and logs, stays in the owner's local private workspace
and is reviewed there. How to run a lobby host is in the
[lobby runbook](../lobby-runbook.md).

| Child | Delivered by |
|---|---|
| #96: the local host registry and operator bootstrap | PR #101 |
| #97: the lobby host and its contract | PR #105 |
| #98: CLI login and campaign selection | PR #107 |
| #99: browser login and campaign picker | PR #108 |
| #100: this record, the runbook and the live acceptance | This PR and a private acceptance record |

## Public checks

The public suite covers the registry and its operator commands, the lobby
contract over raw WebSocket messages (including revocation racing replies,
admission under retries and concurrency, and two players in two campaigns),
the CLI's lobby mode with real client processes, and the browser page in
Chromium against a scripted stand-in for Codex: login, the picker, switching
on one connection with a reply held back past a leave, lost access, revocation
and reconnecting into the same campaign. Ruff and both CI jobs pass at the
merged commit.

## Live acceptance

| | |
|---|---|
| Tesserae | `e9ba1a9` (master after PR #108), a clean detached checkout installed non-editable with its browser test extra into a fresh virtual environment; every installed file matches the commit, and every page asset the lobby host served matches both (the page differing only in naming a lobby host). This PR adds documentation only |
| Driver | A local acceptance driver and its cleanup test, at a fixed private commit, run from a clean checkout of it |
| Platform | macOS arm64, Python 3.14.8, websockets 17.4, codex-cli 0.166.0, Playwright 1.64.1, Chromium 156 (headless) |
| Referee | The referee folder, set up from the clean installation, so each campaign's bridge ran from it |
| Network | Loopback only: `serve --registry --browser`, restarted on the same port, and `serve --authenticated --browser` for the single-game check. The optional VM SSH tunnel was not used |

The driver ran real processes from the clean installation (the host, readable
`connect --lobby` clients and Chromium pages typing into the composer) and
checked the stores after each step. It passed all 95 checks.

**Setup.** A registry with four host players and four campaigns:

| Campaign | Game | State | Granted to |
|---|---|---|---|
| Harbor | Relay, referee mode | Active | Alice, Bob |
| Ridge | Relay, referee mode | Active | Alice, Cara |
| Closed | Dice | Inactive | Alice |
| Moved | Dice | Active; its file moved away after registration | Alice |

Cara already had a Ridge participant with a game token; the operator linked her
host player to it with `host campaign link`. Dana had no grants.

| Requirement | Observed |
|---|---|
| Catalogs | Dana's was empty and said so. Alice saw Harbor, Moved and Ridge, never Closed, in the CLI and the page alike. Bob saw Harbor only. Selecting Moved was refused as unavailable, and nothing was created at its path |
| One token across campaigns, CLI and browser together | Alice's CLI was in Harbor while her page, logged in with the same token, was in Ridge. She was a different participant in each campaign, and Settings named the campaign and player |
| A linked existing participant | Cara entered Ridge as her linked participant; no new one was made |
| Independent referees | Alice's Harbor input (CLI) and Ridge input (page) were both accepted and their jobs ran over the same interval, each in its campaign's own Codex session. Each campaign's players saw only that campaign's words |
| Busy per campaign | While Bob's Harbor job ran, Alice's Harbor input was refused as busy and Cara's Ridge input was accepted and ran; Harbor queued nothing |
| Private output | Bob's private question and its reply went to Bob only, marked Private; Alice (CLI in Harbor, page in Ridge) and Cara received none of it |
| Switching | Alice's page left Ridge, entered Harbor (her visible Harbor history only, nothing of Ridge or of Bob's private exchange) and re-entered Ridge (its history back), all on one connection with no join, as the same participant |
| Exact retry | Alice's CLI left before an answer; the job completed. A new client with `--campaign` and a piped `/retry` showed her last input as completed and replayed its answer: no second job |
| Refresh | Bob reloaded, logged in again and entered Harbor: his last accepted input was shown with the lost-draft note; Resend exactly replayed it with no job and no new message |
| Restart and reconnect | The host stopped cleanly and restarted on the same port; Reconnect returned each page to its campaign as the same participant; Ridge's next job resumed Ridge's own session and Harbor's was unchanged |
| Rotation and revocation while connected | Bob's rotated token: his page cleared on its next input, and the new token logged in with his grant intact. Alice's revoked Ridge grant: her page returned to the picker with the reason, Ridge gone. Deactivated Harbor: Alice's CLI was told and returned to the lobby; Bob's refreshed catalog was empty. Cara's revoked token: her client stopped with `session_revoked` |
| The stores | Per campaign: one participant per host player, every input from its own players, exactly the inputs sent to it, all jobs completed, private messages only to its own participants. Alice had one Harbor link across CLI, page, reconnects and retries |
| Single-game mode | A game-token CLI player talked to the referee and a game-token browser player joined the page. A host token was refused by the single-game host, and a game token by the lobby host |
| The page | No script error in any page |

**Cleanup.** On success and failure alike, the driver closes the browser,
stops and reaps every process it started, revokes every host and game token it
issued and removes the token files, then records that none is left. This
run's record says so, and the driver's cleanup test drives a failing scenario
with the real lobby host, a page and a CLI client and checks the same.

## Remaining limits

- Characters are not part of this tracker. Selecting a campaign enters it as
  the player's participant; character selection and creation, joining an
  ongoing scenario as a new character and the private campaign continuation
  are the next trackers.
- Localhost only. Other computers use the CLI over an SSH tunnel; browser LAN
  access, TLS for the lobby host and remembered logins are later work.
- The registry has no migration, hard delete or location update yet. A
  campaign file must stay where it was registered: a moved copy keeps its
  game ID, so it cannot be registered again.
- Not exercised live: the VM SSH tunnel, a `left` pushed to the CLI by another
  player's delivery, and leaving the page with an unconfirmed input (the last
  two are covered by public tests). The live run asked the referees questions
  only; earlier acceptances covered Relay's actions, choices and arithmetic.

## Private review

A second agent or the owner reviews the private record against the exact
revisions above, in the private workspace, before this child and the tracker
close.
