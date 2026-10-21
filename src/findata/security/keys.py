"""Key derivation: one master key yields independent keys for each encryption layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from findata.security.cipher import KEY_BYTES, FieldCipher
from findata.security.masterkey import load_or_create_master_key

if TYPE_CHECKING:
    from findata.config import Settings


def _derive(master: bytes, label: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=label.encode()).derive(
        master
    )


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


def load_keys(settings: Settings) -> Keys:
    return Keys.from_master(load_or_create_master_key(settings))
