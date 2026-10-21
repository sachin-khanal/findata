"""Finding or creating the master key: OS keychain first, owner-only file as a fallback."""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING

from findata.security import keychain
from findata.security.cipher import KEY_BYTES
from findata.security.files import read_secret_file, write_secret_file

if TYPE_CHECKING:
    from findata.config import Settings

log = logging.getLogger(__name__)


def _encode(master: bytes) -> bytes:
    return master.hex().encode()


def _decode(raw: str | bytes, source: str) -> bytes:
    try:
        master = bytes.fromhex(raw.decode() if isinstance(raw, bytes) else raw)
    except ValueError:
        master = b""
    if len(master) != KEY_BYTES:
        raise RuntimeError(f"Master key from {source} is corrupt (expected 64 hex characters).")
    return master


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
    account = keychain.keyring_account(settings.data_dir)
    key_file = settings.master_key_path
    keychain_error: Exception | None = None

    if backend in ("auto", "keychain"):
        try:
            stored = keychain.keyring_get(account)
            if stored is not None:
                return _decode(stored, "the OS keychain")
        except keychain.KeychainUnavailableError as exc:
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

    master = secrets.token_bytes(KEY_BYTES)
    if backend in ("auto", "keychain") and keychain_error is None:
        try:
            keychain.keyring_set(account, master.hex())
            return master
        except keychain.KeychainUnavailableError as exc:
            if backend == "keychain":
                raise RuntimeError(f"findata_key_backend=keychain but: {exc}") from exc
            keychain_error = exc
    if keychain_error is not None:
        log.warning(
            "No OS keychain available (%s); storing the master key in %s (owner-only).",
            keychain_error,
            key_file,
        )
    write_secret_file(key_file, _encode(master))
    return master
