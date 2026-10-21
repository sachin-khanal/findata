"""Plaid Link: create a link token and exchange the resulting public token."""

from __future__ import annotations

from fastapi import APIRouter

from findata.api.deps import GatewayDep, RuntimeDep, SessionDep, auth
from findata.api.schemas import ExchangeIn, ItemOut
from findata.linking import link_item

LOCAL_CLIENT_USER_ID = "findata-local-user"

router = APIRouter(prefix="/api/link", dependencies=auth)


@router.post("/token")
def link_token(gw: GatewayDep) -> dict[str, str]:
    return {"link_token": gw.create_link_token(LOCAL_CLIENT_USER_ID)}


@router.post("/exchange")
def exchange(body: ExchangeIn, session: SessionDep, gw: GatewayDep, runtime: RuntimeDep) -> ItemOut:
    item = link_item(session, gw, runtime.cipher, body.public_token)
    return ItemOut.model_validate(item, from_attributes=True)
