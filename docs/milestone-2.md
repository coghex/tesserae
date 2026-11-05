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
[Trailhead](trailhead.md) module; CHR-3 (#113) the referee's frozen actors and
private, step-by-step creation; CHR-4 (#114) the lobby host's character
messages and per-connection actors; CHR-5 (#115) the CLI's character
commands; CHR-6 (#116) the browser's character picker and creation
workspace. CHR-7 (#117) accepted the mixed CLI and browser flow with a scripted
referee, under the [model-free testing policy](testing.md): the
[character runbook](character-runbook.md) explains it and the
[acceptance record](acceptance/characters.md) shows it. The private module's
character rules and the private campaign continuation follow separately.

## Fourth slice planned: M2-4

The owner selected campaign hosts, rules and administration after M2-3.
The [delivery plan](designs/campaign_authority_design.md), tracker
[#131](https://github.com/coghex/tesserae/issues/131), records one host
player per campaign, a database-backed rules document with file import/export,
module-defined requirements, host decisions on guest requests, and broad host
inspection/editing of sheets and world data. The host can own no characters
and still administer from either CLI or browser. The host can inspect private
conversations and referee preparation; other guests retain their privacy.

These are agreed product decisions. Delivered so far: GOV-2
([#133](https://github.com/coghex/tesserae/issues/133)), one campaign host per
lobby campaign, with operator setup, adoption, recovery and transfer, and the
host's inspection of every sheet, conversation and the referee's preparation
through the protocol; the host's client controls come with GOV-6 and GOV-7.
GOV-3 ([#134](https://github.com/coghex/tesserae/issues/134)) delivers the
campaign settings document (import, export, list, activation with exact
replay and audit, and `tesserae game rules`), the optional module settings
and requirements capability, the effective policy every character rule and
the referee's context see, and Trailhead's starting stamina bonus with
per-walker exceptions.
GOV-4 ([#135](https://github.com/coghex/tesserae/issues/135)) delivers host
administration through the protocol: a module's `Administration` capability,
`host_command` on another player's character, NPCs and the scene with exact
replay, stale-revision and busy-rule protection and an audit row, and the
`host_operations` and `host_audit` reads; Trailhead demonstrates corrections,
gear, descriptions, NPCs, the scene and referee-only preparation.
GOV-5 ([#136](https://github.com/coghex/tesserae/issues/136)) delivers guest
requests for host decisions: a referee job proposes an exact exception,
campaign setting or host operation and ends; the requester and the host are
told; only the current campaign host approves or denies it, exactly once and
against the state it was proposed for; the host's own jobs get host tools
checked again at every call; and Trailhead records agreed narrative facts
with their provenance. GOV-6 ([#137](https://github.com/coghex/tesserae/issues/137))
delivers them in the CLI: roles, rules and requirements, requests and exact
decisions, host inspection, confirmed administration and transfer, with
stale, busy and privilege-loss refusals, and GOV-7
([#138](https://github.com/coghex/tesserae/issues/138)) in the browser's
Console and Settings.
The filed public slices begin with exact contracts, then authority, configuration,
administration, requests/referee context, the two clients and model-free
acceptance (#132–#139); the
[governance contract](designs/governance_contract.md) (GOV-1) gives their
exact rules. Existing Console/Settings navigation,
memory-only login and localhost scope remain.

## Subsequent private integration

A companion design in the external private workspace covers source-grounded
creation requirements and scripts, host administration, an explicit campaign
import and model-free mixed-client acceptance. It is tracked as M2-5,
[#140](https://github.com/coghex/tesserae/issues/140), with children #141–#147.
It depends on the new public contracts and has its own private implementation
and named-head review. An owner-requested live playtest follows separately;
private source data, paths and evidence do not belong in this repository.
Neither the public framework acceptance nor this overview implements that
private integration or resumes the original campaign.

## Later browser work

LAN certificate trust and interactive maps follow M2-1. Their exact scope and
delivery order have not been selected. Remembered login, potentially through
server-managed cookies, also needs a later design covering expiry, revocation
and automatic credential handling. This overview does not authorize additional
browser features beyond the agreed slices and their reviewed delivery plans.

Do not expand game mechanics, introduce a universal sheet schema or bypass the
referee to demonstrate browser controls. The private module/content and its
full evidence stay outside the public repository.
