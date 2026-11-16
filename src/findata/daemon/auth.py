"""Roles, permissions and per-role credentials for daemon clients.

Imports only stdlib, `findata.privfs` and `findata.locations` so front ends can use it
without loading the key or database stack.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Literal, get_args

from findata.locations import Locations
from findata.privfs import load_or_create_secret, read_secret_file

Role = Literal["web", "cli", "mcp"]
Permission = Literal["read", "sync", "link", "items.remove", "admin"]

ROLES: tuple[Role, ...] = get_args(Role)
_ALL: frozenset[Permission] = frozenset(get_args(Permission))

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    "web": _ALL,
    "cli": _ALL,
    "mcp": frozenset({"read"}),
}


class CredentialStore:
    """One owner-only token file per role under `locations.credentials_dir`."""

    def __init__(self, locations: Locations) -> None:
        self._dir = locations.credentials_dir

    def _path(self, role: Role) -> Path:
        return self._dir / f"{role}.token"

    def ensure_all(self) -> None:
        for role in ROLES:
            load_or_create_secret(self._path(role), lambda: secrets.token_urlsafe(32).encode())

    def _read(self, role: Role) -> bytes:
        path = self._path(role)
        value = read_secret_file(path)  # re-checks permissions every time
        if not value:
            raise RuntimeError(
                f"Credential file {path} is empty; delete it and run `findata init`."
            )
        return value

    def role_for(self, token: str | None) -> Role | None:
        if not token:
            return None
        presented = token.encode()
        match: Role | None = None
        for role in ROLES:  # compare against every role; never stop at the first hit
            if secrets.compare_digest(presented, self._read(role)):
                match = role
        return match
