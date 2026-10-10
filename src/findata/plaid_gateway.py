"""Thin wrapper around plaid-python.

Everything else in the app talks to `PlaidGateway` (a Protocol) and receives plain dicts,
so tests can swap in a fake and the rest of the code never imports Plaid models.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import plaid
from plaid.api import plaid_api
from plaid.model.country_code import CountryCode
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
from plaid.model.sandbox_public_token_create_request_options import (
    SandboxPublicTokenCreateRequestOptions,
)
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from findata.config import Settings

# First Platypus Bank: Plaid's recommended non-OAuth sandbox institution.
SANDBOX_INSTITUTION_ID = "ins_109508"
SANDBOX_TRANSACTIONS_USER = "user_transactions_dynamic"


class PlaidError(Exception):
    def __init__(self, error_code: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code
        self.status = status


class PlaidGateway(Protocol):
    def create_link_token(self, client_user_id: str) -> str: ...
    def exchange_public_token(self, public_token: str) -> tuple[str, str]: ...
    def sandbox_public_token(self, username: str = SANDBOX_TRANSACTIONS_USER) -> str: ...
    def get_item(self, access_token: str) -> dict[str, Any]: ...
    def transactions_sync(
        self, access_token: str, cursor: str | None, count: int = 500
    ) -> dict[str, Any]: ...


def _translate(exc: plaid.ApiException) -> PlaidError:
    try:
        body = json.loads(exc.body or "{}")
    except ValueError:
        body = {}
    return PlaidError(
        body.get("error_code", "UNKNOWN"),
        body.get("error_message", str(exc)),
        getattr(exc, "status", None),
    )


class PlaidClient:
    """Real implementation of PlaidGateway."""

    def __init__(self, settings: Settings) -> None:
        settings.require_plaid_credentials()
        host = {
            "sandbox": plaid.Environment.Sandbox,
            "production": plaid.Environment.Production,
        }[settings.plaid_env]
        cfg = plaid.Configuration(
            host=host,
            api_key={
                "clientId": settings.plaid_client_id,
                "secret": settings.plaid_secret.get_secret_value(),
            },
        )
        self._api = plaid_api.PlaidApi(plaid.ApiClient(cfg))
        self._country_codes = [CountryCode(c) for c in settings.plaid_country_codes]
        self._env = settings.plaid_env

    def _call(self, fn, request) -> dict[str, Any]:
        try:
            return fn(request).to_dict()
        except plaid.ApiException as exc:
            raise _translate(exc) from exc

    def create_link_token(self, client_user_id: str) -> str:
        req = LinkTokenCreateRequest(
            client_name="findata",
            language="en",
            country_codes=self._country_codes,
            products=[Products("transactions")],
            user=LinkTokenCreateRequestUser(client_user_id=client_user_id),
        )
        return self._call(self._api.link_token_create, req)["link_token"]

    def exchange_public_token(self, public_token: str) -> tuple[str, str]:
        req = ItemPublicTokenExchangeRequest(public_token=public_token)
        resp = self._call(self._api.item_public_token_exchange, req)
        return resp["item_id"], resp["access_token"]

    def sandbox_public_token(self, username: str = SANDBOX_TRANSACTIONS_USER) -> str:
        if self._env != "sandbox":
            raise RuntimeError("sandbox_public_token is only available when PLAID_ENV=sandbox")
        req = SandboxPublicTokenCreateRequest(
            institution_id=SANDBOX_INSTITUTION_ID,
            initial_products=[Products("transactions")],
            options=SandboxPublicTokenCreateRequestOptions(
                override_username=username,
                override_password="pass_good",  # noqa: S106
            ),
        )
        return self._call(self._api.sandbox_public_token_create, req)["public_token"]

    def get_item(self, access_token: str) -> dict[str, Any]:
        return self._call(self._api.item_get, ItemGetRequest(access_token=access_token))["item"]

    def transactions_sync(
        self, access_token: str, cursor: str | None, count: int = 500
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"access_token": access_token, "count": count}
        if cursor:
            kwargs["cursor"] = cursor
        return self._call(self._api.transactions_sync, TransactionsSyncRequest(**kwargs))
