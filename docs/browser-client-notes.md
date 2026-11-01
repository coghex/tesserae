# Browser client notes for a later milestone

Status: Milestone 1 is complete. The owner selected localhost-only browser
delivery, console first, on 2026-11-01. The current
[browser console design](designs/browser_player_design.md) adds a collapsible
left navigation with Console (the default screen) and Settings. These notes
preserve the earlier browser discussion; LAN trust and interactive maps remain
later work, and do not expand tracker #25 or the completed Milestone 1.

## Joining and identity

Reuse the existing personal Tesserae token and version-1 join protocol. A later
browser flow can open the game page, accept a pasted token or a user-selected
token file, open the WebSocket, and send the token in its first application
message. The server returns the permitted snapshot and updates. Participant
identity, credential revocation and replay namespaces remain server-owned and
shared with CLI clients.

The [File API](https://developer.mozilla.org/en-US/docs/Web/API/File_API/Using_files_from_web_applications)
allows reading a user-selected file; the page cannot silently read a token from
an arbitrary filesystem path. The
[first-message authentication pattern](https://websockets.readthedocs.io/en/stable/topics/authentication.html#first-message)
fits the existing join protocol. For the initial M2-1 slice, the owner selected
memory-only tokens: select/paste again after a refresh. Remembered login,
potentially via server-managed cookies, remains later design work; do not
assume localStorage or a cloud account system.

## Certificate trust is separate

The personal token identifies the participant. The server TLS certificate
establishes the encrypted endpoint. Custom token files do not replace TLS
certificates, and no SSH/system identity keys are involved.

Browser JavaScript has no CLI-style `--ca-file` parameter; certificate validation
is handled by the browser. See the
[WebSocket API](https://developer.mozilla.org/en-US/docs/Web/API/WebSocket/WebSocket)
and [connection-failure rules](https://websockets.spec.whatwg.org/#feedback-from-the-protocol).
Uploading a public certificate through the page would not install it as browser
trust. The future browser flow must choose a browser-trusted certificate or a
documented operator trust setup. No system-wide certificate installation is
authorized or required by the current CLI design.

Prefer serving the eventual page and socket from the same HTTPS origin when
designing asset serving. Framework, reverse-proxy choices and the final browser
trust experience remain open for that later milestone.

## Retained browser-slice goals

- Show the original demo's map/token, character summary and action result.
- Submit player intent through the existing referee path. The M1-6 authority
  contract supersedes the earlier raw-command browser proposal.
- Construct deliberate player views; do not ship referee secrets hidden by CSS.
- Refresh/reopen and recover authoritative state without duplicate actions.
- Keep private state directories out of general static file serving.
- Prove one click-to-command-to-saved-result loop before expanding the UI.

Browser actions must continue through the same host authority and identity
checks, without another editable source of game state. These goals should be
refined into a separate milestone when browser work is resumed.
