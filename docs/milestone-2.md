# Milestone 2: browser player interface

Status: in progress. Milestone 1 is complete through PR #80. M2-1's page is
delivered: `serve --browser` serves Console and Settings, and players at the
host computer join, talk to the referee, see their state and recover from
dropped connections and refreshes (#84 to #87). The
[browser runbook](browser-runbook.md) explains it and the
[acceptance record](acceptance/browser.md) shows both scenes played in the
browser. M2-2, host identity and campaign selection, is delivered too: the
[lobby runbook](lobby-runbook.md) explains it and the
[acceptance record](acceptance/lobby.md) shows CLI and browser players in two
campaigns at once.
The first agreed browser tracker is M2-1
([#82](https://github.com/coghex/tesserae/issues/82)), the
[localhost Console/Settings client](designs/browser_player_design.md), with
its exact contracts in the [browser contract](designs/browser_contract.md).

## First approved slice: M2-1

Use the existing foreground host, personal tokens, recipient projections and
resumable Codex referee from a browser on the host computer. The left column
is collapsible and contains Console and Settings; Console is selected/open
by default. The owner selected localhost-only, console first and memory-only
tokens initially. Other computers continue to use the existing encrypted CLI
connection until browser LAN access is delivered.

Console provides conversation, explicit private input, referee status, pending
questions and read-only player state. Settings initially provides connection
and identity controls. Navigation shares one session and preserves the draft
within the open page. After refresh, rejoin with a token and recover accepted
input/history from the host. Unsent text is not persisted in browser storage.

The delivery draft proposes six children, from the browser contract and
packaged two-screen shell to conversation, state, recovery and combined
acceptance. Completion requires both existing demonstration scenes, two
browser identities and browser/CLI compatibility, exact retry and active-play
recovery, plus focused Chromium CI coverage and private local review.

## Second slice: M2-2

A stable host player authenticates once, sees only accessible active campaigns,
and selects one per connection. The [host lobby design](designs/host_lobby_design.md)
and tracker [#95](https://github.com/coghex/tesserae/issues/95) separate the
local registry foundation (#96), registry-backed foreground host (#97), CLI
selection (#98), browser selection (#99) and mixed-client acceptance (#100).
The [registry guide](host-registry.md) covers the operator tools and
`serve --registry`, the lobby host implemented by #97 under the
[lobby contract](designs/lobby_contract.md), `tesserae connect --lobby`
(#98) from the CLI and the browser's login and campaign picker
(`serve --registry --browser`, #99); the mixed-client acceptance (#100)
passed and is recorded in [acceptance/lobby.md](acceptance/lobby.md).
Existing single-game connections remain usable.

Character selection/creation, multiple characters per player and joining an
ongoing scenario are the next direction after this foundation; their rules
and private campaign integration are not hidden requirements of M2-2. There is
no universal character attributes schema. Browser login remains memory-only,
and the first lobby stays localhost-only (the VM may use an SSH tunnel).

## Third slice: M2-3

Characters, creation and scoped play: tracker
[#110](https://github.com/coghex/tesserae/issues/110), planned in the
[character lifecycle design](designs/character_lifecycle_design.md), with its
exact rules in the [character contract](designs/character_contract.md)
(CHR-1, #111). A returning player picks a ready character; a newcomer creates
one privately with the referee, approves it and enters play. Characters are
served by the lobby host only. CHR-2 (#112) added the stored characters, the
module capability, seed version 2 and the original
[Trailhead](trailhead.md) module; the referee, host and client slices
follow.

## Later browser work

LAN certificate trust and interactive maps follow M2-1. Their exact scope and
delivery order have not been selected. Remembered login, potentially through
server-managed cookies, also needs a later design covering expiry, revocation
and automatic credential handling. Beyond M2-2, no additional browser slice
is approved by this overview alone.

Do not expand game mechanics, introduce a universal sheet schema or bypass the
referee to demonstrate browser controls. The private module/content and its
full evidence stay outside the public repository.
