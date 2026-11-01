# M2-1: localhost browser player console

Status: design draft. The owner accepted localhost-only browser delivery,
console first, and the navigation layout below on 2026-11-01. No tracker or
children have been filed for this plan, and no browser runtime is implemented.
Baseline: M1-6 completed through PR #80 at `b4384f2`.

## Outcome

Two authenticated players can use the existing referee workflow in a browser
on the host computer: join, converse, inspect their own state, answer a
pending question and recover after a disconnect or refresh. The CLI remains
compatible with that same host, game and referee.

The first screen is Console. A collapsible column on the left contains
exactly two navigation items: **Console** and **Settings**. Interactive maps
and browser access over a LAN follow this tracker.

## Accepted product direction

- Start with localhost only and the console workflow. Browser certificate
  trust and LAN access are later work; existing encrypted CLI access remains.
- Use the proven authoritative host and resumable Codex referee. Every player
  gameplay request passes through the referee. Browser controls do not invoke
  raw mechanical commands or edit game state themselves.
- Console opens and is selected by default. Settings is the other screen in
  the collapsible left navigation. Neither screen is a separate game session.
- Keep the public original demonstration and approved external private scene;
  no new game mechanics or private material enter the public repository.

## Screen design

### Application shell

The left navigation starts expanded. A separate, labelled toggle collapses
it to compact navigation; both items remain reachable and identifiable with
keyboard focus, accessible names and visible hints. Console/Settings show
which screen is selected. A narrow window must leave the main content usable.
The initial selection is Console rather than a remembered last-open screen.

Changing screens or collapsing the column preserves the live connection,
received messages and unsent draft. It does not submit input, change the
referee job, or advance game time. The app has one connection/session owner
shared by both screens, not one socket per screen.

### Console

- A clear connection and referee busy/idle indicator.
- Ordered table conversation, with private messages explicitly distinguished.
- A plain-text composer, Send button and explicit private-message control.
  Text that resembles code or JSON remains player speech.
- The player's pending question, own projected state and last-input status.
- Refresh-state and explicit retry/reconnect controls, with useful errors.
- An initial connection prompt or link to Settings when not joined. Opening
  Console must not require choosing Settings as a hidden prerequisite.

While busy, preserve the draft and explain that new input must wait. Host
refusals remain authoritative if the display is behind. Inspection stays
available. A pending question ends the current job rather than keeping the
composer indefinitely blocked.

### Settings: proposed initial contents

Keep the first Settings screen small: current connection/game/participant
information, selecting or pasting a personal token, explicit join/disconnect
and change-identity controls, and local display preferences where needed.
The connection is to the page's host; this is not a remote-server directory.
These contents are an implementation proposal, not additional owner-selected
features. Future settings can be added when their owning feature exists.

Read a token only from explicit user input or a user-selected file. Keep it
in memory, never in the URL, browser storage or logs; clear the entry field
after joining. A refresh requires providing the token again. Disconnect or
identity change must clear the previous identity's visible/cached data and
draft, so another participant does not inherit it. Ordinary navigation to
Settings does not disconnect or clear the Console.

Host administration and Codex login remain in the operator CLI. No browser
controls for database paths, module installation, referee permissions, model
configuration, or host stop/rebuild are required by this tracker.

## Engineering boundaries to specify in WEB-1

- Serve packaged HTML/CSS/JavaScript and the WebSocket from the same foreground
  host and origin, behind an explicit browser-serving option. Localhost browser
  mode requires authentication and a referee-enabled game. Preserve existing
  CLI-only operation and connection URLs.
- Use explicit packaged asset routes, not a general file server rooted at a
  checkout, database directory, private module or referee home. The page shell
  can be public; all game data still requires an authenticated join.
- Define acceptable browser origins while preserving legitimate CLI clients
  without an Origin header. Do not silently widen the loopback bind.
- Reuse the version-1 join, say, status, messages, sync and notification shapes.
  Keep protocol parsing/session recovery separate from the screen components.
  Render player/referee strings as text, not executable markup.
- State displays consume only the host's recipient projection. Give the
  original demo a readable view and support unknown module fields with a
  structured read-only fallback. Do not impose universal character/inventory
  fields or add a UI-plugin framework.
- Keep pending questions/resource declarations under player control. Browser
  presentation is not a second sheet or an automatic mechanical decision.
- Specify disconnect, refresh, uncertain-send and explicit retry behavior.
  Retrying preserves request ID, text and privacy; no automatic gameplay
  resubmission. Reconcile history/live messages by sequence, including replay
  duplicates. Do not assume recipient-visible sequences have no gaps.
- Define any per-tab recovery record before using browser storage. It must be
  scoped to the authenticated origin/game/participant, contain no token, and
  not restore another identity's private text. Display/recovery caches are not
  authoritative game state. Explicitly cover storage unavailable or cleared.

Packaged HTML/CSS and ordinary JavaScript modules are the initial engineering
proposal. Use existing library capabilities for transport and a few real-browser
workflow tests; avoid building a terminal emulator, frontend plugin system, or
an extensive UI test framework.

## Proposed tracker and children

Tracker title: **M2-1: play the existing referee workflow in a localhost browser**.
The sidebar is a separate delivery slice so its two-screen behavior can be
reviewed independently of network/recovery work.

| Child | Scope | Depends on |
|---|---|---|
| WEB-1: Specify the browser contract | Concrete serving/join/origin, screen, token and recovery boundaries; define the bounded acceptance and any necessary additive protocol change | M1-6 |
| WEB-2: Serve the packaged browser application | Explicit loopback browser-serving option, allowlisted assets and origin handling; foreground lifecycle and CLI compatibility | WEB-1 |
| WEB-3: Build the Console/Settings shell | Collapsible left navigation, exactly two items, Console selected/open by default, accessible controls, narrow-window layout and shared screen state | WEB-2 |
| WEB-4: Join and converse through the host | Settings connection controls and Console join prompt; authenticated join, table/private composition and delivery, busy/status/error presentation, plain text only | WEB-3 |
| WEB-5: Show recipient state and pending questions | Read-only module views, own pending question and last-input state, refresh after revisions, useful fallback for unfamiliar fields | WEB-4 |
| WEB-6: Recover browser play safely | Disconnect/rejoin, refresh, history reconciliation, exact uncertain-input retry, draft/navigation behavior and identity isolation | WEB-4 |
| WEB-7: Verify both browser workflows and publish the runbook | Real browser acceptance, public/private pinned evidence, CLI interoperability, focused CI checks and operator instructions | WEB-5, WEB-6 |

WEB-5 and WEB-6 can proceed independently after WEB-4. Keep each issue's tests
focused on its actual behavior. Public work uses ordinary PRs with related
docs; private adaptation/evidence remains local and receives named-head review.

## Acceptance

- Open the served page: Console is selected and visible. Both navigation
  items remain usable when collapsed. Switching to Settings and back during
  a job preserves the session and draft without causing an extra submission.
- Use two independent authenticated browser contexts for the original scene;
  also demonstrate a browser and CLI participant sharing the same game.
- Show a question without mutation, a consequential action through Codex,
  pending input that releases busy, table and private replies, own state,
  simultaneous-input arbitration and revocation.
- Refresh/disconnect around acceptance and completion; restore authoritative
  state/history and explicitly retry with no duplicate job, draw or spend.
  Cover history/live overlap, identity changes and stale busy displays.
- Use a few automated browser workflow tests with scripted referee output;
  keep public CI independent of an account or private material. Run a bounded
  live Codex browser demonstration at pinned revisions.
- Repeat the existing approved private scene in a local acceptance workspace,
  without expanding its mechanics or resuming the original campaign. Keep
  full private review/logs local and publish sanitized status only.
- Existing public checks and CLI workflows stay green. Test packaged assets
  from an installation outside the checkout. Clean up test processes,
  participant credentials and token files, retaining meaningful evidence.

## Deferred scope

LAN browser trust/setup, interactive maps, character editing or direct gear
buttons, new game mechanics, queues, automatic model-context optimization,
browser host administration, remembered login/cloud accounts and a generic
frontend extension framework are outside this first tracker.

## References

- [Existing browser notes](../browser-client-notes.md).
- [Referee protocol](../protocol.md#referee-mode) and
  [operator runbook](../referee-runbook.md).
- [websockets server hooks and origin controls](https://websockets.readthedocs.io/en/stable/reference/asyncio/server.html).
- [First-message authentication](https://websockets.readthedocs.io/en/stable/topics/authentication.html#first-message).
- [User-selected files in a browser](https://developer.mozilla.org/en-US/docs/Web/API/File_API/Using_files_from_web_applications).
