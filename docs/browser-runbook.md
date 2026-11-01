# Playing a referee game in the browser: operator runbook

The host can serve a small web page so players at the host computer can play
a referee game in a browser instead of the terminal. The page has two screens,
Console and Settings, in a collapsible column on the left. Everything a player
types goes to the AI referee, exactly as with `tesserae connect`, and both
kinds of player can share one game. The rules the page follows are in the
[browser contract](designs/browser_contract.md).

The page is for this computer only. Players on other computers keep using the
encrypted CLI ([LAN runbook](lan-runbook.md)); browser access over the network
is later work. Paths, names and ports below are placeholders.

## 1. Before you start

Set up the referee and the game as in the
[referee runbook](referee-runbook.md), sections 1 to 3: install Tesserae, run
`tesserae referee setup` and log Codex in once, then create the game, add the
participants with their token files, and `tesserae referee enable`. Nothing
else is needed: the page ships inside the Tesserae package, with no build step
or extra download.

## 2. Serve the game with the page

    tesserae serve --authenticated --browser --database game.sqlite3 --port 8765

The host prints its usual line and then the page's address:

    Browser: http://127.0.0.1:8765/ (this computer only; join with a personal token)

- **Use a fixed `--port`.** A page that loses its connection reconnects to the
  same address, so after a restart the host must listen on the same port.
- `--browser` needs `--authenticated` and an existing game in referee mode. It
  refuses `--tls-cert`, `--tls-key` and any `--host` other than `127.0.0.1`,
  and never creates a database. A refusal exits before anything listens.
- CLI players connect to the same host as before:
  `tesserae connect ws://127.0.0.1:8765/ --token-file bob.token`.

## 3. Joining

Open the address in a browser on this computer. The page opens on Console with
a join prompt. Paste your token, or choose your token file, and press Join.

- Each player uses their own browser window or tab. Nothing is shared between
  pages: each holds its own player's token, in memory only.
- The token never goes into the address bar, browser storage or a cookie, and
  the entry is emptied once you join. **After a refresh you join again.**
- If the token is wrong or has been revoked, the page says so and stays
  disconnected.

## 4. Console

From top to bottom:

| Part | What it shows |
|---|---|
| Status line | Who you are joined as, and whether the referee is ready or answering someone |
| Conversation | Everything you may see, oldest first. Messages marked **Private** went only to you and the referee |
| The referee asks you | A question the referee is waiting for you to answer. Answer by typing |
| Your last input | What you last said that the referee accepted, and whether it was answered |
| Your view | Your part of the game: for Relay the scene and your own worker; for other games their fields as a list. It never shows another player's secrets |
| The box at the bottom | Type and press **Enter** to send; **Shift+Enter** starts a new line. Tick **Private** to say it to the referee only (once; it unticks after) |

- **One thing at a time.** While the referee answers someone, sending is
  refused and your text stays in the box with the reason. Send it again when
  the status says the referee is ready. Nothing is queued.
- Anything you type is words to the referee, even if it looks like a command
  or JSON. The page has no buttons that act in the game.

## 5. Settings

Settings shows the connection (host, game, who you joined as, the module) and
the same token entry. **Disconnect** leaves the game; **Change identity**
leaves it and lets another player join in the same page. Both clear everything
the previous player could see, including their text in the box. Switching
between Console and Settings, or collapsing the column, never disconnects or
loses what you typed.

## 6. When the connection drops

- If the host stops or the connection drops, the page keeps the conversation
  and your text and says **Disconnected. What you see may be out of date.**
  Press **Reconnect** once the host is running again.
- If you had just sent something and no answer came, it is shown as **Not
  confirmed**. After reconnecting, the page asks the host: if the referee had
  received it, it is marked accepted. Otherwise use **Resend exactly**, which
  sends the same input again: if the referee did get it, you receive its
  answer again and nothing happens twice. **Edit** turns it back into text you
  can change, which then counts as a new input.
- **Your last input** also has **Resend exactly**, for an answer you never saw.
  It never starts a second turn.
- **A refresh forgets the page.** Join again with your token: the conversation
  and your last accepted input come back from the host. Text you had typed but
  not sent, or that the referee never accepted, is gone, and the page says so.

## 7. Stopping and restarting

Stop the host with Ctrl-C as always. Players' pages show Disconnected. Start
it again with the same command and port, then each player presses Reconnect.
The game, the conversation and the referee's session continue where they
were; a referee job that was running when the host stopped ends `interrupted`,
as in the [referee runbook](referee-runbook.md#5-stopping-restarting-rebuilding).
`referee stop`, `--clear-stuck` and `referee rebuild` work the same with
`--browser`.

## 8. Diagnostics

- The page shows its own problems in plain words: a refused token, a revoked
  player, a host it cannot reach.
- The host's output and the per-job files are as in the
  [referee runbook](referee-runbook.md#7-diagnostics).
- The page will not load from another address: the host answers only
  `127.0.0.1:PORT` and `localhost:PORT`, serves only its own packaged files,
  and refuses connections started by other web sites. A blank page or an error
  in the browser's developer console usually means a different address or
  port.

## Scope

One page per player at the host computer, Console and Settings only. Not yet:
browser play from other computers, maps, remembered logins or display
preferences, and browser controls for running the host. The
[acceptance record](acceptance/browser.md) shows both demonstration scenes
played this way.
