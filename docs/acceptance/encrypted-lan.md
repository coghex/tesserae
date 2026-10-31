# Encrypted LAN acceptance

Tracker [#25](https://github.com/coghex/tesserae/issues/25): **automated checks
complete; the two-computer check is pending.** #29 and #25 stay open until a
genuinely separate computer has completed the [runbook](../lan-runbook.md) and
its result is recorded [below](#two-computer-check). A loopback run, a
container on the same computer, or binding a LAN-looking address does not
count.

| Child | Delivered by |
|---|---|
| #26: TLS helpers and certificate recipe | PR #32 |
| #27: authenticated TLS listener | PR #33 |
| #28: client trust and plaintext refusal | PR #34 |
| #29: combined checks, runbook, two-computer check | This record; the check is pending |

## Automated checks

Verified on 2026-10-30 from a clean virtual environment (`pip install -e
'.[dev]'`) on the #29 branch, based on master at `cd06b0e`. Python 3.14.8,
websockets 17.4, Ruff 0.17.3, OpenSSL 3.6.5 and LibreSSL 3.3.6, on macOS. The
PR's GitHub Actions run repeats them on Python 3.11 on Ubuntu.

```sh
python3 -m ruff check .
python3 -m ruff format --check .
python3 -m unittest discover -s tests -v
```

| #25 acceptance | Evidence |
|---|---|
| Localhost commands unchanged by default | The development and authenticated host, CLI, and offline dice tests pass unchanged (`test_server`, `test_authenticated_host`, `test_client`, `test_dice`). |
| Plaintext or unauthenticated LAN refused; plaintext LAN joins refused | `test_tls_host` covers ten refused `serve` configurations, none of which listen. `test_client_tls` refuses `ws://` to a LAN address or hostname before any contact, and refuses redirects to unchecked destinations. |
| Trusted, matching certificates connect; others fail before the token is sent | `test_client_tls` uses a recording TLS server. It shows joins with a trusted certificate by IP and name, and no join or action for an unrelated certificate, system trust only, a name mismatch, a host without TLS, or a redirect. `test_transport` checks the recipe with every `openssl` on PATH under strict verification. Bad key and trust files give clear, secret-free errors. |
| Two identities, private replies, passive updates, concurrent stale rejection, restart replay, shutdown, all encrypted | `test_encrypted_multiplayer` runs real `serve` with TLS and two real `connect --ca-file` processes. The identities are distinct. The idle player gets only revision metadata, and never the other's expression. Simultaneous fresh rolls give one result and one `stale_revision`. After SIGINT and restart, an unchanged retry returns the exact original result, leaving two actions at revision 2. No token or key material appears in any output. An untrusted or plaintext client changes nothing. |
| The runbook is runnable | Steps 2–7 were rehearsed on one computer with `127.0.0.1` standing in for the LAN address. That covered the recipe, provisioning, `serve --host --tls-cert --tls-key`, two `connect --ca-file` players, an idle update, a private result, reconnect replay, rotation (old token refused, new token is the same participant), and revocation. The friend-side fingerprint one-liner matches `openssl`'s. This rehearsal is not the two-computer check. |
| CI green; generic public material | Ruff and unittest pass locally and in the PR's CI. The repository holds only generated test certificates (made at test time, never committed), placeholder addresses, and original examples. |

## Two-computer check

**Pending.** To complete it, follow the [runbook](../lan-runbook.md) with a
second computer on the same LAN, then fill in this section. Use placeholders
for real addresses, and never paste a token, key, or database.

| | Host computer | Friend's computer |
|---|---|---|
| OS and version | | |
| Python version | | |
| Tesserae commit | | |
| How files were transferred | | |
| Firewall or network settings changed (and undone) | | |

| Check (runbook step 7) | Observed |
|---|---|
| Both snapshots: same `game_id`, different `participant_id`s | |
| 1. Idle friend sees only the revision when the host player rolls | |
| 2. Friend's roll: result only on the friend's side | |
| 3. Simultaneous fresh rolls: one result, one `stale_revision` | |
| 4. Friend reconnects and resends: identical original result | |
| 5. Rotation: old token refused, new token is the same participant | |
| 6. Revocation: token refused | |
| Anything surprising | |

Once this is filled in and agrees with the runbook, #29 and #25 can be closed.
This would complete M1-3's network portion. The demo, rules modules, and the
AI referee are later Milestone 1 work, and the browser is deferred beyond it.
