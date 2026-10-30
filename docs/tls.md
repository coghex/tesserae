# Encrypted connections: TLS reference

This is the reference for encrypted LAN play, tracker
[#25](https://github.com/coghex/tesserae/issues/25). The policy and SSL
contexts live in `tesserae/transport.py`
([#26](https://github.com/coghex/tesserae/issues/26)). The host flags
([#27](https://github.com/coghex/tesserae/issues/27)) and client flags
([#28](https://github.com/coghex/tesserae/issues/28)) below are **planned**:
`tesserae serve` and `tesserae connect` do not accept them yet. Today the
application still listens only on loopback.

## What is allowed

Host (`tesserae serve`):

| Address (`--host`, default 127.0.0.1) | `--authenticated` | `--tls-cert` and `--tls-key` | Result |
|---|---|---|---|
| Loopback | no | no | Development mode, `ws://` (unchanged) |
| Loopback | yes | no | Authenticated, `ws://` (unchanged) |
| Loopback | yes | yes | Authenticated, `wss://`, for local testing |
| Another IPv4 address of this computer | yes | yes | Encrypted LAN host, `wss://` |
| Another IPv4 address | missing either | | Refused |
| Any | no | yes | Refused: TLS requires authentication |
| Any | | only one of the two | Refused: give both or neither |

`--host` takes exactly one IPv4 address. `0.0.0.0` (every interface),
multicast addresses, and `255.255.255.255` are refused. So are hostnames and
IPv6 for now. Nothing falls back to another address, port, or mode.

Client (`tesserae connect`):

| URI | `--ca-file` | Result |
|---|---|---|
| `wss://HOST:PORT/` | a certificate file | Trust exactly that certificate |
| `wss://HOST:PORT/` | none | Trust the system's certificate authorities |
| `ws://` to a loopback IP or exactly `localhost` | none | Plaintext on this computer (unchanged) |
| `ws://` to anything else | | Refused before connecting, so the token is never sent in the clear |
| `ws://` | a file | Refused: a trust file needs `wss://` |

Hostnames are never resolved to decide whether `ws://` is local.
`ws://gamehost.local/` is refused even if it points at this computer; use
`wss://`. URIs with a query, fragment, or user name are still refused, and the
token still comes only from `--token-file`.

Both client trust paths verify the host's certificate chain and that the
certificate names the host in the URI. They also turn on strict X.509
checking (`ssl.VERIFY_X509_STRICT`) on top of Python's defaults. Python 3.13
and later do that by default; 3.11 and 3.12 do not, so Tesserae sets it
explicitly and verifies the same way on all of them. There is no option to
skip verification.

## Files and who holds them

| File | Lives on | Secret? | Used by |
|---|---|---|---|
| `host.key`, the host's private key | The host only, mode 0600 | **Yes**, never copy it | `serve --tls-key` |
| `host.crt`, the host's certificate | The host, plus a copy for each player | No, it is public | `serve --tls-cert`, and each player's `connect --ca-file` |
| `NAME.token`, a player's join token | The host, then that player only | **Yes** | That player's `connect --token-file` |
| The game database | The host only | Yes | `serve --database`, `participant` |

Keep all of these outside the Tesserae repository. A player needs only the
public certificate and their own token file. Neither the key nor the database
ever leaves the host.

## Making the host certificate

The certificate must name the address players will type in their `wss://`
URI, as a `subjectAltName`. Pick a stable LAN address for the host (for
example, a DHCP reservation on your router). Replace `192.168.1.50` below with
it. This recipe works with OpenSSL 3.x and with LibreSSL 3.3.6, the
`/usr/bin/openssl` on macOS. On the host, in a folder outside the repository:

```sh
mkdir -p ~/tesserae-host && cd ~/tesserae-host
(umask 077 && openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
    -pkeyopt ec_param_enc:named_curve -nodes -days 365 -subj "/CN=Tesserae host" \
    -addext "subjectAltName=IP:192.168.1.50" \
    -keyout host.key -out host.crt)
ls -l host.key host.crt   # both -rw------- ; only host.key must stay that way
```

- `ec_param_enc:named_curve` makes the key name its curve (P-256). LibreSSL
  3.3.6 otherwise writes the curve out as explicit parameters, which many TLS
  implementations reject. OpenSSL 3 names the curve either way.
- `-nodes` leaves the key unencrypted. The host refuses an encrypted key
  rather than stopping to ask for a passphrase. The key is protected by its
  0600 permissions instead.
- To let players use a name as well as the address, list both:
  `-addext "subjectAltName=DNS:gamehost.local,IP:192.168.1.50"`.
  Every address or name players will use must be listed.
- The certificate is valid for 365 days (`-days`).

Check what you made, then note its fingerprint. A player can compare the
fingerprint to make sure the copy they received is yours:

```sh
openssl x509 -in host.crt -noout -subject -enddate
openssl x509 -in host.crt -noout -text | grep -A1 "Subject Alternative Name"
openssl x509 -in host.crt -noout -fingerprint -sha256
```

Give each player `host.crt` and their token file, over a channel you trust.
`openssl` is needed only to make the certificate. Players and normal host
startup use Python's built-in `ssl` support.

## Replacing the certificate

There is no automatic renewal. Make a new certificate with the same recipe
and give players the new `host.crt` when either of these happens:

- the host's LAN address or name changes, since a certificate names exactly
  the endpoints in its `subjectAltName`;
- the certificate expires (`-enddate` above), or the key may have been copied.

Players then replace their copy. Until they do, their client refuses the
host, and it refuses before sending their token.

## Tests and the openssl prerequisite

The TLS tests (`tests/test_transport.py`) make throwaway certificates with
this recipe. They use every distinct `openssl` on PATH that reports
OpenSSL 3.x or LibreSSL 3.x, and do a real verified TLS handshake with each.
On macOS that usually means Homebrew's OpenSSL and the system's LibreSSL. If
neither is installed, the tests fail with a setup message rather than skip.
On Debian or Ubuntu, install the `openssl` package.

Verified for #26:

| Where | Python | Tool | Result |
|---|---|---|---|
| macOS (local) | 3.14.8 | OpenSSL 3.6.5 (Homebrew) | Strict handshake by IP and DNS name; untrusted, system-only, and wrong-name cases refused |
| macOS (local) | 3.14.8 | LibreSSL 3.3.6 (`/usr/bin/openssl`) | Same |
| GitHub Actions, Ubuntu | 3.11 | OpenSSL 3.x from the runner | Same tests, with strictness turned on explicitly |
