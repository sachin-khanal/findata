"""Typed client for the daemon's /v1 API: the only way front ends reach findata data.

Imports only stdlib, httpx2, pydantic, `findata.wire`, `findata.locations`, `findata.privfs`
and the `Role` type, so it never loads the key, database or Plaid stack.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

import httpx2
from pydantic import TypeAdapter

from findata.client.errors import (
    NOT_RUNNING_MESSAGE,
    DaemonClientError,
    DaemonNotRunning,
    DaemonRejected,
)
from findata.daemon.auth import Role
from findata.locations import Locations
from findata.privfs import read_secret_file
from findata.wire import (
    AccountOut,
    GrantOut,
    ItemOut,
    JobOut,
    SandboxLinkIn,
    StatusOut,
    SyncOut,
    TransactionOut,
)

__all__ = ["DaemonClient", "DaemonClientError", "DaemonNotRunning", "DaemonRejected"]

CREDENTIAL_HEADER = "X-Findata-Credential"
_BASE_URL = "http://daemon"


def _list_of(model: type) -> TypeAdapter:
    return TypeAdapter(list[model])


class DaemonClient:
    def __init__(
        self,
        data_dir,
        role: Role,
        *,
        timeout: float = 10.0,
        http_client: httpx2.Client | None = None,
    ) -> None:
        self._locations = Locations(data_dir)
        self._role = role
        self._timeout = timeout
        self._http = http_client
        self._owns_http = http_client is None
        self._credential: str | None = None

    def __repr__(self) -> str:
        return f"DaemonClient(data_dir={str(self._locations.data_dir)!r}, role={self._role!r})"

    # ---------- plumbing ----------

    def _credential_value(self) -> str:
        if self._credential is None:
            path = self._locations.credentials_dir / f"{self._role}.token"
            try:
                value = read_secret_file(path).decode().strip()
            except FileNotFoundError:
                raise DaemonClientError(
                    f"No credential file for role {self._role!r} at {path}. "
                    "Run `findata init` (or `findata start`, which creates credentials)."
                ) from None
            if not value:
                raise DaemonClientError(
                    f"Credential file {path} is empty; delete it and run `findata init`."
                )
            self._credential = value
        return self._credential

    def _connect(self) -> httpx2.Client:
        if self._http is not None:
            return self._http
        if os.name == "nt":
            try:
                port = int(self._locations.port_file.read_text().strip())
            except (OSError, ValueError):
                raise DaemonNotRunning(NOT_RUNNING_MESSAGE) from None
            self._http = httpx2.Client(base_url=f"http://127.0.0.1:{port}", timeout=self._timeout)
        else:
            socket_path = self._locations.socket
            if not socket_path.exists():
                raise DaemonNotRunning(NOT_RUNNING_MESSAGE)
            self._http = httpx2.Client(
                transport=httpx2.HTTPTransport(uds=str(socket_path)),
                base_url=_BASE_URL,
                timeout=self._timeout,
            )
        return self._http

    def close(self) -> None:
        if self._owns_http and self._http is not None:
            self._http.close()
            self._http = None

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx2.Response:
        credential = self._credential_value()
        http = self._connect()
        headers = {CREDENTIAL_HEADER: credential}
        try:
            response = http.request(method, path, headers=headers, **kwargs)
        except httpx2.TimeoutException:
            raise DaemonNotRunning(
                f"The findata daemon did not answer in {self._timeout:g} s."
            ) from None
        except (httpx2.TransportError, FileNotFoundError, ConnectionRefusedError):
            raise DaemonNotRunning(NOT_RUNNING_MESSAGE) from None
        if response.is_success:
            return response
        raise self._rejection(response)

    @staticmethod
    def _rejection(response: httpx2.Response) -> DaemonRejected:
        try:
            body = response.json()
            code, detail = body["error_code"], body["detail"]
            if not isinstance(code, str) or not isinstance(detail, str):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return DaemonRejected(
                "BAD_RESPONSE",
                f"The daemon answered HTTP {response.status_code} with an unexpected body.",
                response.status_code,
            )
        extra = {k: v for k, v in body.items() if k not in ("error_code", "detail")}
        return DaemonRejected(code, detail, response.status_code, extra)

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._request(method, path, **kwargs).json()

    # ---------- reads ----------

    def status(self) -> StatusOut:
        return StatusOut.model_validate(self._json("GET", "/v1/status"))

    def items(self) -> list[ItemOut]:
        return _list_of(ItemOut).validate_python(self._json("GET", "/v1/items"))

    def accounts(self) -> list[AccountOut]:
        return _list_of(AccountOut).validate_python(self._json("GET", "/v1/accounts"))

    def transactions(
        self,
        *,
        start: dt.date | None = None,
        end: dt.date | None = None,
        account_id: str | None = None,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TransactionOut]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if start is not None:
            params["start"] = start.isoformat()
        if end is not None:
            params["end"] = end.isoformat()
        if account_id is not None:
            params["account_id"] = account_id
        if category is not None:
            params["category"] = category
        return _list_of(TransactionOut).validate_python(
            self._json("GET", "/v1/transactions", params=params)
        )

    # ---------- writes ----------

    def sync(self) -> list[SyncOut]:
        return _list_of(SyncOut).validate_python(self._json("POST", "/v1/sync"))

    def link_token(self) -> str:
        return self._json("POST", "/v1/link/token")["link_token"]

    def exchange(self, public_token: str) -> ItemOut:
        body = self._json("POST", "/v1/link/exchange", json={"public_token": public_token})
        return ItemOut.model_validate(body)

    def remove_item(
        self, item_id: str, *, local_only: bool = False, request_id: str | None = None
    ) -> None:
        params: dict[str, Any] = {"local_only": str(local_only).lower()}
        if request_id is not None:
            params["request_id"] = request_id
        self._request("DELETE", f"/v1/items/{item_id}", params=params)

    def start_sandbox_link(self, body: SandboxLinkIn) -> str:
        data = self._json("POST", "/v1/jobs/sandbox-link", json=body.model_dump(mode="json"))
        return data.get("job_id") or data["id"]

    def job(self, job_id: str) -> JobOut:
        return JobOut.model_validate(self._json("GET", f"/v1/jobs/{job_id}"))

    # ---------- human controls ----------

    def grant(self, operations: list[str], ttl_s: int) -> GrantOut:
        body = {"operations": list(operations), "ttl_s": ttl_s}
        return GrantOut.model_validate(self._json("POST", "/v1/grants", json=body))

    def revoke(self) -> None:
        self._request("DELETE", "/v1/grants")

    def current_grant(self) -> GrantOut | None:
        data = self._json("GET", "/v1/grants")
        return None if data is None else GrantOut.model_validate(data)

    def approvals(self) -> list[dict]:
        return self._json("GET", "/v1/approvals")

    def approve(self, request_id: str) -> None:
        self._request("POST", f"/v1/approvals/{request_id}")

    def audit(self, limit: int = 100) -> list[dict]:
        return self._json("GET", "/v1/audit", params={"limit": limit})

    def shutdown(self) -> None:
        self._request("POST", "/v1/shutdown")
