# M2-4 governance acceptance

Tracker [#131](https://github.com/coghex/tesserae/issues/131): campaign hosts,
rules, requests and administration passed on 2026-11-05. One lobby host
served an original Trailhead campaign to a campaign host with no character,
a CLI guest with seeded characters and a browser guest who created one, at
the same time, through both clients. Under the
[model-free testing policy](../testing.md) the referee was **scripted**: a
deterministic stand-in for `codex`, launched per job by the host's real
runner and acting only through the real bridge, its proposal tool and its
host tools. No model, AI account, login or inference was used, so this shows
the application's integration, routing and authority end to end, not a
model's judgment or how it negotiates. Only original public material was
used. The full record, with transcript, databases and logs, stays in the
owner's local private workspace and is reviewed there. How to run a governed
campaign is in the [governance runbook](../governance-runbook.md).

| Child | Delivered by |
|---|---|
| #132: the governance contract | PR #149 |
| #133: one campaign host, privileged inspection | PR #150 |
| #134: the settings document and module requirements | PR #151 |
| #135: host administration | PR #152 |
| #136: guest requests and host decisions | PR #153 |
| #137: CLI controls | PR #155 |
| #138: browser controls | PR #157 |
| #139: this record, the runbook and the acceptance | This PR and a private acceptance record |

## Public checks

The public suite covers each slice: the registry's designation and its
upgrade, the settings document and module capabilities in the store, host
operations, requests and decisions through the real store and bridge, the
lobby host over raw WebSocket messages, the CLI with real client processes,
and the browser page in Chromium, each with a scripted referee. Ruff and both
CI jobs pass at the merged commit.

## Acceptance run

| | |
|---|---|
| Tesserae | `f9628c3` (master after #157), a clean detached checkout installed non-editable with its browser test extra into a fresh virtual environment; every installed file (37) matches the commit, and every page asset the lobby host served matches both (the page differing only in naming a lobby host). This PR adds documentation only |
| Driver | A local acceptance driver, its scripted referee stand-in and its cleanup test, at a fixed private commit, run from a clean checkout of it |
| Referee | Scripted: the stand-in as `codex` on the host's `PATH`, with a referee folder of the run's own (set up by the pinned install and removed afterwards). Failing `claude` and `openai` stubs on the same `PATH` recorded no attempt |
| Platform | macOS arm64, Python 3.14.8, websockets 17.4, Playwright 1.64.1, Chromium 156 (headless) |
| Network | Loopback only: `serve --registry --browser`, restarted on the same port, and `serve --authenticated --database` for the single-game checks |

The driver ran real processes from the clean installation (the host, operator
commands, readable `connect --lobby` clients, Chromium pages and raw protocol
requests) and checked the stores after each step. It passed all 94 checks;
all 13 referee jobs completed through the stand-in.

**Setup.** A Trailhead campaign from a version 2 seed: Alice's participant
with two ready walkers, Mara and Wren; referee mode on. Registering it
without `--host` was refused; with `--host Hana` it was registered inactive
and hosted by her; Alice and Bob were granted, Alice's player linked to her
seeded participant, then the campaign activated. The operator imported a
settings document (a candidate only), activated it and exported exactly the
same document.

| Requirement | Observed |
|---|---|
| A host with no character | Hana's CLI entered with no character, as host. `/inspect Mara` showed Alice's walker; a stamina correction was shown for confirmation and ran only on `/admin confirm`; a scene edit reached Alice's view. Both were audited as Hana's, Mara stayed Alice's, and the corrections are recorded as the host's, apart from play |
| A guest's request, waiting | Bob's browser created Ash; an unsupported bonus was refused to the referee and nothing was stored; a bonus of 1 was stored pending and shown in his Console; no job stayed busy, and Alice played a turn as Mara meanwhile. His Settings said the campaign host can read his creation, and showed no host data |
| Approval and validation | Hana approved it in her browser's Settings with a note: a new settings revision became active. Approving Ash at the revision Bob had been shown was refused as stale; his page showed the approval and note, re-read the draft, and approving the new revision gave Ash the bonus |
| Denial, and a claim of consent | Alice's "the host agreed" was only a ruling: her job was refused the host tool and nothing changed. Her own request was denied by Hana's own referee job on Hana's explicit words, audited as Hana's via the referee; Kit started with no bonus and kept its agreed background with its provenance |
| Competing, retried and conflicting decisions | Two of Hana's connections decided one request at once: the first won and the second was refused as already decided; an exact retry returned the recorded decision; the same request ID for another decision was a conflict; a guest's decision was refused |
| A host with several characters | Hana created two drafts of her own; her creation job set the referee's preparation through the host tool |
| Inspection and isolation | Hana's `/history-all` showed Bob's private creation and to whom it went; Alice never saw it; a guest's inspection was refused; nothing of the preparation or of any host view reached Bob's page |
| Stale state | An edit confirmed after the game moved on was refused and changed nothing; a retried import returned the same candidate; the page's activation, made against older settings, was refused as stale |
| Transfer, recovery, revocation, restart | `/transfer` (confirmed) made Alice host and told her at once; Hana's edit was refused and her page cleared its host panels by itself. The operator's `set-host` restored Hana, noticed at her next request; the registry recorded all three designations and who made them. Rotating Hana's token ended her old session; with the new one she was host still. After a host restart Bob's page reconnected to Ash and Alice's decided requests were still listed |
| The stores | Two requests approved and one denied; one audit row per decision naming its request; each approval's effect a settings revision made from it; exactly one revision active; operations, imports, activations and decisions audited |
| M2-3 invariants and legacy | `/retry` replayed Alice's last input with no second job; no settings change refreshed or reallocated a ready walker; a single-game host refused the character game, has no campaign host, and served a Relay game with a game token as before; `tesserae roll` rolled |
| Model-free | The stand-in answered all 13 jobs through the real bridge's tools; no other agent command was attempted |
| The pages | No script error in any page |

**Cleanup.** On success and failure alike, the driver closes the browser,
stops and reaps every process it started, revokes every host and game token
it issued, removes its token files, keeps its referee diagnostics and removes
its own referee folder, then records that none is left. This run's record
says so. The driver's cleanup test fails the scenario mid-way (with the host
running, the campaign host's page inspecting and a CLI client playing),
before anything is served, in its summary, and while keeping the referee
diagnostics, and checks the same each time.

## Remaining limits

- The referee was scripted: this says nothing about how a model negotiates,
  explains or narrates requests and decisions. Live model play needs an
  explicit owner request.
- Loopback only. The private campaign integration, its module and its
  browser access from another computer belong to the private tracker; this
  original-module run does not accept them.
- One campaign host per campaign, no co-hosts; the browser's administration
  takes arguments as JSON.

## Private review

A second agent or the owner reviews the private record against the exact
revisions above, in the private workspace, before this child and the tracker
close.
