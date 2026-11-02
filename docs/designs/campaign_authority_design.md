# M2-4 draft: campaign hosts, rules and administration

Status: design draft, 2026-11-02; not implemented and no tracker filed.
The owner approved the product decisions below. Delivery slices and technical
proposals still need review. Public baseline:
`51f53e14816dd7b1fefa1f3c891e0e2e26d9abfa` (M2-3 and its CLI follow-up merged).
See [D-25](../tesserae_design.md#d-25-campaign-hosts-rules-and-administration),
the [character contract](character_contract.md), and the
[lobby contract](lobby_contract.md).

## Outcome and public/private split

A campaign has one host player, independent of character ownership. The host
sets its rules, decides guest requests and administers its characters and world
through either client. A referee negotiates narrative details with players and
uses approved mechanical settings through installed Python operations.

Two deliveries keep implementation and source-specific material separate:

| Delivery | Contents | Completion evidence |
|---|---|---|
| M2-4, public framework | Host authority, configuration, requirements data, approval requests, host inspection/editing and both clients; original demonstration | Model-free mixed-client acceptance and runbook |
| Subsequent private integration | Source-grounded creation data and scripts, campaign conversion and continuation preparation | Named-head private review and scripted acceptance; owner-requested gameplay separately |

The private design stays in the existing external local repository. Public
documents, examples, issues and tests contain only generic/original material;
no private paths, game names, values, rules, tables or campaign records.
Private work can research/specify in parallel, but its integration depends on
the reviewed public contracts. Neither delivery silently completes the other.

## Owner decisions

- Every new lobby campaign designates exactly one player as its host when
  established. The host may have zero, one or many characters. All other
  admitted players are guests. Authority follows authenticated player identity,
  never a character, name, connection count or claim in conversation.
- The host has ultimate campaign authority. Guest restrictions can be changed
  or waived by that host, including for the host's own characters without a
  second person's approval. No co-host role or general permission hierarchy is
  needed for this delivery.
- Include broad host sheet/world administration now, not just creation
  approvals. The host may inspect all sheets, private creation/conversation
  records and referee-only preparation. This explicitly extends M2-3's
  creator-only privacy rule: private material is available to its recipient
  and the host, never automatically to other guests or the table.
- Establish campaign rules conversationally with the host, present a concrete
  configuration, obtain approval, then activate it. Initially setup happens
  with the owner's assistant and local operator tools; a setup wizard is not
  required. During play guests can ask the referee for exceptions and the
  referee requests a host decision.
- Store one active JSON override document transactionally in the campaign
  database, with file import/export. External file edits are not live changes.
- Module data describes each game's character requirements. No universal role,
  class, level, gender, stat list, inventory or creation sequence is imposed.
  Separate mechanical requirements, narrative guidance and optional details.
- Players may choose or roll narrative details where the module supports those
  methods. The referee asks what matters, negotiates fit and records agreed
  additions; it does not turn every possible field into a mandatory question.
- Only the host approves changes to explicit mechanical requirements. The
  referee can interpret narrative details and ordinary ambiguities, but cannot
  bypass validators, invent numerical outcomes, create scripts or change
  installed Python. A workaround uses supported operations or an agreed
  nonmechanical ruling; otherwise the affected action needs outside development.
- Pending human decisions end their referee job. Other players can continue
  between jobs. Preserve the existing busy refusal while a job actually runs;
  do not introduce a general command queue.

## Verified starting point

- `tesserae/registry.py` schema 1 has stable authenticated players, credentials,
  campaign registrations and grants, but no campaign host designation. Its
  existing registration reads the game database without changing it.
- `tesserae/store.py` and `tesserae/modules.py:CharacterRules` supply owned
  characters, creation commands, pure completion checks and module sheets.
  There is no shared requirements/configuration capability. Most hooks receive
  state and a character ID; `Action` carries a trusted participant/character.
- `tesserae/referee/tools.py` limits bridge operations to the frozen job actor.
  Existing `record_ruling` records an interpretation; it neither authorizes a
  host exception nor changes script validation. Existing choices address a
  participant/character; they are not durable host approval objects.
- `tesserae/referee/runner.py` already disables built-in execution tools and
  uses a read-only sandbox with the game bridge. Preserve that boundary; add
  narrow host-checked bridge tools rather than filesystem or shell access.
- CLI/browser selection and privacy currently have no privileged host view.
  Both require explicit authority-aware inspection, controls and recovery.

## Identity, permissions and compatibility

Persist one authoritative designation naming the registry identity and player
ID. It must not depend on a linked participant retaining one particular
character. Validate the token, campaign access, designation and relevant state
again at the protected read/write boundary. Bridge calls additionally require
the live job grant. Never trust a host flag supplied by a client or model.

Authority is scoped to this campaign. Being its host does not disclose another
campaign, server credentials, bridge tokens or arbitrary filesystem contents.
Host controls are authenticated system operations; ordinary player gameplay
continues through the referee. Both direct host controls and referee-mediated
host instructions use the same checked handlers.

The contract slice must settle the designation's storage ownership and exact
creation/adoption API before implementation. Keep a single authority record;
do not independently edit roles in both SQLite files. Campaign-owned rules,
decisions and effects must commit in the game database. Preserve registry
authorization guards and explicitly sequence any two-file setup; there is no
cross-database atomic transaction.

New lobby campaign establishment requires an explicit host player. Legacy
registered campaigns need an explicit operator designation before new host
controls are enabled; do not infer the first player, character owner or token
holder. Explain any change to registration's previous read-only guarantee.
Offline `game create`, standalone utilities and single-game hosts retain their
existing meaning; they do not implicitly designate a host player.

Exactly one designated identity persists even while its client is offline.
Offline does not promote a guest or stop ordinary play under current rules.
The current host can explicitly transfer authority, and local operator recovery
can replace the designation. A host transfer must
validate the replacement, preserve one host, audit the change and invalidate
old privileges. Never silently grant host authority after credential revocation.
Rotation retains the stable identity. Required access for the host and safe
revoke/deactivate/transfer ordering must be specified in the contract.

Host authority permits explicit control and edits, not invented player consent.
Keep the acting host, target entity and original owner separate in records.
Do not rewrite ownership or old command senders to make administration work.
Normal conversation never implicitly edits a sheet or supplies a guest's
unspoken decisions. A host-requested correction is clearly recorded as such.

## Configuration and module requirements

Use one versioned campaign document with a schema version, module/binding
identity, supported settings, referee guidance and scoped exceptions. Immutable
revision/approval records identify its exact content and approving host. This
extends the SQLite source of truth; an export is a transportable representation,
not a second independently editable live authority.

An import is a candidate until authorized activation validates it. Unknown
mechanical settings, incompatible module versions or unsupported values are
refused clearly; prose does not silently override a Python check. Loading a
file never loads code, changes provider selections or installs a module.

A small optional module capability should provide:

- Defaults and pure validation for supported campaign settings/exceptions.
- JSON requirements data: stable IDs, descriptions, relevant choices/methods
  and simple constraints; mechanical versus narrative/optional distinctions.
- Effective character-specific requirements derived from defaults, approved
  campaign settings and applicable exceptions, consumed by both referee context
  and mechanical validation. Avoid a second hand-maintained rules list.
- Source/support metadata and clear reasons when a requested option requires
  development. Declaring an option is not proof every associated play mechanic
  is implemented.

For the original demonstration, add an adjustable starting allowance and a
character-specific exception to Trailhead, whose characters have no classes or
levels. Retain a module without the optional capability in compatibility tests.
Do not build an expression interpreter, dependency language, form designer or
universal RPG schema. Exact hook names, versioning and trusted policy context
belong in the first contract; existing modules must not silently change meaning.

The host's own requested policy exceptions can apply directly through the same
validated mechanism, without a pending self-approval. Guest limitations are
policy; data shape, referential integrity and the installed operations' supported
capabilities remain necessary for executable state.

## Durable host requests and decisions

The referee can propose a supported policy amendment/exception or a supported
module administrative operation for a guest. The durable request names its
campaign, requester, target, exact proposed values/arguments, scope, explanation,
relevant policy/target revisions and idempotency identity. A change to that
proposal requires a new reviewed proposal, not an approval reused for new args.

Default an individual request to its specified character; changing campaign
defaults requires an explicit campaign-wide proposal. The guest sees that the
request is waiting; the current host receives enough exact context to decide.
Only the authenticated current host can approve or deny. No natural-language
claim of host consent, ordinary `choices.resolve`, narrator ruling or model
reply can manufacture an approval. A conversation can convey the host's explicit
decision, but it must reach a dedicated checked operation with the exact request
ID; clients also provide explicit approve/deny controls.

Approval of a setting commits the decision and effective override together.
Approval of a module administrative operation commits its validated effect and
decision together. Denial changes no mechanics. Requests cannot convey a blanket
host capability to a guest or later referee job. Duplicate submissions/decisions
replay; conflicting IDs and stale proposals are refused without partial changes.
Define cancellation and supersession, including archive/changed ownership and
policy revisions, in the contract. Host transfer leaves requests discoverable
to the new host and removes the former host's ability to decide them.

No model waits for a human inside a running job. Request creation ends the job;
notification/reconnect state is durable. A decision does not automatically
spend resources, draw dice or perform the guest's next action. It is visible in
the next context/read. Approval waiting affects the requested change; other
valid actions and other players remain available.

## Host inspection and administration

Supply module-defined host projections and validated administrative operations
covering characters (including another player's), NPCs and world/scene data.
The framework supplies authentication, targets, revisions, transactions, audit
and delivery; modules supply their data shape and supported edits. Ship broad
inspection and edits in the original example, not just a budget toggle.

Examples of module-specific operations: edit an entity's resources or equipment,
correct its descriptive facts, add/update an NPC, change scene preparation or
world state. Operations have explicit arguments, pure/state validation and
audit results; corrections preserve the original history and identify the host.
No raw SQL, generic unchecked JSON patch, arbitrary file access or script-writing
tool is exposed. An administrative edit is distinguishable from rolled gameplay.

Privileged operations work without a selected/owned character. Their target is
explicit, independent of the ordinary selected play actor. A model can execute
one only for an explicit instruction from a currently authorized host, or the
exact effect of a checked approval. A guest's running job cannot invoke broader
host operations using the referee's infrastructure-level privilege.

Serialize administrative and policy mutations with game commits. Proposed first
delivery: refuse them with a clear busy result while a referee job is active,
then allow retry between jobs; reads and request inspection remain available.
This avoids a queued edit changing the rules or actor beneath a running job.
The contract must test the boundary rather than relying on UI-disabled buttons.
Revisions prevent stale editors overwriting intervening gameplay or host edits.

Changing a rule alone does not rewrite completed sheets. Draft validation uses
current effective settings; a change affecting completion invalidates the prior
displayed approval and requires review. Changes to existing sheets are separate
explicit audited host operations. Never reroll or silently refresh resources.

## Narrative context and privacy

Store agreed character facts and player additions durably, with enough provenance
to distinguish an explicit choice, a roll, an assumption and an agreed revision.
Optional details can be absent or explicitly unspecified; absence is not a
request to fabricate a fact. The referee asks only when a detail helps narrative
integration or a module requirement actually needs it. Prose alone grants no
mechanical effect. Offer rolled or chosen methods from the module data and
record player-authorized rerolls rather than quietly replacing results.

The host's privileged inspection includes private conversations and referee
preparation, including historical campaign records. Guest projections retain
their existing privacy. Clearly explain host visibility to guests during
creation. Host inspection never broadcasts its contents; preserve the private
recipient semantics of ordinary messages. Targeting a host request must not
publish its character details to unrelated guests.

Separate host views from ordinary table/guest snapshots and history. Recheck
authority on every protected read and reconnect; invalidate privileged UI state
when designation, credential or membership changes. Do not retain host-only
data in browser storage or serve it to a former host because it was cached.
No system can erase information a former host already saw; prevent future
delivery. Existing memory-only login and Console/Settings navigation remain.

## Public delivery slices

These are proposed issue boundaries, not filed issues or approved implementation.
Each slice includes focused meaningful tests and its relevant contract/docs.

| Key | Scope | Depends on |
|---|---|---|
| GOV-1 | Exact authority/configuration/requirements/approval/admin contracts; storage ownership, migration and protocol compatibility | None |
| GOV-2 | Single host designation, explicit setup/adoption/recovery, trusted privilege checks and full host inspection | GOV-1 |
| GOV-3 | Versioned campaign document, import/export/activation, optional module requirements/settings capability and policy-aware validation | GOV-1, GOV-2 |
| GOV-4 | Validated host administration across characters/NPCs/world, audit and original module support | GOV-2, GOV-3 |
| GOV-5 | Durable guest proposals and atomic host decisions; bridge/context integration, narrative facts and supported-workaround behavior | GOV-3, GOV-4 |
| GOV-6 | CLI host inspection/admin/rules/request controls, no-character use and recovery | GOV-4, GOV-5 |
| GOV-7 | Browser equivalents in Console/Settings, guest disclosure and privileged-cache isolation | GOV-4, GOV-5 |
| GOV-8 | Combined model-free acceptance, operator/player runbook, compatibility and failure cleanup | GOV-6, GOV-7 |

This arc is larger than M2-3 because broad host administration is included.
Split a child again if its verified contract produces an oversized change;
do not hide private mechanics or a universal editor inside a public child.

## Acceptance and exit boundary

Use only original public data and scripted Python referees through the real
runner/bridge, CLI and Chromium. No live AI agent, account or inference usage.
Pin clean non-editable installs and record exact commits/fingerprints.

Show a host with no character inspecting and editing world/another character,
then a host with multiple characters. A guest creates privately, requests a
supported exception, waits while another player acts, and completes only after
an explicit valid host decision. Exercise approve/deny, unsupported requests,
exact retries, stale policy/draft/editor state, restart and competing decisions.
Show that recording a ruling or claiming approval cannot bypass the validator.

Verify full privileged inspection, guest isolation and host-transfer/revocation
boundaries over real connections, including every projection/history path;
check durable audit and policy/effect atomicity. Verify legacy examples without
the capability, and preserve M2-3 actor/retry/resource invariants.
Failure injection must leave no credentials, token files, owned processes,
sockets, temporary referee folders or browser sessions, including failures in
diagnostic collection. Public acceptance proves the framework, not private
rules correctness or model storytelling.

## Resolved choices and remaining contract work

Owner choices resolved during drafting: database-backed document with file
import/export; broad host sheet/world administration in this delivery; full
host visibility including private creation and referee preparation.

No further product decision blocks this draft. GOV-1 must resolve exact schema,
hook/protocol versions, authority storage/adoption ordering, request lifecycle,
policy revisions, host transfer and edit/busy semantics before code. These are
technical design tasks; flag any choice that would alter the owner decisions.
Issue numbers and a delivery approval are deliberately still absent.
