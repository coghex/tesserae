# Encrypted LAN acceptance

Tracker [#25](https://github.com/coghex/tesserae/issues/25): **accepted for M1-3**.
Automated checks and the [VM network check](#vm-network-check) passed. When a
second physical computer was unavailable, the owner chose an Ubuntu VM in UTM
for this verification. That separate OS, installation and network stack replace
the original physical second-computer gate for #29 and #25. Physical-device
Wi-Fi/firewall coverage remains deferred; the result below is VM evidence.
A loopback-only subprocess run or merely binding a LAN-looking address does
not provide the same coverage.

| Child | Delivered by |
|---|---|
| #26: TLS helpers and certificate recipe | PR #32 |
| #27: authenticated TLS listener | PR #33 |
| #28: client trust and plaintext refusal | PR #34 |
| #29: combined checks, runbook, network verification | PR #35 and the VM result below |

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

<a id="two-computer-check"></a>

## VM network check

Passed on 2026-10-30 at commit `632fb7ea0cdb0414d6c429deb71e732c632ddb58`
on both sides, after PR #35 merged. The owner logged into the installed guest;
the subsequent CLI checks were driven through its own processes over SSH.
SSH used a VM-specific management key and a host key verified through UTM's
guest agent. Game joining still used the personal token and `--ca-file`.

| | Mac host | Ubuntu guest |
|---|---|---|
| OS | macOS 26.7.1, Apple Silicon | Ubuntu Server 26.10, aarch64 |
| Python | 3.14.8 | 3.14.8 |
| websockets | 17.4 | 17.4 |
| Python SSL | OpenSSL 3.6.5 | OpenSSL 4.0.3 |
| Installation | Editable install in a dedicated external venv | Fresh public clone at the host commit, its own venv, `pip install -e .`, CLI help checked |
| Network | Explicit non-loopback IPv4 listener, authenticated TLS, port 0 | UTM 5.0.6 Shared Network (NAT), separate guest network interface; connects to the host's LAN address |
| Files transferred | Public certificate and Bob's token only, via SCP | Certificate fingerprint matched; token mode 0600, outside the checkout |
| Network changes | No host firewall/router changes or port forwarding | UTM shared virtual network; no guest firewall changes |

The database, host private key and Alice's token stayed on the Mac. The Mac's
copy of Bob's token was removed after the transfer was verified. Rotation used
the same handover for the new token. No repository or home-directory share was
mounted in the guest. Actual addresses, tokens, keys and raw test logs remain
outside the public repository.

| Check | Observed |
|---|---|
| Join | Real `serve` on the Mac and real `connect --ca-file` in Ubuntu established verified TLS; snapshots had the same game ID, different participant IDs and revision 0. |
| Passive updates and privacy | Alice's `3d6` produced revision 1; idle Bob received only the four-field revision envelope. Bob's `2d6+7` produced revision 2; Alice received only metadata. Neither client's captured output contained the other's expression. |
| Simultaneous commands | Both clients sent new requests at revision 2 behind a start barrier. Exactly one succeeded and one returned `stale_revision`; the committed revision became 3. |
| Guest reconnect/replay | Bob disconnected, rejoined, and resent his original revision-1 request unchanged. The exact recorded result returned; Alice received no new notification. |
| Foreground restart/replay | Both clients exited normally. The Mac host exited 0 on SIGINT, restarted, and both rejoined at revision 3. Bob's original request again returned its exact recorded result, with no new notification. |
| Transport failures from the VM | Omitted certificate trust failed with `cannot trust`; plaintext to the non-loopback endpoint failed with `only for this computer`. Both exited 1 without a result or committed action. |
| Token rotation | Bob's old open session ended with `session_revoked` at its next request. Old-token rejoin failed with `authentication_failed`. The new token joined as the same Bob ID at revision 3 and retained replay history. |
| Token revocation | Bob's replacement token lost its open session and was refused on a fresh join. |
| Durability and output | Final database contained exactly three actions at revision 3. Captured host/client output contained no token, private-key material or traceback. |
| Cleanup | Test host stopped; Alice and Bob test credentials revoked. Ubuntu VM remains running with Tesserae installed. |

The runbook's installation, file handover and CLI flow worked across the Mac
and a full Linux VM. No application changes were needed. This completes M1-3's
network acceptance under the agreed VM substitution. Physical-device Wi-Fi
isolation, another device's firewall and independent physical-link behavior
were not exercised and remain deferred coverage.

The demo, rules modules and AI referee remain later Milestone 1 work. The
browser is deferred beyond Milestone 1.
