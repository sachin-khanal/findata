"""Composition root: builds the shared dependencies (DB, cipher, Plaid gateway) once.

Interfaces (CLI, HTTP API, later MCP) take a `Runtime` instead of wiring these themselves.
"""

from __future__ import annotations

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from findata.config import Settings, get_settings
from findata.db import make_engine, make_session_factory
from findata.plaid_gateway import PlaidClient, PlaidGateway
from findata.security import FieldCipher, load_keys


class Runtime:
    """Settings, DB session factory, field cipher, and a lazily created Plaid gateway.

    The gateway is created on first use so that everything that doesn't talk to Plaid works
    before Plaid keys are configured; `gateway` raises RuntimeError if they are missing.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker,
        cipher: FieldCipher,
        gateway: PlaidGateway | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.cipher = cipher
        self._gateway = gateway

    @property
    def gateway(self) -> PlaidGateway:
        if self._gateway is None:
            self._gateway = PlaidClient(self.settings)
        return self._gateway


def build_runtime(
    settings: Settings | None = None,
    *,
    gateway: PlaidGateway | None = None,
    engine: Engine | None = None,
) -> Runtime:
    """Load keys and open the database. `gateway` and `engine` let tests inject their own."""
    settings = settings or get_settings()
    keys = load_keys(settings)
    engine = engine or make_engine(settings.db_path, keys.db_key)
    return Runtime(settings, make_session_factory(engine), keys.cipher, gateway)
