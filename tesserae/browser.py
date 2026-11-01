"""HTTP rules for the host's port: the browser page and every handshake's Origin.

docs/designs/browser_contract.md section 3. A request with `Upgrade:
websocket` is a handshake; in browser mode anything else is a page request,
answered from the packaged assets only. Every host refuses a handshake from an
unexpected browser origin; CLI clients send no Origin.
"""

import email.utils
import http

from websockets.datastructures import Headers
from websockets.http11 import Request, Response

LOOPBACK_NAMES = ("127.0.0.1", "localhost")


class RequestPolicy:
    """Decides each HTTP request before the WebSocket handshake.

    `assets` (path to content type and bytes) turns on browser mode. The bound
    port is set once listening; until then, browser mode answers 503.
    """

    def __init__(self, assets=None):
        self.assets = assets
        self.port = None

    def bind(self, port: int):
        self.port = port

    @property
    def names(self) -> frozenset:
        if self.assets is None or self.port is None:
            return frozenset()
        return frozenset(f"{name}:{self.port}" for name in LOOPBACK_NAMES)

    @property
    def origins(self) -> frozenset:
        return frozenset(f"http://{name}" for name in self.names)

    def process_request(self, connection, request: Request) -> Response | None:
        """websockets' `process_request` hook: None continues the handshake."""
        upgrade = any(
            "websocket" in [token.strip().lower() for token in value.split(",")]
            for value in request.headers.get_all("Upgrade")
        )
        if self.assets is not None:
            if self.port is None:
                return self._respond(503, "Not ready yet.\n")
            if not _one_of(request.headers.get_all("Host"), self.names):
                return self._respond(421, "Misdirected request.\n")
        if upgrade:
            origins = request.headers.get_all("Origin")
            if origins and not _one_of(origins, self.origins):
                return self._respond(403, "Forbidden origin.\n")
            return None
        if self.assets is None:
            return None  # websockets answers as before: 426 Upgrade Required
        if request.method != "GET":
            return self._respond(405, "Method not allowed.\n", {"Allow": "GET"})
        asset = self.assets.get(request.path)  # a query never matches
        if asset is None:
            return self._respond(404, "Not found.\n")
        content_type, body = asset
        return self._respond(200, body, {"Content-Type": content_type})

    def _respond(self, status: int, body, extra=None) -> Response:
        if isinstance(body, str):
            body = body.encode()
        headers = Headers(
            [
                ("Date", email.utils.formatdate(usegmt=True)),
                ("Connection", "close"),
                ("Content-Length", str(len(body))),
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "no-referrer"),
            ]
        )
        if self.assets is not None:
            headers["Cross-Origin-Opener-Policy"] = "same-origin"
            headers["Content-Security-Policy"] = self.content_security_policy()
        for name, value in (extra or {}).items():
            if name in headers:
                del headers[name]
            headers[name] = value
        status = http.HTTPStatus(status)
        return Response(status.value, status.phrase, headers, body)

    def content_security_policy(self) -> str:
        sockets = " ".join(f"ws://{name}" for name in sorted(self.names))
        return (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            f"img-src 'self'; connect-src {sockets}; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'; "
            "require-trusted-types-for 'script'"
        )


def _one_of(values: list, allowed: frozenset) -> bool:
    """Exactly one header value, and an allowed one."""
    return len(values) == 1 and values[0] in allowed
