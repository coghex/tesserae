# M2-6 creation overrides contract (OVR-1)

Status: specification for [#173](https://github.com/coghex/tesserae/issues/173)
under tracker [#172](https://github.com/coghex/tesserae/issues/172), written
before any implementation. It turns the
[negotiated creation overrides design](negotiated_creation_overrides_design.md)
into exact rules for OVR-2 to OVR-6 and the acceptance slices: the optional
module capability, category request policy, the negotiation's records and
states, requester acceptance, direct host application, the messages, bridge
tools and client controls that carry them, their bounds, and the upgrade of
existing games. OVR-2 ([#174](https://github.com/coghex/tesserae/issues/174))
implements sections 3 and 4 and section 14's module; OVR-3
([#175](https://github.com/coghex/tesserae/issues/175)) sections 5 to 10
(`tesserae/overrides.py`, schema 10 and the client messages); OVR-4
([#176](https://github.com/coghex/tesserae/issues/176)) section 11 (the
bridge's tools, the context's `overrides` block and the prompt); OVR-5
([#177](https://github.com/coghex/tesserae/issues/177)) section 12's CLI
([governance runbook](../governance-runbook.md#creation-overrides-from-the-cli));
OVR-6 ([#178](https://github.com/coghex/tesserae/issues/178)) its browser
([governance runbook](../governance-runbook.md#creation-overrides-in-the-browser)).

It extends the [governance contract](governance_contract.md), the
[character contract](character_contract.md) and the
[referee contract](referee_contract.md). Everything they say still holds
wherever this contract does not say otherwise. Section 13 lists exactly
which of their rules change, and only for a module that adopts this
capability. Examples use the original [Trailhead](../trailhead.md) module,
extended as section 14 illustrates; OVR-2 builds the real fixtures.

## 1. Decisions this contract follows

Owner decisions (tracker #172, 2026-11-08), as the design records them:

- Every starting quality an installed module's scripts can represent is in
  scope. Modules group them in their own categories; there is no universal
  list of character concepts.
- A campaign can allow guest requests in one category and block them in
  another. The host can still change any supported quality directly.
- A guest asks; the referee shows the host the request and tells the guest
  to wait; the job ends and others keep playing. The host's verdict returns
  to the guest, who accepts it or tries again.
- **Approval creates an offer; only the requester's acceptance applies it.**
  The host may counteroffer. The whole-sheet approval stays separate.
- Scripts perform every mechanical change. The referee cannot install code
  or invent an executable operation.

Agreed in the design review of PR #171 (2026-11-08):

- Host transfer keeps requests that are waiting for the host; only offers
  the former host made, and the requester has not accepted, become stale.
- An offer goes stale when its effects or the facts they depend on change.
  The requester's own unrelated creation steps, and other players' play,
  leave it usable.
- The host can withdraw an unaccepted offer.
- Completing a character expires its unused starting allowances and
  creation-only waivers. Nothing carries into play implicitly.

Defaults of the design, adopted here: every declared category starts as
`approval_required`; there is no automatic-approval tier; offers never
expire by time; one negotiation is open per requester and draft.

Engineering choices made here, within that scope:

- A negotiation is a proposal in the existing `host_requests` table, of a
  new kind, `creation_override`, with its own state set. Offers and
  application records are new tables. Existing kinds, states and rows keep
  their meaning (section 6).
- The framework never interprets a quality. It stores intents and previews,
  checks authority and policy, compares preview digests, and calls the
  module's hooks. The module owns every quality's meaning and effect.
- **Staleness is the module's preview, not a revision number.** An offer
  stays valid while a fresh preview of its intent has the same digest. A
  module must make a preview depend only on the facts it reports
  (section 3).
- Category policy limits what a guest may *request*. It never limits what
  the host may offer or apply, and changing it never stales an offer.
- Accepting an offer, and the host's direct application, are commands in the
  game's history. They are replayed like commands and advance the game
  revision. Verdicts change no game state and are replayed through
  `host_mutations`, as decisions are now.
- Only drafts are targets. A ready character keeps the existing
  administration and legacy exceptions.

## 2. Terms

| Term | Meaning |
|---|---|
| Quality | One starting value, selection, collection, resource, allowance, rolled result or derived value the module can override. It has a stable `quality_id` and belongs to one category |
| Category | A module-declared group of qualities (`category_id`). Category policy is per category |
| Action | One way a quality can change (`action_id`), with declared parameters, such as `set`, `grant` or `replace_roll`. Names and meaning belong to the module |
| Intent | What someone asks for: a list of `{quality_id, action_id, parameters}` (section 3) |
| Preview | The module's pure, deterministic description of exactly what an intent would do to one draft now, including its canonical `plan` (section 3) |
| Preview digest | SHA-256 of the preview's canonical JSON. It identifies "the same effects" |
| Request | A `creation_override` proposal: the requester's intent and explanation for one of its own drafts |
| Offer | The host's immutable authorization of one intent for that request, with the preview the host reviewed. A request may have several offer revisions |
| Acceptance | The requester's explicit response that applies one exact offer |
| Direct override | The host applying an intent to any draft on its own authority, with a reason and no request |
| Allowance | A bounded permission an applied plan grants for later creation choices, such as "pick two starting tools at no cost". It ends at completion |
| Waiver | An ordinary creation rule the plan sets aside for this draft. A creation-only waiver ends at completion |
| Record | A stored application: an accepted offer, a direct override, or the expiry of allowances at completion |

## 3. The module capability

### The hooks

A module opts in with one new optional field, `Module.creation_overrides`,
holding a `CreationOverrides` (in `tesserae/modules.py`):

```python
@dataclass(frozen=True)
class Category:
    id: str  # [a-z][a-z0-9_]{0,47}
    label: str  # 1 to 80 characters
    description: str  # up to 500 characters


@dataclass(frozen=True)
class OverrideAction:
    state: dict
    character_id: str
    plan: dict  # the preview's plan, exactly
    policy: dict  # the draft's effective policy
    authorization: dict  # built by the host, section 3


@dataclass(frozen=True)
class CreationOverrides:
    version: int  # 1; any other is refused
    categories: tuple[Category, ...]
    # Pure: (state, character_id, query) -> one page of discovery.
    discover: Callable[[dict, str, dict], dict]
    # Pure: (state, character_id, intent, policy) -> the preview.
    preview: Callable[[dict, str, dict, dict], dict]
    # Deterministic: the plan's effect, or a refusal with nothing changed.
    apply: Callable[[OverrideAction], Outcome]
    # Pure: (state, character_id) -> the draft's live allowances.
    allowances: Callable[[dict, str], list]
```

The catalog refuses a module that declares this without
`CharacterRules` version 2. It also refuses no categories, more than 16, or
duplicate or malformed IDs. `HOST_API_VERSION` stays 1. As with the
governance contract's capabilities (section 6 there), a module may add this
under its current version and state version only if a game whose state has
none of its new keys behaves exactly as before: absent override state reads
as "no overrides".

**`discover(state, character_id, query)`** describes what can be
overridden on that draft now. `query` has only these optional string fields:
`category_id`, `quality_id`, `parameter`, `match` (text to filter options by)
and `after` (the module's own opaque cursor). It returns
`{"items": [...], "next": cursor or null}`:

- With no `quality_id`, the items are qualities (in `category_id` if given):
  `{"quality_id", "category_id", "label", "description", "current",
  "actions": [{"action_id", "label", "parameters", "later_random"}]}`.
  - `current` is the draft's current effective value, as the owner's sheet
    would show it, or null.
  - `parameters` maps each parameter name to a description:
    - `{"type": "integer", "minimum", "maximum"}`;
    - `{"type": "string", "max_length"}`;
    - `{"type": "enum", "values"}`;
    - `{"type": "option"}`, whose values are listed by a query with
      `quality_id` and `parameter`.

    Each also has `"required": true or false`.
  - `later_random` says whether the action authorizes a later creation step
    that rolls.
- With `quality_id` and `parameter`, the items are that parameter's
  options, `{"value", "label"}`, filtered by `match`.
- Like `CharacterRules.actions`, discovery is guidance, never a validator:
  only `preview` decides.

A page is at most 50 items and 16,384 result bytes (section 10). A larger page is a
module failure (`internal_error`, logged), never cut.

**`preview(state, character_id, intent, policy)`** is pure and
deterministic. It gets no random source and changes nothing. It returns the
preview, or raises `IntentError(problems)`: a new `ModuleError` sibling with
player-safe problems, refused as `invalid_intent` (section 10). An unknown
quality, action or parameter raises `UnsupportedOverride`, refused as
`override_unsupported`; neither echoes client input.

```json
{"plan": {"...": "module-defined; what apply will do"},
 "headline": "Grants a climbing rope and waives its cost.",
 "affected": [{"quality_id": "starting_gear", "category_id": "supplies"}],
 "effects": [{"quality_id": "starting_gear", "summary": "Adds a climbing rope.",
              "before": ["map"], "after": ["map", "climbing rope"]}],
 "waivers": [{"quality_id": "starting_gear", "summary": "No carrying limit for the rope.",
              "creation_only": true}],
 "allowances": [],
 "later_steps": [],
 "consequences": ["The rope counts as carried gear from the start."],
 "limitations": [],
 "dependencies": {"gear": ["map"]}}
```

- Every field is required; lists may be empty.
- `plan` is what `apply` will do, in the module's own terms. It is never a
  raw state path or a JSON patch for the framework to execute.
- `headline` is one line of up to 200 characters.
- `affected` lists every quality the plan changes, consequential ones
  included, each with its declared category.
- `effects`, `waivers`, `allowances`, `later_steps`, `consequences` and
  `limitations` each have at most 32 entries.
  - Each `summary` or text is 1 to 300 characters.
  - An allowance is `{"allowance_id", "quality_id", "summary",
    "creation_only": true}`.
  - A later step is `{"command", "summary", "stakes"}`. It names an ordinary
    creation step the requester runs afterwards, such as a roll the plan
    permits. That step draws then, once, with its normal retry guarantee.
    Nothing is rolled while negotiating or applying.
  - `limitations` states anything the module genuinely cannot do with this
    intent, rather than claiming it.
- **`dependencies`** holds the facts of the draft, and of its effective
  policy, that the preview was computed from. **The rest of the preview must
  be a function of the intent and `dependencies` alone.** A change elsewhere
  in the state must not change the preview. This is what makes an unrelated
  creation step leave an offer valid. A whole-draft digest is not a
  dependency declaration.
- A preview is at most 12,288 result bytes (section 10), and its `headline`
  at most 600. A larger one is refused as
  `invalid_intent` ("too many changes at once; split the request"), never cut.

**`apply(OverrideAction)`** performs exactly `plan` on the draft and returns
an `Outcome`.
- Its `state` replaces the module state.
- Its `result` is the module's receipt (at most 8,192 result bytes; a
  larger one is a module failure, nothing changed): what was applied,
  with the original values it preserved.
- It is deterministic: `Action.operations` is empty, so nothing rolls.
- It may refuse with `ActionRejected`, `IntentError` or
  `UnsupportedOverride`, and nothing changes.
- It must enforce every technical invariant itself: structural validity,
  real catalog references, supported representations and the module's state
  limits. A host's authority waives game rules, never these.

`authorization` is built by the host only, never from a client, a tool
argument or a model:

```json
{"kind": "accepted_offer" | "direct",
 "record_id": "hr-…", "host_player_id": "…",
 "proposal_id": "hp-…" or null, "offer_id": "ho-…" or null,
 "requester_participant_id": "p_…" or null, "reason": "…"}
```

The module stores what it needs of it as the provenance of the change.

**`allowances(state, character_id)`** lists the draft's live allowances as
`{"allowance_id", "quality_id", "summary", "remaining"}`, where `remaining`
is a module-defined JSON value. The framework calls it at completion
(section 8).

### What a module must guarantee

These are module duties. OVR-2's fixtures and OVR-8/9's private adapter
test them; the framework cannot check them.

1. **Originals stay.** A replaced rolled result keeps its original roll. A
   granted or replaced selection keeps what generation gave. Original,
   accepted and effective values stay distinguishable in the state and on
   the sheet.
2. **One calculation.** Preview, `apply`, ordinary creation steps,
   `problems`, `complete`, the sheet, host inspection and play all use the
   same effective values through the module's own rules. No copy of the
   rule arithmetic is kept for overrides, and a derived cache is
   recalculated, never patched.
3. **Accepted authority persists.** A later ordinary step (a role, an
   allocation, a purchase, a return, a background roll) cannot silently
   reset or erase an applied override, or let the guest go beyond an
   allowance. A step that conflicts with a fixed override is refused with a
   reason. It never claims success while re-applying the old value.
4. **Completion ends creation-only authority.** `complete` keeps the effects
   that were used and their history, and drops every live allowance and
   creation-only waiver (section 8).
5. **Reversal is an intent.** Undoing an applied override is a new intent
   (for example a module action that restores the original), applied
   directly by the host or offered and accepted. It is never a framework
   rollback.
6. **No hidden families.** A quality the scripts can represent but the
   module cannot yet override must be declared with a `limitations` entry
   explaining what is missing. It must not be omitted or hidden behind the
   draft guard.

## 4. Category policy

Rules document version 2 adds one required field to version 1's five
(governance contract section 5):

```json
{"document_version": 2,
 "module": {"id": "trailhead", "version": "1", "state_version": 1},
 "settings": {"starting_stamina_bonus": 0},
 "exceptions": [],
 "guidance": null,
 "overrides": {"categories": {"endurance": "approval_required", "supplies": "blocked"}}}
```

- `overrides.categories` maps declared category IDs to `approval_required`
  or `blocked`.
  - A declared category not listed is `approval_required`.
  - An undeclared ID is `invalid_document`.
  - The document is still checked whole at import and activation; the
    256 KiB cap is unchanged.
- **Version 1 documents** are read exactly as before. For an adopting module
  every category is `approval_required`.
- Version 2 is accepted only for a module with the capability. Other
  modules are refused with `invalid_document`.
- A host who wants to block a category imports and activates a version 2
  document, through the existing `rules_import`/`rules_activate` messages,
  `host_propose_rules`/`host_activate_rules` or the operator's commands.
- A legacy approval that writes a new revision keeps the active document's
  version.
- **Activation never stales an offer or a request.** It changes only which
  new guest requests (and revisions) are accepted.
  - A change of a draft's effective settings still adds one to its
    `draft_revision` (governance section 5).
  - It also re-checks that draft's open offer (section 7), which goes stale
    only if its preview digest changes.
- `rules_view` and the referee's context gain the category policy:
  `{"override_categories": [{"category_id", "label", "policy"}]}`.

**What policy limits.** A guest's request (and a revision) is refused with
`override_blocked`, and nothing is stored, if any quality its *intent* names
is in a blocked category. The refusal names the category and its label.
Consequential qualities a preview lists in `affected` do not block a
request; the host sees them. Policy never limits the host: the host may
offer, counteroffer or directly apply intents in any category, and permitting
a category is not a blanket authorization of any change in it.

## 5. Who may do what

| Operation | Who | Target |
|---|---|---|
| Discover, preview | The draft's owner; the campaign host for any draft | A draft |
| Request, revise | The draft's owner, through its client or the referee, when the owner is not the campaign host | One of its own drafts |
| Offer, counteroffer, deny, withdraw | The current campaign host | Any request |
| Accept, decline | The draft's owner (the participant the request was made for, through whichever player is linked to it now) | An open offer on its draft |
| Cancel | The requester | Its request while `pending_host` or `awaiting_player` |
| Apply directly | The current campaign host, with or without a character, in any mode | Any draft |

- Ownership and authority come from the session's authenticated player and
  link, under the guard (governance section 4), checked again inside each
  transaction. They never come from a selected character, list order, a
  name or anything said.
- An explicit ID (`proposal_id`, `offer_id`, `character_id`) names the
  target. Responding never changes the connection's selection.
- The campaign host does not request overrides for its own drafts. A
  request whose requester is the host is `invalid_request` ("apply it
  directly").
- A requester who is later made campaign host keeps its request. An offer
  the former host made goes stale like any other (section 7), and the new
  host can then apply its own change directly.

## 6. Records

### Schema 10

Campaign schema version 10, applied in one transaction when a store is
first opened:

- **`host_requests` is rebuilt**, every row copied unchanged, to widen two
  checks and add six columns:

  ```sql
  kind TEXT NOT NULL CHECK (kind IN ('exception', 'campaign_setting', 'operation', 'creation_override')),
  state TEXT NOT NULL CHECK (
      (kind <> 'creation_override' AND state IN ('pending', 'approved', 'denied', 'cancelled', 'superseded', 'stale'))
      OR (kind = 'creation_override' AND state IN ('pending_host', 'awaiting_player', 'applied', 'denied',
          'declined', 'withdrawn', 'cancelled', 'superseded', 'stale'))),
  revises TEXT REFERENCES host_requests (proposal_id),  -- the request this one revises
  preview TEXT,            -- creation_override: the preview when requested (canonical JSON)
  preview_digest TEXT,
  stale_reason TEXT,       -- section 7
  origin TEXT CHECK (origin IS NULL OR origin IN ('referee', 'client')),
  module TEXT              -- creation_override: the binding and capability version
                           -- its preview was made under (as an offer's `module`)
  ```

  OVR-3 added `module`, a sixth column, so `override_detail` can tell
  `module_changed` for a request that has no offer yet (section 10).

  `job_id` and `step_request_id` become nullable: a request made from a
  client has neither. Its retry identity is in `host_mutations`.
- **New tables:**

  ```sql
  CREATE TABLE override_offers (
      offer_id TEXT PRIMARY KEY,                -- "ho-" and 12 hex digits, made by the host
      proposal_id TEXT NOT NULL REFERENCES host_requests (proposal_id),
      offer_revision INTEGER NOT NULL,          -- 1, 2, … within its request
      origin TEXT NOT NULL CHECK (origin IN ('offer', 'counteroffer')),
      intent TEXT NOT NULL,                     -- canonical JSON
      preview TEXT NOT NULL,                    -- canonical JSON, as the host reviewed it
      preview_digest TEXT NOT NULL,
      module TEXT NOT NULL,                     -- binding and capability version, canonical JSON
      host_player_id TEXT NOT NULL,             -- who made it
      note TEXT,                                -- the host's reason (required for a counteroffer)
      draft_revision INTEGER NOT NULL,          -- when offered: audit only, never a validity check
      state TEXT NOT NULL CHECK (state IN ('open', 'accepted', 'declined', 'withdrawn',
                                           'cancelled', 'superseded', 'stale')),
      created_at TEXT NOT NULL,
      closed_at TEXT, closed_by TEXT, close_note TEXT, stale_reason TEXT,
      UNIQUE (proposal_id, offer_revision)
  );
  CREATE UNIQUE INDEX one_open_offer ON override_offers (proposal_id) WHERE state = 'open';

  CREATE TABLE override_records (
      record_id TEXT PRIMARY KEY,               -- "hr-" and 12 hex digits
      character_id TEXT NOT NULL REFERENCES characters (character_id),
      kind TEXT NOT NULL CHECK (kind IN ('accepted_offer', 'direct', 'expiry')),
      proposal_id TEXT, offer_id TEXT,          -- an accepted offer's
      host_player_id TEXT,                      -- the offering or applying host; null for an expiry
      requester_participant TEXT,               -- an accepted offer's
      intent TEXT, preview TEXT, preview_digest TEXT,   -- null for an expiry
      receipt TEXT NOT NULL,                    -- the module's receipt, or the expired allowances
      reason TEXT,                              -- a direct override's reason
      revision INTEGER NOT NULL,                -- the game revision it committed at
      draft_revision INTEGER NOT NULL,          -- the draft's revision after it
      created_at TEXT NOT NULL
  );
  ```

- **Widened checks:** `host_audit.kind` gains `override` (a direct
  override). Verdicts and withdrawals use the existing `decision`.
  `referee_steps.kind` gains `respond` (section 11). Both tables are rebuilt
  with every row copied.
- Nothing else is rewritten. Older Tesserae versions refuse a schema 10
  store, as with earlier upgrades.

### Upgrade of existing games

- Completed requests, applied exceptions, documents and their revisions,
  rolls, sheets, commands and transcripts are copied unchanged. An approved
  exception stays applied and is never reinterpreted as an unaccepted
  offer.
- **Adopting.** Each time a store is opened with a runtime whose module has
  the capability, one transaction marks every legacy `pending` proposal
  (`exception` or `operation`) whose target is a draft as `superseded`. Its
  decision note is "Negotiated creation overrides replace this request; ask
  again." Each involved connection gets the `proposal` notice when it next
  reads. Nothing is applied, and no request is converted into an offer:
  current consent must be given again. Once none remain it does nothing.
  This also covers a module that gains the capability later under the same
  version.
- From then on, for that game, `propose_host_decision` with kind
  `exception` or `operation` targeting a draft is refused with
  `override_required` ("ask with kind creation_override"). Ready
  characters, NPCs, the world and campaign-wide settings keep their legacy
  requests.
- The host's own direct paths stay: `host_command` operations the module
  allows on drafts, and exceptions the host writes into the rules itself.
  They are host authority, needing no acceptance, and are audited as
  before.
- A module without the capability behaves exactly as at schema 9. Every
  message and tool of this contract is refused there with
  `override_unsupported`.

## 7. The negotiation

### States

A `creation_override` request:

| State | Meaning | Draft effect |
|---|---|---|
| `pending_host` | A valid request waiting for the host | None |
| `awaiting_player` | It has one `open` offer | None |
| `applied` | The requester accepted an offer; it was applied | Applied once |
| `denied` | The host refused it, with a reason | None |
| `declined` | The requester declined the open offer | None |
| `withdrawn` | The host withdrew the open offer, with a reason | None |
| `cancelled` | The requester cancelled it, or the draft was archived, or the requester's link ended | None |
| `superseded` | The requester's revision replaced it | None |
| `stale` | It can no longer be previewed, or its draft was completed | None |

`applied`, `denied`, `declined`, `withdrawn`, `cancelled`, `superseded` and
`stale` are terminal. A requester may revise any request, terminal or not
(section 7, "Revising").

An offer is `open` until exactly one of: `accepted`, `declined`,
`withdrawn`, `cancelled` (its request was cancelled), `superseded` (a
counteroffer or a revision replaced it) or `stale` (below).

### The steps

1. **Request.** The owner submits an intent and a 1 to 1,000-character
   explanation for one of its own drafts, through `propose_host_decision`
   (kind `creation_override`, section 11) or `override_request`
   (section 10). In one transaction the host:
   - checks authority (section 5) and that the draft is a draft;
   - checks category policy (section 4);
   - previews the intent, storing the preview and its digest;
   - supersedes the requester's earlier non-terminal request for the same
     draft, and its open offer;
   - stores the request as `pending_host`.

   An unsupported, invalid or blocked intent stores nothing and is refused
   (`invalid_intent`, `override_unsupported`, `override_blocked`). The job
   says so and never claims a request was saved. A request sends the
   `proposal` notice to the requester and the host. The referee tells the
   requester to wait, and the job ends.
2. **Verdict.** The host reviews the request and its current preview:
   - **Offer** (`decision: "offer"`): offers the requested intent.
   - **Counteroffer** (`decision: "counteroffer"`, with `intent` and a
     `note`): offers a different intent, in any category.
   - **Deny** (`decision: "deny"`, with a `note`): ends the request.

   An offer or counteroffer names the `preview_digest` the host reviewed.
   In one transaction the host previews the intent against the draft now.
   If its digest differs from the one the host named, the reply is
   `stale_preview` with the current preview, and nothing changes. The host
   then reviews again; the request does not go stale. Otherwise an offer
   revision is stored as `open` and the request becomes `awaiting_player`.

   A counteroffer can also replace an offer that is already open: the open
   offer becomes `superseded`. No verdict changes the draft or the policy.
3. **Response.** The requester sees the exact offer (its preview, the
   host's note and the revision) and responds with `accept` or `decline`.
   It names the `offer_id` and the `preview_digest` it was shown.
   - A decline ends the request (`declined`). An optional note goes to the
     host.
   - An acceptance is one transaction (section 8). It applies the plan and
     ends the request as `applied`, or applies nothing.
4. **Withdrawal.** The host may withdraw an `open` offer, with a note. The
   request becomes `withdrawn`. Withdrawal and acceptance serialize. If
   withdrawal commits first, the acceptance is refused with
   `offer_unavailable`. If acceptance commits first, the withdrawal is
   refused the same way; the change stays applied, and undoing it is a new
   intent (section 3, duty 5).
5. **Cancel.** The requester's `request_cancel` works on a request that is
   `pending_host` or `awaiting_player`. The request and any open offer
   become `cancelled`.

Each step sends the `proposal` notice (`{"type": "proposal", "proposal_id",
"state"}`) to the connections of the requester's linked player and of the
current host, while their access holds, as governance section 9 does. No
step holds a referee job open, starts one or calls a model.

### Revising

A revision is a new request with `revises` set to an earlier request of the
same requester and draft. That earlier request may be in any state. If it
is not terminal, it and its open offer become `superseded` in the same
transaction. Policy applies to a revision as to any request. A revision is
always a deliberate action of the requester. Nothing is resubmitted
automatically after a denial, decline, withdrawal or staleness.

### Staleness

An open offer stays valid while all of these hold:

- its request's draft is still a draft, owned by the requester;
- the game's module binding and capability version equal the offer's
  `module`;
- the offer's `host_player_id` is the current campaign host;
- a fresh preview of its intent, against the draft now, succeeds and has the
  offer's `preview_digest`.

The draft revision, the game revision, the policy revision and category
policy are not conditions.

- **Eager check.** Every transaction that changes a draft's module state or
  its effective settings re-previews that draft's open offer, if any, in the
  same transaction. That covers creation steps, host operations, direct
  overrides, another offer's application and rules activations. A failed
  check makes the offer `stale` at once and sends the `proposal` notice.
  There is at most one open offer per draft (one request per requester and
  draft, one open offer per request), so the cost is bounded.
- **At use.** Acceptance, every verdict and withdrawal check all four
  conditions again inside their own transaction.
- **Host transfer** writes only the registry (governance section 3), so
  offers by a former host are found at use. Every read of an offer reports
  `current: false` and `stale_reason: "host_changed"` as soon as the
  designation differs, before any record changes. The first campaign
  transaction that touches the request records it.
- **What staleness does.** The offer becomes `stale` with a reason:
  `host_changed`, `preview_changed`, `module_changed`, `intent_invalid` or
  `completed`.
  - If the intent still previews, its request returns to `pending_host`,
    with the fresh preview stored. The current host reviews it and issues a
    fresh offer, without the guest asking again. A new host sees the former
    host's offers in the request's history.
  - If it no longer previews (`intent_invalid`), or its draft was completed
    (`completed`), the request becomes `stale`, which is terminal.
- **Pending requests** are not staled by a moved preview. The host reviews
  the current one at verdict time (step 2). Host transfer leaves them
  `pending_host` for the new host, as governance section 9 does now. The
  new host's connections get the `proposal` notice for each when they next
  read.

### Access loss and lifecycle

These run in the same transaction as the event:

- **Archiving a draft** makes its non-terminal requests `cancelled`, and
  their open offers too.
- **Ending the requester's link** does the same (governance section 9 does
  this for legacy requests now).
- **Completing a draft** makes its non-terminal requests `stale` (reason
  `completed`), and their open offers too. Completion also expires
  allowances (section 8).
- **A revoked grant or credential** cannot write the campaign file, so it
  changes no record. While access is gone the requester cannot accept,
  because the guard refuses it. The host may deny or withdraw meanwhile.
  Rotating a credential changes nothing: an earlier legitimate decision
  stands.

## 8. Application

### Acceptance

`override_respond {response: "accept"}` (or the bridge's `override_respond`)
runs, in one campaign transaction under the registry guard:

1. Authorization: the sender is the owner's linked player (section 5). The
   offer is `open`, its `preview_digest` equals the one named, and no
   running job acts for the draft other than the accepting job itself
   (`character_busy` otherwise).
2. The staleness conditions (section 7). A failure records the offer stale
   (and its request as section 7 says) and replies `offer_stale` with the
   new state. Nothing else changes.
3. A fresh preview gives the plan. Its digest equals the offer's, so it is
   the offer's plan.
4. `apply` with `authorization.kind = "accepted_offer"`. A refusal applies
   nothing. The offer stays open and the error is returned.
5. Checks on the result: the state's size limits, `sheet` for the draft
   succeeding within its budget, and `allowances` succeeding.
6. Commit together:
   - the module state;
   - one command record in the requester's participant namespace (content:
     the message's type and fields), advancing the game revision;
   - one added to the draft's `draft_revision`;
   - the `override_records` row (`accepted_offer`, with the receipt);
   - the offer `accepted` and the request `applied`;
   - the referee step, when via the bridge;
   - the eager check of no other offer (there is none for this draft).

   Then the usual `revision` notice and the `proposal` notice go out.

Intervening unrelated edits to the draft are kept: the plan applies to the
draft as it is now.

Acceptance applies the offer; it does not complete the character. The
whole-sheet approval (`character_complete`) is unchanged and separate.
Because the draft's revision moves, an approval shown before the acceptance
is `stale_draft`, as for any creation step.

### Direct override

`override_apply {character_id, intent, preview_digest, reason}` from the
current campaign host, or the host's `host_override_apply` tool, runs in one
transaction:

1. The guard checks the sender is the campaign host. The busy rule applies
   as for `host_command` (`referee_busy` unless it is the host's own job's
   tool). The target must be a draft (`invalid_request` otherwise).
2. The intent is previewed. If its digest differs from the named one, the
   reply is `stale_preview` with the current preview.
3. `apply` with `authorization.kind = "direct"` and the reason.
4. The same result checks as an acceptance.
5. Commit together:
   - one command record under `host:<player_id>`, advancing the game
     revision;
   - one added to the draft's revision;
   - the `override_records` row (`direct`);
   - a `host_audit` row (kind `override`, with the intent, the preview
     digest and the reason in `detail`, and the character as `target`);
   - the eager check of the draft's open offer.

No request, offer or acceptance is involved, and category policy does not
apply. The draft's owner sees the change on their sheet and in
`override_history` (section 10). Applying a change is not a proposal: the
referee must never present a direct override as awaiting anyone.

### Completion

`character_complete` gains, inside its existing transaction:

1. `allowances(state, character_id)` before `complete`: the allowances
   about to expire.
2. `complete` as now. The module's duty 4 drops live allowances and
   creation-only waivers.
3. `allowances` of the completed state must be empty for that character,
   and step 1's list must fit a receipt (8,192 result bytes). Anything else
   is a module failure: `internal_error`, nothing changed.
4. If step 1 listed any, an `override_records` row (`expiry`) whose receipt
   is that list.
5. Section 7's completion rule for the draft's requests.

A ready character never gains, spends or extends a creation allowance.
Continuing benefits in play need a separately authorized supported
operation, outside this contract.

## 9. Replay, conflicts and races

| Mutation | Identity | Recorded in |
|---|---|---|
| `override_request` | `request_id` in the requester's participant namespace | `host_mutations` |
| `propose_host_decision` (kind `creation_override`) | its step, as now | `host_requests.step_request_id` |
| `override_decide`, `override_withdraw` | `request_id` in `host:<player_id>` | `host_mutations` |
| `override_respond` decline | `request_id` in the owner's participant namespace | `host_mutations` |
| `override_respond` accept | `request_id` in the owner's participant namespace | `commands`, as any command |
| `override_apply` | `request_id` in `host:<player_id>` | `commands`, as any command |
| `request_cancel` | as now | `host_mutations` |
| Bridge tools | `step_id`, mapped to `ref:<job_id>:<step_id>` | `referee_steps`, and the effect's own record above |

- **Exact retries.** The same identity with the same content returns the
  recorded reply, even if the state has moved since. A retried successful
  acceptance returns its success, never applies twice, never rolls, and
  never reports `offer_unavailable`. The same identity with other content
  is `request_conflict`. A refusal that changed nothing is not recorded, so
  it may be retried.
- **Authority for a replay** is checked as for a new message, by
  namespace:
  - A requester's replay needs only the requester's access to the draft's
    owner, so it works after the campaign host has changed.
  - A host-namespace replay needs the sender to be the current campaign host
    (governance section 8). A former host's retry is `not_campaign_host`,
    and nothing it decided is disclosed.
- **Races** are serialized by the campaign write transaction. Exactly one of
  these wins, and the other is judged against the result:
  - two verdicts on one request (a second is `proposal_decided` once it is
    not `pending_host`, or supersedes an open offer only when it is a
    counteroffer);
  - accept against withdraw, counteroffer, decline or cancel (the loser gets
    `offer_unavailable` or `proposal_unavailable`);
  - accept against a creation step, a direct override, completion or
    archive of the same draft (the step runs first and the eager check may
    stale the offer; or the acceptance runs first and the step sees the new
    draft revision);
  - accept against a transfer (the guard orders them; an acceptance after
    the transfer finds `host_changed`).
- **Unrelated players.** Commands of other characters never touch an
  offer: they change no draft of its, and the game revision is not a
  condition (section 7).

## 10. Messages

All protocol version 1, inside a selected lobby campaign whose module has
the capability; elsewhere each is `override_unsupported` and nothing
changes. Each reply follows the existing pairing rules.

| Message | Fields | Who | Reply |
|---|---|---|---|
| `override_categories` | | Anyone | `override_categories {categories: [{category_id, label, description, policy}]}` |
| `override_discover` | `character_id`, `query` | Owner; host | `override_discover {character_id, items, next}` |
| `override_preview` | `character_id`, `intent` | Owner; host | `override_preview {character_id, preview, preview_digest}` |
| `override_request` | `request_id`, `character_id`, `intent`, `explanation`, `revises` (optional) | Owner, not the host | `proposal {proposal_id, state, request_id, preview_digest}` |
| `override_decide` | `request_id`, `proposal_id`, `decision` (`offer`, `counteroffer`, `deny`), `preview_digest` (offer, counteroffer), `intent` (counteroffer), `note` | Host | `proposal {proposal_id, state, offer_id, offer_revision, request_id}` |
| `override_withdraw` | `request_id`, `offer_id`, `note` | Host | `proposal {proposal_id, state, offer_id, request_id}` |
| `override_respond` | `request_id`, `offer_id`, `response` (`accept`, `decline`), `preview_digest` (accept), `note` (decline, optional) | Owner | `proposal {proposal_id, state, offer_id, request_id, record_id?, revision?}` |
| `override_apply` | `request_id`, `character_id`, `intent`, `preview_digest`, `reason` | Host | `override_applied {character_id, record_id, revision, draft_revision, request_id}` |
| `override_requests` | `character_id` (optional), `states` (`open`, `terminal` or `all`; default `all`), `after` (optional) | Owner; host | `override_requests {items, next}`: request summaries |
| `override_detail` | `proposal_id` | Requester; host | `override_detail {request, offer}` |
| `override_offers` | `proposal_id`, `after` (optional) | Requester; host | `override_offers {proposal_id, items, next}`: offer summaries |
| `override_offer` | `offer_id` | Requester; host | `override_offer {offer}` |
| `override_history` | `character_id`, `after` (optional) | Owner; host | `override_history {character_id, items, next}`: record summaries |
| `override_record` | `record_id` | Owner; host | `override_record {record}` |

- `intent` is exactly `{"changes": [...]}`, 1 to 16 changes, each exactly
  `{"quality_id", "action_id", "parameters"}`, of at most 4,096 result bytes
  (below).
- `explanation`, `note` and `reason` are 1 to 1,000 characters and at most
  4,096 result bytes; a longer one is `invalid_request` ("shorten it"). Notes
  are optional for an offer and a decline, and required for a counteroffer,
  a denial and a withdrawal.

### Result sizes

Every size this contract introduces is in **result bytes**: the length of
`json.dumps(value)` with Python's defaults, exactly as the bridge sends a
tool result (referee contract section 7). That escapes every non-ASCII
character, so one character can be up to 12 bytes; a bound in characters
alone is not a bound. Client replies use the same views, so the same
bounds hold on the WebSocket.

**Excerpts.** A summary carries a text field as an excerpt: the longest
prefix, in whole characters, of at most a stated number of result bytes. A
summary names every shortened field in its `cut` list. The full value is
always in the matching detail read, so nothing authoritative is lost. A
summary carries no intent, preview or receipt, only their digests and the
preview's headline, so it can never hold an effect in part.

| View | Fields | Excerpts | Bound |
|---|---|---|---|
| Request summary | `proposal_id`, `target`, `requester {participant_id, name}`, `state`, `revises`, `categories` (the first 4 of the intent's, sorted) and `categories_more`, `explanation`, `decision_note`, `stale_reason`, `created_at`, `decided_at`, `record_id` (once applied), `offer` (the open or last offer, in the context entry's offer form), `cut` | `name` 120, `explanation` 240, `decision_note` 240 | 2,048 |
| Offer summary | `offer_id`, `offer_revision`, `origin`, `state`, `current`, `stale_reason`, `headline`, `preview_digest`, `note`, `created_at`, `closed_at`, `cut` | `headline` 240, `note` 240 | 1,024 |
| Record summary | `record_id`, `character_id`, `kind`, `proposal_id`, `offer_id`, `revision`, `draft_revision`, `created_at`, `headline`, `cut` | `headline` 240 | 1,024 |
| Context entry (section 11) | `proposal_id`, `character_id`, `requester` (participant ID), `state`, `categories` (first 2) and `categories_more`, `explanation`, `offer {offer_id, offer_revision, origin, state, current, stale_reason, headline, preview_digest, cut}`, `cut` | `explanation` 120, `headline` 120 | 1,024 |
| `override_detail` | `request`: the summary's fields with every category, the whole `explanation` and `decision_note`, `intent`, the stored `preview` and its `preview_digest`, `offer_count`, and `current` (below); `offer`: the open or last offer's summary | `current.problems`: whole problems, each excerpted to 300, up to 2,048 in all | 45,056 |
| `override_offer` | The offer summary's fields with the whole `note`, `close_note`, `host_player_id`, `draft_revision`, `intent` and `preview` | | 45,056 |
| `override_record` | The record summary's fields with `reason`, `intent`, `preview`, `preview_digest` and `receipt` (an expiry's receipt is its expired allowances, held to the same 8,192 bytes) | | 45,056 |

The parts are bounded separately:
- text fields 4,096 bytes;
- intent 4,096;
- preview 12,288 (section 3);
- receipt 8,192 (section 3);
- participant names 100 characters.

Measured worst cases, with every text made of 12-byte characters and every
part at its limit:

| View | Measured | Bound |
|---|---|---|
| Request summary | 1,692 | 2,048 |
| Offer summary | 856 | 1,024 |
| Record summary | 524 | 1,024 |
| Context entry | 794 (+ about 40 for `requester`) | 1,024 |
| `override_detail` (with a full current preview *and* full problems, which cannot occur together) | 41,513 | 45,056 |
| `override_offer` | 25,366 | 45,056 |
| `override_record` | 29,320 | 45,056 |
| A 32,768-byte page of summaries | 32,241 | 45,056 |
| The context's `overrides` block | 8,109 | 10,240 |
| Its floor (no entry; 16 categories, counts of 1,000,000) | 1,738 | 2,048 |

The implementation must build every view under these rules and check its
size. A view over its bound is a host failure (`internal_error`, logged),
never a cut reply. OVR-3 and OVR-4 test each view at its worst case through
the real serialization.

### Reads

- **Pages.** `override_requests`, `override_offers` and `override_history`
  return up to 20 summaries, and at most 32,768 result bytes for the whole
  reply, always at least one item when any remain. `next` is an opaque
  cursor (null at the end) for the next call's `after`.
  - Requests are ordered by creation, oldest first. New requests therefore
    join the end, and paging to the end lists every request once.
  - Offers are ordered by revision. Every revision is listed, including
    superseded, stale, withdrawn, declined and cancelled ones.
  - Records are ordered newest last.
- **`override_requests`** lists every `creation_override` request the
  caller may see, terminal ones included, optionally only one draft's
  (`character_id`) or only open (`pending_host`, `awaiting_player`) or
  terminal ones. With it, a fresh session can find every request and every
  verdict without knowing an ID.
- **History never needs the intent to work.** Every read returns what was
  stored: the request's stored preview (as last previewed when it was
  requested, or when it returned to `pending_host`), each offer's preview as
  the host reviewed it, and each record's preview and receipt. A read never
  calls `apply` and never fails because an intent no longer previews, its
  draft was completed or archived, or the module changed.
- **`override_detail`** is one request in full. Its `request.current` reports
  separately what the intent would do now:

  ```json
  "current": {"status": "same" | "changed" | "invalid" | "not_checked",
              "reason": null | "target_not_draft" | "module_changed",
              "preview": null or the current preview, "preview_digest": null or its digest,
              "problems": null or ["…"], "problems_more": 0}
  ```

  - It is previewed only while the target is still a draft and the module's
    binding and capability version equal the request's. Otherwise `status`
    is `not_checked` with that `reason`, and nothing of the module runs.
  - `same` and `changed` carry the current preview and its digest. `changed`
    means it differs from the stored one.
  - `invalid` carries the module's `IntentError` problems instead, and a null
    preview. A preview hook that fails in any other way is also `invalid`,
    with the problem "The module could not preview this intent." (logged),
    and the read still succeeds.
  - `problems` holds whole problems, each excerpted to 300 result bytes, up
    to 2,048 in all. `problems_more` counts those left out. The full list is
    a diagnostic, not an authoritative effect, and `override_preview`
    returns it again for a draft.
  - The stored preview stays the record of what was asked or offered; the
    current one is only information for a revision. **`override_offers`** pages
  its offer revisions and **`override_offer`** is one in full, so an offer
  that was never accepted stays retrievable. **`override_history`** pages a
  draft's application records (accepted offers, direct overrides and
  expiries) and **`override_record`** is one in full. Records exist only for
  applications. Unaccepted offers are found through `override_offers`, not
  history.
- **`requests`** (governance section 7) and the snapshot's referee
  projection leave `creation_override` requests out. They have their own
  paged read, so `requests` keeps its legacy content and size.
- **Privacy.** These reads run under the guard like every governance read.
  - A guest sees only requests it made, their offers, and the records of its
    own drafts. A request, offer or record of anyone else is
    `proposal_unavailable`, `offer_unavailable` or `character_unavailable`,
    with one code for unknown and foreign alike.
  - The campaign host sees every request, offer and record.
  - A former host sees only its own, as a guest. Nothing here is pushed to
    anyone; the `proposal` notice carries only an ID and a state.
  - A completed or archived target hides nothing from those who may see it:
    the requester and the host still read its requests, offers and records,
    and the owner and the host page its history by `character_id`. Archiving
    changes what a character can do, not who may read its past.

New errors (`request_conflict`, `referee_busy`, `character_busy`,
`character_unavailable`, `not_campaign_host`, `proposal_unavailable` and
`proposal_decided` keep their meanings):

| Code | Cause | Carries |
|---|---|---|
| `override_unsupported` | The module lacks the capability; an unknown category, quality or action | |
| `override_blocked` | A guest's intent names a quality in a blocked category | `categories` |
| `override_required` | A legacy exception or operation request targeting a draft, in an adopting game | |
| `invalid_intent` | The intent's shape, or the module's `IntentError` | `problems` |
| `stale_preview` | The named `preview_digest` is not the current preview's | `preview`, `preview_digest` |
| `offer_unavailable` | An unknown or foreign offer, or one that is not `open` | |
| `offer_stale` | An acceptance that found the offer stale | `proposal_id`, `state`, `stale_reason` |

The snapshot of an adopting campaign gains `override_categories`.

## 11. The referee

**Context.** In an adopting game, `get_context` adds one `overrides` block.
Legacy kinds keep their places in `requests` and `pending_requests`; a
`creation_override` request never appears there.

```json
"overrides": {
  "categories": [{"category_id": "endurance", "policy": "approval_required"}],
  "mine": ["...the sender's open requests (pending_host, awaiting_player), newest first..."],
  "mine_more": 0,
  "recent": ["...the sender's terminal requests, most recently decided first..."],
  "recent_more": 0,
  "pending_host": ["...in a host job: requests pending_host, oldest first..."],
  "pending_host_more": 0
}
```

- `mine` lists at most 3 entries and `recent` at most 2. `pending_host`
  appears only in a host job and lists at most 3. Each entry is the context
  entry of section 10, at most 1,024 result bytes. Each `…_more` counts the
  rest of its kind.
- The block is at most 10,240 result bytes: 8,109 measured at its worst,
  with 16 categories. It is absent in a game whose module lacks the
  capability.
- **It holds what fits** (#190). Its floor is always present: the category
  policies and every count, with each list empty. Entries are then added
  while the block stays within 10,240 bytes and the whole context within
  one tool result. A host job adds `pending_host` first, then `mine`, then
  `recent`; another job adds `mine`, then `recent`. Adding stops at the
  first entry that does not fit. Each list is a prefix in its documented
  order, and its `…_more` counts everything it leaves out. The transcript's
  first page takes what room is left after the block, as before.
- It counts in "everything but the transcript" (governance section 11).
  Only its floor must fit, so an adopting module must leave 2,048 bytes of
  that budget free in its worst case, the floor's bound (1,738 measured
  with 16 categories at their longest). A context whose floor does not fit
  is refused as `context_too_large`, as a context over the result always
  was.
- **Nothing is cut silently.** Every request is reachable:
  - the counts say how many are not listed;
  - `override_requests` pages them all, terminal verdicts included;
  - every entry names its IDs for `override_detail`, `override_offers`,
    `override_offer` and `override_record`.
- **Fresh sessions.** A rebuilt session has only the transcript (referee
  contract section 10). The prompt of every job in an adopting game says:
  when a `…_more` count is not zero, or on a new session, page
  `override_requests` before telling a player about their requests. Read the
  exact offer (`override_offer`) before narrating its effects or acting on
  it.

**Tools**, all journaled where they mutate (referee contract section 6;
governance section 10):

| Tool | Who | Arguments | Effect |
|---|---|---|---|
| `override_discover` | A job of the draft's owner; a host job | `character_id` (default the job's draft), `query` | Section 3's discovery |
| `override_preview` | As above | `character_id`, `intent` | A preview and its digest |
| `override_requests` | A job of a requester; a host job | `character_id`, `states`, `after` | Section 10's paged requests, terminal ones included |
| `override_detail` | A job of the requester; a host job | `proposal_id` | One request in full |
| `override_offers` | A job of the requester; a host job | `proposal_id`, `after` | Section 10's paged offer revisions |
| `override_offer` | A job of the requester; a host job | `offer_id` | One offer in full |
| `override_history` | A job of the draft's owner; a host job | `character_id`, `after` | Section 10's paged application records |
| `override_record` | A job of the draft's owner; a host job | `record_id` | One record in full |
| `propose_host_decision` | A job of the draft's owner | `kind: "creation_override"`, `scope: "target"`, `target` (default the job's draft), `payload: {"intent", "revises"?}`, `explanation`, `step_id` | Section 7, step 1 |
| `override_respond` | A job of the draft's owner | `step_id`, `offer_id`, `response`, `preview_digest`, `note` | Section 7, step 3; a step of kind `respond` in the sender's namespace |
| `host_override_decide` | The campaign host's job | `step_id`, `proposal_id`, `decision`, `preview_digest`, `intent`, `note` | Section 7, step 2 |
| `host_override_withdraw` | The campaign host's job | `step_id`, `offer_id`, `note` | Section 7, step 4 |
| `host_override_apply` | The campaign host's job | `step_id`, `character_id`, `intent`, `preview_digest`, `reason` | Section 8's direct override |

- The read tools follow section 10's privacy rule for the job's sender. A
  host job reads as the host only while the guard finds its sender the
  campaign host at the call; otherwise it reads as a guest.
- The host tools are steps of kind `host` and follow governance section 10:
  reauthorized at each call, available in a host job of any mode (with no
  character selected too), host results through recovery.
- A `respond` step is the owner's own result, like a `propose` step. It is
  listed to, and may be narrated by, only the owner's later jobs, under the
  usual mode rules.
- `override_respond` names its offer explicitly, so it may run in any of
  the owner's jobs (setup, creation or play). It never selects the draft.
- **Prompts.** In an adopting game every job's prompt adds:
  - Ask the host with `propose_host_decision` (kind `creation_override`) and
    end the job.
  - Tell the player when a request is waiting, offered, or applied, and
    never say "approved" or "granted" before acceptance applies it.
  - Accept or decline only on the player's explicit word, naming the exact
    offer and its digest. Ask when consent is ambiguous.
  - Never substitute a stale or counteroffered effect for the one the player
    agreed to.
  - Never resubmit a refused, blocked or denied request unasked.

  A host job's prompt adds that a direct override needs the host's explicit
  instruction, the exact intent and the digest it previewed, and a reason,
  and needs no earlier request.
- **What cannot authorize anything** (governance section 10, extended): a
  ruling, a choice, a reply's text or a claim of consent cannot. Nor can a
  player's word relayed by another job, or an offer's mere existence.
  Effects are applied only by an acceptance through `override_respond` of
  the exact open offer, or by the current host's direct override.
- The referee never edits module state to imitate an override. If no
  supported intent expresses a request, it says so (using the preview's
  `limitations` or `override_unsupported`).

## 12. Clients

Both clients send every command and let the host refuse what the sender may
not do. Both name explicit IDs and digests, confirm before a mutation by
showing exactly what will be sent, reuse a mutation's `request_id` only for
an exact retry, and re-read everything on reconnect. Nothing is kept
locally.

**CLI** (`connect --lobby`), extending governance section 15:

```text
/overrides [CHARACTER] [CATEGORY]    what can be overridden on a draft   (override_categories, override_discover)
/overrides CHARACTER QUALITY PARAM [MATCH]   a parameter's options        (override_discover)
/override preview CHARACTER INTENT-JSON                                  (override_preview)
/override request CHARACTER INTENT-JSON EXPLANATION [--revises ID]       (override_request)
/requests overrides [CHARACTER] [open|terminal] [more]   page your requests (host: all)   (override_requests)
/offer ID                              a request, and its open or last offer exactly   (override_detail, override_offer)
/offers ID [more]                      every offer revision of a request (override_offers)
/accept OFFER                          shows the offer, asks to confirm  (override_respond accept)
/decline OFFER [NOTE]                                                    (override_respond decline)
/decide ID offer [NOTE]                host: shows the preview, confirm  (override_decide offer)
/decide ID counter INTENT-JSON NOTE    host: shows the preview, confirm  (override_decide counteroffer)
/decide ID deny NOTE                   host                              (override_decide deny)
/withdraw OFFER NOTE                   host                              (override_withdraw)
/override apply CHARACTER INTENT-JSON REASON   host: shows the preview, confirm   (override_apply)
/override history CHARACTER [more]     a draft's applications             (override_history)
/override record ID                    one application in full            (override_record)
```

- `CHARACTER` defaults to the selected draft only in `/overrides` and
  `/override preview|request|history`. Responses and verdicts always name
  their ID.
- The confirmation step sends the digest of the preview it showed, so a
  change meanwhile is refused (`stale_preview` or `offer_stale`) and the
  client says to review again. It never resends a different plan.
- `/decide ID approve|deny` keeps working for legacy kinds. On a
  `creation_override` request, `approve` is refused locally with a hint to
  use `offer`.
- `/requests` keeps its legacy listing, and then shows the first page of
  open override requests, saying whose response is awaited ("waiting for
  the host" or "offer N waiting for you") and how to page the rest
  (`/requests overrides … more`). `more` continues from the last page's
  `next`.

**Browser.** The existing panels, no separate character editor:

- The Console's request list shows each of the player's requests, its state
  and whose response is awaited. On an open offer it shows the headline,
  effects, waivers, allowances, later steps, consequences, limitations and
  the host's note, under "Offered, not applied yet", with Accept and
  Decline. Accept shows a confirmation naming the offer revision. A
  terminal request offers "Revise", which starts a new request from its
  intent.
- Settings shows the category policy (`override_categories`).
- The host's Requests panel shows each `creation_override` request with its
  current preview: Offer, Counteroffer (intent as one JSON object,
  prefilled from the request, then Review), Deny, and Withdraw on an open
  offer.
- Administration gains Override: pick a draft, a category and a quality from
  discovery, enter the intent's JSON, Review the preview, give a reason,
  then Apply.
- Every text is rendered as text. Host data is cleared and dropped as
  governance section 15 says. A `proposal` notice re-reads the request list
  and a shown draft's sheet.

The final command names and layouts belong to OVR-5 and OVR-6. These are
their starting point and must keep the rules above.

### One backend

Every path below is a governance control, not gameplay. None is a module
command: `apply` has no command name, so `act`, `referee_act` and
`host_act` can never reach it. Each row is one store operation, which every
interface calls with the authority section 5 names:

| Operation | Client message | Bridge tool | CLI | Browser |
|---|---|---|---|---|
| Discover | `override_discover` | `override_discover` | `/overrides` | Administration › Override; Revise |
| Preview | `override_preview` | `override_preview` | `/override preview` | Review buttons |
| Request, revise | `override_request` | `propose_host_decision` (`creation_override`) | `/override request` | Console › Revise |
| Offer, counteroffer, deny | `override_decide` | `host_override_decide` | `/decide ID offer\|counter\|deny` | Requests › Offer, Counteroffer, Deny |
| Withdraw | `override_withdraw` | `host_override_withdraw` | `/withdraw` | Requests › Withdraw |
| Accept, decline | `override_respond` | `override_respond` | `/accept`, `/decline` | Console › Accept, Decline |
| Cancel | `request_cancel` | (none, as now) | `/cancel` | Console › Withdraw request |
| Direct override | `override_apply` | `host_override_apply` | `/override apply` | Administration › Override › Apply |
| Read | `override_requests`, `override_detail`, `override_offers`, `override_offer`, `override_history`, `override_record` | the same names; `get_context`'s `overrides` | `/requests overrides`, `/offer`, `/offers`, `/override history`, `/override record` | Console and Requests panels, paged with Show more |

The whole-sheet approval (`character_complete`, `/approve` and the browser's
Approve) is not in this table and does not change.

## 13. Changes to existing contracts

Only for a game whose module has the capability, unless noted:

- **Governance section 9.**
  - `creation_override` requests follow this contract's states and steps,
    not `request_decide`'s immediate application. `request_decide` on one
    is `invalid_request` ("use override_decide").
  - "Transfer: pending proposals stay" still holds for `pending_host`;
    offers follow section 7.
  - Legacy draft requests are superseded and refused (section 6).
- **Governance section 5 (all games).** Rules document version 2
  (section 4). Version 1 is unchanged.
- **Governance sections 7, 10 and 15.** The reads, the tools, the context
  and the clients gain what sections 10 to 12 say. The `requests` read, the
  snapshot's referee projection, and `requests` and `pending_requests` in
  `get_context` all exclude `creation_override` requests, which have their
  own paged reads and the context's `overrides` block.
- **Governance section 12 (all games).** Campaign schema 10 (section 6).
- **Character section 9.** `character_complete` also expires allowances and
  stales the draft's requests (sections 7 and 8). Its other rules are
  unchanged. Accepting an offer adds one to the draft revision, like a
  creation step.
- **Character section 3.** Unchanged: `complete` stays pure and
  deterministic, and `problems` alone vetoes completion.
- **Referee section 6.** The bridge's tool list gains section 11's tools.
  `referee_steps.kind` gains `respond`.

The governance, character and referee contracts each gain a pointer to this
contract at the sections named.

## 14. Illustrative module

Trailhead, still version 1 under section 3's rule, with two original
families, as OVR-2 built them ([trailhead.md](../trailhead.md#creation-overrides)).

| Category | Quality | Actions | Notes |
|---|---|---|---|
| `endurance` | `stamina_roll` | `replace {value: 2–4}` | Replaces the rolled stamina once it is rolled; the original roll stays in `rolled` |
| `endurance` | `max_stamina_modifier` | `set {amount: -1–2}` | A starting modifier the completion rule adds, recorded separately from the settings bonus |
| `supplies` | `starting_gear` | `grant {item}`, `remove {carried}`, `allow {count: 1–3}` | `allow` grants an allowance to pick later items with an ordinary new step, `trailhead.pick`; unused picks expire at completion |

A guest asks for `{"changes": [{"quality_id": "stamina_roll", "action_id":
"replace", "parameters": {"value": 4}}]}` with the explanation "A mountain
guide by trade". The exchange runs as follows:

1. The referee proposes it. The host is notified, and the guest is told it
   is waiting. Another player pushes on meanwhile.
2. The host counteroffers `value: 3` with a note. The guest's next input
   hears "offered, not applied".
3. The guest sets a fact (unrelated). The offer stays current, because its
   preview depends only on `rolled` and the replacement.
4. The guest accepts the exact offer. The draft's stamina is 3, its
   original roll is kept, and its revision moves.
5. The guest approves the sheet separately.

In another campaign `supplies` is blocked. The guest's request for a rope
is refused before anything is stored. The host grants the rope directly,
with a reason, and the guest's sheet shows it with its provenance. No
class, level or role is assumed.

## 15. Tests each slice needs

- **OVR-2 (#174):**
  - the capability's catalog checks;
  - discovery paging and its bound;
  - deterministic, pure previews;
  - `dependencies` making an unrelated step leave the digest unchanged, and
    a relevant one change it;
  - `apply` with no random source, refusing technical invalidity with
    nothing changed;
  - rules document version 2 and its refusals;
  - version 1 unchanged;
  - category policy blocking a guest request but not the host;
  - two original families with original values preserved and allowances
    expiring at completion;
  - nonadopting modules unchanged.
- **OVR-3 (#175):**
  - schema 10 migration on copies, preserving every legacy row, and
    superseding legacy pending draft requests on adoption;
  - every state transition of section 7;
  - acceptance's atomicity, with an injected failure at each step;
  - exact retries of every mutation, including an acceptance retried after
    a transfer, and conflicts;
  - the races of section 9;
  - staleness by preview, not revision, including the requester's
    unrelated step keeping the offer and a relevant one staling it;
  - transfer keeping `pending_host` and staling former-host offers;
  - withdrawal against acceptance;
  - archive, unlink and completion;
  - privacy of every read: a guest gets nothing of another's requests,
    offers or records, and a former host reads as a guest;
  - paging to the end lists every request once, terminal ones included,
    while new requests arrive;
  - every view of section 10 at its worst case (12-byte characters, every
    part at its limit), measured with the bridge's own serialization;
  - a request made stale by `intent_invalid` (a creation step removes a
    prerequisite): `override_detail` returns its stored preview with
    `current.status` `invalid` and its problems, and its offers and the
    request stay readable;
  - a completed target and an archived target: their requests, offers and
    records stay readable to the requester or owner and the host, with
    `current.status` `not_checked` and no module hook called;
  - a preview hook that raises unexpectedly during a read: the read still
    succeeds, with `invalid`.
- **OVR-4 (#176):**
  - the scripted conversation through the real runner and bridge: request,
    waiting while another player plays, a counteroffer narrated as not
    applied, acceptance in a later job, denial and revision, direct
    override with no character and no request;
  - unsupported and blocked requests never claimed as saved;
  - recovery of `respond` and host steps;
  - a fresh session, with more open requests than the context lists and
    several terminal verdicts, finding every one through
    `override_requests` and narrating each verdict from `override_offer`;
  - that fresh session explaining a request whose intent no longer previews
    from its stored preview and current problems, without claiming it can
    still be accepted;
  - the `overrides` block at its worst case in a composed `get_context`.
- **OVR-5 and OVR-6 (#177, #178):**
  - the commands and controls against the real backend;
  - confirmation by digest;
  - stale refreshes;
  - authority changes clearing host controls;
  - no private data to other guests;
  - legacy `/decide` unchanged for legacy kinds.
- **OVR-10 (#182):** the design's completion list, model-free.

## 16. Open questions

These are not owner decisions; each has a default that stands until the
owner says otherwise.

- **Consequential qualities in a blocked category.** Default: they do not
  block a guest's request (section 4); the host sees them in the preview.
- **A guest's request from a client.** Default: allowed (`override_request`),
  as well as through the referee, because a revision must be possible from
  either client.
- **Text limits.** Default: notes, reasons and explanations are 1,000
  characters and also at most 4,096 result bytes, so a text written mostly
  in characters outside Latin script may hold fewer characters.
