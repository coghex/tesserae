# Encrypted LAN delivery design

Status: in progress; tracker [#25](https://github.com/coghex/tesserae/issues/25).
LAN-1 (#26) is in review: the transport policy and TLS context helpers
(`tesserae/transport.py`) and the operator reference with the certificate
recipe ([docs/tls.md](../tls.md)). No LAN listener or client flag is exposed
yet; #27 and #28 wire the helpers into `serve` and `connect`.

The owner selected encrypted LAN connections after tracker #12 completed the
localhost flow. This finishes the remaining network portion of M1-3: a friend
on another computer joins the same foreground host with a personal token.
The concrete flag and policy choices below are the implementation proposal for
that direction, rather than additional decisions attributed to the owner.

## Starting point

Master at `b575d75` includes the accepted localhost implementation and 77 tests.
The server binds `127.0.0.1`; the client has no custom certificate trust option.
Participant credentials, revocation, atomic execution, private replies, scoped
snapshots, revision updates and explicit replay/recovery already work. Keep
those mechanisms and the version-1 protocol/schema-2 store.

## Delivery slices

| Child | Outcome | Depends on | Size |
|---|---|---|---|
| [#26](https://github.com/coghex/tesserae/issues/26), LAN-1 | TLS configuration helpers and certificate setup contract | None | Small–medium |
| [#27](https://github.com/coghex/tesserae/issues/27), LAN-2 | Explicit authenticated TLS host listener | #26 | Medium |
| [#28](https://github.com/coghex/tesserae/issues/28), LAN-3 | Verified client trust and plaintext destination policy | #26 | Medium |
| [#29](https://github.com/coghex/tesserae/issues/29), LAN-4 | Combined tests, runbook and actual two-computer proof | #27, #28 | Medium, including manual verification |

Start with #26. Afterward #27 and #28 have independent implementation boundaries;
each can use a library peer for its own tests. #29 verifies the combined stack.
Each child links its concrete requirements and acceptance conditions; tracker
#25 remains open until all combined acceptance is satisfied.

## Transport and configuration

Use Python's `ssl` contexts through the existing `websockets` library. Its
[encryption guide](https://websockets.readthedocs.io/en/stable/howto/encryption.html)
supports a server certificate/key and explicitly trusted self-signed certificates.
The Python [context guidance](https://docs.python.org/3.11/library/ssl.html#ssl.create_default_context)
provides certificate verification and endpoint identity checking. Keep those
checks enabled and use secure defaults. Both client trust paths explicitly OR
`ssl.VERIFY_X509_STRICT` into `context.verify_flags`, retaining other default
flags, required verification and hostname checking. Python 3.13+ enables strict
checking by default, while Python 3.11 CI does not; the explicit flag makes
strictness consistent without claiming every TLS-stack default is identical.
See the [Python 3.13 change](https://docs.python.org/3.13/whatsnew/3.13.html#ssl).
Do not implement cryptography.

Planned commands, using placeholder paths and address:

```sh
python3 -m tesserae serve --database GAME.sqlite3 --port 8790 --host 192.168.1.50 --authenticated --tls-cert host.crt --tls-key host.key
python3 -m tesserae connect wss://192.168.1.50:8790/ --token-file alice.token --ca-file host.crt
```

The default listener stays `127.0.0.1`. `--host` selects one concrete IPv4
interface; wildcard/unspecified, multicast and the limited broadcast address
`255.255.255.255` are rejected.
Hostname binding, IPv6 listeners and address discovery are deferred. A failed
bind has a clear error, without changing the requested address or port.

| Server configuration | Outcome |
|---|---|
| Default loopback, development, plaintext | Existing procedure retained |
| Default loopback, authenticated, plaintext | Existing procedure retained |
| Loopback, authenticated, certificate and key | TLS available for tests/local use |
| Non-loopback, authenticated, certificate and key | Explicit encrypted LAN listener |
| Non-loopback without both authentication and TLS | Refused |
| TLS without authentication, or only one of certificate/key | Refused |

Readiness reports the actual configured address/assigned port and `ws` or `wss`
scheme. Initial LAN support targets the existing macOS/Linux CLI environment.
There is no added listener, HTTP service or automatic background process.

## Certificates and personal identity

An operator creates a self-signed host certificate with an IP/DNS subjectAltName
matching the endpoint that friends will use. #26 supplies and exercises the
exact recipe, compatible with OpenSSL 3.x and macOS LibreSSL 3.3.6, and verifies
it under explicitly strict checking in Python 3.11 CI and local Python 3.13+.
Use whichever `openssl` is on PATH: `/usr/bin/openssl` on macOS may be LibreSSL,
while a Homebrew OpenSSL may take precedence. Verify both tools when available
and record versions. Fail clearly if a compatible executable is absent; do not
silently skip TLS checks or require Homebrew when the system tool works.
The executable is setup/test tooling; normal startup uses Python SSL loading.

| File | Who uses it |
|---|---|
| Host certificate, public | Host presents it; friends trust their supplied copy through `--ca-file` |
| Host private key | Kept on the host, with user-only permissions |
| Personal token file | One participant's application identity; separately provisioned using existing operator commands |

The #29 runbook starts with installing Python 3.11+, obtaining a compatible
Tesserae checkout, creating/activating a virtual environment and running
`python3 -m pip install -e .` from the repository root on the friend's computer.
It then verifies the installed CLI and explains connecting from both computers.
The host keeps its private key and game database. Only the public certificate
and that friend's personal token go to the friend; explicit `--ca-file` trust
does not require a system-wide certificate installation. A normal client run
does not need the `openssl` executable.

All live files stay outside the repository. A replacement endpoint identity or
expired certificate requires an appropriate replacement certificate/trust copy;
automatic certificate issuance or renewal is outside this tracker.

The client uses `--ca-file` for explicit trust, or system trust when omitted for
`wss`. Chain and endpoint identity must be verified before the join token is sent.
Untrusted/mismatched certificates or bad files produce clean startup errors.
There is no insecure mode, automatic trust-on-first-use, or plaintext fallback.

Plaintext `ws` remains available only for a concrete loopback IP or exact
`localhost`. Arbitrary hostnames do not get a DNS-resolution exception. Supplying
a trust file with `ws` is an error. Existing URI credential/query rejection and
proxy bypass remain. `--game-id`, streaming, EOF and uncertainty behavior remain.

## Implementation boundaries

- #26 supplies small shared validation/context helpers and setup documentation.
  It does not expose an application LAN listener. Avoid a general configuration
  framework or changes to the rules/command modules. The helpers are
  `check_server` / `listen_address` / `server_context` for #27 and
  `check_client` / `is_loopback_host` / `client_context` for #28, all raising
  `TransportConfigError` with secret-free messages. The recipe adds
  `-pkeyopt ec_param_enc:named_curve`, because LibreSSL 3.3.6 otherwise
  encodes the P-256 key with explicit curve parameters.
- #27 threads the chosen address/context through `GameHost`, `run_foreground`
  and `serve`. Joining, database work and notifications keep their existing paths.
- #28 threads client trust configuration through `connect` and its entry point,
  checks plaintext destination policy, and tests rejection before application
  credentials/input are sent.
- #29 runs the real processes together and records the separate-computer result.
  It does not replace that observation with an automated loopback demonstration.

## Acceptance and closure

Keep tests focused on the application wiring: configuration rejection, a verified
TLS connection, trust/identity failures before join, foreground lifecycle and
the combined two-client flow. Reuse existing mechanics/replay/privacy tests;
do not build TLS conformance tests or repeat all tests for every flag combination.

The combined encrypted check demonstrates separate identities, private replies,
passive updates, a concurrent stale request, restart/replay without duplicates
and the retained localhost procedures. Ruff/unittest/CI stay green.

Final acceptance also requires a genuinely separate computer to join over LAN,
see permitted updates, make an action, reconnect/replay and reject an old token
after rotation/revocation. Record platform/software versions and sanitized
observations. Live addresses, credentials and keys are not public evidence.

If a second computer/operator is unavailable, #29 can publish its tests/runbook,
but #29 and #25 stay open with manual acceptance pending. A partial-delivery PR
must omit `Closes #29`. Close the tracker only once both automated and actual
device evidence are recorded.

LAN setup may require an operator's normal OS network configuration. This
tracker does not authorize automatic firewall/router changes, port forwarding,
UPnP, public hosting or deployment services. Modules and the AI referee remain
later Milestone 1 work. The browser is deferred beyond Milestone 1; its saved
[authentication and certificate notes](../browser-client-notes.md) do not add
implementation requirements to this tracker.
