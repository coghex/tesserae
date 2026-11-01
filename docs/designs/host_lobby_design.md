# M2-2: host identity and campaign lobby

Status: owner-approved direction (2026-11-01); delivery starts with the local
registry foundation. Network lobby and client selection are not implemented
by that foundation. M2-1 is complete through PR #94. Tracker:
[#95](https://github.com/coghex/tesserae/issues/95); children #96–#100.
[Operator guide and API limits](../host-registry.md).

## Player experience

Authenticate with a personal host token, choose an accessible active campaign,
then choose or create a character before entering play. A player may own any
number of characters in a campaign. The CLI and browser expose the same host
operations. The active campaign/character belongs to the connection, not a
global setting on the player or token.

Character selection, creation and shared-character gameplay follow this
tracker. Their common identity/lifecycle record will be small; rules modules
continue to own character fields, creation rules and mechanical behavior.
The standalone dice utility remains useful without a character.

The eventual acceptance flow starts a returning player through the CLI. A
second browser player joins partway through an ongoing scenario, creates a
character with the referee, and enters that scenario once ready. Creation
questions end their referee jobs so other players can interact between answers.
This tracker supplies the identity/campaign boundary needed by that flow.

## Storage and authority

- A separate, explicitly initialized host-registry SQLite file owns a stable
  host ID, players, hashed revocable credentials, registered campaign locations
  and campaign access grants. It contains no sheets, rules or referee context.
- A token authenticates a stable host player. Rotation/revocation leaves the
  player and campaign grants intact. Display names are labels, never identity.
- A campaign is an existing Tesserae game database, identified by its persisted
  game ID. Operator registration resolves its absolute path and validates its
  identity/schema read-only, without loading module code or migrating it.
  Duplicate game IDs or canonical paths are refused, including copied databases
  with the same ID. Creating a new campaign remains `game create`.
- Campaigns are active or inactive; newly registered campaigns are inactive
  until the operator activates them. Activation expresses operator availability,
  not proof that a runtime is currently serving the file. Runtime admission
  must reopen/verify the registered ID and module before use.
- Explicit grants determine which active campaigns a player can discover and
  enter. Operator records include paths; player summaries contain only campaign
  IDs and titles. Inaccessible campaigns and server filesystem paths stay out
  of player payloads. No client supplies a database or module path.
- Every protected read/admission rechecks the exact host credential and access
  grant. A host context is a separate type from a game participant context;
  neither a client-supplied ID nor a display-name match grants authority.

Registry writes are transactional. The initial registry uses SQLite's DELETE
rollback journal. Its short authorization read guard holds a consistent
credential/grant snapshot through the protected operation; registry revocation
cannot commit until the guard ends. It must never be held over a model job or
human question. HOST-2 must test this ordering across actual game commits and
bridge calls, not claim that writes to two independent databases are atomic.

Game state, module binding, committed actions, history, pending questions and
the Codex referee session remain in/per their campaign. Separate campaigns
have independent busy state and model sessions. Selecting a campaign never
executes gameplay or resets resources.

## Migration and compatibility

The current `serve --database` and version-1 clients/tokens keep their existing
single-game meaning. The registry foundation does not change those messages,
accept host tokens there, or reinterpret old game tokens as host identities.

HOST-2 specifies an explicit registry-serving mode and versioned lobby contract
before implementation. It must persist the mapping from a host player to a
campaign's existing participant ID, retaining ownership, history and replay
namespaces. Linking an existing participant is an explicit operator action;
unmatched players get a stable participant without another bearer token.
Retrying admission must not create duplicate participants. Names never merge
identities. Registry administration alone never changes a campaign database.

Accepting an input freezes its campaign and later its character context.
Switching selections cannot redirect it, replay it in another campaign or
transfer its private output to the new selection. A newly selected campaign
loads only that recipient's state/history. Identity changes clear old caches.

The first new lobby runtime remains localhost-only, compatible with the
development VM's SSH tunnel. Direct browser LAN/TLS trust, remembered login,
maps, queues, public internet accounts and browser operator administration are
separate work. Private content and campaign migration remain external.

## Delivery order

1. **HOST-1 / #96: local registry and operator bootstrap.** Explicit create/open;
   players and token files; register/activate campaigns; grant/revoke access;
   credential-scoped catalog and short authorization guard. No network changes.
2. **HOST-2 / #97: authenticated multi-campaign host.** Specify the lobby protocol and
   participant-link migration, then implement one foreground registry listener,
   authenticated listing/selection, isolated campaign services, credential and
   grant rechecks and safe frozen request/replay context. Preserve legacy mode.
3. **HOST-3 / #98: CLI campaign selection.** Login/lobby, campaign list/join/leave,
   visible selection, errors and reconnect without unintended gameplay.
4. **HOST-4 / #99: browser campaign selection.** Token entry before campaign choice,
   accessible campaign picker, selected-campaign Console/Settings, shared
   connection and recipient/cache isolation. Can follow HOST-2 alongside HOST-3.
5. **HOST-5 / #100: mixed-client acceptance and runbook.** Two original campaigns,
   different grants, one token across both, distinct users, rotation/revocation,
   switching/recovery and no cross-campaign history, actions or private output.

HOST-1 -> HOST-2 -> {HOST-3, HOST-4} -> HOST-5. Character lifecycle and the
private campaign continuation are subsequent trackers, not hidden additions
to host identity. Each delivery uses the ordinary manual project workflow.

## First delivery operator commands

```text
tesserae host init --registry host.sqlite3
tesserae host player add --registry host.sqlite3 --name Alice --token-file alice.token
tesserae host player list|rotate|revoke --registry host.sqlite3 ...
tesserae host campaign add --registry host.sqlite3 --database game.sqlite3 --name Harbor
tesserae host campaign list|activate|deactivate --registry host.sqlite3 ...
tesserae host campaign grant|revoke --registry host.sqlite3 CAMPAIGN_ID PLAYER_ID
tesserae host campaign members --registry host.sqlite3 CAMPAIGN_ID
```

Only `host init` creates a registry; missing/mistyped files and unrelated or
future-schema databases are refused. Token files use the existing private,
exclusive-create writer and are removed if issuance fails. Raw credentials
never appear in registry tables, output or errors. These commands are local
operator tools, not the future player's interactive `/campaign` commands.
