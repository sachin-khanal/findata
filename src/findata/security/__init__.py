"""Local secret handling.

Encryption is layered, all derived from one random 256-bit master key:

* The whole SQLite file is encrypted by SQLCipher (page-level AES-256), so a copied DB,
  its WAL and its indexes are unreadable without the key.
* The most sensitive fields (Plaid access tokens, raw Plaid payloads) are additionally
  encrypted with AES-256-GCM before they reach the database, under a different key.
* The two keys are derived from the master key with HKDF under distinct labels, so
  neither layer's key can be used to attack the other.

All primitives come from `cryptography` and SQLCipher; nothing here implements a cipher.

Submodules, one concern each: `files` (owner-only files), `cipher` (AES-GCM), `keys` (HKDF),
`keychain` (OS keychain), `masterkey` (find/create the master key), `token` (API token).
"""

from findata.security.cipher import FieldCipher
from findata.security.files import (
    create_private_file,
    ensure_private_dir,
    load_or_create_secret,
    read_secret_file,
)
from findata.security.keychain import KeychainUnavailableError
from findata.security.keys import Keys, load_keys
from findata.security.masterkey import load_or_create_master_key
from findata.security.token import load_or_create_api_token

__all__ = [
    "FieldCipher",
    "KeychainUnavailableError",
    "Keys",
    "create_private_file",
    "ensure_private_dir",
    "load_keys",
    "load_or_create_api_token",
    "load_or_create_master_key",
    "load_or_create_secret",
    "read_secret_file",
]
