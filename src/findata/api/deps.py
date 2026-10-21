"""FastAPI dependencies: shared runtime, DB session, Plaid gateway and token auth."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from findata.plaid_gateway import PlaidGateway
from findata.runtime import Runtime

TOKEN_HEADER = "X-Findata-Token"  # noqa: S105 (header name, not a secret)


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


def get_session(runtime: RuntimeDep) -> Iterator[Session]:
    with runtime.session_factory() as session:
        yield session


def get_gateway(runtime: RuntimeDep) -> PlaidGateway:
    try:
        return runtime.gateway
    except RuntimeError as exc:  # Plaid keys not configured; the app still serves other routes
        raise HTTPException(503, str(exc)) from exc


def require_token(
    request: Request,
    token: Annotated[str | None, Header(alias=TOKEN_HEADER)] = None,
) -> None:
    if not token or not secrets.compare_digest(token, request.app.state.api_token):
        raise HTTPException(401, f"Missing or invalid {TOKEN_HEADER} header")


SessionDep = Annotated[Session, Depends(get_session)]
GatewayDep = Annotated[PlaidGateway, Depends(get_gateway)]
auth = [Depends(require_token)]
