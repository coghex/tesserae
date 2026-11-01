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
    """Create a user-only token file; refuse to replace anything at `path`.

    Either the whole token is durably written, or the new file is removed and
    the error raised, so a retry with the same path can succeed.
    """
    data = memoryview((token + "\n").encode("utf-8"))
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            while data:
                data = data[os.write(descriptor, data) :]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        os.remove(path)
        raise


def read_token_file(path) -> str:
    with open(path, encoding="utf-8") as file:
        return file.read().rstrip("\n")


class IssuedToken:
    """Write a new token file first; remove it again if the store change fails.

    Writing first means an existing file or bad path fails before anything is
    committed, and a committed credential always has its token on disk.
    """

    def __init__(self, path):
        self.path = path

    def __enter__(self) -> str:
        token = new_token()
        try:
            write_token_file(self.path, token)
        except FileExistsError:
            raise OSError(f"Refusing to overwrite existing file {self.path}.") from None
        return token

    def __exit__(self, kind, error, traceback):
        if kind is not None:
            os.remove(self.path)
