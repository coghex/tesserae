"""The browser page's packaged files (docs/designs/browser_contract.md section 4).

The host serves exactly the paths in ASSETS, read from this package once at
startup. No URL is ever mapped onto a directory. A lobby host's page says so
in one meta tag (docs/designs/lobby_contract.md section 10), so the page logs
in instead of joining.
"""

from importlib import resources
from pathlib import Path

ASSETS = {
    "/": "index.html",
    "/app.css": "app.css",
    "/app.js": "app.js",
    "/session.js": "session.js",
    "/views.js": "views.js",
    "/favicon.svg": "favicon.svg",
}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


# The page's one host-specific line: which first message it sends.
GAME_HOST = b'<meta name="tesserae-host" content="game">'
LOBBY_HOST = b'<meta name="tesserae-host" content="lobby">'


class AssetsError(RuntimeError):
    """A packaged file is missing or unreadable: the installation is incomplete."""


def load_assets(root=None, lobby=False) -> dict[str, tuple[str, bytes]]:
    """Each URL path's content type and bytes, from this package or `root`;
    with `lobby`, the page of a lobby host."""
    folder = resources.files(__name__) if root is None else Path(root)
    assets = {}
    for path, name in ASSETS.items():
        try:
            body = (folder / name).read_bytes()
        except OSError as error:
            raise AssetsError(
                f"the browser page's {name} is missing ({error.strerror or error}); "
                "the installation is incomplete"
            ) from None
        assets[path] = (CONTENT_TYPES[Path(name).suffix], body)
    content_type, page = assets["/"]
    if page.count(GAME_HOST) != 1:
        raise AssetsError(
            "the browser page's index.html does not name its host; "
            "the installation is incomplete"
        )
    if lobby:
        assets["/"] = (content_type, page.replace(GAME_HOST, LOBBY_HOST))
    return assets
