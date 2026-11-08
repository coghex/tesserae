# Negotiated creation overrides

Status: proposed implementation design, following the owner's decisions on
2026-11-08. Issue filing is authorized. No runtime behavior is changed by this
document. Public baseline: `c8a954cc275934cb159a099624ce6511a396a29e`.

This extends the [campaign authority design](campaign_authority_design.md)
and [governance contract](governance_contract.md). Where they currently make
approval apply a creation exception immediately, the new flow below supersedes
that behavior for modules adopting the new capability. Other governance
operations keep their existing semantics.

## Problem and outcome

The current exception mechanism changes only settings explicitly declared by
a module. Broad host administration is a different capability and may refuse
mechanical edits to drafts. A referee therefore can recognize the host's
authority but have no executable way to honor a legitimate starting-quality
override. Supporting one additional hardcoded exception at a time does not
meet the intended host authority.

Cover every script-backed starting quality in an adopting module, organize
those qualities into module-defined categories, and provide one negotiated
override mechanism. A guest asks, the host approves, denies or counteroffers,
and the guest accepts, declines or revises. Approval authorizes a concrete
offer; acceptance applies it. The host remains able to administer a draft
directly on its own explicit instruction.

## Owner decisions and implementation defaults

The owner has agreed:

- All starting qualities the installed scripts can represent are in scope,
  rather than just the examples that exposed the gap.
- Categories matter: a host can permit requests in one category and block
  another. Category membership comes from the game module, not a universal
  list of character concepts.
- The referee presents the request to the host and tells the requester to
  wait. That job ends; other players keep playing.
- The host's verdict returns to the requester, who may accept or try again.
- Scripts perform supported mechanical changes. The referee cannot install
  code or invent executable behavior to satisfy a request.

Implementation defaults in this design, rather than additional attributed
owner decisions: every advertised category initially requires host approval;
no automatic-approval tier is introduced. Offers have no wall-clock expiry.
They persist across disconnects/restarts and become stale on relevant changes.
One outstanding negotiation per requester/target is sufficient initially;
a deliberate revised request supersedes its predecessor while retaining links
and history. Host counteroffers replace the outstanding offer, not its audit
record. These choices keep the first delivery bounded.

## Framework and module responsibilities

Add an optional versioned `CreationOverrides` capability beside
`CharacterRules`, `CampaignSettings` and `Administration`. Modules without it
continue to work. Public examples and tests use only original public content.
No private module names, paths, data, rules or transcripts belong here.

The module supplies:

- Stable category and quality IDs, player-safe labels/descriptions, applicable
  qualities for this draft and bounded discovery of their current values.
- Declared actions and parameter shapes per quality: for example setting a
  value, granting a collection entry or changing a creation allowance. Names
  and semantics belong to the module; these are not universal game fields.
- A pure deterministic preview that normalizes an intent into a canonical
  plan, identifies affected qualities/categories, describes before/after and
  consequential effects/waivers, and reports technical limitations.
- A deterministic application hook for that exact plan, with trusted host
  authorization/provenance supplied by the framework. It returns validated
  module state and an application receipt, or refuses without changes.

Conceptual intent, deliberately independent of storage paths:

```json
{"changes": [
  {"quality_id": "starting_supplies", "action_id": "grant",
   "parameters": {"item_id": "public-example-kit", "quantity": 1}}
]}
```

The contract child fixes the exact Python hooks, JSON/wire shapes, versions,
error codes and bounds before runtime implementation. The sketch above is
not a raw JSON patch or permission to write arbitrary module state. Quality
IDs resolve through installed module handlers, never a client-supplied path,
Python expression, script or provider choice.

Preview and acceptance draw no dice. A host may explicitly replace a supported
rolled outcome, preserving the original roll. If a grant authorizes a later
random creation step, the offer describes that step and its stakes; the normal
module command performs it once after acceptance, with normal retry guarantees.
An unknown random outcome must not be silently generated while negotiating.

The framework owns authenticated identities, category request policy,
negotiation/offer history, authority checks, privacy, revisions, idempotency,
atomic application, notifications and client controls. Modules implement the
meaning and persistence of accepted changes, not a second approval system.

## Category policy and host authority

Each campaign's versioned rules document records policy for declared category
IDs: `approval_required` or `blocked`. Use a documented new document version
if the existing exact envelope must change; read existing version-1 documents
with their meaning intact. The framework must not put these controls into
every module's ordinary setting names by convention.

Unknown category/quality/action IDs are unsupported. A blocked guest request
is explained before a request is queued; a supported request in an allowed
category requires a host verdict. Permitting a category is not a blanket
authorization to change every value within it. No override has an implicit
campaign-wide scope: the negotiated target is one owned draft.

The host can change this policy and can directly preview/apply supported
overrides to any draft, with a reason and the normal audit/confirmation path,
even when guest requests in that category are blocked. Direct host editing
does not require a guest proposal or guest acceptance. A host can also make
an explicit counteroffer containing qualities the guest could not originate;
the guest still must accept that entire offer.

Host authority waives ordinary game creation restrictions, not structural
validity, ownership, authentication, supported data references or storage/tool
budgets. Derived caches are recalculated through module-owned semantics;
the referee cannot patch a cache that the next command discards. Any genuine
unsupported engine behavior is explained rather than claimed as applied.

## Negotiation and application

Reuse the existing request/authority machinery with an explicit negotiated
creation kind. Do not change the meaning of every historical `approved` row.
Conceptual states, with final wire names fixed in the contract:

| State | Meaning | Draft effect |
|---|---|---|
| `pending_host` | Valid supported request, waiting for host | None |
| `awaiting_player` | Host-approved or counteroffered immutable offer | None |
| `applied` | Requester accepted this exact offer revision | Applied once |
| `denied` | Host declined, with explanation | None |
| `declined` | Requester declined the offer | None |
| `cancelled` | Requester withdrew or lost access | None |
| `superseded` | Deliberate revised request/offer replaced it | None |
| `stale` | Relevant target, policy, authority or plan changed | None |

1. The guest explains an exception. The referee discovers relevant qualities
   and submits a structured intent for the authenticated requester's draft.
   The module previews it; unsupported/blocked requests have no stored fake
   proposal. The host gets a durable private notification with the referee's
   explanation and concrete consequences. The referee tells the guest to wait
   and ends the job. A pending decision never holds the global referee busy.
2. The host sees the actual request and preview, not an unverified summary of
   supposed consent. Approval creates an exact immutable offer revision.
   Denial records a reason. A counteroffer is a separately validated replacement
   intent/preview linked to the request and previous offer, with its own reason.
   None changes the draft or effective mechanical policy yet.
3. The requester gets a private verdict notification immediately. A later
   referee job reads the durable verdict and explains it conversationally.
   Accept, decline, cancel and revise are explicit actions, available in both
   clients and through the referee bridge. No background model call, polling
   conversation, timeout decision or automatic retry is needed.
4. Acceptance names the exact offer ID/revision and runs one transaction:
   reauthorize, check/re-preview, apply the complete plan, validate the result,
   update affected draft metadata, record the receipt/audit and mark it applied.
   Either all effects and records commit or none do. Accepting an offer is not
   approval of the completed character sheet; ordinary final review remains.
5. A refusal is shown honestly. The guest can decline or deliberately submit
   a revised request; denial does not trigger repeated automatic requests.

An offer can authorize a bounded continuing allowance, not just a one-off
value. Its scope and allowed future choices must be explicit. Module state
keeps original generation results distinct from accepted effective values,
allowances and waivers. Later normal creation steps, recalculation, completion
and reload preserve the accepted authority. They cannot silently erase it or
let guests extend it beyond the approved scope.

## Persistence, races and compatibility

Use existing frozen actor/participant/link identities and host registry guards.
Natural-language authority is not a credential. Only the target's current owner
can accept/decline its offer; a selected character must never accidentally pick
the target by list order. Explicit governance controls may name an owned offer
without changing the selected character. The host can operate with no character.

Freeze the draft revision/digest, effective policy revision, module/capability
version, current host identity and canonical preview digest. Re-preview before
offering and accepting. Unrelated activity by another player must not stale an
offer merely because the global game revision increased. Relevant changes must
not silently update an already offered effect: mark stale and require renewed
host authorization and player acceptance of the changed offer.

Offer history, request lineage, decisions and receipts survive restart and Codex
session loss. Exact retries replay, including after role changes; conflicting
reuse of an ID is refused. Replay never reapplies effects, rolls again or
discloses a former host's private results. Concurrent decisions/acceptance and
acceptance versus editing/completion/archive have one serialized outcome.

Unlink, grant revocation, character archive/completion or host transfer invalidates
relevant pending work. A new host can issue a new offer after review; already
applied historical changes remain applied. Credential rotation alone does not
erase a previously legitimate host decision. Policy changes trigger the documented
stale checks; category requestability is not confused with the host's authority
to make an offer.

Database migrations preserve completed requests, applied exceptions, original
rolls, current sheets and transcripts. Existing approved/immediately applied
exceptions stay applied and are not reinterpreted as unaccepted offers.
For an adopting module, old pending immediate-apply creation requests must be
explicitly superseded or safely converted without an effect; clients/referee
are told to obtain a current offer. New draft requests must not retain a legacy
immediate-approval path that bypasses acceptance. Nonadopting modules and
ready-character/world operation requests retain their documented behavior.

## Referee and clients

Extend `propose_host_decision` with the negotiated kind and add bridge actions
for host verdict/counteroffer, requester response and direct host override.
Reuse the current request journals and trusted guards. Contract tests cover
both raw governance controls and natural-language bridge paths into the same
backend. Gameplay mechanics remain behind the referee command bridge.

The referee sees bounded discovery, current category policies, actionable
requests/offers and recorded outcomes. It cannot invent an operation name or
represent a rejected proposal as awaiting approval. A host instruction need
not refer to a failed earlier proposal: direct supported override is available.
Every narrative message distinguishes waiting, offered and actually applied.

CLI controls extend `/requests` and `/decide` with explicit offer/reply actions;
the contract fixes final commands. Browser Console/Settings render the same
states, previews, notes and accept/decline/revise controls using text-safe
rendering. No separate character builder or generic schema form editor is
required. A character's full sheet approval remains a separate control.

Only requester and current host receive request/offer details. Other guests,
table conversation and ordinary public views get no private draft, host note
or referee preparation. A host-only action/result remains host-only on recovery.

Catalog discovery and previews are bounded/paged as needed. Define concrete
input/offer/result limits in the contract and measure against the existing
one-result budget and composed `get_context` budget. Do not embed an entire
module catalog or duplicate every offer into every job; expose selected detail
through tools without silent truncation of authoritative effects.

## Delivery slices

| Slice | Delivery | Depends on |
|---|---|---|
| OVR-1 | Normative capability, policy, negotiation and migration contract | None |
| OVR-2 | Optional module capability, discovery, preview and category policy | OVR-1 |
| OVR-3 | Persist requests/offers; guarded atomic acceptance and direct host application | OVR-2 |
| OVR-4 | Referee tools, private notifications and conversational recovery | OVR-3 |
| OVR-5 | CLI host verdict and requester response controls | OVR-3; OVR-4 for conversational validation |
| OVR-6 | Browser host verdict and requester response controls | OVR-3; OVR-4 for conversational validation |
| OVR-7 | Private complete quality inventory and integration contract | OVR-1 for alignment; research can start now |
| OVR-8 | Private general override engine and scalar/generated-quality integration | OVR-2, OVR-7; OVR-3 for integration checks |
| OVR-9 | Private collection/grant integration and completion persistence | OVR-8 |
| OVR-10 | Public original-fixture scripted mixed-client acceptance and runbook | OVR-4, OVR-5, OVR-6 |
| OVR-11 | Private real-module mixed-client/VM acceptance and live upgrade handoff | OVR-9, OVR-10 |

The private slices contain no public source-specific examples. Their detailed
quality inventory, reproducer and integration plan live in the owner's local
private workspace. Public issues record sanitized scope and results only.

## Completion and limits

Acceptance proves no change before host verdict or player acceptance; denial,
counteroffer, decline and deliberate revision; permitted/blocked categories;
host direct editing without a guest proposal; two players acting between jobs;
durable reconnect/rebuild; stale/racing/conflicting/retried operations; all-or-none
application; authority loss and privacy. Meaningful original fixtures exercise
different quality categories and absence of universal class/level assumptions.

The private adapter must cover its complete script-backed starting-quality
inventory, not close after a couple of examples work. It proves approved changes
survive normal creation steps and completion without repeated rolls or loss of
original history. It also proves a copied in-progress draft upgrades intact.

Use scripted Python referees only for automated tests/acceptance: no live AI,
inference account or model login. Reuse the reviewed Mac-initiated VM tunnel
and owned-resource cleanup for the private browser check. Preserve the existing
live server, credentials, draft and original pilot while planning/implementing.
Test upgrade, backup and rollback on copies first; changing the live deployment
and resuming its negotiation is a separate owner-coordinated step.

This scope is starting-quality overrides and negotiation. It does not build a
universal rule interpreter, execute user-provided scripts, automatically generate
new mechanics, replace normal creation, introduce approval timeouts, or add
background AI jobs.
