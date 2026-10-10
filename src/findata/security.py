"""Local secret handling.

* Plaid access tokens are encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256).
  The key lives in a separate 0600 file, so a copied/leaked DB alone does not expose tokens.
* A random local API token gates the HTTP API. Requiring it in a custom header also
  forces a CORS preflight, which blocks drive-by requests from other websites.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def _write_secret_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # O_EXCL: never silently overwrite an existing key (that would orphan encrypted tokens).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def load_or_create_secret(path: Path, factory) -> bytes:
    if path.exists():
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise PermissionError(
                f"{path} is readable by other users (mode {oct(mode)}). Run: chmod 600 {path}"
            )
        return path.read_bytes().strip()
    value = factory()
    _write_secret_file(path, value)
    return value


class TokenCipher:
    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def from_key_file(cls, path: Path) -> TokenCipher:
        return cls(load_or_create_secret(path, Fernet.generate_key))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise RuntimeError(
                "Could not decrypt a stored Plaid token. The key file does not match this DB."
            ) from exc


def load_or_create_api_token(path: Path) -> str:
    return load_or_create_secret(path, lambda: secrets.token_urlsafe(32).encode()).decode()
