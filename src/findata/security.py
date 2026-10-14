"""Local secret handling.

Encryption is layered, all derived from one random 256-bit master key:

* The whole SQLite file is encrypted by SQLCipher (page-level AES-256), so a copied DB,
  its WAL and its indexes are unreadable without the key.
* The most sensitive fields (Plaid access tokens, raw Plaid payloads) are additionally
  encrypted with AES-256-GCM before they reach the database, under a different key.
* The two keys are derived from the master key with HKDF under distinct labels, so
  neither layer's key can be used to attack the other.

All primitives come from `cryptography` and SQLCipher; nothing here implements a cipher.

The master key lives in the OS keychain (macOS Keychain, Windows Credential Locker,
Secret Service on Linux) and falls back to an owner-only file where no keychain exists
(headless servers, containers, CI). A random local API token gates the HTTP API; requiring
it in a custom header also forces a CORS preflight, which blocks drive-by requests from
other websites.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

if TYPE_CHECKING:
    from findata.config import Settings

log = logging.getLogger(__name__)

KEYRING_SERVICE = "findata"
_FORMAT_V1 = b"\x01"
_NONCE_BYTES = 12
_KEY_BYTES = 32
# POSIX permission bits are meaningless on Windows (st_mode always looks world-accessible);
# there the protection is the per-user profile ACL that the data directory inherits.
_POSIX = os.name == "posix"


def ensure_private_dir(path: Path) -> None:
    """Create `path` owner-only, or refuse an existing one that others can enter.

    mkdir's mode is masked by the umask, applies only to the last component, and is ignored
    when the directory already exists, so the result is checked rather than assumed. An
    owner-only directory protects everything inside it regardless of its parents' modes.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = path.stat().st_mode & 0o777
    if _POSIX and mode & 0o077:
        raise PermissionError(
            f"{path} is accessible by other users (mode {oct(mode)}). Run: chmod 700 {path}"
        )


def create_private_file(path: Path) -> None:
    """Make sure `path` exists, owner-only from its first byte; refuse a looser existing one."""
    ensure_private_dir(path.parent)
    try:
        # O_EXCL + mode: the file never exists with broader permissions, even briefly.
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        mode = path.stat().st_mode & 0o777
        if _POSIX and mode & 0o077:
            raise PermissionError(
                f"{path} is readable by other users (mode {oct(mode)}). Run: chmod 600 {path}"
            ) from None


def _write_secret_file(path: Path, data: bytes) -> None:
    ensure_private_dir(path.parent)
    # O_EXCL: never silently overwrite an existing key (that would orphan encrypted data).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def read_secret_file(path: Path) -> bytes:
    mode = path.stat().st_mode & 0o777
    if _POSIX and mode & 0o077:
        raise PermissionError(
            f"{path} is readable by other users (mode {oct(mode)}). Run: chmod 600 {path}"
        )
    return path.read_bytes().strip()


def load_or_create_secret(path: Path, factory) -> bytes:
    if path.exists():
        return read_secret_file(path)
    value = factory()
    _write_secret_file(path, value)
    return value


def load_or_create_api_token(path: Path) -> str:
    return load_or_create_secret(path, lambda: secrets.token_urlsafe(32).encode()).decode()


# ---------- field-level encryption (AES-256-GCM) ----------


class FieldCipher:
    """Authenticated encryption for individual values (strings or JSON-able text).

    Output is `base64url(version || nonce || ciphertext+tag)`. A fresh random nonce is used
    for every call. `context` is bound in as associated data, so a ciphertext copied to a
    different column or row (e.g. another item's token) fails to decrypt.
    """

    def __init__(self, key: bytes) -> None:
        if len(key) != _KEY_BYTES:
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


# ---------- master key and derived keys ----------


def _derive(master: bytes, label: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=_KEY_BYTES, salt=None, info=label.encode()
    ).derive(master)


@dataclass(frozen=True)
class Keys:
    db_key: bytes  # raw 32-byte SQLCipher key
    cipher: FieldCipher

    @classmethod
    def from_master(cls, master: bytes) -> Keys:
        return cls(
            db_key=_derive(master, "findata/v1/sqlcipher"),
            cipher=FieldCipher(_derive(master, "findata/v1/field-aes-gcm")),
        )


def _keyring_account(data_dir: Path) -> str:
    # One keychain entry per data directory, so separate data dirs never share a key.
    return "master-key:" + hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()[:16]


def _encode(master: bytes) -> bytes:
    return master.hex().encode()


def _decode(raw: str | bytes, source: str) -> bytes:
    try:
        master = bytes.fromhex(raw.decode() if isinstance(raw, bytes) else raw)
    except ValueError:
        master = b""
    if len(master) != _KEY_BYTES:
        raise RuntimeError(f"Master key from {source} is corrupt (expected 64 hex characters).")
    return master


class KeychainUnavailableError(RuntimeError):
    pass


def _keyring_get(account: str) -> str | None:
    import keyring
    from keyring.errors import KeyringError

    try:
        if keyring.get_keyring().priority < 1:  # the "fail" backend: no real keychain here
            raise KeychainUnavailableError("no OS keychain is available")
        return keyring.get_password(KEYRING_SERVICE, account)
    except KeyringError as exc:
        raise KeychainUnavailableError(str(exc)) from exc


def _keyring_set(account: str, value: str) -> None:
    import keyring
    from keyring.errors import KeyringError

    try:
        keyring.set_password(KEYRING_SERVICE, account, value)
    except KeyringError as exc:
        raise KeychainUnavailableError(str(exc)) from exc


def _is_legacy_layout(settings: Settings) -> bool:
    """True for a pre-encryption data dir: a plaintext SQLite file (SQLCipher files have no
    readable header) or the old Fernet `token.key` beside the database."""
    try:
        with settings.db_path.open("rb") as fh:
            plaintext = fh.read(16).startswith(b"SQLite format 3")
    except OSError:
        plaintext = False
    return plaintext or (settings.data_dir / "token.key").exists()


def load_or_create_master_key(settings: Settings) -> bytes:
    """Find the master key (keychain, then file) or create one in the best available place.

    A key is only ever *created* when no database exists yet. If a database is already there
    but no key can be found, that is an error: silently minting a new key would make the
    existing data permanently unreadable.
    """
    backend = settings.findata_key_backend
    account = _keyring_account(settings.data_dir)
    key_file = settings.master_key_path
    keychain_error: Exception | None = None

    if backend in ("auto", "keychain"):
        try:
            stored = _keyring_get(account)
            if stored is not None:
                return _decode(stored, "the OS keychain")
        except KeychainUnavailableError as exc:
            if backend == "keychain":
                raise RuntimeError(f"findata_key_backend=keychain but: {exc}") from exc
            keychain_error = exc

    if backend in ("auto", "file") and key_file.exists():
        return _decode(read_secret_file(key_file), str(key_file))

    if settings.db_path.exists() and _is_legacy_layout(settings):
        raise RuntimeError(
            f"{settings.db_path} was created by a version before encryption at rest "
            "(plaintext SQLite with a Fernet token.key) and cannot be opened. There is no "
            "migration. If it only holds Sandbox data, move or delete it "
            f"(rm -r {settings.data_dir}), then run `findata init` and `findata sandbox-link` "
            "again. If it holds real data, keep it and open it with the old version."
        )
    if settings.db_path.exists():
        raise RuntimeError(
            f"{settings.db_path} exists but its master key was not found "
            f"(looked in {'the OS keychain and ' if backend != 'file' else ''}{key_file}). "
            "Restore the key from a backup; without it the data cannot be recovered."
        )

    master = secrets.token_bytes(_KEY_BYTES)
    if backend in ("auto", "keychain") and keychain_error is None:
        try:
            _keyring_set(account, master.hex())
            return master
        except KeychainUnavailableError as exc:
            if backend == "keychain":
                raise RuntimeError(f"findata_key_backend=keychain but: {exc}") from exc
            keychain_error = exc
    if keychain_error is not None:
        log.warning(
            "No OS keychain available (%s); storing the master key in %s (owner-only).",
            keychain_error,
            key_file,
        )
    _write_secret_file(key_file, _encode(master))
    return master


def load_keys(settings: Settings) -> Keys:
    return Keys.from_master(load_or_create_master_key(settings))
