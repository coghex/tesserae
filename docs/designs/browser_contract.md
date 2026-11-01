# M2-1 browser contract (WEB-1)

Status: specification for [#83](https://github.com/coghex/tesserae/issues/83)
under tracker [#82](https://github.com/coghex/tesserae/issues/82). Nothing in
it is implemented yet. It turns the
[M2-1 delivery plan](browser_player_design.md) into exact contracts for WEB-2
to WEB-6 ([#84](https://github.com/coghex/tesserae/issues/84) to
[#88](https://github.com/coghex/tesserae/issues/88)). The wire messages are the
existing [protocol](../protocol.md) and
[referee contract](referee_contract.md#7-messages-and-the-reply-schema); this
document adds one snapshot field (section 11) and nothing else to the wire.

Checked against websockets 17.4 (section 14). Browser checks target current
Chromium; other browsers should work but are not tested.

## 1. Decisions this contract follows

Owner decisions (2026-11-01, tracker #82):

- The browser runs on the host computer only (loopback). Other computers keep
  using the encrypted CLI. LAN browser access, certificate trust and maps come
  later.
- A collapsible left navigation with exactly two items, Console and Settings.
  Console is selected and open when the page loads, never the last screen used.
- Tokens are kept in memory only: no URL, browser storage, cookie or log. A
  refresh needs the token again. Remembered login and display preferences are
  later work.
- All gameplay is plain-text input to the referee. The browser never sends a
  game command, rolls, edits state or makes a player's choice.

Engineering choices made here, within that scope:

- Browser serving is a `serve` option, on the same port and origin as the
  WebSocket, so existing connection URLs keep working (section 2).
- Every host refuses a WebSocket handshake from an unexpected browser origin,
  in browser mode or not (section 3). CLI clients send no Origin and are
  unaffected.
- In a narrow window (under 40rem) the navigation starts collapsed instead of
  expanded, so the content stays usable (section 5).
- Reconnecting is one explicit click, not automatic (section 7).
- The snapshot gains the participant's own display name, so a page can say
  who it is joined as (section 11).

## 2. Serving

`tesserae serve --authenticated --browser --database DB --port PORT`

- `--browser` requires `--authenticated` and a game in referee mode. It refuses
  `--tls-cert`/`--tls-key` and any `--host` other than the default
  `127.0.0.1`: browser certificate trust is later work. Each refusal exits 2
  before anything is opened or bound, with a message naming the conflict.
- Without `--browser`, `serve` behaves exactly as today, except for the Origin
  rule in section 3.
- The existing listening line is unchanged (scripts read it). Browser mode
  prints one more line after it: `Browser: http://127.0.0.1:PORT/ (this
  computer only; join with a personal token)`, with the bound port.
- The page and the WebSocket share one origin and port. A request with
  `Upgrade: websocket` is a WebSocket handshake, at any path, as today; anything
  else is a page request (section 3). `ws://127.0.0.1:PORT/` keeps working for
  the CLI, and the page connects to `ws://` plus its own `location.host` and
  `/`.
- Lifecycle is unchanged: the foreground host stops on Ctrl-C, closing page
  requests and sockets alike, and leaves no listener behind.

## 3. Requests

In browser mode, the host knows two loopback names for itself,
`127.0.0.1:PORT` and `localhost:PORT`, with the actual bound port (`--port 0`
included). Its two page origins are `http://` plus each.

| Request | Response |
|---|---|
| Browser mode, `Host` header not one of the two names | 421, before anything else (blocks DNS rebinding) |
| Handshake with no `Origin` header | Handshake as today (the CLI) |
| Handshake with `Origin` one of the page origins, browser mode | Handshake as today |
| Handshake with any other `Origin`, any mode | 403 |
| Browser mode, `GET` of a listed asset path, no query | 200 with the asset |
| Browser mode, `GET` of any other path, or with a query | 404 |
| Browser mode, any other method | 405, `Allow: GET` |
| Without `--browser`, a non-handshake request | As today |

Every response the host builds carries no game data and no request detail.
Until the bound port is known, page requests get 503.

Asset responses carry:

- `Content-Type` by extension: `text/html; charset=utf-8`,
  `text/css; charset=utf-8`, `text/javascript; charset=utf-8`;
  `image/svg+xml` if icons are files;
- `Cache-Control: no-store` (the assets are small; an upgrade never runs stale
  code);
- `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Cross-Origin-Opener-Policy: same-origin`;
- `Content-Security-Policy: default-src 'none'; script-src 'self';
  style-src 'self'; img-src 'self'; connect-src ws://127.0.0.1:PORT
  ws://localhost:PORT; base-uri 'none'; form-action 'none';
  frame-ancestors 'none'; require-trusted-types-for 'script'`. Both socket
  addresses are listed explicitly rather than relying on `'self'`. No inline
  script or style, `eval` or remote resource.

## 4. Packaged assets

- The page is static HTML, CSS and plain JavaScript modules in the
  `tesserae.web` package, declared as package data, with no build step,
  framework or runtime dependency.
- The host reads a fixed table of URL path to resource (`/` is `index.html`)
  into memory when browser mode starts. It never maps a URL onto a directory,
  so no path can reach the checkout, database, module package or referee
  folder. A missing resource is a startup failure (exit 1, "the installation
  is incomplete"), not a 404 later.
- The page shell is the same for everyone and holds no game data. Everything
  about a game arrives only over a joined socket.
- Protocol and session handling (sections 7 and 9) live in their own module,
  separate from the screens, with no DOM access. Screens render from one
  application state; they never talk to the socket themselves.
- Every string from the host, a player, the referee or a module is put into
  the page as text (`textContent`), never as markup. The Trusted Types policy
  in the CSP makes an accidental `innerHTML` with a string fail.

## 5. The shell

- A left column, `<nav aria-label="Main">`, with a toggle button above two
  items, Console and Settings, in that order. There is no other navigation
  item, link or screen.
- On load, Console is selected and shown. The URL never changes with the
  screen (no hash or history routing), so a refresh always opens Console.
- The toggle is a `<button>` with `aria-expanded` and `aria-controls`, labelled
  "Collapse navigation" or "Expand navigation". The column starts expanded,
  unless the window is under 40rem wide when the page loads; resizing later
  never overrides the player's choice. The state is kept in page memory only.
- Expanded, each item shows an icon and its name. Collapsed, it shows the icon,
  keeps the name as its accessible name, and shows the name as a tooltip on
  hover and focus. The selected item has `aria-current="page"` and a visible
  marker that is not color alone. All three controls are reachable by Tab in
  that order, with a visible focus ring, and work with Enter and Space.
- Both screens stay in the document; the unselected one is `hidden`. Changing
  screens or collapsing the column touches only the display: it never sends,
  connects, disconnects or clears anything.
- At 320 CSS pixels wide, with the column collapsed, the main content has no
  horizontal scrolling and the composer is usable.

## 6. Settings and identity

Settings shows, when joined: the connection state, the host (the page's own
`location.host`), the game ID, the participant's name and ID, and the module ID
and version. Its controls:

- **Token entry:** a password-type field (`autocomplete="off"`, not inside a
  `<form>`) and a file chooser. A chosen file is read with the File API and
  follows the CLI's token-file rule: the token alone, UTF-8, an optional
  trailing newline removed. The page never reads a file the player did not
  choose. Console shows the same entry and Join button whenever it is not
  joined, so Settings is never a hidden first step.
- **Join:** opens the socket and sends `join` (section 7). The entry field is
  emptied as soon as the join is sent. The token is then held in one
  JavaScript variable, for reconnecting, until disconnect, identity change or
  a refusal. It is never written to a URL, `localStorage`, `sessionStorage`,
  IndexedDB, a cookie, the console or an error message.
- **Disconnect:** closes the socket and clears the identity: the token, the
  conversation, view, referee status, last input, pending sends, the draft and
  the private toggle. Console returns to the join prompt.
- **Change identity:** the same as Disconnect, then focuses the token entry.

A refused join or a revoked session (`authentication_failed`,
`session_revoked`, or any session error that closes with 1008) also clears the
identity, with a readable reason. A dropped connection does not (section 7).

## 7. The session

**Join.** The page opens `ws://<location.host>/` only after the player asks
to join, so the host's 10-second join timeout never runs out on an idle page.
The first message is `join`. The `snapshot` reply sets the participant, the
module, the view and the referee projection (`state`, `choices`, `last_seq`,
`your_last_input`). The page then sends `messages` with `after_seq` 0, on
every join and reconnect alike, and merges the reply into what it holds
(below). It keeps no "complete through" cursor: messages can arrive out of seq
order (an exact replay's stored reply can come before earlier messages still
queued for delivery), so the highest seq held proves nothing about the ones
before it. The whole visible history is one reply, which is small at this
scale.

**Pairing.** The host answers each text message with exactly one reply, in
order, so the session keeps a first-in, first-out queue of what it sent and
pairs each reply with the oldest. `revision`, `message` and `referee` are
unsolicited and never pair. Several requests may be outstanding at once.

**Keeping current.**

- `revision` newer than the displayed view's: send `sync`, unless one is
  already outstanding; when its reply comes, sync again if a newer revision
  arrived meanwhile. Revisions may be skipped (they coalesce).
- `referee` (`busy` or `idle`): set the shown state at once, then send
  `referee_status`, coalesced the same way. A new question or a finished input
  changes the referee projection without any revision, so this is the only way
  the page learns it.
- A `snapshot` replaces the view and the referee projection; a
  `referee_status` replaces the projection. Neither is ever relabelled: the
  page shows the revision it was read at.

**Conversation.** Messages are kept by seq. Each arriving `message`, `messages`
entry or replayed reply is inserted in seq order, and a seq already held is
ignored, so arrival order never matters. Seqs a player cannot see are
legitimately missing, so a gap is never treated as loss. The page asks for
history only at join and reconnect; in between, the open connection delivers
everything stored after the history was read.

**Busy.** The shown state is a hint. The host decides: a `say` refused with
`referee_busy` keeps the text in the composer and shows who the referee is
answering (`job.for`). Reading (status, sync, history, Settings) is always
available.

**Closed connections.**

| Close | What the page does |
|---|---|
| 1008 with a session error (refused, revoked) | Clears the identity (section 6) and shows the reason |
| Anything else (host stopped, 1001; network, 1006; 1011; slow, 1013) | Keeps everything shown, marked "disconnected, may be out of date"; keeps the token and draft; offers **Reconnect** |

Reconnect joins again with the held token. If the snapshot's participant
differs from before, the page clears the old identity first. Every socket's
events are tied to its session object, and a closed or replaced session's
late events and replies are dropped, so nothing from a previous connection or
identity can appear.

## 8. Console

From top to bottom:

- **Status line:** who the page is joined as (`participant_name`), the
  connection state and referee state (`idle`, `busy`, or "answering NAME" when
  known), plus a Reconnect button when disconnected.
- **Conversation:** a `role="log"` list, oldest first, of
  `Sender: text` lines with `Referee` or the player's name. Private messages
  carry a visible "Private" label, not only a color. Newlines in text are kept.
- **Pending question:** the viewer's own entry in `choices`, as "The referee
  asks you: ...". It is answered by typing, like any input; there are no
  answer buttons.
- **Your last input:** `your_last_input`'s text, privacy and job state, with a
  **Resend exactly** button (section 9).
- **State:** the player's view (section 10).
- **Composer:** a text area, a "Private: only the referee sees this" checkbox
  and Send. Enter sends; Shift+Enter adds a newline. Text that looks like JSON
  or a command is sent as words. The page checks only that the text is not
  blank and at most 2000 characters; the host decides the rest and its error
  is shown. The private box resets to off after each accepted input, as the
  CLI's `/private` applies to one line.

## 9. Sending, retrying and refreshing

**One input.** Send creates a new request ID, `b-` and 16 random hex
characters from `crypto.getRandomValues`, and sends `say` with the text and
privacy. Only a new intention gets a new ID. The input is then:

| Reply | Then |
|---|---|
| `accepted` | The composer empties; this becomes the page's last accepted input |
| `referee_busy` or another error | The text stays in the composer as a draft, with the reason; nothing is remembered as sent |
| None, the connection closed first | The input is **unconfirmed**: shown with its text, kept in memory with its ID and privacy |

After a reconnect, an unconfirmed input whose ID equals
`your_last_input.request_id` was accepted, and is shown as such. Any other
unconfirmed input stays **unconfirmed**: `your_last_input` is only the
participant's latest accepted input, and a newer one from another page or the
CLI can replace it, so not being latest does not mean not accepted. The page
says it does not know whether the referee received it, and offers **Resend
exactly**, which settles it: the host returns the recorded `accepted` and
replies if it had accepted it, busy or not, and otherwise takes it as new. So
a `referee_busy` reply to a resend proves it was never accepted, and it goes
back to the composer as a draft. **Edit** makes it a draft again, warning that
if the first was received, sending the edited text is a second input with a
new ID. Nothing is ever resent automatically.

**Resend exactly** sends the same request ID, text and privacy again. The
target is the page's unconfirmed input if there is one; else its last
accepted input (an input later refused as busy does not replace it); else the
host-reported `your_last_input`. This is the CLI's `/retry` rule. The host
returns the recorded `accepted` and the stored replies, never a second job,
roll or spend; if it had never accepted the input, it is taken as new.

**Refresh.** A refresh ends the page's memory. The player provides the token
again; the page joins, loads the visible history and shows `your_last_input`
with its state. It shows once, in the Console, that text which was never sent,
or never accepted, is not kept anywhere and is gone. It never fills the
composer with `your_last_input` or calls it the lost draft.

**Drafts.** The composer's text lives in the application state, so screen
changes and dropped connections keep it. It is cleared only by an accepted
send, Disconnect, Change identity, a refusal or a refresh.

One participant may be joined from several pages and the CLI at once; each
keeps its own unconfirmed and last inputs, and the host's replay keeps them
safe.

## 10. State

- The panel renders the snapshot's `view` read-only and nothing else. It shows
  the revision it was read at, and "may be out of date" while disconnected.
- A renderer is chosen by `module.id`. Relay gets a readable panel: the scene
  title, description and whether the relay is restored; the player's worker
  with its fields, or "no worker yet". `commands` and `rules` are not shown:
  players talk to the referee instead.
- Any other module gets a generic tree: object keys as labels, strings and
  numbers as text, `null` as "none", lists and objects nested, at most six
  levels and 200 entries per level with "N more" after. Nothing is assumed
  about character, inventory or turn fields, and a missing or empty view
  shows "nothing to show yet".
- The panel never offers an action. Another player's hidden values are not in
  the payload at all: the host's projection is the only source.

## 11. Protocol change: the participant's name

The snapshot carries `participant_name`, the joined participant's own display
label, in authenticated mode (join and sync). The page must say who it is
joined as, which matters when two players share the host computer, and only
the opaque `participant_id` is available today. Messages keep their existing
`from.name`; the page does not guess which are its own, since names need not
be unique.

- It is the caller's own name only, never a roster, and it does not select
  an identity.
- It is additive: the CLI already ignores unknown snapshot fields
  (`_is_snapshot` checks only the fields it uses), and it does not join the
  fields a session must keep constant.
- WEB-3 implements it, with a test, and documents it in
  [protocol.md](../protocol.md#authenticated-sessions).

## 12. Tests and tooling

- **Python checks** (the existing suite, always run): request handling
  (section 3: assets, 404, 405, Host, Origin, headers and CSP), `serve`'s
  refusals (section 2), the CLI still joining a browser-mode host, and
  `participant_name`.
- **Browser checks:** Python Playwright driving Chromium, in
  `tests/test_browser_*.py`, run by `unittest` like the rest. Playwright is an
  optional extra (`.[browser-test]`, pinned exactly, like Ruff), never an
  application dependency. Without it, browser tests skip; with
  `TESSERAE_BROWSER_TESTS=1` set, a missing Playwright fails instead.
- **CI:** a second job installs `.[dev,browser-test]`, runs
  `python -m playwright install --with-deps chromium` and the browser tests,
  with a 10-minute limit. The existing job is unchanged.
- **Inputs:** an in-process host with a scripted `referee_executor` (as in
  `tests/test_referee_host.py`), original fixtures and temporary folders. No
  Codex, model account, private file or network beyond loopback.
- **Budget:** about 15 browser tests across WEB-2 to WEB-5, each a workflow
  (two players in two browser contexts where it matters), not a check per
  control. No browser matrix, screenshot comparison or custom framework.
- **Installed package:** CI's existing outside-the-checkout step also checks
  that `tesserae.web`'s resources are installed.

## 13. Children

| Child | Sections | Its checks |
|---|---|---|
| WEB-2 (#84) | 2 to 5, 12 tooling | Python: section 3's table and headers, `serve` refusals, the CLI joining a browser-mode host, missing-asset startup failure, packaged resources installed. Browser: the page loads with Console selected; Settings and back; collapse and expand by keyboard with names kept; 320px collapsed layout; a disconnected Console with a join prompt and no simulated join |
| WEB-3 (#85) | 6, 7, 8, 11 | Join with a pasted token and a chosen file; bad and revoked tokens; two players in two contexts; table and private delivery; JSON-looking text as words; busy refusal keeping the draft; Settings during a job keeps one socket and the draft with no extra `say`; change identity leaves nothing of the old one; a CLI player in the same game |
| WEB-4 (#86) | 10, and 7's keeping current | Relay panel matches the stored projection; another player's worker absent from the payload and DOM; a pending question only its player sees, cleared once answered; a question arriving with no revision change; the generic tree on an unfamiliar view |
| WEB-5 (#87) | 7's closed connections, 9 | Connection lost before and after `accepted`; an unconfirmed input matching `your_last_input` shown accepted; one replaced as latest by another connection's newer input still unconfirmed, and its exact resend returning the recorded reply with no second job; a replay delivered before earlier queued messages, then a reconnect, losing none of them; refresh then rejoin showing the accepted input and the lost-draft notice, with nothing resent; busy refusal not replacing the resend target; history and live overlap; host restart and Reconnect; no late event from a closed session |
| WEB-6 (#88) | all | The tracker's live acceptance and runbook, with these checks green in CI |

## 14. Feasibility evidence (2026-11-01)

A probe on websockets 17.4 (`websockets.asyncio.server.serve` with a
`process_request` hook, one port) confirmed:

- one listener can answer a plain `GET /` with an HTML page and a missing
  path with 404, and still complete WebSocket handshakes on the same port;
- a CLI-style client with no `Origin` header connects; a page origin
  (`http://localhost:PORT`) connects; another origin gets 403;
- a `Host` header naming another site gets the hook's 421;
- **`POST` and `HEAD` reach the hook too and would be answered 200** unless it
  checks the method: section 3's 405 rule is required, not optional.

The browser itself was not probed; WEB-2's browser checks are the first.
