# Milestone 2: browser player interface

Status: in progress. Milestone 1 is complete through PR #80. M2-1's page is
delivered: `serve --browser` serves Console and Settings, and players at the
host computer join, talk to the referee, see their state and recover from
dropped connections and refreshes (#84 to #87). The
[browser runbook](browser-runbook.md) explains it and the
[acceptance record](acceptance/browser.md) shows both scenes played in the
browser. Later browser work below is not yet selected. The first agreed browser tracker is M2-1
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

## Later browser work

LAN certificate trust and interactive maps follow M2-1. Their exact scope and
delivery order have not been selected. Remembered login, potentially through
server-managed cookies, also needs a later design covering expiry, revocation
and automatic credential handling. No additional browser slice is approved by
this overview alone.

Do not expand game mechanics, introduce a universal sheet schema or bypass the
referee to demonstrate browser controls. The private module/content and its
full evidence stay outside the public repository.
