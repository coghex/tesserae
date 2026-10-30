# Encrypted LAN delivery design

Status: planned; tracker [#25](https://github.com/coghex/tesserae/issues/25)
and its four children are filed. No LAN runtime changes are implemented by
this design document.

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
checks enabled and use the secure defaults; do not implement cryptography.

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
exact OpenSSL 3.x recipe, including compatibility with Python 3.11 defaults.
OpenSSL is setup/test tooling; normal startup uses only Python SSL loading.
Tests generate temporary certificates with the same tooling and do not silently
skip verification if that prerequisite is missing.

| File | Who uses it |
|---|---|
| Host certificate, public | Host presents it; friends trust their supplied copy through `--ca-file` |
| Host private key | Kept on the host, with user-only permissions |
| Personal token file | One participant's application identity; separately provisioned using existing operator commands |

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
  framework or changes to the rules/command modules.
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
UPnP, public hosting or deployment services. Browser, modules and the AI referee
remain later milestone work.
