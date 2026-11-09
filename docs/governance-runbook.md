# Campaign governance: operator, host and player runbook

Every adopted campaign on a [lobby host](lobby-runbook.md) has one **campaign
host**: a player who sets the campaign's rules, decides the guests' requests,
inspects everything in the campaign and administers its characters and
world, with or without characters of their own. Every other player is a
**guest**. This runbook covers setting that up, the rules document, requests
and decisions, administration, recovery and backups. The exact rules are in
the [governance contract](designs/governance_contract.md); a recorded run is
in the [acceptance record](acceptance/governance.md). Set up the
[lobby](lobby-runbook.md) and, for characters, the
[character runbook](character-runbook.md) first. Paths, names and IDs below
are placeholders.

Everything here is local: the lobby host and the operator commands run on
the host computer, on loopback, with the browser page or `tesserae connect
--lobby`. Single-game hosts (`serve --database`) have no campaign host and are
unchanged.

## 1. Who decides what

| Who | Decides |
|---|---|
| The operator (whoever runs `tesserae host …` and `tesserae game rules …` on the host computer) | Who the campaign host is, players, tokens, grants and links; may import and activate rules |
| The campaign host | The campaign's rules and exceptions, guests' requests, corrections to any character, NPC or the world; may read every sheet and every message, private ones included |
| A guest | Their own characters, through the referee, and their own requests; may withdraw a pending request |
| The referee | Narration, and the module's own steps for the player it is answering. It may **ask** the host for a decision; it never makes one, and nothing said in conversation (a ruling, a choice, a claim that "the host agreed") changes a rule |

Authority follows the logged-in player, never a character, a name or a
connection. The host's privilege is this campaign's only.

## 2. Setting up a campaign with a host

```sh
tesserae host campaign add --registry host.sqlite3 --database trail.sqlite3 --name Trail --host HOST_PLAYER_ID
tesserae host campaign grant --registry host.sqlite3 CAMPAIGN_ID GUEST_PLAYER_ID
tesserae host campaign link --registry host.sqlite3 CAMPAIGN_ID PLAYER_ID PARTICIPANT_ID   # existing participants only
tesserae host campaign activate --registry host.sqlite3 CAMPAIGN_ID
```

`--host` is required: the campaign is registered **inactive**, with its host
granted and designated in one step. Link any player who already has a
participant in the game (a seeded or older campaign) before activating, so
nobody is admitted as a new participant by mistake.

**Adopting an older campaign** (registered before campaign hosts existed, so
with none): deactivate it, link any player not yet linked, then

```sh
tesserae host campaign set-host --registry host.sqlite3 CAMPAIGN_ID HOST_PLAYER_ID
tesserae host campaign activate --registry host.sqlite3 CAMPAIGN_ID
```

Until it is adopted it plays exactly as before, with no host controls.

## 3. The rules document

A campaign's settings and agreed exceptions are one versioned JSON document
in the campaign's own file, if its module has settings (the original
[Trailhead](trailhead.md#campaign-settings) has a starting stamina bonus).
Importing a file only stores a **candidate**; nothing live changes until it
is activated.

```sh
tesserae game rules import --database trail.sqlite3 --file rules.json   # a candidate
tesserae game rules list --database trail.sqlite3                       # every revision
tesserae game rules activate --database trail.sqlite3 2                  # now live
tesserae game rules export --database trail.sqlite3 --file active.json  # never overwrites
```

Add `--module-dir PACKAGE` for a game whose module is not bundled. A document
that does not suit the module or the game (an unknown setting, a value the
module refuses, an exception for a character that does not exist, another
module's binding) is refused whole, with every problem listed, and nothing is
stored. Activation is refused while the referee is answering. Each command is
one transaction, so it is safe beside a running host.

The campaign host does the same from either client: `/rules list`, `/rules
show [N]`, `/rules import FILE`, `/rules activate N` (asks to confirm) and
`/rules reject N` in the CLI, or **Rules** in the browser's Settings.
Activating names the settings revision you saw: if another change landed
meanwhile it is refused, and you look again.

A new revision never changes an approved character: nothing is recalculated,
rerolled or refreshed. A draft whose settings change gets a new revision, so
an approval shown before is refused and the new sheet shown instead.

## 4. Requests and decisions

A guest who wants something only the host can allow (an exception for their
character, a campaign-wide setting, or a host operation) says so to the
referee. The referee checks that the request is one the rules could allow,
records it exactly, says it waits for the host, and ends the job: nobody is
kept waiting, and others play meanwhile. An unsupported request is refused
at once and never stored.

**Guests** see their requests and the outcomes: `/requests` in the CLI, or
the Console's "Your requests to the campaign host" in the browser, with
**Withdraw** (or `/cancel ID`) for a pending one. `/rules` (or Settings) shows
their settings and requirements.

**The campaign host** sees every request with its exact content and decides
each once:

- CLI: `/requests`, then `/decide ID approve [NOTE]` or `/decide ID deny [NOTE]`.
- Browser: Settings, **Requests**: a note, then **Approve** or **Deny**.
- In conversation: the host may tell the referee in its own job to decide a
  request by its exact ID.

An approval applies exactly what was asked, once, against the game as it was
asked about. If the settings, the draft or the target has changed since, the
request becomes **stale** and nothing changes; the guest asks again. A denial
changes nothing. A second decision on the same request is refused. A request
is cancelled with its character (archived) or its player's link (unlinked).

### Creation overrides from the CLI

In a campaign whose module negotiates creation overrides (Trailhead does),
a change to a **draft's** starting qualities is not approved and applied in
one step. It is asked, offered, and applied only when the player accepts the
exact offer ([creation overrides
contract](designs/creation_overrides_contract.md) section 12). The whole-sheet
`/approve` stays separate. Legacy requests for a draft are refused there
(`override_required`); ready characters, NPCs, the world and campaign
settings keep the requests above.

**Players** (`connect --lobby`):

| Command | Does |
|---|---|
| `/overrides [CHARACTER] [CATEGORY]` | What can be changed on a draft (the selected one by default), or with no draft the categories and their policy |
| `/overrides CHARACTER QUALITY PARAM [MATCH]` | A parameter's options |
| `/overrides more` | The next page of the last `/overrides`, with the same query |
| `/override preview [CHARACTER] INTENT-JSON` | Exactly what an intent would do now; nothing changes |
| `/override request [CHARACTER] INTENT-JSON WHY [--revises ID]` | Ask the host; it waits for the host |
| `/requests overrides [CHARACTER] [open\|terminal] [more]` | Your requests, saying whose response is awaited ("waiting for the host", "offer N waiting for you, not applied yet") |
| `/offer ID`, `/offers ID [more]` | A request and its offer, or every offer revision |
| `/accept OFFER` | Shows the exact offer; `/accept OFFER confirm` applies exactly that one |
| `/decline OFFER [NOTE]` | Declines it |
| `/override history [CHARACTER] [more]`, `/override record ID` | What was applied, by whom, with the module's receipt |

**The campaign host** (no character needed):

| Command | Does |
|---|---|
| `/requests` | The legacy list, then the first page of open override requests |
| `/decide ID offer [NOTE]` | Shows what it would do now; `/decide ID confirm` offers it |
| `/decide ID counter INTENT-JSON NOTE` | Shows the counteroffer's preview; `/decide ID confirm` offers it |
| `/decide ID deny NOTE` | Denies it |
| `/withdraw OFFER NOTE` | Withdraws an offer not yet accepted (an accepted one stays applied) |
| `/override apply CHARACTER INTENT-JSON REASON` | Shows the preview; `/override apply confirm` applies it directly, with no request |

A confirmation sends the digest of the preview it showed. If the draft
changed meanwhile, the host refuses it (`stale_preview` or `offer_stale`),
the client shows what it would do now, and nothing is applied or resent:
review it and confirm again. Only the latest review can be confirmed:
starting another review, another confirmation, or a change of role clears
it, and a reply to an older or failed review never makes it confirmable
again. Previews show each effect's quality with its values before and
after, not only the module's words; with `--json`, the client's own lines
go to stderr.
`/decide ID approve` on a negotiated request is refused locally with a hint
to use `offer`. Every ID and preview is read from the host again after a
reconnect; nothing is kept on disk.

### Creation overrides in the browser

The same negotiation, in the panels players and the host already use:

- **Console, "Your override requests"** (every player): each request with
  whose response it waits for ("Waiting for the host", "Offer N waiting for
  you: offered, not applied yet", "Applied", or why it ended) and the host's
  note. An open offer is shown in full under **Offered, not applied yet**:
  its headline, each change with its values before and after, waivers,
  allowances (they end at approval), later steps, consequences and what the
  module cannot do. **Accept…** asks to confirm, naming the offer revision,
  and sends the digest of what was shown; **Decline** takes an optional
  note. An open request has **Withdraw request**; an ended one **Revise**,
  which starts a new request from its intent, linked to the old one.
  Approving the whole character stays the separate **Approve**.
- **Settings, "This campaign"**: which kinds of change the host may be asked
  for, and which are not taken.
- **Settings, Requests** (the campaign host): the open override requests.
  **Review** shows what each would do now; then **Offer**,
  **Counteroffer…** (the intent as one JSON object, prefilled from the
  request, then **Review**, a note, and **Send the counteroffer**), **Deny**
  (with a note) or, on an open offer, **Withdraw the offer** (with a note).
  Editing a reviewed counteroffer drops its review.
- **Settings, Override** (the campaign host, no character needed): a draft's
  ID, **What can change** (optionally in one category, or a quality's
  parameter options, with a match; **Show more** continues the same
  lookup), the intent, **Review**, a reason, then **Apply**. No request or
  acceptance is involved. Editing the draft or the intent while a review
  loads drops it: only the change shown is ever applied.

A change since the review is refused with nothing applied, and the page
says to look again. Every request, offer and preview is read from the host
on joining, after a refresh and whenever a request changes; nothing is kept
in the browser. The Console lists only the player's own requests, even for
the campaign host, and everything read under one role is dropped when the
role changes. Another guest's page shows none of it, and the host panels
close the moment the page's player is no longer the campaign host.

### Consent, notices and what ends a request

What the negotiation guarantees, from both clients:

- **Nothing applies before acceptance.** An offer or counteroffer changes
  nothing on the draft. Only the requester's acceptance of that exact offer
  applies it, by the digest of the preview they were shown, and only while
  it still holds. A referee job accepts only on the player's explicit word.
  The campaign host's own referee job may offer or deny on the host's
  explicit word; a guest's job cannot.
- **Notices.** Each step tells the requester's and the current host's
  connections (`[request] ID is now STATE.`, or the Console and Requests
  panels). A host may deny straight from the notice
  (`/decide ID deny NOTE`).
- **When an offer no longer holds** it shows why: `preview_changed` (a
  relevant change, such as another override applied to the same quality),
  `host_changed` (the host was transferred), `intent_invalid` (the draft can
  no longer take the offer as it stands, for example the item it would
  remove is gone) or `completed` (the draft was completed). What happens next
  depends on the request itself, not on that reason. Read the request's
  state (`/requests overrides`, or the Console):
  - **It waits for the host again** (`pending_host`) when the player's
    original request can still be done as asked. This holds even if the
    offer was a counteroffer that became invalid. The host reviews it and
    offers again; the player never asks twice.
  - **It ends as `stale`** when the original request can no longer be done
    as asked, or its draft was completed. The player revises it
    (**Revise**, or `/override request ... --revises ID`) or asks for
    something else.

  Unrelated creation steps and other players' turns leave an offer current.
  So do new settings that do not change what the offer would do. A module
  may make a preview depend on settings, and then a settings change that
  alters it stales the offer.
- **Host transfer.** A request waiting for a verdict waits for the new host.
  An offer the former host made no longer holds; the new host offers again.
- **Withdrawal** ends the request (`withdrawn`); the player asks again if
  they still want it. An offer accepted first cannot be withdrawn, and
  nothing applied is ever rolled back: a later change is a new override.
- **Completion and archiving.** Completing a draft ends its open requests
  (`stale`) and expires unused creation-only allowances, such as Trailhead's
  gear picks. The expiry is recorded with what remained; what was used stays,
  with its authorization history. A ready character cannot spend a leftover
  allowance. Archiving a draft cancels its open requests.
- **Access loss.** While a player's access is revoked they cannot accept,
  and their client leaves the campaign. Nothing of the offer applies. With
  access restored, the same offer can still be accepted if it holds.
- **Direct changes.** The campaign host applies a change with no character
  and no request (`/override apply`, or **Settings, Override**). Category
  policy never limits the host. Each change is recorded as `direct`, with
  its reason and receipt, in the character's override history.

### Upgrading an existing campaign

Back up the registry and the campaign files first (section 7). An
installation with negotiated overrides upgrades a campaign file in place
the first time it serves it (schema 10). The module state, every settings
revision and every decided request are kept as they were: a legacy
exception already applied to a draft stays applied. A request for a draft
still pending is superseded, with the note "Negotiated creation overrides
replace this request; ask again"; nothing of it is applied, and the player
asks again as an override. From then on a legacy exception or operation
request for a draft is refused (`override_required`). Ready characters, NPCs,
the world and campaign-wide settings keep the legacy requests above. In a
campaign whose module does not negotiate overrides, nothing changes: the
override messages are refused (`override_unsupported`).

Run `tesserae referee setup --referee-dir DIR` again with the new
installation, so the referee's bridge is the new one.

### When an override is refused

| Code | Meaning | What to do |
|---|---|---|
| `override_required` | A legacy request for a draft | Ask with `/override request` (or the referee) instead |
| `override_blocked` | The request names a category the campaign's settings block | Ask about another quality, or ask the host to change the settings; the host can still apply it directly |
| `override_unsupported` | The module does not negotiate overrides, or has no such quality, action or parameter | `/overrides` lists what can be asked |
| `invalid_intent` | The module cannot do that as asked: the problems are shown | Change the intent; a change that changes nothing is refused too |
| `stale_preview` | The preview confirmed is no longer what the change would do | Review it again; the client shows what it would do now |
| `offer_stale` | The offer no longer holds (its reason is shown) | If the request waits for the host again, wait for a fresh offer; if it ended `stale`, revise it or ask anew |
| `offer_unavailable` | No such open offer: withdrawn, accepted, or someone else's | `/requests overrides` shows its state |
| `proposal_unavailable` | Not your request (or none by that ID) | |
| `proposal_decided` | Another decision landed first | Read the request again |
| `request_conflict` | A request ID reused for something else | Use a new request ID |
| `not_campaign_host` | A host-only step from a guest, or from a former host | |
| `context_too_large` | A module's context alone is over one tool result | The module's budgets need lowering; see the [creation overrides contract](designs/creation_overrides_contract.md) section 11 |

## 5. Inspecting and administering

The campaign host needs no character for any of this, and can do it while
playing one.

| | CLI | Browser (Settings) |
|---|---|---|
| Every sheet, drafts included, the module's data and the referee's preparation | `/inspect [CHARACTER]` | **Inspection**: Inspect the campaign |
| Every message, private and creation ones, and to whom | `/history-all [N]` | Show every message |
| What hosts changed | `/audit [N]` | Show what hosts changed |
| What can be changed now | `/admin` | **Administration**: Show the operations |
| One change | `/admin COMMAND ARGS-JSON`, then `/admin confirm` | choose, edit the arguments, **Review**, **Run it** |
| Hand the campaign over | `/transfer PLAYER_ID`, then `/transfer PLAYER_ID confirm` | **Hand the campaign over** |

A change runs only at the game revision you reviewed: if anyone played or
another edit landed first, it is refused, and you review it again. A change
refused because the referee was answering stores nothing; `/retry` (or
**Retry exactly**) resends exactly the same request once it is free, but
never after your role or campaign has changed. Changes never roll dice and
never change who owns a character or what was played; each is recorded
under the host, apart from play, with an audit row. What a module lets the
host change is the module's own (Trailhead: a walker's stamina, trait, gear
and description, NPCs, the scene and referee-only preparation).

Guests cannot reach any of this: their host commands are refused by the host,
and host data never appears in their pages or clients.

## 6. Transfer, recovery and revocation

- **Transfer** (the host's own choice): `/transfer` or **Hand the campaign
  over**. The new host must already have access and an active token. Every
  connection of the campaign is told its new role at once; the former
  host's host panels and pending changes are cleared.
- **Recovery** (a lost host token, or a host who has left): the operator's
  `tesserae host campaign set-host --registry host.sqlite3 CAMPAIGN_ID
  PLAYER_ID` alone replaces the host; connected clients learn it at their
  next request.
- **Rotation and revocation:** after `tesserae host player rotate` or
  `revoke`, the player's sessions with the old token are refused (at their
  next request at the latest). A rotated host stays the host and logs in with
  the new token; a revoked host stays designated but powerless, and
  nobody is promoted automatically: use `set-host`.
- The host's own grant cannot be revoked until someone else is host.
- **Reconnect and restart:** pending requests, decisions, rules and the
  audit are in the campaign's file and survive a host restart. A client that
  reconnects learns its role again from the host; the browser shows no host
  data until it does.

## 7. Backups

The campaign's rules, requests, decisions, host changes and their audit are
in the campaign's own file; the campaign host's designation and its history
are in the registry. Back both up as the
[lobby runbook](lobby-runbook.md#9-storage-and-backups) says, together, with
the referee folder's `codex-home/sessions/`. Restoring the registry restores
who the host was at backup time; restoring a campaign file restores its rules
and requests as they were.

## 8. Limits

- The referee in the recorded acceptance was scripted, under the
  [model-free testing policy](testing.md): it shows the application routes
  requests, decisions and host tools correctly, not how a model negotiates or
  explains them. Live model play needs an explicit owner request.
- Loopback only. Browser access from another computer, and the private
  campaign integration with its own module, are separate work (the private
  tracker).
- One campaign host per campaign; no co-hosts, and no standing grants: an
  approval applies once and never lets a guest use host tools.
- The browser's Administration takes arguments as one JSON object prefilled
  from the module's description, not a generated form.
