# Playing over a LAN: two-computer runbook

This walks an operator and one friend through an encrypted game across a local
network: a **host computer**, which runs the game and may also have a player,
and a **friend's computer**. It uses only the commands Tesserae ships. It does
not change either computer's firewall, router, or services; any network
settings below are steps you take yourself. Background and the certificate
recipe are in [tls.md](tls.md).

Addresses, paths, and names below are placeholders. `192.168.1.50` stands for
the host's LAN address. Live databases, keys, and tokens stay outside the
repository.

## Who holds what

| File | Host computer | Friend's computer |
|---|---|---|
| Game database (`game.sqlite3`) | Yes | **Never** |
| Host private key (`host.key`, mode 0600) | Yes | **Never** |
| Host certificate (`host.crt`, public) | Yes | A copy, used as `--ca-file` |
| Host player's token (`alice.token`) | Yes | No |
| Friend's token (`bob.token`) | Briefly, then deleted after handing it over | Yes, mode 0600, outside the checkout |

The friend needs nothing else. There are no SSH keys and no system-wide
certificate installation, and the friend does not need the `openssl` command.
`--ca-file` trusts the one certificate explicitly.

## 1. Install Tesserae on both computers

Both computers need Python 3.11 or newer and the same Tesserae version. On the
host, note the version you are running:

```sh
cd ~/tesserae && git rev-parse --short HEAD    # for example: cd06b0e
```

On the friend's computer, starting from nothing:

```sh
python3 --version                              # 3.11 or newer
git clone https://github.com/coghex/tesserae.git ~/tesserae
cd ~/tesserae
git checkout cd06b0e                           # the host's version
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
python3 -m tesserae connect --help             # must list --ca-file
```

Run later commands in the activated virtual environment. A new terminal needs
`cd ~/tesserae && . .venv/bin/activate` again.

## 2. On the host: choose the address and make the certificate

Find the host's LAN address, and give it a stable one if you can, for example
with a DHCP reservation on the router:

```sh
ipconfig getifaddr en0          # macOS (en1 on some Macs)
hostname -I                     # Linux
```

Make the certificate for exactly that address, using the recipe in
[tls.md](tls.md#making-the-host-certificate), in a folder outside the
repository such as `~/tesserae-host`. Note its fingerprint:

```sh
openssl x509 -in ~/tesserae-host/host.crt -noout -fingerprint -sha256
```

## 3. On the host: provision the players

```sh
python3 -m tesserae participant add --database ~/tesserae-host/game.sqlite3 \
    --name Alice --token-file ~/tesserae-host/alice.token
python3 -m tesserae participant add --database ~/tesserae-host/game.sqlite3 \
    --name Bob --token-file ~/tesserae-host/bob.token
```

## 4. On the host: start the game

```sh
python3 -m tesserae serve --database ~/tesserae-host/game.sqlite3 --port 8790 \
    --authenticated --host 192.168.1.50 \
    --tls-cert ~/tesserae-host/host.crt --tls-key ~/tesserae-host/host.key
```

The first line should say `listening on wss://192.168.1.50:8790/ ... TLS`.
Leave this terminal running. Ctrl-C stops the game, and the database keeps
every committed roll.

**Network settings are yours to change, and to undo afterwards.**

- **macOS:** if the firewall is on (System Settings → Network → Firewall), the
  first connection may make macOS ask whether Python may accept incoming
  connections. Allow it, or connections from the friend will time out.
- **Linux with `ufw`:** allow the port from your LAN only, for example
  `sudo ufw allow from 192.168.1.0/24 to any port 8790 proto tcp`. Delete the
  rule when you are done.
- **The network itself:** some guest or "isolated" Wi-Fi networks block
  traffic between devices, and then no setting on either computer helps. Both
  computers must be on the same network. Never forward the port on your router;
  this is for a local network only.

## 5. Hand the friend their files

Give the friend `host.crt` and `bob.token` only, over a channel you trust,
such as a USB stick, AirDrop, or a direct copy. Then delete the host's copy
of `bob.token`. On the friend's computer, keep them outside the checkout and
private, and check the certificate's fingerprint against the host's:

```sh
mkdir -p ~/tesserae-friend && mv host.crt bob.token ~/tesserae-friend/
chmod 600 ~/tesserae-friend/bob.token
python3 -c "import ssl,hashlib;print(hashlib.sha256(ssl.PEM_cert_to_DER_cert(open('$HOME/tesserae-friend/host.crt').read())).hexdigest())"
```

That prints the same fingerprint as step 2, in lowercase and without colons.

## 6. Connect and play

The host's player, in a second terminal on the host:

```sh
python3 -m tesserae connect wss://192.168.1.50:8790/ \
    --token-file ~/tesserae-host/alice.token --ca-file ~/tesserae-host/host.crt
```

The friend, on their computer:

```sh
python3 -m tesserae connect wss://192.168.1.50:8790/ \
    --token-file ~/tesserae-friend/bob.token --ca-file ~/tesserae-friend/host.crt
```

Each prints a `snapshot` first. Check that both show the **same `game_id`**,
**different `participant_id`s**, and the same `revision`. Then play by typing
JSON lines, as in the
[README](../README.md#two-players-on-one-computer), for example:

```json
{"version": 1, "type": "command", "request_id": "b1", "expected_revision": 0, "command": "roll", "args": {"expression": "1d20"}}
```

## 7. Check it works

Work through these together and note what you see:

1. **Shared updates.** Alice rolls. Bob, without typing anything, sees only
   `{"type": "revision", ...}`, with no result, expression, or name.
2. **Private results.** Bob rolls. Only Bob sees his `result`; Alice sees the
   revision.
3. **Conflicts.** Both send a fresh roll with the same current
   `expected_revision` at about the same time. One gets a `result`, the other
   `stale_revision`.
4. **Reconnect and replay.** Bob ends his client (Ctrl-D), starts it again, and
   resends his earlier line unchanged. He gets the exact original result, and
   nobody gets a new revision notification.
5. **Rotation.** On the host, run
   `python3 -m tesserae participant rotate --database ~/tesserae-host/game.sqlite3 BOB_ID --token-file ~/tesserae-host/bob-new.token`,
   with Bob's `participant_id` from `participant list`. Bob's open session ends
   at his next request (`session_revoked`), and joining again with the old
   file fails with `authentication_failed`. With the new token file, handed
   over the same way, he joins as the same `participant_id`.
6. **Revocation.** `participant revoke` likewise stops the current token.

Record the results in
[acceptance/encrypted-lan.md](acceptance/encrypted-lan.md#two-computer-check).

## When something goes wrong

| What the client says | Usual cause |
|---|---|
| `cannot connect ... Connect call failed` / refused | The host is not running, or the port is wrong |
| A connection that hangs, then times out | The firewall dropped it, the address is wrong, or the computers are not on the same network |
| `cannot trust ...: IP address mismatch` | The URI's address is not the one in the certificate; remake the certificate for the address you use |
| `cannot trust ...: self-signed certificate` | `--ca-file` is missing, or is not this host's certificate |
| `cannot connect ... securely` | The host was started without `--tls-cert` and `--tls-key` |
| `plaintext ws:// is only for this computer` | Use `wss://` |
| `refused to join: authentication_failed` | Wrong token file, or the token was rotated or revoked |
| `answered with a redirect` | Something else is listening on that address and port |

If the host's address changes, or the certificate expires, make a new
certificate and hand out the new `host.crt` ([tls.md](tls.md#replacing-the-certificate)).
Tokens are unaffected.

## Afterwards

Stop the host with Ctrl-C, and remove any firewall rule you added. Keep or
delete `~/tesserae-host` as you like; it is the whole game. The friend can
delete `~/tesserae-friend`.
