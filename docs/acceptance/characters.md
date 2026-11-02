# M2-3 character acceptance

Tracker [#110](https://github.com/coghex/tesserae/issues/110): characters,
creation and scoped play passed on 2026-11-02. One lobby host served an
original Trailhead campaign to a CLI player with seeded characters and a
browser newcomer who created one, at the same time. Under the
[model-free testing policy](../testing.md) the referee was **scripted**: a
deterministic stand-in for `codex`, launched per job by the host's real runner
and acting only through the real bridge. No model, AI account, login or
inference was used, so this shows the application's integration and routing
end to end, not a model's judgment or storytelling. Only original public
material was used. The full record, with transcript, databases and logs,
stays in the owner's local private workspace and is reviewed there. How to
run it is in the [character runbook](../character-runbook.md).

| Child | Delivered by |
|---|---|
| #111: the character contract | PR #119 |
| #112: stored characters and the module capability | PR #120 |
| #113: frozen referee actors and private creation | PR #121 |
| #114: the lobby host's character messages | PR #122 |
| #115: CLI character selection and creation | PR #123 |
| #116: the browser picker and creation workspace | PR #124 |
| #117: this record, the runbook and the acceptance | This PR and a private acceptance record |

## Public checks

The public suite covers the store and the module capability, the referee's
frozen actors and private creation through the real runner and bridge, the
lobby host's character messages over raw WebSocket messages, the CLI with real
client processes, and the browser page in Chromium, each with a scripted
referee. Ruff and both CI jobs pass at the merged commit.

Follow-up [#128](https://github.com/coghex/tesserae/issues/128) adds the
module's missing-step list to readable CLI approval errors, matching the
browser. Client-process tests cover that display and the unchanged JSON
reply. The historical acceptance run below predates this display improvement.

## Acceptance run

| | |
|---|---|
| Tesserae | `8730b98` (master after #126), a clean detached checkout installed non-editable with its browser test extra into a fresh virtual environment; every installed file matches the commit, and every page asset the lobby host served matches both (the page differing only in naming a lobby host). This PR adds documentation only |
| Driver | A local acceptance driver, its scripted referee stand-in and its cleanup test, at a fixed private commit, run from a clean checkout of it |
| Referee | Scripted: the stand-in as `codex` on the host's `PATH`, with a referee folder of the run's own (set up by the pinned install and removed afterwards). Failing `claude` and `openai` stubs on the same `PATH` recorded no attempt |
| Platform | macOS arm64, Python 3.14.8, websockets 17.4, Playwright 1.64.1, Chromium 156 (headless) |
| Network | Loopback only: `serve --registry --browser`, restarted on the same port, and `serve --authenticated --database` for the single-game checks |

The driver ran real processes from the clean installation (the host, readable
`connect --lobby` clients, Chromium pages and raw protocol requests) and
checked the store after each step. It passed all 87 checks.

**Setup.** A Trailhead campaign created from a version 2 seed: Alice's
participant with two ready walkers, Mara and Wren; referee mode on; Alice's
host player linked to her seeded participant; Bob new.

| Requirement | Observed |
|---|---|
| A returning CLI player | `--campaign` and `--character Mara`: the client listed Mara and Wren, played Mara, and started the scene; the input was stored as Mara's, in play |
| A browser newcomer, created privately | Bob's page showed the empty picker and took a setup question that changed nothing. He created Ash; each creation answer was one short job whose referee steps set the trait and rolled stamina once, on his draft only. Every creation line was marked private and creating; all 4 creation messages went to Bob only, and Alice's CLI saw none of them |
| Others play between creation answers | No job stayed busy while Bob decided; between his answers Alice pushed on as Mara and spent a stamina |
| Approval and entry | Bob approved the sheet's revision 3; completion rolled nothing (keen added its stamina). His first play input was introduced by the referee at the table, seen by Alice as "Bob as Ash" |
| No draft acting early | No command ran as Ash before approval; Ash's only steps were creation steps |
| One player, two characters | A second CLI connection played Wren while the first played Mara; Mara's question showed only in Mara's status; switching away and back left Mara's stamina as it was |
| Retries and duplicates | A new client's `/retry` replayed Mara's input with no second job; after switching to Wren it resent Wren's own. The same request sent as Wren was a conflict; a duplicate create returned the same draft |
| Incomplete and stale approval | Refused with the module's list of what was missing, then as stale with the current revision |
| Archive | Refused while the referee acted for Wren; then the CLI asked for confirmation and archived her, and the connection playing Wren was told and returned to setup. Wren's old input could not come back as Mara's |
| Recovery | A refreshed page kept nothing in browser storage; after a host restart, Reconnect returned Bob's page to Ash and the CLI played Mara again |
| Relink | Unlinking Alice and re-entering admitted a new participant with no characters; nothing was retried and Mara could not be selected |
| Revocation | A revoked grant returned Bob's page to the picker, cleared; a revoked host token ended Alice's session |
| The store | Every character's owner as expected; all 12 jobs completed; every input acted only as its own participant's characters; questions scoped to their characters; nothing of Ash's creation in Alice's projections |
| Single-game host and legacy | The character game was refused (`serve --database`, exit 2); a Relay game was served with a game token as before; `tesserae roll` rolled |
| Model-free | The scripted stand-in answered all 12 jobs through the real bridge's tools; no other agent command was attempted |
| The page | No script error in any page |

**Cleanup.** On success and failure alike, the driver closes the browser,
stops and reaps every process it started, revokes every host and game token
it issued, removes its token files and its own referee folder, then records
that none is left. This run's record says so, and the driver's cleanup test
fails the scenario mid-way, before anything is served, and in its summary, and
checks the same.

## Remaining limits

- The referee was scripted: this says nothing about how a model narrates,
  judges or paces creation. Live model play needs an explicit owner request.
- The private module's character rules, importing or continuing the private
  campaign and its two-client playtest are separate, later work.
- Localhost only; no transfer, un-archiving or renaming after approval; the
  VM SSH tunnel was not used.

## Private review

A second agent or the owner reviews the private record against the exact
revisions above, in the private workspace, before this child and the tracker
close.
