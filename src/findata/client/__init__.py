"""Typed client for the daemon's /v1 API: the only way front ends reach findata data.

Imports only stdlib, httpx2, pydantic, `findata.wire`, `findata.locations`, `findata.privfs`
and the `Role` type, so it never loads the key, database or Plaid stack.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from urllib.parse import quote

import httpx2
from pydantic import TypeAdapter

from findata.client.errors import (
    NOT_RUNNING_MESSAGE,
    DaemonClientError,
    DaemonNotRunning,
    DaemonRejected,
    DaemonTimeout,
)
from findata.client.liveness import daemon_lock_held
from findata.daemon.auth import Role
from findata.locations import Locations
from findata.privfs import read_secret_file
from findata.wire import (
    JOB_FINISHED_EVENT,
    KEEPALIVE_EVENT,
    AccountOut,
    GrantOut,
    ItemOut,
    JobOut,
    SandboxLinkIn,
    StatusOut,
    SyncOut,
    TransactionOut,
)

__all__ = [
    "DaemonClient",
    "DaemonClientError",
    "DaemonNotRunning",
    "DaemonRejected",
    "DaemonTimeout",
]

CREDENTIAL_HEADER = "X-Findata-Credential"
_BASE_URL = "http://daemon"


def _seg(value: str) -> str:
    return quote(value, safe="")


def _list_of(model: type) -> TypeAdapter:
    return TypeAdapter(list[model])


def _timed_out(seconds: float) -> DaemonTimeout:
    return DaemonTimeout(
        f"The findata daemon did not answer within {seconds:g} s; the operation may still be "
        "running. Check `findata status` before retrying."
    )


def _event(line: str, status: int) -> dict:
    try:
        event = json.loads(line)
    except ValueError:
        event = None
    if not isinstance(event, dict):
        raise DaemonRejected(
            "BAD_RESPONSE", "The daemon sent a stream line that is not a JSON object.", status
        )
    return event


class DaemonClient:
    def __init__(
        self,
        data_dir,
        role: Role,
        *,
        timeout: float = 10.0,
        long_timeout: float = 180.0,
        http_client: httpx2.Client | None = None,
        async_http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._locations = Locations(data_dir)
        self._role = role
        self._timeout = timeout
        self._long_timeout = long_timeout
        self._http = http_client
        self._owns_http = http_client is None
        self._ahttp = async_http_client
        self._owns_ahttp = async_http_client is None
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
            except PermissionError as e:
                # read_secret_file's message names the file and the `chmod 600` fix.
                raise DaemonClientError(str(e)) from None
            except (OSError, UnicodeError) as e:
                raise DaemonClientError(
                    f"Cannot read credential file {path} ({type(e).__name__}); "
                    "delete it and run `findata init`."
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
            self._http = self._connect_tcp()
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

    def _connect_tcp(self) -> httpx2.Client:
        return httpx2.Client(base_url=self._tcp_base_url(), timeout=self._timeout)

    def _tcp_base_url(self) -> str:
        """Loopback fallback. Never sends anything unless a live daemon holds its lock, so a
        stale port file cannot lead the credential to some other local process."""
        if not daemon_lock_held(self._locations.lock):
            raise DaemonNotRunning(NOT_RUNNING_MESSAGE)
        try:
            port = int(self._locations.port_file.read_text().strip())
        except (OSError, ValueError):
            raise DaemonNotRunning(NOT_RUNNING_MESSAGE) from None
        return f"http://127.0.0.1:{port}"

    def _aconnect(self) -> httpx2.AsyncClient:
        """The async twin of `_connect`, made on first use inside the running event loop."""
        if self._ahttp is not None:
            return self._ahttp
        if os.name == "nt":
            self._ahttp = httpx2.AsyncClient(base_url=self._tcp_base_url(), timeout=self._timeout)
        else:
            socket_path = self._locations.socket
            if not socket_path.exists():
                raise DaemonNotRunning(NOT_RUNNING_MESSAGE)
            self._ahttp = httpx2.AsyncClient(
                transport=httpx2.AsyncHTTPTransport(uds=str(socket_path)),
                base_url=_BASE_URL,
                timeout=self._timeout,
            )
        return self._ahttp

    def close(self) -> None:
        if self._owns_http and self._http is not None:
            self._http.close()
            self._http = None

    async def aclose(self) -> None:
        if self._owns_ahttp and self._ahttp is not None:
            await self._ahttp.aclose()
            self._ahttp = None

    def _request(
        self, method: str, path: str, *, timeout: float | None = None, **kwargs: Any
    ) -> httpx2.Response:
        credential = self._credential_value()
        http = self._connect()
        headers = {CREDENTIAL_HEADER: credential}
        effective = self._timeout if timeout is None else timeout
        try:
            response = http.request(
                method,
                path,
                headers=headers,
                extensions={"timeout": httpx2.Timeout(effective).as_dict()},
                **kwargs,
            )
        except httpx2.TimeoutException:
            raise _timed_out(effective) from None
        except (httpx2.TransportError, FileNotFoundError, ConnectionRefusedError):
            raise DaemonNotRunning(NOT_RUNNING_MESSAGE) from None
        if response.is_success:
            return response
        raise self._rejection(response)

    def _stream(
        self, path: str, *, params: dict[str, Any] | None = None, until: str | None = None
    ) -> Iterator[dict]:
        """Open an NDJSON stream now, so a refusal or a stopped daemon raises here as for any
        other call, and return an iterator over its objects with keep-alives skipped.

        Only the connect (and write) timeout applies: a stream may rightly be idle for as long
        as nothing happens. With `until`, the iterator ends after that event and raises
        DaemonNotRunning if the stream ends without it. Closing the iterator, or reaching its
        end, closes the connection.
        """
        lines = self._stream_lines(path, params, until)
        next(lines)  # runs up to the response's arrival; the generator closes the stream
        return lines

    def _stream_lines(
        self, path: str, params: dict[str, Any] | None, until: str | None
    ) -> Iterator[dict]:
        credential = self._credential_value()
        http = self._connect()
        timeout = httpx2.Timeout(self._timeout, read=None)
        try:
            with http.stream(
                "GET",
                path,
                params=params,
                headers={CREDENTIAL_HEADER: credential},
                extensions={"timeout": timeout.as_dict()},
            ) as response:
                if not response.is_success:
                    response.read()
                    raise self._rejection(response)
                yield {}  # the stream is open; `_stream` drops this one
                for line in response.iter_lines():
                    if not line:
                        continue
                    event = _event(line, response.status_code)
                    if event.get("event") == KEEPALIVE_EVENT:
                        continue
                    yield event
                    if until is not None and event.get("event") == until:
                        return
        except httpx2.TimeoutException:
            raise _timed_out(self._timeout) from None
        except (httpx2.TransportError, FileNotFoundError, ConnectionRefusedError):
            raise DaemonNotRunning(NOT_RUNNING_MESSAGE) from None
        if until is not None:
            raise DaemonNotRunning(
                "The findata daemon stopped before the job finished; the job did not survive "
                "it. Start the daemon with `findata start` and run the command again."
            )

    async def _astream(
        self, path: str, *, params: dict[str, Any] | None, keepalives: bool
    ) -> AsyncIterator[dict]:
        """`_stream` for an event loop: opened before it returns, so a refusal or a stopped
        daemon raises here; holds no thread while idle. Closing the iterator (`aclose`) closes
        the connection, which frees the daemon's stream slot."""
        lines = self._astream_lines(path, params, keepalives)
        await anext(lines)  # runs up to the response's arrival; the generator closes the stream
        return lines

    async def _astream_lines(
        self, path: str, params: dict[str, Any] | None, keepalives: bool
    ) -> AsyncIterator[dict]:
        credential = self._credential_value()
        http = self._aconnect()
        timeout = httpx2.Timeout(self._timeout, read=None)
        try:
            async with http.stream(
                "GET",
                path,
                params=params,
                headers={CREDENTIAL_HEADER: credential},
                extensions={"timeout": timeout.as_dict()},
            ) as response:
                if not response.is_success:
                    await response.aread()
                    raise self._rejection(response)
                yield {}  # the stream is open; `_astream` drops this one
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    event = _event(line, response.status_code)
                    if keepalives or event.get("event") != KEEPALIVE_EVENT:
                        yield event
        except httpx2.TimeoutException:
            raise _timed_out(self._timeout) from None
        except (httpx2.TransportError, FileNotFoundError, ConnectionRefusedError):
            raise DaemonNotRunning(NOT_RUNNING_MESSAGE) from None

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

    def _long(self, timeout: float | None) -> float:
        return self._long_timeout if timeout is None else timeout

    # ---------- reads ----------

    def status(self, *, timeout: float | None = None) -> StatusOut:
        return StatusOut.model_validate(self._json("GET", "/v1/status", timeout=timeout))

    def items(self, *, timeout: float | None = None) -> list[ItemOut]:
        return _list_of(ItemOut).validate_python(self._json("GET", "/v1/items", timeout=timeout))

    def accounts(self, *, timeout: float | None = None) -> list[AccountOut]:
        return _list_of(AccountOut).validate_python(
            self._json("GET", "/v1/accounts", timeout=timeout)
        )

    def transactions(
        self,
        *,
        start: dt.date | None = None,
        end: dt.date | None = None,
        account_id: str | None = None,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
        timeout: float | None = None,
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
            self._json("GET", "/v1/transactions", params=params, timeout=timeout)
        )

    # ---------- writes (these wait on Plaid, so they use the long timeout) ----------

    def sync(self, *, timeout: float | None = None) -> list[SyncOut]:
        return _list_of(SyncOut).validate_python(
            self._json("POST", "/v1/sync", timeout=self._long(timeout))
        )

    def link_token(self, *, timeout: float | None = None) -> str:
        return self._json("POST", "/v1/link/token", timeout=self._long(timeout))["link_token"]

    def exchange(self, public_token: str, *, timeout: float | None = None) -> ItemOut:
        body = self._json(
            "POST",
            "/v1/link/exchange",
            json={"public_token": public_token},
            timeout=self._long(timeout),
        )
        return ItemOut.model_validate(body)

    def remove_item(
        self,
        item_id: str,
        *,
        local_only: bool = False,
        request_id: str | None = None,
        timeout: float | None = None,
    ) -> None:
        params: dict[str, Any] = {"local_only": str(local_only).lower()}
        if request_id is not None:
            params["request_id"] = request_id
        self._request(
            "DELETE", f"/v1/items/{_seg(item_id)}", params=params, timeout=self._long(timeout)
        )

    def start_sandbox_link(self, body: SandboxLinkIn, *, timeout: float | None = None) -> str:
        data = self._json(
            "POST",
            "/v1/jobs/sandbox-link",
            json=body.model_dump(mode="json"),
            timeout=self._long(timeout),
        )
        return data["job_id"]

    def start_history(self, item_id: str, *, timeout: float | None = None) -> str:
        """Start a job that waits for an already-linked Item's history; returns its job id."""
        data = self._json(
            "POST", "/v1/jobs/history", json={"item_id": item_id}, timeout=self._long(timeout)
        )
        return data["job_id"]

    def job(self, job_id: str, *, timeout: float | None = None) -> JobOut:
        return JobOut.model_validate(self._json("GET", f"/v1/jobs/{_seg(job_id)}", timeout=timeout))

    # ---------- streams (open until they end; no read timeout between events) ----------

    def follow_job(self, job_id: str) -> Iterator[dict]:
        """The job's events as they happen, those recorded so far first, ending with
        `{"event": "job_finished", "state", "result"}`. DaemonNotRunning if the daemon stops
        before the job finishes."""
        return self._stream(f"/v1/jobs/{_seg(job_id)}/events", until=JOB_FINISHED_EVENT)

    def events(self, after: int | None = None) -> Iterator[dict]:
        """Daemon events as they happen, as `{"seq", "event", "at", ...}`. Without `after`,
        those published from now on; with it, first the ones after that seq still buffered
        (`{"event": "gap"}` first if some are gone: re-read what you show). Ends when the
        daemon stops."""
        params = {} if after is None else {"after": after}
        return self._stream("/v1/events", params=params)

    async def aevents(
        self, after: int | None = None, *, keepalives: bool = False
    ) -> AsyncIterator[dict]:
        """`events` for an event loop: `stream = await client.aevents()`, then `async for` it
        and `await stream.aclose()` when done. Keep-alive lines are skipped unless
        `keepalives` (a relay forwards them so its own client can tell the link is alive)."""
        params = {} if after is None else {"after": after}
        return await self._astream("/v1/events", params=params, keepalives=keepalives)

    # ---------- human controls ----------
    # The daemon asks for Touch ID or the password before a grant or an approval and waits up
    # to 60 s for it, so those two use the long timeout.

    def grant(self, operations: list[str], ttl_s: int, *, timeout: float | None = None) -> GrantOut:
        body = {"operations": list(operations), "ttl_s": ttl_s}
        data = self._json("POST", "/v1/grants", json=body, timeout=self._long(timeout))
        return GrantOut.model_validate(data)

    def revoke(self, *, timeout: float | None = None) -> None:
        self._request("DELETE", "/v1/grants", timeout=timeout)

    def current_grant(self, *, timeout: float | None = None) -> GrantOut | None:
        data = self._json("GET", "/v1/grants", timeout=timeout)
        return None if data is None else GrantOut.model_validate(data)

    def approvals(self, *, timeout: float | None = None) -> list[dict]:
        return self._json("GET", "/v1/approvals", timeout=timeout)

    def approve(self, request_id: str, *, timeout: float | None = None) -> None:
        self._request("POST", f"/v1/approvals/{_seg(request_id)}", timeout=self._long(timeout))

    def audit(self, limit: int = 100, *, timeout: float | None = None) -> list[dict]:
        return self._json("GET", "/v1/audit", params={"limit": limit}, timeout=timeout)

    def shutdown(self, *, timeout: float | None = None) -> None:
        self._request("POST", "/v1/shutdown", timeout=timeout)
