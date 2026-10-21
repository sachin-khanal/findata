"""Field-level encryption (AES-256-GCM) for the most sensitive values."""

from __future__ import annotations

import base64
import binascii
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
_FORMAT_V1 = b"\x01"
_NONCE_BYTES = 12


class FieldCipher:
    """Authenticated encryption for individual values (strings or JSON-able text).

    Output is `base64url(version || nonce || ciphertext+tag)`. A fresh random nonce is used
    for every call. `context` is bound in as associated data, so a ciphertext copied to a
    different column or row (e.g. another item's token) fails to decrypt.
    """

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise ValueError("field key must be 32 bytes")
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str, context: str) -> str:
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ct = self._aead.encrypt(nonce, plaintext.encode(), context.encode())
        return base64.urlsafe_b64encode(_FORMAT_V1 + nonce + ct).decode()

    def decrypt(self, ciphertext: str, context: str) -> str:
        try:
            blob = base64.urlsafe_b64decode(ciphertext.encode())
            if blob[:1] != _FORMAT_V1:
                raise ValueError("unknown ciphertext version")
            nonce, ct = blob[1 : 1 + _NONCE_BYTES], blob[1 + _NONCE_BYTES :]
            return self._aead.decrypt(nonce, ct, context.encode()).decode()
        except (InvalidTag, ValueError, binascii.Error) as exc:
            raise RuntimeError(
                f"Could not decrypt a stored value ({context}). The master key does not match "
                "this database, or the value was tampered with or moved."
            ) from exc
