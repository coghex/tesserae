"""Personal join tokens: generation, hashing, and token files.

Tokens are random bearer credentials, not passwords. The store keeps only a
SHA-256 digest. A raw token exists in memory and in the token file it was
written to, and nowhere else.
"""

import hashlib
import os
import secrets

TOKEN_PREFIX = "tess_"
TOKEN_BYTES = 32


def new_token() -> str:
    """A fresh token: a recognizable prefix and 32 random bytes, URL-safe."""
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def write_token_file(path, token: str):
    """Create a user-only token file; refuse to replace anything at `path`."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, (token + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_token_file(path) -> str:
    with open(path, encoding="utf-8") as file:
        return file.read().rstrip("\n")
