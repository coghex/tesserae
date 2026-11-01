"""The browser page's packaged files (docs/designs/browser_contract.md section 4).

The host serves exactly the paths in ASSETS, read from this package once at
startup. No URL is ever mapped onto a directory.
"""

from importlib import resources
from pathlib import Path

ASSETS = {
    "/": "index.html",
    "/app.css": "app.css",
    "/app.js": "app.js",
    "/session.js": "session.js",
    "/favicon.svg": "favicon.svg",
}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


class AssetsError(RuntimeError):
    """A packaged file is missing or unreadable: the installation is incomplete."""


def load_assets(root=None) -> dict[str, tuple[str, bytes]]:
    """Each URL path's content type and bytes, from this package or `root`."""
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
    return assets
