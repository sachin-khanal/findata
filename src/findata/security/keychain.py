"""OS keychain access (macOS Keychain, Windows Credential Locker, Secret Service on Linux)."""

from __future__ import annotations

import hashlib
from pathlib import Path

KEYRING_SERVICE = "findata"


def keyring_account(data_dir: Path) -> str:
    # One keychain entry per data directory, so separate data dirs never share a key.
    return "master-key:" + hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()[:16]


class KeychainUnavailableError(RuntimeError):
    pass


def keyring_get(account: str) -> str | None:
    import keyring
    from keyring.errors import KeyringError

    try:
        if keyring.get_keyring().priority < 1:  # the "fail" backend: no real keychain here
            raise KeychainUnavailableError("no OS keychain is available")
        return keyring.get_password(KEYRING_SERVICE, account)
    except KeyringError as exc:
        raise KeychainUnavailableError(str(exc)) from exc


def keyring_set(account: str, value: str) -> None:
    import keyring
    from keyring.errors import KeyringError

    try:
        keyring.set_password(KEYRING_SERVICE, account, value)
    except KeyringError as exc:
        raise KeychainUnavailableError(str(exc)) from exc
