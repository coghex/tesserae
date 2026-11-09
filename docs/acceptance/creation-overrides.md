# OVR-10 negotiated creation overrides acceptance

Tracker [#172](https://github.com/coghex/tesserae/issues/172): negotiated
creation overrides passed end to end on 2026-11-09. One lobby host served an
original Trailhead campaign to a campaign host with no character and three
guests, through both clients, at the same time. Under the
[model-free testing policy](../testing.md) the referee was **scripted**: a
deterministic stand-in for `codex`, launched per job by the host's real
runner and acting only through the real bridge. No model, AI account, login
or inference was used. So this shows the application's integration, consent
and routing end to end, not how a model negotiates. Only original public
material was used. The full record, with transcript, databases and logs,
stays in the owner's local private workspace and is reviewed there. How to
use overrides is in the [governance runbook](../governance-runbook.md#4-requests-and-decisions).

| Child | Delivered by |
|---|---|
| #173: the contract | PR #184 |
| #174: module capability and category policy | PR #185 |
| #175: offers and acceptance in the store | PR #186 |
| #176: the referee | PR #187 |
| #177: CLI controls | PR #188 |
| #178: browser controls | PR #189 |
| #190: the overrides block fits the context | PR #191 |
| #192: a host's verdict straight from a notice (found by this acceptance) | PR #193 |
| #182: this record, the runbook and the acceptance | This PR and a private acceptance record |

## Public checks

Each slice's tests run the real store, bridge, lobby host, CLI processes and
Chromium with scripted referees. Ruff and both CI jobs pass at the merged
commit.

## Acceptance run

| | |
|---|---|
| Tesserae | `65bbfa1` (master after #193), a clean detached checkout installed non-editable with its browser test extra into a fresh virtual environment. Every installed file (39) matches the commit, and every page asset served matches both. This PR adds documentation only |
| Earlier installation | `c8a954c`, the last release before overrides, installed the same way, to make the campaign that is then upgraded |
| Driver | A local acceptance driver, its scripted referee stand-in and its cleanup test, at a fixed private commit, run from a clean checkout of it |
| Referee | Scripted: the stand-in as `codex` on the host's `PATH`, with a referee folder of the run's own (set up by each installation in turn and removed afterwards). Failing `claude` and `openai` stubs on the same `PATH` recorded no attempt |
| Platform | macOS arm64, Python 3.14.8, websockets 17.4, Playwright 1.64.1, Chromium 156 (headless) |
| Network | Loopback only: `serve --registry --browser` from each installation, restarted on the same port, and `serve --authenticated --database` for the single-game checks |

The driver ran real processes from the clean installations: the hosts,
operator commands, readable `connect --lobby` clients, Chromium pages and raw
protocol requests. It checked the stores after each step and passed all 97
checks; all 19 referee jobs completed through the stand-in.

**Setup.** A Trailhead campaign from a version 2 seed, made with the earlier
installation: Alice's participant with two ready walkers, Mara and Wren; Hana
the campaign host with no character; Bob and Dora guests; referee mode on.
On that installation Alice's draft Kit asked for a legacy starting bonus,
which Hana approved; Bob's draft Ash asked for one, left pending. Trailhead
has two quality families, endurance (a replaced roll and a modifier) and
supplies (gear and picks), and no classes or levels. A Relay campaign that
does not adopt overrides shared the registry.

| Requirement | Observed |
|---|---|
| The upgrade | Served by the pinned installation, the schema 9 file became schema 10. The module state and every settings revision were unchanged, and Kit's approved exception stayed approved. Ash's pending request was superseded ("Negotiated creation overrides replace this request; ask again"), with nothing applied. A new legacy exception for a draft was refused (`override_required`). Kit, completed after the upgrade, started with its legacy bonus |
| Category policy | A version 2 settings document blocked supplies. Bob's request for gear was refused (`override_blocked`); a modifier request (endurance) went to the host |
| Request, waiting, offer, acceptance | Bob's browser request waited for the host with no job busy, while Alice played a turn as Mara. Hana offered it from her CLI and nothing applied. Bob made an unrelated creation step and Hana activated new settings: the offer stayed current. Bob accepted in his page; exactly the +1 offered applied and his other edits stayed |
| Counteroffer, denial, decline, cancel, revision | Hana counteroffered +1 to Alice's +2 from her page, and Alice accepted it in her CLI: +1 applied. Hana denied a request straight from its notice with a note (this is how #192 was found and fixed), and nothing changed. Alice declined an offer, and nothing changed. Bob withdrew a request in his page (`cancelled`), then revised it: a new request linked to the old one |
| A direct change | Hana, with no character and no request, applied a supplies change (blocked to guests) from her CLI; the history records it as `direct` |
| A relevant change | Hana's direct replacement of the same roll staled an open offer at once (`preview_changed`), and the request waited for her again. Her fresh offer, accepted, applied over the current draft with its other changes kept |
| Retries, conflicts, consent | An exact retry returned the recorded reply; the same request ID with other content was a conflict. Accepting with anything but the offered digest was refused (`stale_preview`), and another guest could not accept the offer (`offer_unavailable`) |
| Races | An acceptance and a withdrawal sent at once: exactly one won, here the withdrawal, and the acceptance was refused with nothing applied. Two decisions on one request sent at once: exactly one was taken, and the other was refused (`proposal_decided`). An accepted offer cannot be withdrawn; nothing applied is rolled back |
| Verdict recovery | A verdict reached while Dora was disconnected was listed by her client on return |
| Host transfer | Hana transferred the campaign to Alice. Dora's request awaiting a verdict waited for Alice, who offered it, and Dora accepted without asking again. Hana's open offer to Bob no longer held (`host_changed`), and accepting it was refused as stale. Alice's fresh offer, accepted in Bob's page, applied; no request was asked twice. The operator then made Hana host again |
| Access loss | With Dora's access revoked while her offer was open, her acceptance was refused, her client left the campaign, and nothing applied. Restored, she accepted the same offer |
| Privacy | A guest listed only her own requests, and another's was unavailable to her. Nothing of Alice's or Dora's requests reached Bob's page |
| Allowances and completion | A direct grant of two gear picks; Bob spent one and approved the walker. The unused pick expired at completion, recorded with what remained. The item picked and every authorization stayed in the history, and a ready walker could not pick in play. Completing a draft ended its open request (`stale`), and archiving one cancelled its request |
| The referee | Hana's own referee job offered a request on her explicit words; a guest's job could not use the host's tools |
| Restart | After a host restart, Bob's page reconnected and listed his applied requests, Alice's CLI listed her ended ones, the host paged every request, and a referee job read the requests through the bridge |
| Budgets | Every job's composed context fit one tool result: the largest was 14,092 of 45,056 bytes, with an overrides block of at most 997 of 10,240. A discovery page was 1,432 of 16,384 bytes. A three-change preview was 1,614 of 12,288. A request's detail with a 1,000-character explanation was 3,161 of 45,056 |
| Compatibility | The Relay campaign had no override categories and refused the override messages (`override_unsupported`). A single-game host served a Relay game to a game-token player as before, and `tesserae roll` rolled |
| Model-free | The stand-in answered all 19 jobs through the real bridge's tools; no other agent command was attempted |
| The pages | No script error in any page |

**Cleanup.** On success and failure alike, the driver does the following,
then records that nothing is left:
- closes the browser;
- stops and reaps every process it started;
- revokes every host and game token it issued, and removes its token files;
- keeps its referee diagnostics and removes its own referee folder.

This run's record says so. The driver's cleanup test fails the scenario at
five points and checks the same each time:
- mid-way, with the host running, the campaign host's page open and a CLI
  request waiting for the host;
- before anything is served;
- in its summary;
- while keeping the referee diagnostics;
- in its preflight.

## Remaining limits

- The referee was scripted: this says nothing about how a model negotiates,
  explains or relays offers. Live model play needs an explicit owner request.
- Loopback only, and one original module. The private campaign's adapter
  and its upgrade belong to the private tracker; this run does not accept
  them.
- A race's outcome is whichever request the host serialized first. This run
  saw the withdrawal win; an earlier development run of the same driver saw
  the acceptance win and the late withdrawal refused, with nothing rolled
  back.

## Private review

A second agent or the owner reviews the private record against the exact
revisions above, in the private workspace, before this child and the tracker
close.
