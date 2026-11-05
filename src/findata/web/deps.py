"""FastAPI dependencies for the web front end: the daemon client and token auth."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from findata.client import DaemonClient

TOKEN_HEADER = "X-Findata-Token"  # noqa: S105 (header name, not a secret)


def get_client(request: Request) -> DaemonClient:
    return request.app.state.client


ClientDep = Annotated[DaemonClient, Depends(get_client)]


def require_token(
    request: Request,
    token: Annotated[str | None, Header(alias=TOKEN_HEADER)] = None,
) -> None:
    if not token or not secrets.compare_digest(token, request.app.state.api_token):
        raise HTTPException(401, f"Missing or invalid {TOKEN_HEADER} header")


auth = [Depends(require_token)]
