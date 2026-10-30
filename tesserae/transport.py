"""Transport policy and TLS contexts for the host and the client.

Plain functions, shared by `tesserae serve` and `tesserae connect`:

- which server configurations are allowed (plaintext only on loopback; TLS and
  any non-loopback address require authenticated mode);
- which client URIs are allowed (plaintext ws only to a loopback IP or exactly
  localhost, never resolving other names to make an exception);
- SSL contexts with Python's secure defaults, with strict X.509 checking
  turned on explicitly so every supported Python verifies the same way.

No cryptography is implemented here. See docs/tls.md.
"""

import ipaddress
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_HOST = "127.0.0.1"


class TransportConfigError(ValueError):
    """A transport setting is unsafe, incomplete, or unusable. Never secret."""


@dataclass(frozen=True)
class ServerTransport:
    host: str
    authenticated: bool
    tls: bool

    @property
    def scheme(self) -> str:
        return "wss" if self.tls else "ws"


def check_server(
    host: str = DEFAULT_HOST,
    *,
    authenticated: bool = False,
    tls_cert=None,
    tls_key=None,
) -> ServerTransport:
    """Validate a server's address and security settings together."""
    address = listen_address(host)
    if (tls_cert is None) != (tls_key is None):
        raise TransportConfigError("--tls-cert and --tls-key must be given together.")
    tls = tls_cert is not None
    if tls and not authenticated:
        raise TransportConfigError("TLS requires --authenticated.")
    if not address.is_loopback and not (authenticated and tls):
        raise TransportConfigError(
            f"Listening on {address}, which is not loopback, requires "
            "--authenticated, --tls-cert, and --tls-key."
        )
    return ServerTransport(str(address), authenticated, tls)


def listen_address(host: str) -> ipaddress.IPv4Address:
    """One concrete IPv4 interface address to bind."""
    try:
        address = ipaddress.IPv4Address(host)
    except (ipaddress.AddressValueError, TypeError):
        raise TransportConfigError(
            f"--host must be one IPv4 address, such as {DEFAULT_HOST} or a LAN "
            "address; hostnames and IPv6 are not supported yet."
        ) from None
    if address.is_unspecified:
        raise TransportConfigError(
            f"--host {address} would listen on every interface; name one address."
        )
    if address.is_multicast or address == ipaddress.IPv4Address("255.255.255.255"):
        raise TransportConfigError(f"--host {address} is not an interface address.")
    return address


def server_context(tls_cert, tls_key) -> ssl.SSLContext:
    """A server context presenting the certificate, with secure defaults.

    The key must be unencrypted: an encrypted one would otherwise make OpenSSL
    prompt on the terminal and stall startup.
    """
    _require_file(tls_cert, "certificate")
    _require_file(tls_key, "private key")
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    try:
        context.load_cert_chain(tls_cert, tls_key, password=_refuse_encrypted_key)
    except ssl.SSLError as error:
        if getattr(error, "reason", None) == "KEY_VALUES_MISMATCH":
            problem = "the key does not belong to that certificate"
        else:
            problem = "they are not a PEM certificate and its PEM private key"
        raise TransportConfigError(
            f"Cannot use certificate {tls_cert} with key {tls_key}: {problem}."
        ) from None
    return context


def _refuse_encrypted_key():
    raise TransportConfigError(
        "The private key is encrypted. Use an unencrypted key file, readable "
        "only by you, as made by the recipe in docs/tls.md."
    )


def check_client(uri: str, ca_file=None) -> ssl.SSLContext | None:
    """Validate a client URI and trust setting; the context for wss, else None.

    Never echoes the URI on failure, since a mistaken one might carry a secret,
    and never resolves a hostname.
    """
    shape = "the host URI must look like wss://HOST:PORT/ or ws://127.0.0.1:PORT/"
    try:
        parts = urlsplit(uri)
        parts.port  # validates the port
    except ValueError:
        raise TransportConfigError(shape) from None
    if parts.scheme not in ("ws", "wss") or not parts.hostname:
        raise TransportConfigError(shape)
    if parts.query or parts.fragment or parts.username or parts.password:
        raise TransportConfigError(
            "the host URI must not carry a query, fragment, or credentials; "
            "the token comes only from --token-file"
        )
    if parts.scheme == "wss":
        return client_context(ca_file)
    if ca_file is not None:
        raise TransportConfigError("--ca-file needs a wss:// URI.")
    if not is_loopback_host(parts.hostname):
        raise TransportConfigError(
            "plaintext ws:// is only for this computer (a loopback address or "
            "localhost); use wss:// to reach another computer"
        )
    return None


def is_loopback_host(hostname: str) -> bool:
    """A loopback IP literal or exactly localhost. Other names are not resolved."""
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def client_context(ca_file=None) -> ssl.SSLContext:
    """Verify the host's chain and name: against `ca_file`, else system trust.

    Strict X.509 checking is added to Python's defaults explicitly. Python
    3.13+ enables it already; 3.11 and 3.12 do not.
    """
    if ca_file is not None:
        _require_file(ca_file, "trust file")
    try:
        context = ssl.create_default_context(cafile=ca_file)
    except ssl.SSLError:
        raise TransportConfigError(
            f"{ca_file} does not contain a PEM certificate to trust."
        ) from None
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    return context


def _require_file(path, role: str):
    try:
        with open(path, "rb"):
            pass
    except OSError as error:
        raise TransportConfigError(
            f"Cannot read the {role} {path}: {error.strerror or error}."
        ) from None
